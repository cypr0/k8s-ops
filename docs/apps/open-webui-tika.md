# Tika (open-webui)

> **Namespace**  open-webui
> **Source**     `oci://ghcr.io/bjw-s-labs/helm/app-template` (v5.0.1), image `apache/tika:3.3.1.0-full`
> **Hostname**   none — internal-only, `tika.open-webui.svc.cluster.local:9998`

## What it does here
Dedicated document text-extraction backend for Open WebUI's RAG/file-upload pipeline in the `open-webui` namespace. Open WebUI's own HelmRelease points `CONTENT_EXTRACTION_ENGINE`/`TIKA_SERVER_URL` at this instance (`kubernetes/apps/open-webui/open-webui/app/helmrelease.yaml:87-88`) so that uploaded documents get parsed into text before being chunked into the pgvector RAG store. It is a single-purpose sidecar-style service, not shared with any other namespace — the `tika` app under `kubernetes/apps/paperless/` is a separate, unrelated instance dedicated to Paperless-ngx.

## Architecture at a glance
- **Depends on:** nothing — no database, no cache, no ExternalSecret. It's a stateless HTTP text-extraction server.
- **Depended on by:** `open-webui` (same namespace) for RAG document extraction, wired via `TIKA_SERVER_URL` in `kubernetes/apps/open-webui/open-webui/app/helmrelease.yaml:88`.

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/open-webui/tika/app/helmrelease.yaml` | app-template chart values: image, resources, probes, security context, emptyDir mount |
| `kubernetes/apps/open-webui/tika/app/ocirepository.yaml` | Pins `app-template` chart to `5.0.1` |
| `kubernetes/apps/open-webui/tika/app/kustomization.yaml` | Wires the two resources above, sets `namespace: open-webui` |
| `kubernetes/apps/open-webui/tika/ks.yaml` | Flux Kustomization — 1h interval, no `dependsOn` |
| `kubernetes/apps/open-webui/open-webui/app/ciliumnetworkpolicy.yaml` | Contains **this app's** CiliumNetworkPolicy (named `tika`), defined alongside open-webui's rather than in tika's own directory |

There is no `externalsecret*.yaml`, `httproute.yaml`, or Authentik blueprint for this app — it has no secrets, no external exposure, and no auth of its own.

## Secrets
None. No `ExternalSecret` exists under `kubernetes/apps/open-webui/tika/app/`.

## Routing & access
Not exposed via Gateway/HTTPRoute — internal-only, reached at `tika.open-webui.svc.cluster.local:9998`.

The CiliumNetworkPolicy for this app lives in the *open-webui* app's manifest, not its own directory (`kubernetes/apps/open-webui/open-webui/app/ciliumnetworkpolicy.yaml:124-156`):
- **Ingress:** only from pods labeled `app.kubernetes.io/name: open-webui` in the same namespace, on port 9998.
- **Egress:** DNS only (kube-dns on port 53). It never talks to anything else — no world egress, no other namespace.
- Open WebUI's own CNP grants itself the matching egress to `app.kubernetes.io/name: tika` on port 9998.

No OIDC/SSO — this service is not user-facing.

## Storage
No PVC. `persistence.tmp` is an `emptyDir` mounted at `/tmp`, required because the container runs with `readOnlyRootFilesystem: true` and Tika needs somewhere writable to stage documents during extraction. Being fully ephemeral, it carries no Velero/Kopia backup coverage and needs none — nothing here is durable state.

## Known quirks
- Open WebUI's RAG config comment notes a real functional trade-off from using Tika as the extraction engine: web-search HTML result pages come back **empty** from Tika, so if web-search context is ever needed again, `CONTENT_EXTRACTION_ENGINE` would need to be switched away from `tika` (`kubernetes/apps/open-webui/open-webui/app/helmrelease.yaml:83-87`). This is a property of the consumer's config, not this app's manifests, but it's the main reason to know this service exists before touching RAG behavior.
- Single replica (`replicas: 1`) with no PDB or HPA — a restart briefly breaks RAG document ingestion for Open WebUI, but chat itself is unaffected since Tika isn't in the request path for normal conversations.
- Runs as fixed non-root UID/GID `35002` with all capabilities dropped — if a chart bump changes the image's expected user, extraction requests would start failing with permission errors on `/tmp`.

## Common operations
- Upgrade Tika image: edit the `tag:` in `kubernetes/apps/open-webui/tika/app/helmrelease.yaml` (renovate-tracked via the inline comment), commit, push.
- Upgrade the app-template chart: edit `ref.tag` in `kubernetes/apps/open-webui/tika/app/ocirepository.yaml`.
- Force reconcile: `flux reconcile helmrelease tika -n open-webui`.
- Pause reconciliation: `flux suspend kustomization tika -n flux-system` / `flux suspend helmrelease tika -n open-webui`.

## TODOs / unknowns
- No incident under `docs/incidents/` currently references this app.
- Not verified against the upstream `apache/tika` image whether UID `35002` is baked into the `-full` image or an override — the HelmRelease sets it explicitly either way, so behavior is deterministic regardless.

---
_Cite every non-obvious claim with a repo-root-relative file path (e.g. `kubernetes/apps/security/authentik/app/helmrelease.yaml`), not a bare filename — this doc lives under `docs/apps/`, so relative paths must resolve from there._
