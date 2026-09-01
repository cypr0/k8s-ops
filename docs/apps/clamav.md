# ClamAV

> **Namespace**  nextcloud
> **Source**     `app-template` OCIRepository v5.1.0 (`kubernetes/apps/nextcloud/clamav/app/ocirepository.yaml`), chart-templated `clamav/clamav:1.5.4` image (`kubernetes/apps/nextcloud/clamav/app/helmrelease.yaml`)
> **Hostname**   none — internal only, `clamav.nextcloud.svc.cluster.local:3310`

## What it does here
Runs `clamd` as a daemon that Nextcloud's `files_antivirus` app calls synchronously over TCP to scan every uploaded file before it's accepted (`kubernetes/apps/nextcloud/nextcloud/app/post-install-job.yaml:86-89` sets `av_mode=daemon`, `av_host=clamav.nextcloud.svc.cluster.local`, `av_port=3310`). It is not exposed to anything outside the `nextcloud` namespace and has no other consumer in this cluster.

## Architecture at a glance
- **Depends on:** `csi-driver-nfs` (`kube-system`) for its PVC (`kubernetes/apps/nextcloud/clamav/ks.yaml:12-13`, explicit `dependsOn`); the public internet for `freshclam` virus-database updates (egress rule below).
- **Depended on by:** `nextcloud` (`files_antivirus` app, upload-scan path only — see `kubernetes/apps/nextcloud/nextcloud/app/post-install-job.yaml`). Nothing else in the repo references `clamav.nextcloud.svc.cluster.local`.

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/nextcloud/clamav/ks.yaml` | Flux Kustomization — `dependsOn: csi-driver-nfs`, 1h reconcile interval |
| `kubernetes/apps/nextcloud/clamav/app/ocirepository.yaml` | Pins the `bjw-s-labs/app-template` chart (v5.1.0) used to render the workload |
| `kubernetes/apps/nextcloud/clamav/app/helmrelease.yaml` | Container image/tag, resources, probes, service, persistence — no chart-native ClamAV values, everything is app-template's generic `controllers`/`persistence` schema |
| `kubernetes/apps/nextcloud/clamav/app/ciliumnetworkpolicy.yaml` | Ingress: `nextcloud` namespace → `:3310` only. Egress: DNS + world on 80/443 (freshclam) |
| `kubernetes/apps/nextcloud/clamav/app/kustomization.yaml` | Wires the three resources above, sets `namespace: nextcloud` |

## Secrets
None. There is no `externalsecret*.yaml` in `kubernetes/apps/nextcloud/clamav/app/` — the only configuration is the container image/env in `helmrelease.yaml`, and clamd itself takes no credentials.

## Routing & access
- No HTTPRoute — this app has no HTTP interface and is never reached from outside the cluster.
- **CiliumNetworkPolicy** (`kubernetes/apps/nextcloud/clamav/app/ciliumnetworkpolicy.yaml`): ingress restricted to any pod in the `nextcloud` namespace hitting TCP `3310` (i.e. Nextcloud's `files_antivirus`, not scoped further by pod label). Egress allows only kube-dns (UDP/TCP 53) and `toEntities: world` on TCP 80/443 — the latter is what lets `freshclam` (bundled in the `clamav/clamav` image) pull virus-signature updates; there is no other egress path, so a failure here degrades to a stale, not broken, signature database.
- Nextcloud's own network policy (`kubernetes/apps/nextcloud/nextcloud/app/ciliumnetworkpolicy.yaml:86-89`) correspondingly allows the reverse direction: any pod in `nextcloud` may reach ClamAV's `:3310` alongside Elasticsearch/Collabora/Whiteboard, grouped under one same-namespace egress rule rather than one rule per sibling.

## Storage
Single PVC, `storageClass: zfs-nfs`, `accessMode: ReadWriteOnce`, `size: 2Gi`, mounted at `/var/lib/clamav` (`kubernetes/apps/nextcloud/clamav/app/helmrelease.yaml:78-85`) — holds the freshclam-managed virus database, not scan targets (files are streamed to clamd over the wire, never written to this PVC). `strategy: Recreate` with `replicas: 1` avoids two pods holding the RWO claim at once, same rationale as Nextcloud itself (`docs/apps/nextcloud.md`). Backup: the `nextcloud` namespace as a whole is in Velero's daily/weekly/monthly GFS schedules (`kubernetes/apps/velero/schedules/schedule-daily.yaml:13-14`, 14-day TTL; weekly 90-day; monthly 365-day), so this PVC is swept up by namespace-level inclusion — there's no dedicated backup for it, and since it's just a redownloadable virus database, restoring it is low-value versus letting freshclam repopulate from empty.

## Known quirks
- **PVC is `zfs-nfs`-backed but `ReadWriteOnce`**, the same pattern flagged as a latent restore risk for Nextcloud's own data PVC (`docs/apps/nextcloud.md` "Known quirks") — no incident has hit this for ClamAV specifically, and the risk is lower here since the volume only holds a re-downloadable signature database rather than user data.
- **Liveness/readiness/startup probes all shell out to `clamdscan --ping 3`** rather than a native TCP or HTTP check (`kubernetes/apps/nextcloud/clamav/app/helmrelease.yaml:41-71`); `startup` alone allows up to 60s + 30×15s (~8 minutes) before the container is considered failed, consistent with clamd's slow cold-start while it loads the virus database from the PVC.
- **`install.remediation.retries: -1`** (unlimited) on first install vs. `3` on upgrade (`helmrelease.yaml:14-19`) — tolerates a slow first-time database download without Flux giving up, but a genuinely broken upgrade will still fail after 3 tries.

## Common operations
- Upgrade image tag: edit `spec.values.controllers.clamav.containers.app.image.tag` in `kubernetes/apps/nextcloud/clamav/app/helmrelease.yaml`, commit, push, Flux reconciles within 1h (or `flux reconcile helmrelease clamav -n nextcloud`).
- Check scan connectivity from Nextcloud's side: `kubectl exec -n nextcloud deploy/nextcloud -c nextcloud -- php occ config:app:get files_antivirus av_host`.
- Force a virus-database refresh: `kubectl exec -n nextcloud deploy/clamav -- freshclam` (freshclam also runs on its own internal schedule inside the image).
- Pause reconciliation: `flux suspend kustomization clamav -n flux-system` / `flux suspend helmrelease clamav -n nextcloud`.

## TODOs / unknowns
- No incident or inline `# NOTE`/`# HACK`/`# WORKAROUND` comment exists for this app as of this writing — "Known quirks" above are inferred from config, not from an observed failure.
- Whether `files_antivirus` fails uploads open or closed if ClamAV is unreachable is not configured anywhere in this repo (it's an app-side Nextcloud setting, not visible in `files_antivirus`'s `occ config:app:set` calls in `kubernetes/apps/nextcloud/nextcloud/app/post-install-job.yaml`) — unverified, worth checking directly in the Nextcloud admin UI if it matters operationally.

---
_Cite every non-obvious claim with a repo-root-relative file path (e.g. `kubernetes/apps/security/authentik/app/helmrelease.yaml`), not a bare filename — this doc lives under `docs/apps/`, so relative paths must resolve from there._
