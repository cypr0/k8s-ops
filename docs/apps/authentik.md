# Authentik

> **Namespace**  security
> **Source**     `https://charts.goauthentik.io`, chart `authentik` v2026.8.0 (`kubernetes/apps/security/authentik/app/helmrepository.yaml`, `helmrelease.yaml`)
> **Hostname**   `id.${SECRET_DOMAIN}` (public via `envoy-external`, and internally via `envoy-internal` — see Routing below)

## What it does here
SSO/OIDC broker for every user-facing app in the cluster (Grafana, Paperless, Open WebUI, Nextcloud, the Wazuh dashboard, Proxmox, plus the Kubernetes API server itself) and the sole gate for Pushover-based alert delivery credentials. If `authentik-server` has no Service endpoints, nothing behind it can authenticate — this is the single most cluster-wide-impactful app in the deployment (confirmed live 2026-08-16, see `docs/incidents/2026-08-16-authentik-geoip-sidecar-sso-outage.md`).

## Architecture at a glance
- **Depends on:** CNPG postgres (`postgres-rw.database.svc.cluster.local`, database `authentik`), Dragonfly (`dragonfly.database.svc.cluster.local`, redis DB 1), 1Password (`ClusterSecretStore/onepassword`) for `authentik-secret` and 7 per-client OIDC secrets. MaxMind GeoLite2 via the `geoip` sidecar (enabled again since 2026-09-27) is a soft dependency: if MaxMind refuses, pods run without geo data but stay Ready (see Known quirks).
- **Depended on by:** Grafana, Proxmox, Paperless, Open WebUI, Nextcloud, Immich, Wazuh, hermes-agent's desktop backend (`10`, public client — no secret) (OIDC clients — numbered blueprints in `kubernetes/apps/security/authentik/app/blueprints/`; `07` added for Immich in commit `214b2a6`, `09` for Wazuh; `02` was OpenSearch and was removed with it on 2026-09-20, leaving a gap in the numbering), plus 7 numbered `00N-cisotop-*` blueprints for password/MFA/enrollment/Turnstile/passwordless/notification flows.

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/security/authentik/app/helmrelease.yaml` | Chart version, server/worker autoscaling, geoip sidecar, resources |
| `kubernetes/apps/security/authentik/app/externalsecret.yaml` | Core secret (DB/redis creds, MaxMind, Turnstile) from 1Password |
| `kubernetes/apps/security/authentik/app/externalsecret-*-oidc.yaml` | One per OIDC client (grafana, proxmox, paperless, open-webui, nextcloud, immich, wazuh, pushover) |
| `kubernetes/apps/security/authentik/app/blueprints/` | OAuth2Provider + flow blueprints, hash-triggered re-apply |
| `kubernetes/apps/security/authentik/app/httproute.yaml` | Dual-gateway routing (see Routing) |

## Secrets
| ExternalSecret | 1Password source | Consumed by |
| --- | --- | --- |
| `authentik` | `authentik-secret` item — `AUTHENTIK_SECRET_KEY`, Postgres/Dragonfly creds, MaxMind account/license, Turnstile site/secret keys | `authentik-server`/`authentik-worker` env, `geoip` sidecar |
| `authentik-grafana-oidc` etc. (one per client — grafana, proxmox, paperless, open-webui, nextcloud, immich, wazuh) | per-app OAuth2 client id/secret | matching app's OIDC config + the corresponding blueprint in `blueprints/` |
| `authentik-pushover` | Pushover OIDC client (optional — `envFrom` marks it `optional: true`) | Alertmanager's OIDC-gated Pushover delivery path |

`secret.reloader.stakater.com/reload` annotation on `global.deploymentAnnotations` (helmrelease.yaml) restarts server/worker automatically when any of these secrets change.

## Routing & access
- **Dual-gateway HTTPRoute** (`httproute.yaml`): `id.${SECRET_DOMAIN}` is attached to both `envoy-external` (public) and `envoy-internal`. The internal attachment exists because in-cluster OIDC clients (the Wazuh dashboard, and its indexer validating JWKS) hitting the public hostname get hairpinned through Cloudflare, which drops it as a CDN loop — breaking token/JWKS validation. CoreDNS split-horizon (`kubernetes/apps/kube-system/coredns/app/helmrelease.yaml`, `hosts` plugin) resolves `id.${SECRET_DOMAIN}` in-cluster straight to the internal gateway ClusterIP instead of forwarding upstream.
- OIDC issuer for every dependent app is `https://id.${SECRET_DOMAIN}/application/o/<slug>/` — also used directly by the Kubernetes API server itself (`talos/patches/controller/cluster.yaml`, `cluster.apiServer.extraArgs.oidc-issuer-url`) as an *additive* auth method alongside the Talos-generated client-cert admin kubeconfig.

## Storage
No PVCs of its own — state lives entirely in CNPG postgres + Dragonfly (both covered by their own backup schedules, not this app's).

## Known quirks
- **The GeoIP sidecar cannot fail the pod any more (since 2026-10-17).** The chart runs `geoip` (`geoipupdate`, MaxMind GeoLite2) as a plain sidecar whose stock entrypoint exits on any failed download; a crash-looping `geoip` held the *whole* pod NotReady (a Pod is Ready only when every container is) and blocked Helm upgrades and rollbacks on 2026-08-16, 2026-10-11 and 2026-10-17. A `postRenderers` JSON patch in `helmrelease.yaml` now wraps the entrypoint: a failed run logs `# STATE: geoipupdate exited (<rc>), retrying in 1 hour` and the container keeps running. Remaining cost of a refused download: authentik loads the database only if it exists at process start, so that pod runs without geo data until its next restart (GeoIP only enriches events here; no GeoIP policy exists). Every new pod downloads both editions (per-pod `emptyDir`), and MaxMind's limit behaves like a rolling 24 h window, not a midnight reset. See `docs/incidents/2026-10-17-authentik-upgrade-stalled-geoip-quota.md` and, for the original outage, `docs/incidents/2026-08-16-authentik-geoip-sidecar-sso-outage.md`.
- **`geoip.resources` has no chart default.** Without an explicit `resources.requests.cpu` (added 2026-08-16, `helmrelease.yaml`, retained even while `geoip.enabled: false`), the server/worker HPAs (`targetCPUUtilizationPercentage`) would fail outright with "missing request for cpu in container geoip" if re-enabled without it — confirmed against the chart's `values.yaml` (no default shipped for this key).
- **`worker.resources.requests.cpu` is 50m and `KubeHpaMaxedOut` fires permanently (known, deferred 2026-10-11).** Measured over 14 days the worker uses about 100m in total (p50 100m, p99 133m, max 243m, summed over replicas), so at a 50m request (plus the 10m geoip sidecar) the HPA's 70% target sits far below baseline and it holds 2 of 2 replicas. A request of ~200m would let a single replica run at ~48% (p99 ~63%) and the HPA settle on 1; 100m would not (91-121%). It was reverted on 2026-10-11 only because the restarted pods' `geoip` sidecars hit MaxMind's 429 and never became Ready; since the 2026-10-17 wrapper (quirk above) that no longer blocks a rollout, so 200m can be re-applied. The alert itself is only a warning repeated every 3 days. See `docs/incidents/2026-10-11-wazuh-pushover-quota-exhaustion.md`.
- **`envFrom`d `authentik-pushover` is `optional: true`** deliberately, since Pushover OIDC is provisioned later than the rest — a missing secret here must not block server/worker startup.
- **The server HPA has a 5-minute scale-up window (`server.autoscaling.behavior`, since 2026-10-17).** Without it the HPA added the second replica on any 15-second CPU burst and removed it 5 minutes later: 18-29 times a day from at least 2026-10-05 (the oldest data Prometheus still holds, since its 18GiB size limit applies before the 30d retention does). Every new pod's `geoip` sidecar downloads both editions, which used up MaxMind's daily limit each day. Most bursts are gunicorn recycling one of its 2 Django workers (`max_requests` 1000 + jitter 50, authentik's `/lifecycle/gunicorn.conf.py`, configurable via `AUTHENTIK_WEB__MAX_REQUESTS`). At the steady ~25 req/min, almost all of them probes and metrics scrapes, that is a recycle about every 40 minutes on a single replica. The log shows `Worker exiting` followed by `Booting worker`, and each re-boot takes about 1 s of CPU: 90-300m in one scrape, against a ~20m median and the 77m (70% of 110m + 10% tolerance) that triggers a scale-up. Replaying 2026-10-06 to 2026-10-17 against the window: 0s gives ~24 scale-ups a day (matches what was observed), 60s gives ~0, and 300s leaves 1 in 12 days, during the restart storm of the stalled 2026.8.4 upgrade. A `behavior` change only touches the HPA object, not the pod template, so it restarts no pods.

## Common operations
- Upgrade chart: edit `helmrelease.yaml` `spec.chart.spec.version`, commit, push — Flux reconciles within `interval: 1h`, or force with `flux reconcile helmrelease authentik -n security`.
- If the HelmRelease gets stuck oscillating between Helm rollback attempts (symptom: `helm history authentik -n security` shows repeated `Rollback to N`, `flux get helmrelease` shows alternating `UpgradeFailed`/`RollbackSucceeded`): `flux suspend helmrelease authentik -n security`, fix the live Deployments directly if there's an active outage, then `flux resume helmrelease authentik -n security` once live state matches git.
- Re-apply blueprints after an edit: Authentik's worker hash-checks blueprint files and re-applies changed ones automatically — no manual trigger needed (confirmed via prior session's blueprint work, not yet re-verified against current chart source for this doc — see TODO).

## TODOs / unknowns
- The blueprint hash-based re-apply behavior above is asserted from prior operational memory, not re-verified against `authentik-worker`'s source for this doc — cite the chart source before relying on it for something time-sensitive.
- `podDisruptionBudget.minAvailable: 1` is set for both server and worker (autoscaling `minReplicas: 1`) — with only 1 replica minimum, a PDB of `minAvailable: 1` can block voluntary node drains entirely; worth revisiting once `minReplicas` is confirmed safe to raise.
