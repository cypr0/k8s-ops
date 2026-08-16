# Immich

> **Namespace**  `immich`
> **Source**     bjw-s app-template (OCI, tag 5.1.0) wrapping ghcr.io/immich-app/immich-server v3.1.0
> **Hostname**   `media.${SECRET_DOMAIN}` (both envoy-external and envoy-internal gateways)

## What it does here
Photo and video management platform with ML-powered face recognition, duplicate detection, and CLIP-based semantic search. Serves as the cluster's primary media library, backed by a dedicated CNPG PostgreSQL cluster (`immichdb` with pgvector extension) and the shared Dragonfly instance (DB index 2). SSO-only access via Authentik OIDC; local password login is disabled.

## Architecture at a glance
- **Depends on:** CNPG cluster `postgres` (namespace `database`, database `immichdb`), Dragonfly (namespace `database`, DB index 2), ExternalSecret → 1Password items `immich` and `dragonfly`, NFS-backed PVC (`zfs-nfs` StorageClass, 500Gi), Authentik OIDC
- **Depended on by:** None (leaf service)

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/immich/immich/ks.yaml` | Kustomization with dependencies on `cloudnative-pg-databases`, `csi-driver-nfs`, `external-secrets-stores` |
| `kubernetes/apps/immich/immich/app/helmrelease.yaml` | Chart version (app-template 5.1.0), immich-server + immich-machine-learning containers, resource limits, probes |
| `kubernetes/apps/immich/immich/app/externalsecret.yaml` | Pulls DB/Redis credentials, JWT secret, OAuth client secret; renders `immich.json` config file |
| `kubernetes/apps/immich/immich/app/httproute.yaml` | Routes `media.${SECRET_DOMAIN}` to immich-server:2283 via both external and internal gateways, 1h request timeout |
| `kubernetes/apps/immich/immich/app/ciliumnetworkpolicy.yaml` | Ingress from envoy (network ns) + gatus (monitoring ns); egress to DNS, database ns (postgres/dragonfly), authentik (via envoy-internal :10443), same-namespace pods, and world (HTTPS + SMTP) |
| `kubernetes/apps/immich/immich/app/pvc.yaml` | 500Gi RWX claim on `zfs-nfs` for media storage |
| `kubernetes/apps/immich/immich/app/ocirepository.yaml` | Flux OCIRepository pointing to bjw-s app-template 5.1.0 |

## Secrets
| ExternalSecret | 1Password item → field | Consumer |
| --- | --- | --- |
| `immich-secret` | `immich` → `IMMICH_POSTGRESQL_USERNAME`, `IMMICH_POSTGRESQL_PASSWORD` | `DB_USERNAME`, `DB_PASSWORD` env vars (both containers) |
| `immich-secret` | `immich` → `IMMICH_JWT_SECRET` | `JWT_SECRET` env var (both containers) |
| `immich-secret` | `dragonfly` → `DRAGONFLY_PASSWORD` | `REDIS_PASSWORD` env var (both containers); note this pulls the shared Dragonfly instance's password, not a separate `IMMICH_REDIS_PASSWORD` field from the `immich` item — see inline comment in `kubernetes/apps/immich/immich/app/externalsecret.yaml` |
| `immich-secret` | `immich` → `IMMICH_OPENID_CLIENT_ID`, `IMMICH_OPENID_CLIENT_SECRET` | Rendered into `immich.json` (mounted as secret file at `/usr/src/app/immich.json` in immich-server container) for OAuth config |

The `immich.json` file is mounted from the secret (not a ConfigMap) because it embeds the OAuth client secret inline — see `kubernetes/apps/immich/immich/app/externalsecret.yaml` template block and `kubernetes/apps/immich/immich/app/helmrelease.yaml` persistence.config.

## Routing & access
- **Gateway:** HTTPRoute `media.${SECRET_DOMAIN}` attached to both `envoy-external` (public via cloudflare-tunnel) and `envoy-internal` (LAN-only) gateways in namespace `network`, section `https`. Request and backend timeouts set to 1 hour (large video uploads).
- **SSO:** OIDC via Authentik, issuer `https://id.${SECRET_DOMAIN}/application/o/immich/`, auto-launch enabled, local password login disabled (`passwordLogin.enabled: false` in `immich.json`). OAuth client ID/secret pulled from 1Password item `immich`. TODO: locate the corresponding Authentik blueprint file (not found in `kubernetes/apps/immich/` or `kubernetes/apps/security/authentik/app/blueprints/` in the provided manifests).
- **Network policy:** CiliumNetworkPolicy allows ingress from envoy (network ns) and gatus (monitoring ns), egress to DNS, database namespace (postgres + dragonfly), authentik via envoy-internal :10443, same-namespace pods, and world (HTTPS for map tiles/reverse geocoding/version checks, SMTP :25 for notifications).

## Storage
- **PVC:** `immich-media`, 500Gi, `ReadWriteMany`, StorageClass `zfs-nfs` (NFS-backed via csi-driver-nfs). Mounted at `/usr/src/app/upload` in both containers.
- **Cache:** `emptyDir` volume at `/cache` in immich-machine-learning container only (model cache).
- **Backup:** TODO: verify Velero/Kopia schedule coverage — no explicit annotation or label found in the provided manifests.

## Known quirks
- **Runs as root (UID 0):** The media PVC is NFS-backed, and csi-driver-nfs does not enforce `fsGroup` — a non-root UID cannot reliably write to a freshly-provisioned NFS export without it. See `defaultPodOptions.securityContext` in `kubernetes/apps/immich/immich/app/helmrelease.yaml`. This is the same reasoning/fix applied to open-webui's PVC (cross-reference: `docs/apps/open-webui.md` if available).
- **Dragonfly DB index 2:** Chosen because indexes 0 (open-webui, paperless-ngx), 1 (nextcloud), 3 (whiteboard), and 4 (firecrawl) were already allocated at the time immich was added. See `REDIS_DBINDEX: "2"` in `kubernetes/apps/immich/immich/app/helmrelease.yaml` configMaps.immich-config.
- **pgvector, not vectorchord:** The CNPG cluster uses the `pgvector` extension (`DB_VECTOR_EXTENSION: "pgvector"` in helmrelease.yaml). The comment in helmrelease.yaml references `database-immich.yaml` for rationale, but that file was not provided in the manifests — TODO: locate and cite the reasoning (likely performance or compatibility with immich's vector search requirements).
- **Hardcoded timezone:** `TZ: Europe/Berlin` is set directly in the ConfigMap; no `TIMEZONE` key exists in `cluster-secrets`. Every other app in this repo (paperless-ngx, gatus, open-webui, hermes-agent) also hardcodes `Europe/Berlin` rather than substituting from a shared variable — see inline comment in `kubernetes/apps/immich/immich/app/helmrelease.yaml`.
- **SMTP without auth:** Notifications use `${SECRET_MAIL_SERVER}` on port 25 with empty username/password and `ignoreCert: true` — this assumes an internal relay that does not require authentication. See `notifications.smtp.transport` in the `immich.json` template within `kubernetes/apps/immich/immich/app/externalsecret.yaml`.

## Common operations
- **Upgrade immich version:** Edit `image.tag` for both `immich-server` and `immich-machine-learning` containers in `kubernetes/apps/immich/immich/app/helmrelease.yaml`, commit, push. Flux reconciles within 1h or force with `flux reconcile helmrelease immich -n immich`.
- **Upgrade app-template chart:** Edit `ref.tag` in `kubernetes/apps/immich/immich/app/ocirepository.yaml`, commit, push. Flux reconciles the OCIRepository within 1h, then the HelmRelease.
- **Rotate a secret:** Update the 1Password item (`immich` or `dragonfly`), then force sync: `kubectl annotate externalsecret immich -n immich force-sync=$(date +%s)` (or wait for the default refresh interval).
- **Pause reconciliation:** `flux suspend kustomization immich -n immich` (pauses the entire app) or `flux suspend helmrelease immich -n immich` (pauses only the HelmRelease).
- **Check ML model cache:** `kubectl exec -n immich deploy/immich-main -c immich-machine-learning -- du -sh /cache`
- **Inspect rendered config:** `kubectl get secret immich-secret -n immich -o jsonpath='{.data.immich\.json}' | base64 -d | jq .` (contains OAuth client secret — do not log or share output).

## TODOs / unknowns
- **Authentik blueprint:** The OAuth issuer URL and client ID are configured, but the corresponding Authentik blueprint file (expected under `kubernetes/apps/security/authentik/app/blueprints/` or `kubernetes/apps/immich/`) was not found in the provided manifests. Verify the blueprint exists and link it here.
- **Backup schedule:** No Velero or Kopia annotation/label found in the manifests. Confirm whether the `immich-media` PVC and the `immichdb` CNPG cluster are included in a backup schedule, and document the retention policy.
- **pgvector vs vectorchord rationale:** The helmrelease.yaml comment references `database-immich.yaml` for the reasoning behind choosing `pgvector` over `vectorchord`, but that file was not provided. Locate and cite the explanation.
- **Node affinity:** The helmrelease sets `nodeAffinity` to exclude control-plane nodes, but no explicit node selector for GPU or high-memory workers is present. Confirm whether immich-machine-learning benefits from GPU acceleration (the `ffmpeg.accel` config is set to `disabled` in `immich.json`) or if the current CPU-only setup is intentional.

---
**Secret/IP scan:** Clean. All credentials cited via ExternalSecret resource and 1Password item/field names; no resolved secret values, real public IPs, or account-identifying paths present. The `${SECRET_DOMAIN}` and `${SECRET_MAIL_SERVER}` substitutions are cluster-secrets references, not literal values.

_Cite every non-obvious claim with a repo-root-relative file path (e.g. `kubernetes/apps/immich/immich/app/helmrelease.yaml`), not a bare filename — this doc lives under `docs/apps/`, so relative paths must resolve from there._
