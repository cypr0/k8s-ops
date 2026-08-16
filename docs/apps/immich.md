# Immich

> **Namespace**  `immich`
> **Source**     OCI chart `ghcr.io/bjw-s-labs/helm/app-template` v5.1.0 (generic app wrapper)
> **Hostname**   `media.${SECRET_DOMAIN}` (both external and internal gateways)

## What it does here
Self-hosted photo and video management platform. Provides mobile-app-compatible media upload/sync, ML-powered face recognition and duplicate detection, reverse geocoding, and OIDC-backed SSO via Authentik. Backs onto a dedicated CNPG PostgreSQL cluster (`immichdb` with pgvector extension) and the shared Dragonfly instance (Redis-compatible cache, DB index 2).

## Architecture at a glance
- **Depends on:** CNPG cluster `postgres` (namespace `database`, database `immichdb`), Dragonfly (namespace `database`, index 2), ExternalSecret → 1Password item `immich` + `dragonfly`, NFS-backed PVC (`zfs-nfs` StorageClass via `csi-driver-nfs`), Authentik OIDC (issuer `https://id.${SECRET_DOMAIN}/application/o/immich/`)
- **Depended on by:** None directly; consumed by end-users via mobile/web clients

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/immich/immich/ks.yaml` | Kustomization: depends on `cloudnative-pg-databases`, `csi-driver-nfs`, `external-secrets-stores` |
| `kubernetes/apps/immich/immich/app/helmrelease.yaml` | Chart version (app-template 5.1.0), immich-server + immich-machine-learning containers (both v3.1.0), resource limits, probes |
| `kubernetes/apps/immich/immich/app/externalsecret.yaml` | Pulls DB credentials, JWT secret, Redis password, OIDC client secret; templates `immich.json` config file |
| `kubernetes/apps/immich/immich/app/httproute.yaml` | Routes `media.${SECRET_DOMAIN}` to immich-server service (port 2283), 1h request timeout |
| `kubernetes/apps/immich/immich/app/ciliumnetworkpolicy.yaml` | Ingress from envoy (network ns) + Gatus; egress to DNS, database ns (postgres/dragonfly), Authentik via envoy-internal, world (HTTPS + SMTP) |
| `kubernetes/apps/immich/immich/app/pvc.yaml` | 500Gi RWX claim on `zfs-nfs` for media storage |
| `kubernetes/apps/immich/immich/app/ocirepository.yaml` | Flux OCIRepository for app-template chart |

## Secrets
| ExternalSecret | 1Password item → field | Consumer |
| --- | --- | --- |
| `immich` | `immich` → `IMMICH_POSTGRESQL_USERNAME`, `IMMICH_POSTGRESQL_PASSWORD`, `IMMICH_JWT_SECRET`, `IMMICH_OPENID_CLIENT_ID`, `IMMICH_OPENID_CLIENT_SECRET` | Templated into `immich-secret`; DB creds → `DB_USERNAME`/`DB_PASSWORD` env vars, JWT → `JWT_SECRET`, OIDC client ID/secret embedded in `immich.json` (mounted as secret file at `IMMICH_CONFIG_FILE`) |
| `immich` | `dragonfly` → `DRAGONFLY_PASSWORD` | Templated into `immich-secret` → `REDIS_PASSWORD` env var (shared Dragonfly instance password, not a separate immich-specific credential) |

**Note:** The ExternalSecret's `immich.json` template embeds the real OAuth client secret inline, so it must be mounted as a secret volume, not a ConfigMap — see `kubernetes/apps/immich/immich/app/externalsecret.yaml` lines 21–149 and `kubernetes/apps/immich/immich/app/helmrelease.yaml` lines 163–173.

## Routing & access
- **HTTPRoute:** `media.${SECRET_DOMAIN}` via both `envoy-external` (public, presumably via cloudflare-tunnel) and `envoy-internal` (LAN-only) gateways, 1-hour request timeout for large uploads (`kubernetes/apps/immich/immich/app/httproute.yaml`)
- **SSO:** OIDC via Authentik (`issuerUrl: https://id.${SECRET_DOMAIN}/application/o/immich/`), auto-launch enabled, password login disabled — see `immich.json` oauth block in `kubernetes/apps/immich/immich/app/externalsecret.yaml` lines 96–111
- **CiliumNetworkPolicy:** Ingress from envoy (network ns) + Gatus health checks; egress to kube-dns, database ns (postgres port 5432 + dragonfly port 6379), Authentik via envoy-internal (port 10443), same-namespace (immich-server ↔ immich-machine-learning), and world (HTTPS for map tiles/reverse geocoding/version checks, SMTP port 25 for notifications) — `kubernetes/apps/immich/immich/app/ciliumnetworkpolicy.yaml`

## Storage
- **PVC:** `immich-media`, 500Gi, `ReadWriteMany`, StorageClass `zfs-nfs` (NFS-backed via `csi-driver-nfs`) — `kubernetes/apps/immich/immich/app/pvc.yaml`
- **Backup:** TODO — no Velero/Kopia schedule reference found in manifests; CNPG database backup is handled separately (see `kubernetes/apps/database/` for postgres cluster backup config)
- **Cache:** `emptyDir` volume mounted at `/cache` in immich-machine-learning container only (ephemeral ML model cache)

## Known quirks
- **Runs as root (UID 0):** The media PVC is NFS-backed, and `csi-driver-nfs` doesn't enforce `fsGroup` — a non-root UID can't reliably write to a freshly-provisioned NFS export without it. See `defaultPodOptions.securityContext` in `kubernetes/apps/immich/immich/app/helmrelease.yaml` lines 25–30 and inline comment lines 21–24. Same pattern as `open-webui`.
- **Dragonfly DB index 2:** Chosen because indexes 0 (open-webui, paperless-ngx), 1 (nextcloud), 3 (whiteboard), 4 (firecrawl) were already allocated — see `REDIS_DBINDEX: "2"` in `kubernetes/apps/immich/immich/app/helmrelease.yaml` line 141 and inline comment lines 138–141.
- **Shared Dragonfly password:** The ExternalSecret pulls `DRAGONFLY_PASSWORD` from the `dragonfly` 1Password item (not the `immich` item's own `IMMICH_REDIS_PASSWORD` field, if one exists) — every Dragonfly consumer in this cluster authenticates the same way, and a separately-stored copy could drift stale if Dragonfly's password is rotated. See `kubernetes/apps/immich/immich/app/externalsecret.yaml` lines 15–20.
- **pgvector, not vectorchord:** The `DB_VECTOR_EXTENSION` is explicitly set to `pgvector` in `kubernetes/apps/immich/immich/app/helmrelease.yaml` line 145 — comment references `database-immich.yaml` for rationale (file not provided in this manifest set, likely in `kubernetes/apps/database/`).
- **Hardcoded timezone:** `TZ: Europe/Berlin` is hardcoded in the ConfigMap (`kubernetes/apps/immich/immich/app/helmrelease.yaml` line 130) — no `TIMEZONE` key exists in `cluster-secrets`, and every other app in this repo (paperless-ngx, gatus, open-webui, hermes-agent) hardcodes the literal instead. See inline comment lines 127–129.
- **1-hour request timeout:** HTTPRoute sets both `request` and `backendRequest` timeouts to 1 hour (`kubernetes/apps/immich/immich/app/httproute.yaml` lines 28–29) — accommodates large photo/video uploads without gateway timeout.

## Common operations
- **Upgrade chart version:** Edit `kubernetes/apps/immich/immich/app/ocirepository.yaml` (app-template chart tag) or `kubernetes/apps/immich/immich/app/helmrelease.yaml` (immich-server/immich-machine-learning image tags), commit, push; Flux reconciles within 1h or force with `flux reconcile helmrelease immich -n immich`.
- **Rotate a secret:** Update the 1Password item (`immich` or `dragonfly`), then `kubectl annotate externalsecret immich -n immich force-sync=$(date +%s)` (or wait for the refresh interval).
- **Pause reconciliation:** `flux suspend kustomization immich -n immich` / `flux suspend helmrelease immich -n immich`.
- **Check ML service connectivity:** `kubectl exec -n immich deploy/immich-main -c immich-server -- curl -v http://immich-immich-machine-learning.immich.svc.cluster.local:3003/ping` (ML service URL is in `immich.json` machineLearning.urls array).
- **Inspect config file:** `kubectl get secret immich-secret -n immich -o jsonpath='{.data.immich\.json}' | base64 -d | jq` (contains OAuth client secret, so handle carefully).

## TODOs / unknowns
- **Backup coverage:** No Velero/Kopia schedule reference found in provided manifests — verify whether `immich-media` PVC is included in any cluster-wide backup job (check `kubernetes/apps/velero/` or equivalent).
- **CNPG cluster name:** Manifests reference `postgres-rw.database.svc.cluster.local` and database name `immichdb`, but the actual CNPG cluster resource name isn't in this file set — likely `postgres` in namespace `database`, but confirm with `kubectl get cluster -n database`.
- **Authentik blueprint:** OIDC client ID/secret are pulled from 1Password, but no Authentik blueprint file is present in `kubernetes/apps/immich/` or cross-referenced — verify whether `kubernetes/apps/security/authentik/app/blueprints/` contains an `immich.yaml` or if this was manually configured.
- **SMTP relay authentication:** `immich.json` SMTP config has empty `username`/`password` fields and `ignoreCert: true` — confirm whether `${SECRET_MAIL_SERVER}` is an unauthenticated internal relay or if credentials should be added (see `kubernetes/apps/immich/immich/app/externalsecret.yaml` lines 119–127).

---
**Secret/IP scan:** Clean. No resolved secret values, real public IPs, or account-identifying paths restated. All credentials cited via ExternalSecret resource + 1Password item/field names only.

_Cite every non-obvious claim with a repo-root-relative file path (e.g. `kubernetes/apps/immich/immich/app/helmrelease.yaml`), not a bare filename — this doc lives under `docs/apps/`, so relative paths must resolve from there._
