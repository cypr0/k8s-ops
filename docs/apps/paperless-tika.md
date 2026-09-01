# Tika (Paperless)

> **Namespace**  paperless
> **Source**     `apache/tika` image (`3.3.1.0-full`) deployed via the `app-template` chart (bjw-s-labs, OCIRepository `oci://ghcr.io/bjw-s-labs/helm/app-template`, tag `5.1.0`) — `kubernetes/apps/paperless/tika/app/ocirepository.yaml`, `kubernetes/apps/paperless/tika/app/helmrelease.yaml`
> **Hostname**   None — internal-only, reached at `tika-http.paperless.svc.cluster.local:9998`

## What it does here
Apache Tika content-detection/text-extraction backend for Paperless-ngx's document consumption pipeline. Paperless-ngx enables it with `PAPERLESS_TIKA_ENABLED: 1` and points `PAPERLESS_TIKA_ENDPOINT` at this service, alongside `PAPERLESS_TIKA_GOTENBERG_ENDPOINT` pointing at the sibling `gotenberg` app — the two run as a pair in Paperless's non-PDF ingestion path. It has no configuration of its own beyond the stock image; all Tika-related tuning (OCR language, DPI, mode) lives in `paperless-ngx`'s `helmrelease.yaml`, not here.

## Architecture at a glance
- **Depends on:** nothing else in the cluster — no CNPG, no ExternalSecret, no OIDC, no S3. Single stateless container.
- **Depended on by:** `paperless-ngx` (same namespace), which calls `http://tika-http.paperless.svc.cluster.local:9998`. Whether Paperless-ngx degrades gracefully or fails consumption tasks when this endpoint is unreachable is not verified from the repo (see TODOs).

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/paperless/tika/app/helmrelease.yaml` | Chart ref, image tag, resources, security context, service, `/tmp` emptyDir mount |
| `kubernetes/apps/paperless/tika/app/ocirepository.yaml` | `app-template` chart source (OCI, tag `5.1.0`) |
| `kubernetes/apps/paperless/tika/app/kustomization.yaml` | Wires the two resources above, sets namespace `paperless` |
| `kubernetes/apps/paperless/tika/ks.yaml` | Flux `Kustomization` — 1h interval, `prune: true`, targets `paperless` namespace |

No `externalsecret*.yaml`, `httproute.yaml`, or `ciliumnetworkpolicy.yaml` exist under this app's own directory — networking for this app is instead defined inside the `paperless-ngx` app's shared policy file (see Routing below).

## Secrets
None. No `ExternalSecret` resource in `kubernetes/apps/paperless/tika/app/`.

## Routing & access
No `HTTPRoute` — not exposed outside the cluster. Its `CiliumNetworkPolicy` is defined as a second policy block inside `kubernetes/apps/paperless/paperless-ngx/app/ciliumnetworkpolicy.yaml` (search `name: tika`), not in its own file:
- **Ingress:** only from pods labeled `app.kubernetes.io/name: paperless` in the `paperless` namespace, on TCP `9998`.
- **Egress:** only to `kube-dns` in `kube-system` for DNS resolution (UDP/TCP `53`).

The `paperless-ngx` policy's egress side mirrors this: it allows traffic to `app.kubernetes.io/name: tika` on TCP `9998`.

No SSO/OIDC — this is a backend service with no user-facing interface.

## Storage
No PVC. `persistence.tmp` is an `emptyDir` mounted at `/tmp` — ephemeral scratch space for in-flight document processing, discarded on pod restart. The `paperless` namespace as a whole is in Velero's daily/weekly/monthly schedules, but since tika holds no persistent state, it contributes nothing to back up or restore.

## Known quirks
- CPU/memory `requests`/`limits` (200m/512Mi request, 1000m/2Gi limit) were added in commit `03a2e1e` specifically as HPA prep ("Prerequisite for future HPA on gotenberg and tika") — no `HorizontalPodAutoscaler` actually exists yet for this app, so it currently runs as a fixed single replica (`replicas: 1`).
- Hardened `securityContext`: non-root (`runAsUser`/`runAsGroup: 3000`), `readOnlyRootFilesystem: true`, `allowPrivilegeEscalation: false`, all capabilities dropped — this is why `/tmp` needs an explicit writable `emptyDir` mount rather than relying on the container's own filesystem.

## Common operations
- Upgrade image tag: edit `tag:` in `kubernetes/apps/paperless/tika/app/helmrelease.yaml` (renovate-tracked), commit, push, Flux reconciles within the 1h interval (or force with `flux reconcile helmrelease tika -n paperless`).
- Pause reconciliation: `flux suspend kustomization tika -n flux-system` / `flux suspend helmrelease tika -n paperless`.

## TODOs / unknowns
- Behavior of `paperless-ngx` when this service is unreachable (fails the specific task vs. broader impact) is not verified from the repo — would require checking Paperless-ngx's own source/docs rather than this cluster's config.
- No incident in `docs/incidents/` currently references `tika` — nothing to cross-link.

---
_Cite every non-obvious claim with a repo-root-relative file path (e.g. `kubernetes/apps/security/authentik/app/helmrelease.yaml`), not a bare filename — this doc lives under `docs/apps/`, so relative paths must resolve from there._
