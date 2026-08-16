# firecrawl

> **Namespace**  hermes-agent
> **Source**     `ghcr.io/bjw-s-labs/helm/app-template` (`kubernetes/apps/hermes-agent/firecrawl/app/ocirepository.yaml`, tag `5.0.1`); app images `ghcr.io/firecrawl/firecrawl:2.11.202-production` and `ghcr.io/firecrawl/playwright-service` (pinned by digest, `tag: latest@sha256:...`) (`kubernetes/apps/hermes-agent/firecrawl/app/helmrelease.yaml`)
> **Hostname**   none — ClusterIP only, no HTTPRoute; reachable in-cluster at `firecrawl-api.hermes-agent.svc.cluster.local:3002`

## What it does here
Self-hosted web-scrape/crawl/search backend for two callers in this cluster: `hermes-agent`'s `web` tool (`FIRECRAWL_API_URL` in `kubernetes/apps/hermes-agent/hermes-agent/app/deployment.yaml:354`, gated by `config.yaml`'s `web.backend: firecrawl` in `kubernetes/apps/hermes-agent/hermes-agent/app/configmap.yaml:65`) and `open-webui`'s web-search feature (`WEB_SEARCH_ENGINE: firecrawl`, `kubernetes/apps/open-webui/open-webui/app/helmrelease.yaml:59`), which replaced a flaky searxng sidecar. Deployed following Firecrawl's own Kubernetes reference architecture (api/worker/nuq-worker/playwright-service, Postgres-backed "nuq" job queue) rather than its docker-compose monolith, deliberately without RabbitMQ — that's only needed by the compose harness mode (`kubernetes/apps/hermes-agent/firecrawl/app/helmrelease.yaml:1-13`).

## Architecture at a glance
- **Depends on:** Dragonfly (Redis-compatible, shared cluster instance, db index `/4`) for queue/rate-limit state; the shared CNPG `postgres` cluster's `firecrawl` database for the "nuq" job queue (`kubernetes/apps/database/cloudnative-pg/databases/database-firecrawl.yaml`); its own `playwright-service` controller for headless-browser rendering; open, unrestricted egress to the public internet (the actual pages being scraped).
- **Depended on by:** `hermes-agent` (web tool backend) and `open-webui` (web-search backend) — both in-cluster only, both reach it via the `firecrawl-api` Service on port 3002.

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/hermes-agent/firecrawl/app/helmrelease.yaml` | app-template values: 4 controllers (`api`, `worker`, `nuq-worker` ×2, `playwright-service`), resources sized down from upstream, per-controller Services |
| `kubernetes/apps/hermes-agent/firecrawl/app/ocirepository.yaml` | `app-template` chart source, pinned to `5.0.1` |
| `kubernetes/apps/hermes-agent/firecrawl/app/configmap.yaml` | Non-secret app config (`firecrawl-config`) and `playwright-service-config` — concurrency knobs, `PLAYWRIGHT_MICROSERVICE_URL` |
| `kubernetes/apps/hermes-agent/firecrawl/app/externalsecret.yaml` | `firecrawl-secret`: Redis URL(s), nuq Postgres DSN, bare `PGPASSWORD` |
| `kubernetes/apps/hermes-agent/firecrawl/app/job-nuq-schema.yaml` | Force-recreated Job that applies the "nuq" schema (tables/indexes, `pgcrypto`) to the `firecrawl` database |
| `kubernetes/apps/hermes-agent/firecrawl/app/cronjob-nuq-maintenance.yaml` | Two CronJobs (`firecrawl-nuq-reaper` every minute, `firecrawl-nuq-cleanup` every 5 min) standing in for upstream's `pg_cron` jobs |
| `kubernetes/apps/hermes-agent/firecrawl/app/ciliumnetworkpolicy.yaml` | Two CNPs: one for the four app-template controllers, one for the schema Job/CronJobs |
| `kubernetes/apps/hermes-agent/firecrawl/ks.yaml` | Flux Kustomization — depends on `cloudnative-pg-databases` and `external-secrets-stores` |

## Secrets
| Key (in `firecrawl-secret`, from ExternalSecret `firecrawl-env`) | 1Password source | Consumed by |
| --- | --- | --- |
| `REDIS_URL` / `REDIS_RATE_LIMIT_URL` | `dragonfly` item (`DRAGONFLY_PASSWORD`), templated into a Redis DSN pointed at db index `/4` | `api`/`worker`/`nuq-worker` containers via `envFrom` |
| `NUQ_DATABASE_URL` | `firecrawl` item (`FIRECRAWL_POSTGRESQL_PASSWORD`, URL-escaped), templated into a Postgres DSN for `postgres-rw.database.svc.cluster.local:5432/firecrawl` | same three containers via `envFrom` |
| `PGPASSWORD` | same `firecrawl` item, bare copy of the password | `job-nuq-schema.yaml` and both maintenance CronJobs, which use discrete `PGHOST`/`PGDATABASE`/`PGUSER`/`PGPASSWORD` env vars rather than a DSN |

A second ExternalSecret, `firecrawl-db-role` (`kubernetes/apps/database/cloudnative-pg/databases/externalsecret-firecrawl.yaml`), pulls the same `firecrawl` 1Password item to provision the CNPG managed role `firecrawlusr` — the password is intentionally identical in both places so the app and the database role agree.

## Routing & access
- No HTTPRoute — fully internal. `USE_DB_AUTHENTICATION` is `false` on the API itself (`kubernetes/apps/hermes-agent/firecrawl/app/configmap.yaml:24`), so the CiliumNetworkPolicy is the only real access control.
- `ciliumnetworkpolicy.yaml`'s first policy allows ingress to port 3002 only from pods labeled `app.kubernetes.io/name: hermes-agent` (namespace `hermes-agent`) and `app.kubernetes.io/name: open-webui` (namespace `open-webui`) — the two consumers above. It also allows unrestricted intra-release traffic (any `app.kubernetes.io/instance: firecrawl` pod to any other), DNS egress, egress to the shared `database` namespace on 6379/5432, and unrestricted egress to `world` on 80/443 (fetching arbitrary scraped URLs is inherently open-ended).
- The second policy in the same file (`firecrawl-nuq-maintenance`) covers the schema Job and both CronJobs, which aren't part of the HelmRelease release/instance and so aren't matched by the first policy's selector — DNS + Postgres egress only, no ingress.
- No SSO — no user-facing UI.

## Storage
No PVCs. All `persistence` entries in `helmrelease.yaml` are `emptyDir` (`/tmp` for `api`/`worker`/`nuq-worker`, plus `playwright-service`'s cache/tmp and a memory-backed `/dev/shm` sized to 512Mi — the same Chromium-crash-under-load fix `hermes-agent`'s own browser tool needed). All durable state is the `firecrawl` database on the shared CNPG `postgres` cluster, covered by that cluster's own scheduled backups (`kubernetes/apps/database/cloudnative-pg/cluster/scheduledbackup.yaml`) rather than any firecrawl-specific mechanism. The `hermes-agent` namespace is included in Velero's daily/weekly/monthly schedules (`kubernetes/apps/velero/schedules/schedule-daily.yaml`), but with no PVCs there's nothing app-specific for that to pick up here.

## Known quirks
- **`PLAYWRIGHT_MICROSERVICE_URL` pointed at the wrong Service name for an unknown period.** `app-template` names a release's Services `<release>-<service-key>` once a release has more than one Service — so the `playwright-service` controller's Service is `firecrawl-playwright-service`, not bare `playwright-service`. The ConfigMap had the bare name, meaning `api`/`worker` could never actually reach the Playwright renderer — silently breaking JS-rendered scraping since first deploy. Fixed alongside the open-webui cutover; see `git log`'s `7370b82` and the comment in `kubernetes/apps/hermes-agent/firecrawl/app/configmap.yaml:16-22`.
- **`$$` dollar-quoting in a Job's `command`/`args` gets silently collapsed by kubelet.** Kubelet's `$(VAR)` expansion runs over container `command`/`args` unconditionally and turns every `$$` into a literal `$` before exec — turning Postgres' `DO $$ BEGIN ... END $$;` into invalid SQL, even though the Job spec stored in etcd still shows `$$` (making it look fine on inspection). `job-nuq-schema.yaml` uses a named dollar-quote tag (`$nuq$`) instead, which has no repeated `$` to collapse (`kubernetes/apps/hermes-agent/firecrawl/app/job-nuq-schema.yaml:72-76`; full explanation there and in commit `325ce17`).
- **Postgres backend was refactored from a dedicated CNPG cluster to a database on the shared one** (commit `7bc5304`) once `pg_cron` was dropped — the only reason a separate cluster existed was `pg_cron` needing to be preloaded server-side, which isn't worth touching `shared_preload_libraries` on the shared cluster for. The functional half of upstream's `pg_cron` jobs (stalled-lock reaping, backlog/completed-job pruning, group-finish detection) is reimplemented as the two plain CronJobs in `cronjob-nuq-maintenance.yaml`, on 1-minute/5-minute k8s schedules instead of `pg_cron`'s 15-second cadence — acceptable slack for a low-traffic homelab queue.
- **Concurrency is intentionally far below upstream's docker-compose defaults** (`CRAWL_CONCURRENT_REQUESTS`/`MAX_CONCURRENT_JOBS`/`BROWSER_POOL_SIZE` at 3/2/2 vs. upstream's 10/5/5) — sized to what `playwright-service`'s resource budget can actually support on this homelab cluster, not upstream's target scale (`kubernetes/apps/hermes-agent/firecrawl/app/configmap.yaml:6-9`).
- **Recollection, not a citable file (no `docs/incidents/` entry exists for this):** during the 2026-08-16 cluster health-check session, `nuq-worker` was flagged as showing a crash-retry pattern, but the diagnosis traced it to a downstream symptom of the (since-unreproduced) Cilium apiserver single-backend issue rather than anything in Firecrawl's own config — no infra-level fix was applied here as a result. See the Cilium apiserver-backend investigation notes if this resurfaces; nothing in this app's own files points to a firecrawl-specific cause.

## Common operations
- Upgrade the Firecrawl image: bump the `tag:` under each controller in `helmrelease.yaml` (renovate-tracked, `# renovate: datasource=docker depName=ghcr.io/firecrawl/firecrawl` — three occurrences, one per `api`/`worker`/`nuq-worker`, must stay in sync), commit, push.
- Force a re-apply of the nuq schema after editing `job-nuq-schema.yaml`: not needed manually — `kustomize.toolkit.fluxcd.io/force: "enabled"` makes Flux delete-and-recreate the Job on any content change, and every statement in it is idempotent.
- Rotate the Postgres/Redis passwords: update the `firecrawl`/`dragonfly` 1Password items, then `kubectl annotate externalsecret firecrawl-env -n hermes-agent force-sync=$(date +%s)` (also re-sync `firecrawl-db-role` in the `database` namespace if rotating the Postgres password, so the CNPG role and this app's copy stay in sync).
- Pause reconciliation: `flux suspend kustomization firecrawl -n flux-system` / `flux suspend helmrelease firecrawl -n hermes-agent`.

## TODOs / unknowns
- Whether the nuq-worker crash-retry symptom noted above has recurred since 2026-08-16, or was ever confirmed (vs. assumed) to be downstream of the Cilium issue — not verified from any file in this repo, flagged here only because the operator's brief mentioned it. No `docs/incidents/` entry exists for it as of this writing.
- Exact behavior when `playwright-service` itself is overloaded (3 `MAX_CONCURRENT_PAGES` vs. `BROWSER_POOL_SIZE: 2`) under concurrent crawl+search load from both hermes-agent and open-webui simultaneously — not observed/tested from this repo.
