# Nextcloud

> **Namespace**  nextcloud
> **Source**     `nextcloud` HelmRepository, chart `nextcloud` v9.2.6 (`kubernetes/apps/nextcloud/nextcloud/app/helmrelease.yaml`, `helmrepository.yaml`), image `docker.io/library/nextcloud:34.0.3-fpm-alpine`
> **Hostname**   `cloud.${SECRET_DOMAIN}` (public via `envoy-external`, and internally via `envoy-internal` — see Routing)

## What it does here
File sync/share and groupware (calendar, contacts, mail) for personal/household use, backed by CNPG postgres, Dragonfly (cache + PHP sessions), and a set of sibling apps under `kubernetes/apps/nextcloud/` that this deployment integrates with rather than bundles: Collabora (Office document editing), ClamAV (upload antivirus scanning), Elasticsearch (full-text search backend), Whiteboard, and a dedicated metrics exporter.

## Architecture at a glance
- **Depends on:** CNPG postgres (database `nextcloud`, `externalDatabase` block in `helmrelease.yaml`), Dragonfly (`dragonfly.database.svc.cluster.local`, redis db 1 for cache, db 3 for PHP sessions — via the `before-starting` hook), Authentik (OIDC), 1Password (`nextcloud-env` and `nextcloud-postgres` ExternalSecrets).
- **Depended on by:** `collabora` (WOPI callback — see Routing), `whiteboard`, `nextcloud-exporter`, `clamav` (upload-scan hook), `elasticsearch` (search backend) — all separate Kustomizations under `kubernetes/apps/nextcloud/`, gated as dependents of this one.

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/nextcloud/nextcloud/app/helmrelease.yaml` | Chart version, PHP config (`configs`/`phpConfigs`), init containers, resources |
| `kubernetes/apps/nextcloud/nextcloud/app/externalsecret.yaml` | Two ExternalSecrets — app credentials (`nextcloud-env`) and postgres creds (`nextcloud-postgres`) |
| `kubernetes/apps/nextcloud/nextcloud/app/httproute.yaml` | Dual-gateway routing (see Routing) |
| `kubernetes/apps/nextcloud/nextcloud/app/ciliumnetworkpolicy.yaml` | Ingress allow-list: envoy, same-namespace siblings, Gatus health check |
| `kubernetes/apps/nextcloud/nextcloud/app/post-install-job.yaml` | One-time post-install setup job |
| `kubernetes/apps/nextcloud/nextcloud/app/pvc.yaml` | `nextcloud-pvc` (app/config) + `nextcloud-data-pvc` (user data) |

## Secrets
| ExternalSecret | 1Password source | Consumed by |
| --- | --- | --- |
| `nextcloud-env` → `nextcloud-credentials` | `nextcloud` + `dragonfly` items — admin creds, SMTP, Dragonfly password, OIDC client id/secret | Nextcloud container env, `existingSecret` admin bootstrap |
| `nextcloud-postgres` → `nextcloud-postgres-credentials` | postgres host/db/user/password | `externalDatabase` block |

## Routing & access
- **Dual-gateway HTTPRoute**, same pattern as Authentik: `cloud.${SECRET_DOMAIN}` is attached to both `envoy-external` (public) and `envoy-internal`. The internal attachment exists specifically for Collabora's WOPI `CheckFileInfo` server-to-server callback — over the public Cloudflare hairpin, Nextcloud saw the connection arrive from Cloudflare's edge IP, which `richdocuments`' `wopi_allowlist` (RFC1918-only) rejected with a 403 ("Unauthorised WOPI host"). CoreDNS's split-horizon `hosts` rule resolves `cloud.${SECRET_DOMAIN}` in-cluster to the internal gateway ClusterIP so the connection lands inside the pod CIDR instead.
- 1h request/backend timeouts on the HTTPRoute (`httproute.yaml`) — accommodates large file uploads/long-running WebDAV operations.
- SSO: OIDC via Authentik, client credentials in `nextcloud-env` (`OIDC_CLIENT_ID`/`OIDC_CLIENT_SECRET`); the actual OAuth2Provider blueprint lives under `security/authentik/app/blueprints/06-nextcloud-oidc.yaml`.
- CiliumNetworkPolicy allows ingress only from: `envoy` (network ns), same-namespace siblings (Collabora/Whiteboard callbacks, the exporter), and Gatus (`monitoring` ns) for the `/status.php` health check.

## Storage
Two PVCs (`persistence.existingClaim`/`nextcloudData.existingClaim` in `helmrelease.yaml`): `nextcloud-pvc` (app root/config/custom_apps) and `nextcloud-data-pvc` (user files, mounted with `fsGroup: 82`, `fsGroupChangePolicy: OnRootMismatch`). Backup coverage not yet cited from this repo directly (TODO).

## Known quirks
- **Two ownership-fixing init containers, for different reasons.** `validate-subpaths` (best-effort `chown`/`chmod`, `|| echo WARN` — never fatal) checks all the chart's various subPath mounts exist and are owned correctly; `fix-data-permissions` does the same specifically for the data PVC, but **without** the best-effort guard (`chown`/`chmod` run unguarded). Given this session's own hermes-agent incident (`docs/incidents/2026-08-16-hermes-agent-restore-pvc-chown-permission-denied.md`) confirmed that a long-lived NFS-backed mount can reject `chown` outright even under `CAP_CHOWN`, `fix-data-permissions` here carries the same latent risk if `nextcloud-data-pvc` is ever restored from backup or otherwise becomes a long-lived mount past its first provisioning — worth hardening the same way if it's ever hit.
- **`deploymentStrategy: Recreate`**, not `RollingUpdate` — because `replicaCount: 1` and the PVCs are `ReadWriteOnce`-shaped; a rolling update would need two pods holding the same volume simultaneously.
- **PHP sessions are Redis-backed via a raw `.ini` file dropped by the `before-starting` hook**, not a chart-native option — necessary so sessions (and OIDC state) survive across the single replica's restarts and would survive a future multi-replica setup. Uses `$$` escaping throughout `configs`/`hooks` blocks to keep these as runtime shell/PHP variable references, not Flux `postBuild` substitution targets.
- **This app was the surfaced victim of the 2026-08-16 CoreDNS incident** (`docs/incidents/2026-08-16-coredns-aaaa-nxdomain-breaks-internal-dns.md`) — its `nginx` startup probe (`GET /status.php`, `failureThreshold: 360` — very tolerant, ~1h) is exactly what made the DNS regression visible fast, since it retries every 10s and fails loudly, unlike many other apps that might have degraded more silently.

## Common operations
- Upgrade chart: edit `helmrelease.yaml` `spec.chart.spec.version`, commit, push. Given `deploymentStrategy: Recreate` and a 30m install/upgrade timeout, expect a real downtime window during any change that recreates the pod.
- Run `occ` commands: `kubectl exec -n nextcloud deploy/nextcloud -c nextcloud -- php occ <command>`.
- Force reconcile: `flux reconcile helmrelease nextcloud -n nextcloud`.

## TODOs / unknowns
- Backup coverage (Velero/Kopia schedule inclusion) for `nextcloud-pvc`/`nextcloud-data-pvc` not verified from this repo directly for this doc — check `kubernetes/apps/velero/` schedules before relying on this claim operationally.
- Whether `fix-data-permissions`' unguarded `chown` has ever actually failed against `nextcloud-data-pvc` in this cluster is unconfirmed — flagged above as a latent risk, not an observed incident.
