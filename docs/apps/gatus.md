# Gatus

> **Namespace**  monitoring
> **Source**     `app-template` chart v5.0.1 via `OCIRepository` `oci://ghcr.io/bjw-s-labs/helm/app-template` (`kubernetes/apps/monitoring/gatus/app/ocirepository.yaml`, `helmrelease.yaml`), image `ghcr.io/twin/gatus:v5.36.0`
> **Hostname**   `status.${SECRET_DOMAIN}` — internal-only, via `envoy-internal` (`kubernetes/apps/monitoring/gatus/app/httproute.yaml`); not reachable through the public Cloudflare tunnel, which only forwards `*.${SECRET_DOMAIN}` to `envoy-external`

## What it does here
Synthetic uptime/health-check monitor for this cluster: polls a fixed list of internal service endpoints and two external hostnames on a schedule, evaluates simple pass/fail conditions, and fires Pushover alerts on failure/recovery (`kubernetes/apps/monitoring/gatus/app/configmap.yaml`). It is the cluster's own "is anything down" dashboard, separate from Prometheus/Alertmanager's metric-threshold alerting — it tests reachability and response shape directly, the way a human would curl each app.

## Architecture at a glance
- **Depends on:** ExternalSecret `gatus-pushover` → 1Password (Pushover alerting credentials), CoreDNS (DNS resolution/rewrites for the external checks — see Known quirks), and per the Flux Kustomization, `kube-prometheus-stack` and `external-secrets-stores` must be ready first (`kubernetes/apps/monitoring/gatus/ks.yaml` `spec.dependsOn`). No CNPG/S3 dependency of its own — SQLite is local, not a shared database.
- **Depended on by:** nothing — confirmed by grepping the repo for `gatus.` outside its own directory (no hits). It is a pure observer; every dependency arrow in this doc points *away* from Gatus, toward the ~15 things it checks.

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/monitoring/gatus/app/helmrelease.yaml` | Chart values: image, probes, resources, security context, ServiceMonitor, persistence mounts |
| `kubernetes/apps/monitoring/gatus/app/configmap.yaml` | The actual Gatus config — alerting block + every monitored endpoint (this is the file that defines "what Gatus checks") |
| `kubernetes/apps/monitoring/gatus/app/externalsecret.yaml` | Pushover credentials pulled from 1Password |
| `kubernetes/apps/monitoring/gatus/app/httproute.yaml` | Status page routing (internal gateway only) |
| `kubernetes/apps/monitoring/gatus/app/ciliumnetworkpolicy.yaml` | Ingress (Envoy, Prometheus scrape, kubelet) + per-target egress rules for every namespace it checks |
| `kubernetes/apps/monitoring/gatus/ks.yaml` | Flux Kustomization — `dependsOn: kube-prometheus-stack, external-secrets-stores`, `postBuild.substituteFrom: cluster-secrets` |

## Secrets
| ExternalSecret | 1Password source | Consumed by |
| --- | --- | --- |
| `gatus-pushover` (`kubernetes/apps/monitoring/gatus/app/externalsecret.yaml`) | `pushover` item, fields `GATUS_PUSHOVER_API_TOKEN` and `PUSHOVER_USER_KEY` | Container env, via `envFrom.secretRef.name: gatus-pushover` (`helmrelease.yaml:31-33`) — expanded by Gatus itself at runtime inside `configmap.yaml`'s `alerting.pushover` block |

The Pushover placeholders in `configmap.yaml` are written as `$${GATUS_PUSHOVER_API_TOKEN}` (double `$`), not `${...}` — this is deliberate escaping so Flux's `postBuild` variable substitution leaves the literal placeholder alone for Gatus to expand at runtime, rather than silently substituting it to an empty string (see Known quirks).

## Routing & access
- Exposed only via `envoy-internal` (`network` namespace, `https` section) at `status.${SECRET_DOMAIN}` — no public attachment (`httproute.yaml`). No SSO/OIDC in front of it; the internal-only network path is the sole access control.
- `ciliumnetworkpolicy.yaml` ingress: Envoy (`network` ns) for the status page, Prometheus (`monitoring` ns) for `/metrics` scraping, and `fromEntities: host` for kubelet probes.
- Egress is one rule block per namespace of checked targets (`monitoring`, `logging`, `database`, `nextcloud`, `open-webui`, `paperless`, `security`), plus a Cilium L7 DNS rule and a `toEntities: world` catch-all on port 443 for the genuinely external checks. A dedicated egress rule sends `id.${SECRET_DOMAIN}` traffic to `envoy-internal:10443` instead of the internet, because CoreDNS rewrites that hostname in-cluster.

## What it monitors
Grounded in `kubernetes/apps/monitoring/gatus/app/configmap.yaml`, grouped as it is in that file:

- **Security:** Authentik SSO (`https://id.${SECRET_DOMAIN}`, 1m interval, status 200 + certificate expiry >24h) — resolves in-cluster to the internal Envoy gateway via a CoreDNS rewrite rather than hitting the real internet; 1Password Connect (`http://onepassword-connect.security.svc.cluster.local/health`, 1m).
- **Infrastructure:** Echo connectivity test (`https://echo.${SECRET_DOMAIN}`, 5m) — the one check deliberately routed over the real public Cloudflare edge, to prove the external ingress path end-to-end.
- **Monitoring:** Prometheus, Alertmanager, Loki — all plain `/-/healthy`- or `/ready`-style HTTP checks against their in-cluster Service DNS names, 1m interval.
- **Database:** PostgreSQL CNPG primary (`tcp://postgres-rw.database.svc.cluster.local:5432`) and DragonflyDB (`tcp://dragonfly.database.svc.cluster.local:6379`) — both 30s-interval plain TCP-connect checks (`[CONNECTED] == true`), no authentication attempted.
- **Logging:** OpenSearch (TCP 9200) and OpenSearch Dashboards (`/api/status`, permissive `< 500`).
- **Apps:** Grafana (`/api/health`), Nextcloud (`/status.php`), Collabora (`/hosting/discovery`), Open WebUI and Paperless-ngx — the latter two use a permissive `[STATUS] == any(200, 302, 401, 403)` condition because their exact unauthenticated response was never independently confirmed (inline comment).
- A Paperless-AI check existed here too but was removed when that app was decommissioned (commit `c61fee3`) — its Gatus entry and dedicated egress rule were deleted in the same commit that removed the app.

## Storage
`/data` is an `emptyDir` (`helmrelease.yaml` `persistence.data`), holding the SQLite status-history DB (`configmap.yaml` `storage.path: /data/data.db`) — **not persisted**: a pod reschedule or restart loses all uptime history. `/config/config.yaml` is a read-only ConfigMap mount. The `monitoring` namespace is not in Velero's `includedNamespaces` (`kubernetes/apps/velero/schedules/schedule-daily.yaml` lists only `nextcloud`, `paperless`, `open-webui`, `hermes-agent`), consistent with this being disposable monitoring state rather than an oversight.

## Known quirks
- **Pushover alerting was silently broken from initial deployment until fixed.** Flux's `postBuild` substitution was matching Gatus's own `${GATUS_PUSHOVER_API_TOKEN}`/`${PUSHOVER_USER_KEY}` placeholders (meant for Gatus's own runtime env expansion) and quietly substituting them to empty strings, since those vars don't exist in `cluster-secrets`. Fixed by escaping to `$${VAR}` (commit `04d8fef`). The failure mode was silent (no alerts ever fired) rather than noisy, so it went unnoticed for a while.
- **The Echo check hung until fixed at the DNS layer, not the client layer.** `echo.${SECRET_DOMAIN}` publishes AAAA records and this cluster has no IPv6 routing, so every check hung until Gatus's client timeout. Gatus's `client.network: ip4` setting looked like the fix but is documented as ICMP-only with no effect on HTTP(S) checks — confirmed by testing it live, then reverted (commit `8d10a7c`). The real fix is a CoreDNS `template` rule returning NXDOMAIN for AAAA queries on that hostname only (`kubernetes/apps/kube-system/coredns/app/helmrelease.yaml`), while its A record keeps resolving dynamically.
- **The Authentik certificate-expiry threshold was miscalibrated for over a month.** It required `[CERTIFICATE_EXPIRATION] > 336h` (14 days) against a cert-manager `Certificate` deliberately provisioned for only 160h (~6.7 days) — a check that could never pass. Fixed to `>24h` (commit `6814ef6`), comfortably inside the cert's ~53h renewal buffer.
- **Adding a new endpoint requires touching Gatus's own CNP *and* the target's CNP.** Cilium enforces both sides of a connection; commit `6814ef6` shows several endpoints (Loki, in-cluster Authentik) were false negatives at first because only one side's policy was updated. Anyone adding a check should expect to edit both `ciliumnetworkpolicy.yaml` here and the target app's own.
- **`reloader.stakater.com/auto: "true"`** (`helmrelease.yaml:23`) means any change to a ConfigMap/Secret referenced by this pod — including editing an endpoint in `configmap.yaml` — triggers an automatic rolling restart via Reloader, without a manual `kubectl rollout restart`.

## Common operations
- Add/change a monitored endpoint: edit the `endpoints` list in `kubernetes/apps/monitoring/gatus/app/configmap.yaml`, commit, push — Reloader restarts the pod automatically once the ConfigMap change lands; add matching CNP egress here and CNP ingress on the target if it's a new namespace/port.
- Upgrade chart version: bump `spec.ref.tag` in `ocirepository.yaml` (this HelmRelease uses `chartRef`, not `spec.chart.spec.version`).
- Force reconcile: `flux reconcile helmrelease gatus -n monitoring`.
- Rotate Pushover credentials: update the `pushover` 1Password item, then `kubectl annotate externalsecret gatus-pushover -n monitoring force-sync=$(date +%s)` (or wait out the refresh interval) — Reloader picks up the resulting Secret change automatically.
- View the live status page: `https://status.${SECRET_DOMAIN}` — reachable only from inside the LAN/cluster network, not from the public internet.

## TODOs / unknowns
- Whether the `status.${SECRET_DOMAIN}` page has any access control beyond network-level internal-only exposure (no auth/OIDC block found in this app's files) — not verified further.
- What, if anything, consumes the ServiceMonitor's `/metrics` scrape (`helmrelease.yaml` `serviceMonitor.app`) — no Grafana dashboard or PrometheusRule referencing Gatus's own metrics was found in this pass.
- Whether losing SQLite status history on every pod restart/reschedule (emptyDir, see Storage) has ever been felt as a real gap, or is accepted as fine for a monitor whose value is live alerting, not historical uptime reporting — not documented either way in the repo.

---
_Cite every non-obvious claim with a repo-root-relative file path (e.g. `kubernetes/apps/security/authentik/app/helmrelease.yaml`), not a bare filename — this doc lives under `docs/apps/`, so relative paths must resolve from there._
