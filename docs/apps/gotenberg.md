# Gotenberg

> **Namespace**  paperless
> **Source**     `app-template` OCIRepository v5.0.1 (`kubernetes/apps/paperless/gotenberg/app/ocirepository.yaml`), chart-templated `docker.io/gotenberg/gotenberg:8.36.0` image (`kubernetes/apps/paperless/gotenberg/app/helmrelease.yaml`)
> **Hostname**   none — internal only, `gotenberg-http.paperless.svc.cluster.local:3000`

## What it does here
A stateless HTTP microservice wrapping headless Chromium plus LibreOffice-style converters to turn HTML/office documents into PDF. Paperless-ngx points its Tika integration at it via `PAPERLESS_TIKA_GOTENBERG_ENDPOINT` (`kubernetes/apps/paperless/paperless-ngx/app/helmrelease.yaml:123`), alongside the sibling `tika` service (`PAPERLESS_TIKA_ENDPOINT`, same file line 124), so that non-PDF consumed documents get converted before OCR/archival. It holds no state of its own and has no consumer outside `paperless-ngx`.

## Architecture at a glance
- **Depends on:** nothing internal — no database, no cache, no secrets. Runs on the `app-template` chart pinned via `ocirepository.yaml`.
- **Depended on by:** `paperless-ngx` only. A repo-wide grep for `gotenberg` outside this directory turns up exactly one consumer: `kubernetes/apps/paperless/paperless-ngx/app/helmrelease.yaml:123` plus the matching CiliumNetworkPolicy egress rule. Nothing else in the cluster reaches it.

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/paperless/gotenberg/ks.yaml` | Flux Kustomization — 1h reconcile interval, no `dependsOn`, `targetNamespace: paperless` |
| `kubernetes/apps/paperless/gotenberg/app/ocirepository.yaml` | Pins the `bjw-s-labs/app-template` chart (v5.0.1) used to render the workload |
| `kubernetes/apps/paperless/gotenberg/app/helmrelease.yaml` | Image tag, Chromium command flags, resources, probes, securityContext, `emptyDir` persistence |
| `kubernetes/apps/paperless/gotenberg/app/kustomization.yaml` | Wires the two resources above, sets `namespace: paperless` |

Note: unlike most apps in this repo, gotenberg's own `app/` directory has **no** `ciliumnetworkpolicy.yaml` or `httproute.yaml` — its network policy is defined as one document in a shared multi-document file living under the `paperless-ngx` app instead (see Routing & access below). Auditing `kubernetes/apps/paperless/gotenberg/app/` in isolation would miss it.

## Secrets
None. There is no `externalsecret*.yaml` under this app's directory — the container takes no credentials, config, or env vars beyond the CLI flags baked into `command:`.

## Routing & access
- No HTTPRoute — never reached from outside the cluster; only `paperless-ngx` pods talk to it.
- **CiliumNetworkPolicy** (`kubernetes/apps/paperless/paperless-ngx/app/ciliumnetworkpolicy.yaml`, the `gotenberg` policy object): ingress limited to pods in the `paperless` namespace labeled `app.kubernetes.io/name: paperless` on TCP `3000`; egress limited to kube-dns on `53`/UDP+TCP only — no `toEntities: world` rule, so unlike `paperless-ngx` (which needs egress for OpenRouter and IMAP) gotenberg has no outbound internet path at all. That's consistent with it doing local rendering only.
- The reverse direction — `paperless-ngx` reaching gotenberg — is one of several same-namespace egress rules in that app's own policy.
- **Chromium hardening flags** in `command:`: `--chromium-disable-javascript=true` disables JS execution while rendering HTML-derived conversions, and `--chromium-allow-list=file:///tmp/.*` restricts the embedded browser to only ever open `file://` URIs under `/tmp` — defense-in-depth against a converted document trying to make Chromium fetch or read something outside its sandbox.

## Storage
No PVC. `persistence.tmp` is an `emptyDir` mounted at `/tmp`, matched by `readOnlyRootFilesystem: true` in the container's `securityContext` — `/tmp` is the only writable path, and it's wiped on every pod restart. The `paperless` namespace as a whole is in Velero's daily/weekly/monthly GFS schedule, but gotenberg contributes no PVC to that — there is nothing here to back up or restore.

## Known quirks
- **Image bumped 8.34.0 → 8.36.0 on 2026-08-16** (commit `02628d6`): Trivy flagged 8.34.0's bundled Chromium (149.0.7827.102) with 48 CRITICAL / 664 HIGH CVEs — "by far the worst image in the cluster" per the commit message — and Renovate hadn't yet picked up 8.35.0/8.36.0 despite them being released, so the bump was done by hand ahead of Renovate. This is the second manual bump to this image: an earlier commit (`12cc9fa`) moved it 8.26.0 → 8.34.0 on 2026-06-27.
- **CPU/memory `requests` only exist since 2026-07-05** (commit `03a2e1e`), added explicitly as HPA prep ("HPA needs `resources.requests.cpu` to compute utilization" per the commit message) alongside `tika` and `envoy-gateway`. No `HorizontalPodAutoscaler` resource for gotenberg exists anywhere in the repo as of this writing — it still runs as a static `replicas: 1`, the requests are prep for autoscaling that hasn't landed yet.
- **`runAsUser`/`runAsGroup: 3000`** match `paperless-ngx`'s `USERMAP_UID`/`USERMAP_GID`, even though the two pods share no volume (gotenberg's only mount is its own `emptyDir`) — reads as a cluster-wide UID convention for this app family rather than a functional dependency.
- No `docs/incidents/` entry references gotenberg as of this writing, and no `# NOTE`/`# HACK`/`# WORKAROUND` inline comments exist in its files beyond the dated bump comment cited above.

## Common operations
- Upgrade image tag: edit `spec.values.controllers.gotenberg.containers.gotenberg.image.tag` in `helmrelease.yaml`, commit, push, Flux reconciles within 1h (or `flux reconcile helmrelease gotenberg -n paperless`).
- Check it's reachable from paperless-ngx: `kubectl exec -n paperless deploy/paperless -c paperless -- curl -sf http://gotenberg-http.paperless.svc.cluster.local:3000/health`.
- Pause reconciliation: `flux suspend kustomization gotenberg -n flux-system` / `flux suspend helmrelease gotenberg -n paperless`.

## TODOs / unknowns
- Whether/when an actual `HorizontalPodAutoscaler` gets added for gotenberg (resource requests are already in place, but the HPA object itself doesn't exist yet) — future work, not yet trackable from the repo.
- Whether `readOnlyRootFilesystem: true` plus the `/tmp`-only `emptyDir` will keep working across future Gotenberg image versions is unverified beyond "the current 8.36.0 image runs fine with this config."

---
_Cite every non-obvious claim with a repo-root-relative file path (e.g. `kubernetes/apps/security/authentik/app/helmrelease.yaml`), not a bare filename — this doc lives under `docs/apps/`, so relative paths must resolve from there._
