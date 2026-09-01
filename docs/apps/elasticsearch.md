# Elasticsearch (Nextcloud)

> **Namespace**  nextcloud
> **Source**     `app-template` chart v5.1.0 via OCIRepository `oci://ghcr.io/bjw-s-labs/helm/app-template` (`kubernetes/apps/nextcloud/elasticsearch/app/ocirepository.yaml`, `helmrelease.yaml`)
> **Hostname**   none — cluster-internal only, no HTTPRoute; reachable at `elasticsearch.nextcloud.svc.cluster.local:9200`

## What it does here
Single-node full-text search index backing Nextcloud's `fulltextsearch`/`fulltextsearch_elasticsearch` apps — nothing else in the cluster talks to it. It is a wholly separate deployment from the OpenSearch instance in the `logging` namespace — the shared "elasticsearch" naming across the two is coincidental, not a shared backend.

## Architecture at a glance
- **Depends on:** `csi-driver-nfs` (Flux `dependsOn` in `kubernetes/apps/nextcloud/elasticsearch/ks.yaml`, since its PVC uses the `zfs-nfs` StorageClass). No CNPG, no cache, no ExternalSecret, no OIDC — the smallest dependency footprint of any app in `nextcloud/`.
- **Depended on by:** `nextcloud` only, and only softly. `kubernetes/apps/nextcloud/nextcloud/app/post-install-job.yaml:97` sets `fulltextsearch_elasticsearch elastic_host` to `http://elasticsearch.nextcloud.svc.cluster.local:9200`, but there is **no Flux `dependsOn` between the two Kustomizations** in either direction — if Elasticsearch isn't up yet, Nextcloud itself still comes up fine; only search indexing/queries degrade until it is.

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/nextcloud/elasticsearch/app/helmrelease.yaml` | `app-template`-wrapped single-replica Elasticsearch container, resources, probes, persistence |
| `kubernetes/apps/nextcloud/elasticsearch/app/ocirepository.yaml` | Chart source pin (`app-template` v5.1.0) |
| `kubernetes/apps/nextcloud/elasticsearch/app/ciliumnetworkpolicy.yaml` | Ingress allow-list (same-namespace only) + DNS-only egress |
| `kubernetes/apps/nextcloud/elasticsearch/app/kustomization.yaml` | Wires the three manifests above under the `nextcloud` namespace |
| `kubernetes/apps/nextcloud/elasticsearch/ks.yaml` | Flux Kustomization: `dependsOn: csi-driver-nfs`, 1h interval, `targetNamespace: nextcloud` |

No `externalsecret*.yaml` and no `httproute.yaml` exist under `app/` — confirmed by directory listing, not omission.

## Secrets
None. No ExternalSecret is defined for this app; `xpack.security.enabled: "false"` in `helmrelease.yaml` means there is no credential to manage in the first place — access control is entirely at the network layer (see Routing & access).

## Routing & access
- No HTTPRoute — not exposed via either Envoy gateway, internal or external. Only reachable in-cluster.
- `ciliumnetworkpolicy.yaml` ingress: allows TCP/9200 only from pods in the `nextcloud` namespace (Nextcloud's own containers reaching `elasticsearch:9200`, alongside `clamav`/`collabora`/`whiteboard`).
- Egress is locked to kube-dns only (UDP/TCP 53) — no path out to the internet at all. This is likely why `ingest.geoip.downloader.enabled: "false"` is set in `helmrelease.yaml` (inference, not a cited rationale): Elasticsearch's built-in GeoIP database downloader would otherwise try to reach `geoip.elastic.co` over HTTPS on startup and be dropped by this egress policy.
- No SSO/OIDC — not applicable, nothing browser-facing here.

## Storage
One PVC via `persistence.data` in `helmrelease.yaml`: 30Gi, `zfs-nfs` StorageClass, `ReadWriteOnce`, mounted at `/usr/share/elasticsearch/data`. Lives in the `nextcloud` namespace, which is in the `includedNamespaces` list of all three Velero GFS schedules — no namespace-level or resource-level exclusion applies to this PVC, so it is backed up as part of the namespace-wide daily/weekly/monthly sweep, not via an app-specific schedule.

## Known quirks
- **Single node, no security, no TLS — by design, not oversight.** `discovery.type: single-node`, `xpack.security.enabled: "false"`, `xpack.security.http.ssl.enabled: "false"`. This is safe only because the CiliumNetworkPolicy above restricts ingress to the `nextcloud` namespace — there is no other layer of auth on port 9200.
- **`node.store.allow_mmap: "false"`** (with an inline comment) is set specifically so Talos doesn't need a `vm.max_map_count` sysctl bump — Elasticsearch normally requires raising that host-level limit for mmap-based storage, which isn't available the same way under Talos's immutable OS model.
- **Major-version upgrades on existing data must step through the last 8.x release first.** Commit `2398c5b` ("downgrade Elasticsearch to 8.19.0 as intermediate upgrade step") records that a direct `8.15.3 → 9.4.3` jump (commit `60d747b`) made Elasticsearch refuse to boot against the existing data volume. The working path taken was 8.15.3 → 8.19.0 → 8.19.18 → 8.19.19 → 9.5.0 → 9.5.1. Relevant again for any future major-version bump.
- **`fix-perms` init container runs as root (`runAsUser: 0`, `runAsNonRoot: false`) to `chown -R 1000:1000` the data dir on every pod start**, because the app container itself runs as uid 1000 and the volume is NFS-backed. Commit `181fed2` ("fsGroup is pod-level; chown ES data dir on NFS") explains this was needed because `fsGroup` can't be set per-container in this chart schema and pod-level `fsGroup` doesn't reliably take effect on NFS the same way it does on block storage.
- **`strategy: Recreate`, single replica.** Matches the `ReadWriteOnce` PVC — no case for `RollingUpdate` with one replica and one exclusive-mount volume.

## Common operations
- Upgrade Elasticsearch image tag: edit `image.tag` in `helmrelease.yaml`, commit, push. **Do not jump major versions directly** if the data volume already has data on it — see Known quirks; step through the latest patch of the current major first.
- Force reconcile: `flux reconcile helmrelease elasticsearch -n nextcloud`.
- Pause reconciliation: `flux suspend helmrelease elasticsearch -n nextcloud` / `flux suspend kustomization elasticsearch -n flux-system`.
- Re-trigger a Nextcloud full-text search reindex after any Elasticsearch downtime/data loss: rerun the `occ` commands in `kubernetes/apps/nextcloud/nextcloud/app/post-install-job.yaml:95-98`, or trigger via `kubectl exec -n nextcloud deploy/nextcloud -c nextcloud -- php occ fulltextsearch:live-index` (not yet confirmed against this specific chart version — validate before relying on this exact subcommand).

## TODOs / unknowns
- No incident under `docs/incidents/` currently references this app — no history to draw "known quirks" from beyond commit messages.
- The `elastic_index` value Nextcloud configures (`nextcloud`) is app-level config, not a secret, but its actual index contents/size were not inspected for this doc.
- Whether the geoip-downloader-egress inference above (Routing & access) is the actual reason it was disabled, versus simply "not needed for this use case," is unconfirmed — no commit message or comment ties the two together explicitly.

---
_Cite every non-obvious claim with a repo-root-relative file path (e.g. `kubernetes/apps/security/authentik/app/helmrelease.yaml`), not a bare filename — this doc lives under `docs/apps/`, so relative paths must resolve from there._
