# Authentik

> **Namespace**  security
> **Source**     `https://charts.goauthentik.io`, chart `authentik` v2026.8.0 (`kubernetes/apps/security/authentik/app/helmrepository.yaml`, `helmrelease.yaml`)
> **Hostname**   `id.${SECRET_DOMAIN}` (public via `envoy-external`, and internally via `envoy-internal` — see Routing below)

## What it does here
SSO/OIDC broker for every user-facing app in the cluster (Grafana, Paperless, Open WebUI, Nextcloud, OpenSearch Dashboards, Proxmox, plus the Kubernetes API server itself) and the sole gate for Pushover-based alert delivery credentials. If `authentik-server` has no Service endpoints, nothing behind it can authenticate — this is the single most cluster-wide-impactful app in the deployment (confirmed live 2026-08-16, see `docs/incidents/2026-08-16-authentik-geoip-sidecar-sso-outage.md`).

## Architecture at a glance
- **Depends on:** CNPG postgres (`postgres-rw.database.svc.cluster.local`, database `authentik`), Dragonfly (`dragonfly.database.svc.cluster.local`, redis DB 1), 1Password (`ClusterSecretStore/onepassword`) for `authentik-secret` and 7 per-client OIDC secrets. MaxMind GeoLite2 via the `geoip` sidecar is *not* currently a live dependency — `geoip.enabled: false` since commit `aff638d` and unchanged since (see Known quirks).
- **Depended on by:** Grafana, OpenSearch Dashboards, Proxmox, Paperless, Open WebUI, Nextcloud, Immich (OIDC clients — blueprints `01`–`08` in `kubernetes/apps/security/authentik/app/blueprints/`, `07` added for Immich in commit `214b2a6`), plus 7 numbered `00N-cisotop-*` blueprints for password/MFA/enrollment/Turnstile/passwordless/notification flows.

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/security/authentik/app/helmrelease.yaml` | Chart version, server/worker autoscaling, geoip sidecar, resources |
| `kubernetes/apps/security/authentik/app/externalsecret.yaml` | Core secret (DB/redis creds, MaxMind, Turnstile) from 1Password |
| `kubernetes/apps/security/authentik/app/externalsecret-*-oidc.yaml` | One per OIDC client (grafana, opensearch, proxmox, paperless, open-webui, nextcloud, pushover) |
| `kubernetes/apps/security/authentik/app/blueprints/` | OAuth2Provider + flow blueprints, hash-triggered re-apply |
| `kubernetes/apps/security/authentik/app/httproute.yaml` | Dual-gateway routing (see Routing) |

## Secrets
| ExternalSecret | 1Password source | Consumed by |
| --- | --- | --- |
| `authentik` | `authentik-secret` item — `AUTHENTIK_SECRET_KEY`, Postgres/Dragonfly creds, MaxMind account/license, Turnstile site/secret keys | `authentik-server`/`authentik-worker` env, `geoip` sidecar |
| `authentik-grafana-oidc` etc. (×7 — grafana, opensearch, proxmox, paperless, open-webui, nextcloud, immich) | per-app OAuth2 client id/secret | matching app's OIDC config + the corresponding blueprint in `blueprints/` |
| `authentik-pushover` | Pushover OIDC client (optional — `envFrom` marks it `optional: true`) | Alertmanager's OIDC-gated Pushover delivery path |

`secret.reloader.stakater.com/reload` annotation on `global.deploymentAnnotations` (helmrelease.yaml) restarts server/worker automatically when any of these secrets change.

## Routing & access
- **Dual-gateway HTTPRoute** (`httproute.yaml`): `id.${SECRET_DOMAIN}` is attached to both `envoy-external` (public) and `envoy-internal`. The internal attachment exists because in-cluster OIDC clients (OpenSearch Dashboards, data nodes validating JWKS) hitting the public hostname get hairpinned through Cloudflare, which drops it as a CDN loop — breaking token/JWKS validation. CoreDNS split-horizon (`kubernetes/apps/kube-system/coredns/app/helmrelease.yaml`, `hosts` plugin) resolves `id.${SECRET_DOMAIN}` in-cluster straight to the internal gateway ClusterIP instead of forwarding upstream.
- OIDC issuer for every dependent app is `https://id.${SECRET_DOMAIN}/application/o/<slug>/` — also used directly by the Kubernetes API server itself (`talos/patches/controller/cluster.yaml`, `cluster.apiServer.extraArgs.oidc-issuer-url`) as an *additive* auth method alongside the Talos-generated client-cert admin kubeconfig.

## Storage
No PVCs of its own — state lives entirely in CNPG postgres + Dragonfly (both covered by their own backup schedules, not this app's).

## Known quirks
- **GeoIP sidecar is currently disabled (`geoip.enabled: false`, `helmrelease.yaml`) and has been since commit `aff638d` (2026-08-16) — not re-enabled as of this doc.** It can take down the whole app if turned back on without care: the `geoip` container (`geoipupdate`, MaxMind GeoLite2) has no readiness-probe override, so a crash-looping `geoip` blocks the *entire* pod's `Ready` condition — even though `server`/`worker` are healthy — which empties `authentik-server`'s Service endpoints. Hit live 2026-08-16 when MaxMind's daily download quota was exhausted (likely from several unrelated Helm upgrades restarting the pod in quick succession, each retriggering a download). See `docs/incidents/2026-08-16-authentik-geoip-sidecar-sso-outage.md` for the full incident and the Helm-rollback-loop recovery technique. MaxMind geo-enrichment is simply not live while this stays disabled — not a partial-degradation state.
- **`geoip.resources` has no chart default.** Without an explicit `resources.requests.cpu` (added 2026-08-16, `helmrelease.yaml`, retained even while `geoip.enabled: false`), the server/worker HPAs (`targetCPUUtilizationPercentage`) would fail outright with "missing request for cpu in container geoip" if re-enabled without it — confirmed against the chart's `values.yaml` (no default shipped for this key).
- **`envFrom`d `authentik-pushover` is `optional: true`** deliberately, since Pushover OIDC is provisioned later than the rest — a missing secret here must not block server/worker startup.

## Common operations
- Upgrade chart: edit `helmrelease.yaml` `spec.chart.spec.version`, commit, push — Flux reconciles within `interval: 1h`, or force with `flux reconcile helmrelease authentik -n security`.
- If the HelmRelease gets stuck oscillating between Helm rollback attempts (symptom: `helm history authentik -n security` shows repeated `Rollback to N`, `flux get helmrelease` shows alternating `UpgradeFailed`/`RollbackSucceeded`): `flux suspend helmrelease authentik -n security`, fix the live Deployments directly if there's an active outage, then `flux resume helmrelease authentik -n security` once live state matches git.
- Re-apply blueprints after an edit: Authentik's worker hash-checks blueprint files and re-applies changed ones automatically — no manual trigger needed (confirmed via prior session's blueprint work, not yet re-verified against current chart source for this doc — see TODO).

## TODOs / unknowns
- No decision recorded on if/when to re-enable `geoip` (disabled since 2026-08-16, commit `aff638d`'s message frames it as "temporarily" but it has stayed off since) — worth a deliberate call rather than leaving it disabled by default drift.
- The blueprint hash-based re-apply behavior above is asserted from prior operational memory, not re-verified against `authentik-worker`'s source for this doc — cite the chart source before relying on it for something time-sensitive.
- `podDisruptionBudget.minAvailable: 1` is set for both server and worker (autoscaling `minReplicas: 1`) — with only 1 replica minimum, a PDB of `minAvailable: 1` can block voluntary node drains entirely; worth revisiting once `minReplicas` is confirmed safe to raise.
