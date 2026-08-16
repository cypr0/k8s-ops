# Immich

> **Namespace**  `immich`
> **Source**     OCI chart `ghcr.io/bjw-s-labs/helm/app-template` (v5.1.0), wrapping upstream `ghcr.io/immich-app/immich-server` and `immich-machine-learning` v3.1.0 images
> **Hostname**   `media.${SECRET_DOMAIN}` (both external and internal gateways)

## What it does here
Self-hosted photo and video management platform. Provides mobile-app-compatible media upload, ML-powered face detection and duplicate detection, reverse geocoding, and OIDC-backed SSO via Authentik. Backs onto a dedicated CNPG PostgreSQL cluster (`immichdb`) with pgvector extension, shared Dragonfly (Redis-compatible) instance for job queues, and a 500Gi NFS-backed PVC for media storage.

## Architecture at a glance
- **Depends on:** CNPG cluster `postgres` (namespace `database`, database `immichdb`), Dragonfly (namespace `database`, DB index 2), ExternalSecret → 1Password items `immich` and `dragonfly`, NFS storage class `zfs-nfs`, Authentik OIDC (issuer `https://id.${SECRET_DOMAIN}/application/o/immich/`)
- **Depended on by:** None directly; consumed by end-users via mobile apps and web UI

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/immich/immich/app/helmrelease.yaml` | Chart version, two-container pod (immich-server + immich-machine-learning), resource limits, probes |
| `kubernetes/apps/immich/immich/app/externalsecret.yaml` | Pulls DB credentials, JWT secret, Redis password, OAuth client secret; templates `immich.json` config file |
| `kubernetes/apps/immich/immich/app/httproute.yaml` | Routes `media.${SECRET_DOMAIN}` to immich-server service on port 2283, 1h request timeout |
| `kubernetes/apps/immich/immich/app/ciliumnetworkpolicy.yaml` | Ingress from envoy-external/internal + Gatus; egress to DNS, database (PostgreSQL + Dragonfly), Authentik OIDC, external (map tiles, SMTP, version checks) |
| `kubernetes/apps/immich/immich/app/pvc.yaml` | 500Gi RWX NFS-backed media storage (`zfs-nfs` StorageClass) |
| `kubernetes/apps/immich/immich/app/ocirepository.yaml` | Flux OCIRepository for bjw-s app-template chart v5.1.0 |
| `kubernetes/apps/immich/immich/ks.yaml` | Kustomization with 15m timeout, depends on `cloudnative-pg-databases`, `csi-driver-nfs`, `external-secrets-stores` |

## Secrets
| ExternalSecret | 1Password item → field | Consumer |
| --- | --- | --- |
| `immich` | `immich` → `IMMICH_POSTGRESQL_USERNAME`, `IMMICH_POSTGRESQL_PASSWORD`, `IMMICH_JWT_SECRET`, `IMMICH_OPENID_CLIENT_ID`, `IMMICH_OPENID_CLIENT_SECRET` | Templated into `immich-secret`; DB credentials and JWT secret as env vars, OAuth client ID/secret embedded in `immich.json` config file |
| `immich` | `dragonfly` → `DRAGONFLY_PASSWORD` | Templated as `REDIS_PASSWORD` env var (shared Dragonfly instance password, not a separate immich-specific credential) |

The `immich.json` config file is mounted as a Secret volume (not ConfigMap) at `/usr/src/app/immich.json` because it embeds the OAuth client secret — see inline comment in `kubernetes/apps/immich/immich/app/externalsecret.yaml`.

## Routing & access
- **Gateway:** Both `envoy-external` and `envoy-internal` (namespace `network`), HTTPS section, hostname `media.${SECRET_DOMAIN}` — accessible from WAN (via cloudflare-tunnel, presumed from gateway name) and LAN
- **SSO:** OIDC via Authentik, issuer `https://id.${SECRET_DOMAIN}/application/o/immich/`, auto-launch and auto-register enabled, password login disabled (`passwordLogin.enabled: false` in `immich.json` template)
- **Timeouts:** 1h request and backend request timeout (HTTPRoute spec) — accommodates large photo/video uploads
- **CiliumNetworkPolicy:** Allows ingress from envoy (both gateways) and Gatus health checks; egress to DNS, database namespace (PostgreSQL + Dragonfly), Authentik OIDC via envoy-internal (:10443), same-namespace (immich-server ↔ immich-machine-learning), and world (HTTPS + SMTP for map tiles, reverse geocoding, version checks, email notifications)

## Storage
- **PVC:** `immich-media`, 500Gi, `ReadWriteMany`, StorageClass `zfs-nfs` (NFS-backed via csi-driver-nfs)
- **Mount:** `/usr/src/app/upload` in both containers
- **Backup:** TODO — no Velero schedule reference found in `kubernetes/apps/velero/` or inline annotations; CNPG database backup is enabled (daily 02:00, 14-day retention) per `immich.json` template's `backup.database` stanza, but PVC backup coverage is unknown

## Known quirks
- **Runs as root (UID 0):** The media PVC is NFS-backed, and `csi-driver-nfs` doesn't enforce `fsGroup` — a non-root UID can't reliably write to a freshly-provisioned NFS export without it. Explicitly sets `runAsNonRoot: false`, `runAsUser: 0`, `runAsGroup: 0`, `fsGroup: 0` in `defaultPodOptions.securityContext` (`kubernetes/apps/immich/immich/app/helmrelease.yaml`). Same pattern as `open-webui` (see `docs/apps/open-webui.md` if it exists).
- **Dragonfly password sourced from shared instance, not immich's own 1Password item:** The `REDIS_PASSWORD` env var is templated from the `dragonfly` 1Password item's `DRAGONFLY_PASSWORD` field, not from an `IMMICH_REDIS_PASSWORD` field in the `immich` item. Inline comment in `kubernetes/apps/immich/immich/app/externalsecret.yaml` explains this is intentional — every Dragonfly consumer in the cluster (whiteboard, open-webui, paperless-ngx) authenticates the same way, and a separately-stored copy could drift stale if Dragonfly's password is rotated.
- **Dragonfly DB index 2:** Chosen because indexes 0, 1, 3, 4 were already allocated to other apps at the time immich was added (comment in `helmrelease.yaml` configMap data).
- **pgvector, not vectorchord:** The `DB_VECTOR_EXTENSION` env var is set to `pgvector` — a comment in `helmrelease.yaml` references `database-immich.yaml` for rationale (file not provided in this manifest set, likely in `kubernetes/apps/database/`).
- **No TIMEZONE substitution:** Hardcodes `TZ: Europe/Berlin` in the configMap instead of using a `${SECRET_TIMEZONE}` substitution — inline comment notes no such key exists in `cluster-secrets`, and other apps (paperless-ngx, gatus, open-webui, hermes-agent) also hardcode the literal.
- **Two-container pod:** `immich-server` (port 2283, user-facing API) and `immich-machine-learning` (port 3003, internal ML inference service) run in the same pod. The machine-learning container is referenced by URL in the `immich.json` config (`machineLearning.urls: ["http://immich-immich-machine-learning.immich.svc.cluster.local:3003"]`), but both containers share the same pod IP, so this could also resolve via localhost — the full cluster DNS name is used anyway.

## Common operations
- **Upgrade chart version:** Edit `kubernetes/apps/immich/immich/app/ocirepository.yaml` (change `ref.tag`), commit, push; Flux reconciles within 1h or force with `flux reconcile ocirepository app-template -n immich`, then `flux reconcile helmrelease immich -n immich`.
- **Upgrade immich image version:** Edit `kubernetes/apps/immich/immich/app/helmrelease.yaml` (change `image.tag` for `immich-server` and/or `immich-machine-learning`), commit, push; Flux reconciles within 1h or force with `flux reconcile helmrelease immich -n immich`.
- **Rotate a secret:** Update the 1Password item (`immich` or `dragonfly`), then `kubectl annotate externalsecret immich -n immich force-sync=$(date +%s)` (or wait for the default refresh interval). Note that rotating the Dragonfly password affects all Dragonfly consumers cluster-wide.
- **Pause reconciliation:** `flux suspend kustomization immich -n immich` (pauses the entire app) or `flux suspend helmrelease immich -n immich` (pauses only the HelmRelease, leaves ExternalSecret/HTTPRoute/PVC active).
- **Check database backup status:** Database backups are configured in `immich.json` (daily 02:00, 14-day retention) — check CNPG cluster status with `kubectl get cluster postgres -n database -o yaml` and look for backup completion timestamps.

## TODOs / unknowns
- **PVC backup coverage:** No Velero schedule annotation found on the PVC or in `kubernetes/apps/velero/` manifests — unclear if the 500Gi media volume is backed up, or if the CNPG database backup alone is considered sufficient.
- **Authentik blueprint location:** The OAuth client ID/secret are pulled from 1Password, but the corresponding Authentik application blueprint (which would define the client in Authentik's config) is not in `kubernetes/apps/immich/immich/app/` — likely in `kubernetes/apps/security/authentik/app/blueprints/` but not verified.
- **database-immich.yaml reference:** The `helmrelease.yaml` comment mentions `database-immich.yaml` for the pgvector vs vectorchord decision, but that file is not in the provided manifest set — likely in `kubernetes/apps/database/` or a CNPG cluster definition.

---
**Secret/IP scan:** Clean. No resolved secret values, real public IPs, or account-identifying paths restated. All credentials cited via ExternalSecret resource + 1Password item/field names. The `${SECRET_DOMAIN}` and `${SECRET_MAIL_SERVER}` substitutions are templated at apply-time by Flux from `cluster-secrets` and never appear in plaintext in this doc.

_All file paths are relative to repo root. This doc lives at `docs/apps/immich.md`._
