# paperless-ngx

> **Namespace**  paperless
> **Source**     `app-template` chart v5.0.1 via `OCIRepository` `oci://ghcr.io/bjw-s-labs/helm/app-template` (`kubernetes/apps/paperless/paperless-ngx/app/ocirepository.yaml`, `helmrelease.yaml`) — image `ghcr.io/paperless-ngx/paperless-ngx:3.0.5`
> **Hostname**   `paperless.${SECRET_DOMAIN}` — internal only, `httproute.yaml` attaches solely to the `envoy-internal` Gateway, no `envoy-external`/cloudflare-tunnel exposure

## What it does here
The cluster's document management system: ingests scanned business documents (from a Brother ADS-4700W duplex scanner over an NFS "consume" share, plus an IMAP mailbox), OCRs them (via Tika/Gotenberg sidecars in the same namespace), and applies native AI suggestions (title/date/tags/correspondent/type via OpenRouter) and document-chat/RAG (via a local embedding model). Its own "Document Added" Workflow action fires a webhook that drives hermes-agent's auto-tagging pipeline, and its REST API is the tool surface both `paperless-mcp` and Open WebUI's `paperless_full` Tool operate against. SSO is Authentik OIDC only — password login is disabled outright.

## Architecture at a glance
- **Depends on:**
  - CNPG postgres cluster `postgres` (namespace `database`), database `paperlessdb`, owner `paperlessusr` (`kubernetes/apps/database/cloudnative-pg/databases/database-paperless.yaml`) — see `docs/apps/cloudnative-pg.md`.
  - Dragonfly (`kubernetes/apps/paperless/paperless-ngx/app/externalsecret.yaml:29`), DB index 0 (omitted from the connection string) — same index Open WebUI uses; see `docs/apps/dragonfly.md` Known quirks for the collision.
  - Tika (`tika-http.paperless.svc.cluster.local:9998`) and Gotenberg (`gotenberg-http.paperless.svc.cluster.local:3000`) — separate Flux-managed apps in the same namespace (`kubernetes/apps/paperless/tika/`, `kubernetes/apps/paperless/gotenberg/`), not part of this app's own directory.
  - Authentik OIDC for SSO login (`kubernetes/apps/security/authentik/app/blueprints/04-paperless-oidc.yaml`).
  - OpenRouter (`https://openrouter.ai/api/v1`, model `google/gemini-2.5-flash-lite`) for native AI suggestions; a local in-process `sentence-transformers` embedding model (no external egress) for document chat/RAG (`helmrelease.yaml`).
  - External IMAP mailbox (`${SECRET_PAPERLESS_IMAP_SERVER}`, `ciliumnetworkpolicy.yaml`) for mail-fetch ingestion.
  - The NFS "consume" share, whose vsftpd side (Brother scanner FTP drop) is provisioned by `proxmox-ansible` — see `docs/apps/proxmox-ansible.md` and `kubernetes/apps/automation/proxmox-ansible/app/configmap-playbook.yaml`. `USERMAP_UID`/`USERMAP_GID: 3000` here (`helmrelease.yaml`) matches the `3000:3000` ownership that playbook maintains on the host side.
- **Depended on by:**
  - `hermes-agent` — its Workflow "Webhook" action (trigger: Document Added) calls hermes-agent's webhook front-end on port 8644 (`ciliumnetworkpolicy.yaml`); see `docs/apps/hermes-agent.md`.
  - `paperless-mcp` (namespace `hermes-agent`) — reads/writes this app's REST API on hermes-agent's behalf; see `docs/apps/paperless-mcp.md`.
  - Open WebUI's `paperless_full` Tool — same REST API, its own token/ExternalSecret (`kubernetes/apps/open-webui/open-webui/app/externalsecret-paperless-token.yaml`).
  - `paperless-stats-exporter` CronJob (own Flux Kustomization, `config/` — see Repo layout) — polls `statistics`/`status`/`tasks` and writes to OpenSearch.
  - Gatus health check (`ciliumnetworkpolicy.yaml`).

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/paperless/paperless-ngx/app/ocirepository.yaml` | `app-template` chart pin (v5.0.1) |
| `kubernetes/apps/paperless/paperless-ngx/app/helmrelease.yaml` | Chart values — image tag, all `PAPERLESS_*` env, resources, persistence mounts |
| `kubernetes/apps/paperless/paperless-ngx/app/externalsecret.yaml` | DB/admin/OIDC/AI/Redis secrets, built into `paperless-secret` |
| `kubernetes/apps/paperless/paperless-ngx/app/pvc.yaml` | `paperless-data-pvc` (RWX) + statically-bound `paperless-consume-pvc`/PV |
| `kubernetes/apps/paperless/paperless-ngx/app/httproute.yaml` | `paperless.${SECRET_DOMAIN}`, `envoy-internal` only |
| `kubernetes/apps/paperless/paperless-ngx/app/ciliumnetworkpolicy.yaml` | Three policies in one file: `paperless-ngx`, `tika`, `gotenberg` |
| `kubernetes/apps/paperless/paperless-ngx/app/jobs.yaml` | `paperless-cronjob-fix-ownership` CronJob — **currently disabled**, see Known quirks |
| `kubernetes/apps/paperless/paperless-ngx/app/kustomization.yaml` | Resource list — note `jobs.yaml` is commented out |
| `kubernetes/apps/paperless/paperless-ngx/ks.yaml` | Two Flux Kustomizations: `paperless` (path `app/`, `dependsOn` CNPG/csi-driver-nfs/external-secrets-stores) and `paperless-stats` (path `config/`, deliberately no `dependsOn` on the main one) |
| `kubernetes/apps/paperless/paperless-ngx/config/cronjob-stats-exporter.yaml` | `paperless-stats-exporter` CronJob (every 15 min) → OpenSearch |
| `kubernetes/apps/paperless/paperless-ngx/config/externalsecret-opensearch.yaml` | OpenSearch write credentials for the exporter |
| `kubernetes/apps/paperless/paperless-ngx/config/ciliumnetworkpolicy.yaml` | Network policy for the exporter CronJob |

## Secrets
`paperless-secret` (`kubernetes/apps/paperless/paperless-ngx/app/externalsecret.yaml`), built from three 1Password items via `dataFrom.extract`:

| Key | 1Password source | Consumed by |
| --- | --- | --- |
| `PAPERLESS_DBNAME`/`DBUSER`/`DBPASS`, `PAPERLESS_ADMIN_USER`/`ADMIN_PASSWORD`, `PAPERLESS_SECRET_KEY`, `PAPERLESS_API_TOKEN`/`USERNAME` | item `paperless` | main container `envFrom` (`helmrelease.yaml`) |
| `PAPERLESS_AI_LLM_API_KEY` | item `openrouter`, field `OPENROUTER_PAPERLESSAI_API_KEY` (kept from the now-decommissioned `paperless-ai`, reused rather than a new key) | native AI suggestions/chat |
| `PAPERLESS_REDIS` (assembled connection string) | item `dragonfly`, field `DRAGONFLY_PASSWORD` | Dragonfly cache/task-queue connection |
| `PAPERLESS_SOCIALACCOUNT_PROVIDERS` (assembled JSON) | item `paperless`, fields `PAPERLESS_OPENID_CLIENT_ID`/`PAPERLESS_OPENID_CLIENT_SECRET` | Authentik OIDC provider config |

Two more ExternalSecrets outside this app's own directory pull the same `paperless` item: `kubernetes/apps/database/cloudnative-pg/databases/externalsecret-paperless.yaml` (feeds the CNPG-managed `paperlessusr` role) and `kubernetes/apps/open-webui/open-webui/app/externalsecret-paperless-token.yaml` (Open WebUI's `paperless_full` Tool — explicitly documented there as reusing this app's own `PAPERLESS_API_TOKEN`, no separate credential provisioned). A fourth, `kubernetes/apps/paperless/paperless-ngx/config/externalsecret-opensearch.yaml`, pulls item `opensearch` (field `OPENSEARCH_ADMIN_PASSWORD`) for the stats-exporter CronJob's write access.

The Deployment carries `reloader.stakater.com/auto`/`secret.reloader.stakater.com/reload: "paperless-secret"` annotations (`helmrelease.yaml`) — unlike hermes-agent or paperless-mcp, a `paperless-secret` change auto-rolls this pod; no manual restart needed after a force-sync.

## Routing & access
- `paperless.${SECRET_DOMAIN}` via `httproute.yaml`, attached only to `envoy-internal` — internal-only, no cloudflare-tunnel path.
- SSO: OIDC via Authentik, blueprint `kubernetes/apps/security/authentik/app/blueprints/04-paperless-oidc.yaml` (application slug `paperless-ngx`). `PAPERLESS_DISABLE_REGULAR_LOGIN: "true"` (`helmrelease.yaml`) means OIDC is the only login path. `PAPERLESS_SOCIAL_ACCOUNT_SYNC_GROUPS` is deliberately `"false"` — locally-managed Paperless groups aren't in Authentik's `groups` claim, so syncing would wipe them on every login (inline comment, `helmrelease.yaml`).
- CiliumNetworkPolicy (`ciliumnetworkpolicy.yaml`), three policies in one file:
  - `paperless-ngx`: ingress from envoy (network ns, port 80), Prometheus, `paperless-stats-exporter` (same ns), `openclaw`, Open WebUI, Gatus, and `paperless-mcp` (namespace `hermes-agent`) — all restricted to port 80. Egress: DNS, `database` namespace (5432/6379), Tika (9998), Gotenberg (3000), Authentik direct (9000/9443) plus a second Authentik rule allowing the envoy pod's DNAT'd port 10443 — per the inline comment, OIDC discovery/token calls resolve to the internal gateway's ClusterIP and get DNAT'd to that container port, so policy is enforced post-DNAT there. Also: the external IMAP FQDN on 993 (both bare and namespace-suffixed forms, since `ndots:5` search-domain resolution can produce either), `world` on 443 (OpenRouter), and hermes-agent's webhook port 8644.
  - `tika`/`gotenberg`: ingress from `paperless-ngx` only, on 9998/3000 respectively; egress is DNS-only.

## Storage
- `paperless-data-pvc` — 200Gi, `zfs-nfs`, RWX, mounted at `/library` (`pvc.yaml`, `helmrelease.yaml` `advancedMounts`); backs `PAPERLESS_DATA_DIR`/`MEDIA_ROOT`/`EXPORT_DIR`.
- `paperless-consume-pvc` — 50Gi, statically bound (`volumeName: paperless-consume-pv`) to a fixed PV at NFS path `/rpool/k8s-rwx/paperless-consume` on `${SECRET_NFS_SERVER}` (`pvc.yaml`), mounted at `/consume`, polled every 60s (`PAPERLESS_CONSUMER_POLLING`, recursive, subdirs-as-tags). This is the landing zone for the scanner/FTP path documented in `docs/apps/proxmox-ansible.md`.
- Backup: `paperless` namespace is in Velero's daily/weekly/monthly schedules (`kubernetes/apps/velero/schedules/schedule-daily.yaml` et al.) — `paperless-data-pvc` is covered. `paperless-consume-pvc` is explicitly excluded (`velero.io/exclude-from-backup: "true"`, `pvc.yaml`): its static `claimRef` binding can never rebind under a different namespace/restore target, and it's a transient scan inbox with nothing worth keeping.

## Known quirks
- **The ownership-fix CronJob is currently disabled.** `jobs.yaml` (`paperless-cronjob-fix-ownership`, bulk-fixes tag/correspondent/document-type permissions via the REST API) has been commented out of `kustomization.yaml` since `chore(paperless): tweak AI model, tag name, PVC mode and disable jobs` (commit `8cdfe2b`, 2026-06-27), "for now." Other docs (`docs/apps/paperless-mcp.md`, and the comment in `kubernetes/apps/open-webui/open-webui/app/externalsecret-paperless-token.yaml`) describe the shared `PAPERLESS_API_TOKEN` as something this job "already uses" — that describes its designed purpose, not a currently running consumer. Verify live (`kubectl get cronjob -n paperless`) before assuming it's active.
- **The admin GUI lies about AI/OCR config state.** Per an inline comment in `helmrelease.yaml`, paperless-ngx's own `src/paperless/config.py` (`AIConfig`/`OcrConfig`) always displays the GUI toggle's database-backed override, never the resolved effective value — so the "KI-Einstellungen"/"OCR-Einstellungen" admin pages show disabled/blank even though `PAPERLESS_AI_ENABLED` and the `PAPERLESS_OCR_*` env vars are genuinely in effect (confirmed live per the same comment). Don't use the admin GUI as a source of truth for whether these are on.
- **`paperless-ai` is fully decommissioned** in favor of native AI (paperless-ngx 3.0+) — its directory no longer exists under `kubernetes/apps/paperless/` (commit `c61fee3`). Native AI reuses the exact same OpenRouter 1Password field paperless-ai used, per the comment in `externalsecret.yaml`.
- **Suggestions and document-chat use two different AI backends.** Suggestions go through OpenRouter (`openai-like` backend); chat/RAG needs its own embedding backend, which is deliberately the local `huggingface`/`sentence-transformers` model rather than OpenRouter — OpenRouter's embeddings-endpoint support is undocumented for paperless-ngx, so the in-process option avoids both an unsupported integration and an extra egress rule (`helmrelease.yaml`).
- **`PAPERLESS_TIME_ZONE` is hardcoded (`Europe/Berlin`), not a cluster-secrets var.** No `TIMEZONE` key exists in `cluster-secrets`, so an earlier `${TIMEZONE}` reference failed Flux's strict `postBuild` substitution and blocked the whole Kustomization (commit `a8fe460`) — matches how every other app in this repo hardcodes TZ literally.
- **Resource requests/limits were added late.** Previously fully unset (`BestEffort` QoS) despite steadily using 1+Gi live (2 task workers, OCR/Tika round-trips, the in-process embedding model) — made it the scheduler's first OOM target despite being a heavy real consumer. Now `512Mi`/`3Gi` (`helmrelease.yaml`), sized against observed ~1.1Gi usage and its Tika/Gotenberg siblings.
- **The IMAP mail-fetch egress rule exists, but no matching env var does.** `ciliumnetworkpolicy.yaml` allowlists `${SECRET_PAPERLESS_IMAP_SERVER}:993`, but no `PAPERLESS_EMAIL_*` config appears anywhere in `helmrelease.yaml` — the actual Mail Account/Mail Rule is paperless-ngx's own DB-backed config, set via its admin GUI, not tracked in this repo.
- **Dragonfly DB-index collision (index 0) with Open WebUI**, since this app's Redis URL (`externalsecret.yaml`) omits an index and defaults to 0 — same index Open WebUI uses. See `docs/apps/dragonfly.md` Known quirks; no observed collision, but no isolation either.
- **Gotenberg (same namespace, separate app) was bumped 8.34.0 → 8.36.0 on 2026-08-16** after Trivy flagged the older image's bundled Chromium with 48 CRITICAL/664 HIGH CVEs (`kubernetes/apps/paperless/gotenberg/app/helmrelease.yaml`) — worth knowing since Gotenberg is this app's PDF-conversion dependency.

## Common operations
- Upgrade the app image or `app-template` chart: edit `helmrelease.yaml`/`ocirepository.yaml`, commit, push; Flux reconciles within `interval: 1h` (or `flux reconcile helmrelease paperless -n paperless`).
- Rotate a secret: update the relevant 1Password item (`paperless`, `openrouter`, or `dragonfly`), then `kubectl annotate externalsecret paperless -n paperless force-sync=$(date +%s)` — the Stakater Reloader annotations on the Deployment auto-roll the pod once `paperless-secret` changes, no manual restart step needed here (unlike hermes-agent/paperless-mcp).
- Rotate the OpenSearch write credential for the stats exporter: update 1Password item `opensearch`, then `kubectl annotate externalsecret opensearch-write-credentials -n paperless force-sync=$(date +%s)`.
- Re-enable the ownership-fix CronJob: uncomment `- ./jobs.yaml` in `kubernetes/apps/paperless/paperless-ngx/app/kustomization.yaml`.
- Pause reconciliation: `flux suspend kustomization paperless -n flux-system` (main app) or `flux suspend kustomization paperless-stats -n flux-system` (stats exporter — independent lifecycle, per `ks.yaml`).

## TODOs / unknowns
- Whether the disabled `paperless-cronjob-fix-ownership` CronJob is meant to be re-enabled or was permanently retired — the commit message only says "for now" (`8cdfe2b`); not resolved anywhere else in the repo.
- The exact Workflow trigger condition ("Document Added" scope/tag filter) and the webhook payload contract live in Paperless's own database, not this repo — see `docs/apps/paperless-mcp.md`'s TODOs for the same gap from the consumer side.
- Which Mail Account/Mail Rule is configured for IMAP ingestion, and whether it's actually enabled, isn't visible from any file here — GUI/DB-only config, not GitOps-tracked.
- No `docs/incidents/` entry currently references `paperless-ngx` by name (checked via `grep -rl paperless docs/incidents/`) — notable given the OCR/AI/mail integration surface, but there may simply not have been a SEV yet.

---
_See also: `docs/apps/hermes-agent.md` and `docs/apps/paperless-mcp.md` for the document-processing webhook/tool-call workflow; `docs/apps/proxmox-ansible.md` for the scan-to-consume FTP/NFS path; `docs/apps/cloudnative-pg.md` and `docs/apps/dragonfly.md` for the database/cache dependencies._
