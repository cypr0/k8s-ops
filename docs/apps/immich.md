# Immich

> **Namespace**  `immich`
> **Source**     OCI chart `ghcr.io/bjw-s-labs/helm/app-template` v5.1.0 (wraps upstream `ghcr.io/immich-app/immich-server` and `immich-machine-learning` v3.1.0)
> **Hostname**   `media.${SECRET_DOMAIN}` (both external and internal gateway)

## What it does here
Self-hosted photo and video management platform. Provides mobile-app-backed media upload, ML-powered face recognition and duplicate detection, reverse geocoding, and OIDC-authenticated access via Authentik. Backs onto a dedicated CNPG PostgreSQL cluster (`immichdb`) with pgvector extension, shared Dragonfly (Redis-compatible) instance for job queues, and a 500Gi NFS-backed PVC for media storage.

## Architecture at a glance
- **Depends on:** CNPG cluster `postgres` (namespace `database`, database `immichdb`), Dragonfly (namespace `database`, DB index 2), ExternalSecret → 1Password items `immich` and `dragonfly`, NFS storage class `zfs-nfs`, Authentik OIDC (issuer `https://id.${SECRET_DOMAIN}/application/o/immich/`)
- **Depended on by:** None directly; consumed by end-users via mobile app and web UI

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/immich/immich/ks.yaml` | Kustomization: depends on `cloudnative-pg-databases`, `csi-driver-nfs`, `external-secrets-stores`; 15m timeout, waits for ready |
| `kubernetes/apps/immich/immich/app/helmrelease.yaml` | Chart version (app-template 5.1.0), image tags (v3.1.0), resource limits, dual-container pod (server + ML), env/config references |
| `kubernetes/apps/immich/immich/app/externalsecret.yaml` | Pulls DB credentials, JWT secret, Redis password, OAuth client secret; templates `immich.json` config file inline |
| `kubernetes/apps/immich/immich/app/httproute.yaml` | Routes `media.${SECRET_DOMAIN}` via both `envoy-external` and `envoy-internal` gateways; 1h request timeout for large uploads |
| `kubernetes/apps/immich/immich/app/ciliumnetworkpolicy.yaml` | Ingress from Envoy (network ns) and Gatus (monitoring ns); egress to DNS, database ns (postgres + dragonfly), Authentik via envoy-internal :10443, world (HTTPS + SMTP) |
| `kubernetes/apps/immich/immich/app/pvc.yaml` | 500Gi RWX claim on `zfs-nfs` for media storage |
| `kubernetes/apps/immich/immich/app/ocirepository.yaml` | Flux OCI source for bjw-s app-template chart |

## Secrets
| ExternalSecret | 1Password item → field | Consumed by |
| --- | --- | --- |
| `immich` | `immich` → `IMMICH_POSTGRESQL_USERNAME`, `IMMICH_POSTGRESQL_PASSWORD`, `IMMICH_JWT_SECRET`, `IMMICH_OPENID_CLIENT_ID`, `IMMICH_OPENID_CLIENT_SECRET` | `immich-secret` → env vars `DB_USERNAME`, `DB_PASSWORD`, `JWT_SECRET`; OAuth fields templated into `immich.json` mounted as secret file at `IMMICH_CONFIG_FILE` |
| `immich` | `dragonfly` → `DRAGONFLY_PASSWORD` | `immich-secret` → env var `REDIS_PASSWORD` (shared Dragonfly instance password, not a separate immich-specific credential — see inline comment in `kubernetes/apps/immich/immich/app/externalsecret.yaml`) |

**Secret/IP scan:** clean — no resolved values, real IPs, or account-identifying paths restated; all citations point to ExternalSecret resource structure and 1Password item/field names only.

## Routing & access
- **Gateway:** `envoy-external` and `envoy-internal` (namespace `network`), HTTPS section, hostname `media.${SECRET_DOMAIN}` — accessible both from public internet (via cloudflare-tunnel, implied by external gateway) and internal cluster/LAN
- **SSO:** OIDC via Authentik, issuer `https://id.${SECRET_DOMAIN}/application/o/immich/`, auto-launch enabled, auto-register enabled, password login disabled — OAuth client ID/secret pulled from 1Password `immich` item and templated into `immich.json` (see `kubernetes/apps/immich/immich/app/externalsecret.yaml` template section)
- **Network policy:** `kubernetes/apps/immich/immich/app/ciliumnetworkpolicy.yaml` allows:
  - Ingress from Envoy (network ns) on port 2283, Gatus health checks (monitoring ns), and same-namespace pods (server ↔ ML sidecar)
  - Egress to kube-dns, database namespace (postgres :5432, dragonfly :6379), Authentik via envoy-internal :10443, and world (:443 for map tiles/geocoding/version checks, :25 for SMTP notifications)

## Storage
- **PVC:** `immich-media`, 500Gi, `ReadWriteMany`, storage class `zfs-nfs` (NFS-backed via `csi-driver-nfs`) — mounted at `/usr/src/app/upload` in both containers
- **Cache:** `emptyDir` volume mounted at `/cache` in `immich-machine-learning` container only
- **Backup:** TODO — verify whether Velero/Kopia schedules cover this namespace; `immich.json` config enables daily database backups (cron `0 02 * * *`, keep 14 days) but that's app-internal postgres dumps, not cluster-level PVC snapshots

## Known quirks
- **Runs as root (UID 0):** The media PVC is NFS-backed, and `csi-driver-nfs` doesn't enforce `fsGroup` — a non-root UID can't reliably write to a freshly-provisioned NFS export without it. See `defaultPodOptions.securityContext` in `kubernetes/apps/immich/immich/app/helmrelease.yaml` and the inline comment there. Same pattern as `open-webui` (see `docs/apps/open-webui.md` if that exists).
- **Dragonfly DB index 2:** Explicitly set to avoid collision with other consumers — index 0 (open-webui, paperless-ngx), 1 (nextcloud), 3 (whiteboard), 4 (firecrawl) were already allocated. See `REDIS_DBINDEX: "2"` in `kubernetes/apps/immich/immich/app/helmrelease.yaml` configMap data.
- **pgvector, not vectorchord:** The `DB_VECTOR_EXTENSION` is set to `pgvector` — the HelmRelease comment references `database-immich.yaml` for reasoning, but that file is not in the provided manifests. TODO: locate `kubernetes/apps/database/*/database-immich.yaml` or equivalent CNPG cluster definition to verify the extension choice rationale.
- **Shared Dragonfly password:** The `REDIS_PASSWORD` field in the ExternalSecret pulls from the `dragonfly` 1Password item's `DRAGONFLY_PASSWORD` field, not a separate `IMMICH_REDIS_PASSWORD` from the `immich` item — this is intentional (every Dragonfly consumer in the cluster authenticates the same way), but means rotating Dragonfly's password requires updating all consumers, not just immich. See inline comment in `kubernetes/apps/immich/immich/app/externalsecret.yaml`.
- **1h request timeout:** HTTPRoute sets `request: "1h"` and `backendRequest: "1h"` timeouts to accommodate large video uploads without gateway-level cuts. See `kubernetes/apps/immich/immich/app/httproute.yaml`.
- **Dual-container pod:** `immich-server` (port 2283, user-facing API) and `immich-machine-learning` (port 3003, internal-only ML inference) run in the same pod, not separate deployments — the ML container is referenced by `immich-server` via `http://immich-immich-machine-learning.immich.svc.cluster.local:3003` (see `machineLearning.urls` in the templated `immich.json`).

## Common operations
- **Upgrade chart version:** Edit `kubernetes/apps/immich/immich/app/ocirepository.yaml` (app-template tag) or `kubernetes/apps/immich/immich/app/helmrelease.yaml` (immich-server/immich-machine-learning image tags), commit, push. Flux reconciles within 1h or force with `flux reconcile helmrelease immich -n immich`.
- **Rotate a secret:** Update the 1Password item (`immich` or `dragonfly`), then force ExternalSecret sync: `kubectl annotate externalsecret immich -n immich force-sync=$(date +%s)` (or wait for the default refresh interval). Note: rotating the Dragonfly password affects all consumers (open-webui, paperless-ngx, nextcloud, whiteboard, firecrawl, immich) — coordinate accordingly.
- **Pause reconciliation:** `flux suspend kustomization immich -n immich` or `flux suspend helmrelease immich -n immich`.
- **Check ML model cache:** `kubectl exec -n immich deploy/immich-main -c immich-machine-learning -- ls -lh /cache` — models are downloaded on first use and cached in the emptyDir volume (lost on pod restart).
- **Trigger manual media scan:** Access admin UI at `https://media.${SECRET_DOMAIN}/admin` (requires OIDC login), navigate to Jobs → Library, click "Scan All Libraries" — or wait for the daily cron (`0 0 * * *`, see `library.scan.cronExpression` in `immich.json`).

## TODOs / unknowns
- **CNPG cluster definition:** The HelmRelease references `database-immich.yaml` in a comment explaining the `pgvector` choice, but that file is not in the provided manifests. Locate `kubernetes/apps/database/*/database-immich.yaml` (or equivalent CNPG `Cluster` resource) to verify the PostgreSQL cluster name, pgvector extension setup, and any immich-specific tuning.
- **Backup coverage:** Verify whether Velero or Kopia schedules include the `immich` namespace and the `immich-media` PVC. The app's internal database backup (daily at 02:00, keep 14 days) is a postgres dump, not a cluster-level snapshot.
- **Authentik blueprint:** Confirm whether `kubernetes/apps/security/authentik/app/blueprints/` contains an `immich.yaml` blueprint defining the OAuth application (client ID/secret, redirect URIs, scopes) — not provided in these manifests, but implied by the OIDC issuer URL.
- **Node affinity rationale:** The HelmRelease sets `nodeAffinity` to exclude control-plane nodes — verify whether this is a resource constraint (ML workload too heavy for control-plane nodes) or a general policy. No inline comment explains the choice.

---
_All file paths are relative to repo root. Claims about upstream chart defaults, Dragonfly DB index allocation, and the "runs as root" NFS workaround are drawn from inline comments in the manifests; the pgvector vs vectorchord choice references a file not provided and is marked as a TODO._
