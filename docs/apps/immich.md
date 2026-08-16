# Immich

> **Namespace**  `immich`
> **Source**     OCI chart `ghcr.io/bjw-s-labs/helm/app-template` (v5.1.0), wrapping upstream `ghcr.io/immich-app/immich-server` and `ghcr.io/immich-app/immich-machine-learning` (both v3.1.0)
> **Hostname**   `media.${SECRET_DOMAIN}` (both external and internal gateways)

## What it does here
Photo and video management platform with ML-powered face recognition, object detection, and duplicate detection. Serves as the cluster's media library, backed by a dedicated CNPG PostgreSQL cluster (`immichdb` with pgvector extension), shared Dragonfly instance (DB index 2), and a 500Gi NFS-backed PVC for media storage. SSO-only authentication via Authentik OIDC; local password login is disabled.

## Architecture at a glance
- **Depends on:** CNPG cluster `postgres` (database `immichdb`, namespace `database`), Dragonfly (namespace `database`, DB index 2), ExternalSecret → 1Password items `immich` and `dragonfly`, NFS storage via `zfs-nfs` StorageClass, Authentik OIDC (issuer `https://id.${SECRET_DOMAIN}/application/o/immich/`)
- **Depended on by:** None directly; monitored by Gatus (health checks on port 2283)

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/immich/immich/app/helmrelease.yaml` | Chart version, two-container deployment (immich-server + immich-machine-learning), resource limits, probes |
| `kubernetes/apps/immich/immich/app/externalsecret.yaml` | DB credentials, JWT secret, Redis password (from shared Dragonfly), OAuth client secret, full `immich.json` config file |
| `kubernetes/apps/immich/immich/app/httproute.yaml` | Gateway routing for `media.${SECRET_DOMAIN}`, 1-hour request timeout (for large uploads/transcodes) |
| `kubernetes/apps/immich/immich/app/ciliumnetworkpolicy.yaml` | Ingress from envoy-external/internal + Gatus; egress to DNS, database (PostgreSQL + Dragonfly), Authentik OIDC, external (map tiles, geocoding, SMTP) |
| `kubernetes/apps/immich/immich/app/pvc.yaml` | 500Gi RWX claim on `zfs-nfs` for media storage |
| `kubernetes/apps/immich/immich/ks.yaml` | Kustomization with dependencies on `cloudnative-pg-databases`, `csi-driver-nfs`, `external-secrets-stores` |

## Secrets
| ExternalSecret | 1Password item → field | Consumer |
| --- | --- | --- |
| `immich-secret` | `immich` → `IMMICH_POSTGRESQL_USERNAME`, `IMMICH_POSTGRESQL_PASSWORD` | `DB_USERNAME`, `DB_PASSWORD` env vars (both containers) |
| `immich-secret` | `immich` → `IMMICH_JWT_SECRET` | `JWT_SECRET` env var (both containers) |
| `immich-secret` | `dragonfly` → `DRAGONFLY_PASSWORD` | `REDIS_PASSWORD` env var (both containers); note: pulls the shared Dragonfly instance's password, not a separate `IMMICH_REDIS_PASSWORD` field from the `immich` item, to avoid drift if Dragonfly's password is rotated |
| `immich-secret` | `immich` → `IMMICH_OPENID_CLIENT_ID`, `IMMICH_OPENID_CLIENT_SECRET` | Embedded in `immich.json` file (mounted as secret volume at `/usr/src/app/immich.json` in immich-server container only) |

The `immich.json` config file is templated inside the ExternalSecret and mounted as a secret volume (not a ConfigMap) because it embeds the OAuth client secret inline (`kubernetes/apps/immich/immich/app/externalsecret.yaml`).

## Routing & access
- **Gateway:** Exposed via both `envoy-external` and `envoy-internal` gateways on `media.${SECRET_DOMAIN}` (`kubernetes/apps/immich/immich/app/httproute.yaml`). Request timeout extended to 1 hour to accommodate large media uploads and video transcoding operations.
- **SSO:** OIDC via Authentik, issuer `https://id.${SECRET_DOMAIN}/application/o/immich/`, auto-launch and auto-register enabled, local password login disabled (`oauth.enabled: true`, `passwordLogin.enabled: false` in the `immich.json` config). OAuth client ID/secret pulled from 1Password item `immich`. TODO: locate the corresponding Authentik blueprint file (not found in `kubernetes/apps/immich/` or `kubernetes/apps/security/authentik/app/blueprints/` in the provided manifests).
- **Network policy:** `kubernetes/apps/immich/immich/app/ciliumnetworkpolicy.yaml` allows:
  - Ingress from `envoy` (network namespace) and `gatus` (monitoring namespace) on port 2283, plus same-namespace traffic (immich-server ↔ immich-machine-learning)
  - Egress to DNS, PostgreSQL (5432), Dragonfly (6379), Authentik OIDC via envoy-internal (10443), same-namespace, and world (443 for map tiles/geocoding/version checks, 25 for SMTP notifications)

## Storage
- **PVC:** `immich-media`, 500Gi, `zfs-nfs` StorageClass, RWX (`kubernetes/apps/immich/immich/app/pvc.yaml`), mounted at `/usr/src/app/upload` in both containers
- **Cache:** EmptyDir volume at `/cache` in the immich-machine-learning container only (for ML model caching)
- **Backup:** TODO: verify Velero/Kopia schedule coverage (not found in provided manifests). The `immich.json` config enables daily database backups at 02:00 (cron `0 02 * * *`), keeping last 14, but this is an in-app PostgreSQL dump feature, not cluster-level PVC backup.

## Known quirks
- **Runs as root (UID 0):** Both containers run as root because the NFS-backed PVC (`zfs-nfs` via `csi-driver-nfs`) doesn't enforce `fsGroup`, and a non-root UID can't reliably write to a freshly-provisioned NFS export without it (`kubernetes/apps/immich/immich/app/helmrelease.yaml`, `defaultPodOptions.securityContext`). This is the same reasoning/fix applied to `open-webui`'s PVC.
- **Dragonfly DB index 2:** Immich uses Redis DB index 2 on the shared Dragonfly instance. Indexes 0 (open-webui, paperless-ngx), 1 (nextcloud), 3 (whiteboard), and 4 (firecrawl) were already allocated at the time this was added (`kubernetes/apps/immich/immich/app/helmrelease.yaml`, `REDIS_DBINDEX` in `immich-config` ConfigMap).
- **pgvector, not vectorchord:** The `DB_VECTOR_EXTENSION` is explicitly set to `pgvector` (`kubernetes/apps/immich/immich/app/helmrelease.yaml`, `immich-config` ConfigMap). The comment references `database-immich.yaml` for reasoning, but that file was not provided in the manifests. TODO: locate `kubernetes/apps/database/*/database-immich.yaml` or equivalent to document why vectorchord was not used.
- **No TIMEZONE substitution:** The `TZ` env var is hardcoded to `Europe/Berlin` in the ConfigMap because no `TIMEZONE` key exists in `cluster-secrets` (`kubernetes/apps/immich/immich/app/helmrelease.yaml`). Every other app in this repo that needs a timezone (paperless-ngx, gatus, open-webui, hermes-agent) hardcodes the literal value instead of substituting from a cluster-wide secret.
- **1-hour request timeout:** HTTPRoute sets both `request` and `backendRequest` timeouts to 1 hour (`kubernetes/apps/immich/immich/app/httproute.yaml`) to prevent gateway timeouts during large media uploads or video transcoding jobs.

## Common operations
- **Upgrade chart version:** Edit `kubernetes/apps/immich/immich/app/ocirepository.yaml` (app-template chart) or `kubernetes/apps/immich/immich/app/helmrelease.yaml` (immich-server/immich-machine-learning image tags), commit, push. Flux reconciles within 1 hour, or force with `flux reconcile helmrelease immich -n immich`.
- **Rotate a secret:** Update the 1Password item (`immich` or `dragonfly`), then force sync: `kubectl annotate externalsecret immich -n immich force-sync=$(date +%s)` (or wait for the default refresh interval). Note: rotating the Dragonfly password affects every app using that instance (open-webui, paperless-ngx, nextcloud, whiteboard, firecrawl, immich).
- **Pause reconciliation:** `flux suspend kustomization immich -n immich` or `flux suspend helmrelease immich -n immich`.
- **Check ML model cache:** `kubectl exec -n immich deploy/immich-main -c immich-machine-learning -- du -sh /cache`
- **Trigger manual database backup:** The in-app backup feature runs daily at 02:00 (configured in `immich.json`), but can be triggered manually via the Immich admin UI (Administration → Jobs → Database Backup).

## TODOs / unknowns
- Locate the Authentik blueprint file for the `immich` OIDC application (not found in provided manifests under `kubernetes/apps/immich/` or `kubernetes/apps/security/authentik/app/blueprints/`).
- Locate `database-immich.yaml` or equivalent to document the reasoning for `pgvector` over `vectorchord` (referenced in a comment in `helmrelease.yaml` but file not provided).
- Verify Velero/Kopia backup schedule coverage for the `immich-media` PVC (no backup manifests provided).
- Confirm whether the `immichdb` database is part of a dedicated CNPG cluster or shares the `postgres` cluster with other apps (the Kustomization depends on `cloudnative-pg-databases`, but the specific cluster resource was not provided).

---
**Secret/IP scan:** Clean. All credentials cited via ExternalSecret resource + 1Password item/field names; no resolved values, real IPs, or account-identifying paths restated. The `${SECRET_DOMAIN}` and `${SECRET_MAIL_SERVER}` substitutions are from `cluster-secrets` and remain templated (not resolved) in the manifests.

---
_All file paths are repo-root-relative. This doc lives at `docs/apps/immich.md`._
