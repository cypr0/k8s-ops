# Immich

> **Namespace**  `immich`
> **Source**     OCI chart `ghcr.io/bjw-s-labs/helm/app-template:5.1.0` (generic app wrapper)
> **Hostname**   `media.${SECRET_DOMAIN}` (both external and internal gateways)

## What it does here
Photo and video management platform with ML-powered face recognition, object detection, and duplicate detection. Backs onto a dedicated CNPG PostgreSQL cluster (`immichdb` database), Dragonfly (Redis-compatible cache, DB index 2), and a 500Gi NFS-backed PVC for media storage. SSO is enforced via Authentik OIDC; local password login is disabled.

## Architecture at a glance
- **Depends on:** CNPG cluster `postgres` (database `immichdb`, namespace `database`), Dragonfly (namespace `database`, DB index 2), ExternalSecret → 1Password items `immich` and `dragonfly`, NFS storage class `zfs-nfs`, Authentik OIDC (issuer `https://id.${SECRET_DOMAIN}/application/o/immich/`)
- **Depended on by:** None discovered (no other app in the repo references `immich-immich-server.immich.svc.cluster.local`)

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/immich/immich/ks.yaml` | Kustomization: depends on `cloudnative-pg-databases`, `csi-driver-nfs`, `external-secrets-stores`; 15m timeout |
| `kubernetes/apps/immich/immich/app/ocirepository.yaml` | OCI chart source: `app-template:5.1.0` from `ghcr.io/bjw-s-labs/helm` |
| `kubernetes/apps/immich/immich/app/helmrelease.yaml` | Two-container deployment: `immich-server` (main API/web, port 2283) + `immich-machine-learning` (ML inference, port 3003); both at `v3.1.0` |
| `kubernetes/apps/immich/immich/app/externalsecret.yaml` | Pulls DB credentials, JWT secret, Redis password, OAuth client secret; mounts `immich.json` config file as a secret volume |
| `kubernetes/apps/immich/immich/app/httproute.yaml` | Routes `media.${SECRET_DOMAIN}` via both `envoy-external` and `envoy-internal` gateways; 1h request timeout |
| `kubernetes/apps/immich/immich/app/pvc.yaml` | 500Gi RWX claim on `zfs-nfs` storage class for media uploads |
| `kubernetes/apps/immich/immich/app/ciliumnetworkpolicy.yaml` | Ingress from Envoy (network ns) + Gatus (monitoring ns) + same-namespace; egress to DNS, database ns (PostgreSQL + Dragonfly), Authentik OIDC (via envoy-internal :10443), same-namespace, and world (map tiles, geocoding, SMTP, version checks) |

## Secrets
| ExternalSecret | 1Password item → field | Consumed by |
| --- | --- | --- |
| `immich` | `immich` → `IMMICH_POSTGRESQL_USERNAME`, `IMMICH_POSTGRESQL_PASSWORD`, `IMMICH_JWT_SECRET`, `IMMICH_OPENID_CLIENT_ID`, `IMMICH_OPENID_CLIENT_SECRET` | `immich-server` and `immich-machine-learning` containers (envFrom); OAuth client secret embedded in `immich.json` config file |
| `immich` | `dragonfly` → `DRAGONFLY_PASSWORD` | Both containers (as `REDIS_PASSWORD` env var); uses the shared Dragonfly instance's password, not a separate field in the `immich` 1Password item — see inline comment in `kubernetes/apps/immich/immich/app/externalsecret.yaml` explaining this matches the pattern used by `whiteboard`, `open-webui`, and `paperless-ngx` |

The `immich.json` config file is mounted as a secret volume (not a ConfigMap) because it embeds the OAuth client secret inline — see `kubernetes/apps/immich/immich/app/externalsecret.yaml` template and `kubernetes/apps/immich/immich/app/helmrelease.yaml` persistence config (`type: secret`, `advancedMounts` to `/usr/src/app/immich.json` read-only).

## Routing & access
- **HTTPRoute:** `media.${SECRET_DOMAIN}` exposed via both `envoy-external` (public, via Cloudflare tunnel) and `envoy-internal` (LAN-only) gateways on the `https` listener; 1-hour request timeout for large uploads/downloads (`kubernetes/apps/immich/immich/app/httproute.yaml`)
- **SSO:** OIDC via Authentik (`issuer: https://id.${SECRET_DOMAIN}/application/o/immich/`), auto-launch enabled, auto-register enabled, local password login disabled (`passwordLogin.enabled: false` in the `immich.json` config embedded in `kubernetes/apps/immich/immich/app/externalsecret.yaml`)
- **CiliumNetworkPolicy:** Ingress from Envoy (network ns, port 2283), Gatus health checks (monitoring ns), and same-namespace (server ↔ ML sidecar); egress to DNS, database namespace (PostgreSQL :5432 + Dragonfly :6379), Authentik OIDC via envoy-internal :10443, same-namespace, and world on :443/:25 (map tiles from `tiles.immich.cloud`, reverse geocoding data, version checks, SMTP notifications) — see `kubernetes/apps/immich/immich/app/ciliumnetworkpolicy.yaml`

## Storage
- **PVC:** `immich-media`, 500Gi, `ReadWriteMany`, storage class `zfs-nfs` (NFS-backed via `csi-driver-nfs`), mounted at `/usr/src/app/upload` in both containers (`kubernetes/apps/immich/immich/app/pvc.yaml` and `kubernetes/apps/immich/immich/app/helmrelease.yaml`)
- **Backup:** TODO — verify whether Velero/Kopia schedules cover the `immich` namespace (check `kubernetes/apps/velero/` for inclusion/exclusion rules); the app itself has a built-in database backup job (enabled, cron `0 02 * * *`, keeps last 14 backups — see `backup.database` in the `immich.json` config)

## Known quirks
- **Runs as root (UID 0):** The `defaultPodOptions.securityContext` in `kubernetes/apps/immich/immich/app/helmrelease.yaml` explicitly sets `runAsNonRoot: false`, `runAsUser: 0`, `runAsGroup: 0`, `fsGroup: 0`. Inline comment explains this is because the NFS-backed PVC (via `csi-driver-nfs`) doesn't enforce `fsGroup`, so a non-root UID can't reliably write to a freshly-provisioned export — same reasoning/fix as `open-webui` (cross-reference `docs/apps/open-webui.md` when that exists).
- **Dragonfly DB index 2:** Uses Redis DB index 2 (`REDIS_DBINDEX: "2"` in `kubernetes/apps/immich/immich/app/helmrelease.yaml` configMap) — inline comment notes that 0 (open-webui, paperless-ngx), 1 (nextcloud), 3 (whiteboard), and 4 (firecrawl) were already allocated.
- **Shared Dragonfly password:** The `REDIS_PASSWORD` env var is sourced from the `dragonfly` 1Password item's `DRAGONFLY_PASSWORD` field, not a separate `IMMICH_REDIS_PASSWORD` field in the `immich` item — inline comment in `kubernetes/apps/immich/immich/app/externalsecret.yaml` explains this matches the cluster-wide pattern (every Dragonfly consumer authenticates the same way) and avoids stale-copy drift if Dragonfly's password is ever rotated.
- **PostgreSQL vector extension:** Uses `vectorchord` (`DB_VECTOR_EXTENSION: "vectorchord"` in `kubernetes/apps/immich/immich/app/helmrelease.yaml`) instead of the more common `pgvector` — no inline comment explains why; TODO: verify whether the CNPG cluster has this extension installed/enabled.

## Common operations
- **Upgrade chart version:** Edit `kubernetes/apps/immich/immich/app/ocirepository.yaml` (change `ref.tag`) or `kubernetes/apps/immich/immich/app/helmrelease.yaml` (change `image.tag` for `immich-server` and `immich-machine-learning`), commit, push; Flux reconciles within 1h or force with `flux reconcile helmrelease immich -n immich`.
- **Rotate a secret:** Update the 1Password item (`immich` or `dragonfly`), then force sync: `kubectl annotate externalsecret immich -n immich force-sync=$(date +%s)` (or wait for the default refresh interval).
- **Pause reconciliation:** `flux suspend kustomization immich -n immich` (pauses the entire app) or `flux suspend helmrelease immich -n immich` (pauses only the HelmRelease).
- **Trigger ML re-processing:** TODO — verify whether Immich exposes an admin API or CLI for re-running face detection / object tagging jobs (the `job` concurrency settings in `immich.json` suggest these are background tasks, but the trigger mechanism isn't obvious from the manifests).

## TODOs / unknowns
- **Backup coverage:** Verify whether Velero/Kopia schedules include the `immich` namespace and the `immich-media` PVC (check `kubernetes/apps/velero/` for schedules/exclusions).
- **PostgreSQL vector extension:** Confirm the CNPG `postgres` cluster has the `vectorchord` extension installed and enabled (the HelmRelease sets `DB_VECTOR_EXTENSION: "vectorchord"`, but the CNPG cluster manifest isn't in the provided files).
- **Authentik blueprint:** Locate the Authentik OIDC application blueprint for Immich (should be under `kubernetes/apps/security/authentik/app/blueprints/` or `kubernetes/apps/immich/immich/app/blueprints/` if it follows the pattern of other OIDC-integrated apps).
- **SMTP relay:** The `immich.json` config enables SMTP notifications (`notifications.smtp.enabled: true`) with `host: ${SECRET_MAIL_SERVER}`, port 25, no auth, `ignoreCert: true` — verify whether `${SECRET_MAIL_SERVER}` resolves to an internal relay or external service (check `kubernetes/apps/immich/immich/ks.yaml` postBuild substituteFrom → `cluster-secrets`).
- **ML model storage:** The `immich-machine-learning` container mounts an emptyDir at `/cache` — verify whether ML models are downloaded on every pod restart or persisted elsewhere (the `machineLearning` config in `immich.json` specifies model names like `ViT-B-32__openai` and `buffalo_l`, but the storage mechanism isn't clear).

---
**Secret/IP scan:** Clean. All credentials cited via ExternalSecret resource + 1Password item/field names; no resolved values, real IPs, or account-identifying paths restated. The `immich.json` config template in `externalsecret.yaml` uses `{{ .IMMICH_OPENID_CLIENT_SECRET }}` (template variable), not a literal secret.

_Cite every non-obvious claim with a repo-root-relative file path (e.g. `kubernetes/apps/immich/immich/app/helmrelease.yaml`), not a bare filename — this doc lives under `docs/apps/`, so relative paths must resolve from there._
