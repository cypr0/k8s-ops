# hermes-agent

> **Namespace**  hermes-agent
> **Source**     plain manifests, hand-rolled (not the upstream `ultraworkers/hermes-agent-helm-chart` — see Known quirks), image `nousresearch/hermes-agent:v2026.8.16.2` (`kubernetes/apps/hermes-agent/hermes-agent/app/deployment.yaml`)
> **Hostname**   none — ClusterIP only, no external exposure

## What it does here
A personal LLM agent (OpenRouter-backed) that runs as a long-lived gateway process with multiple front-ends: Telegram, an in-cluster webhook receiver (currently just Paperless-ngx's "new document" workflow notification), WhatsApp pairing, and scheduled cron scripts (a portfolio price check, a cluster-health check). Not a stateless request/response service — `HERMES_HOME` (`/opt/data`) holds mutable state (config, WhatsApp session, agent memory) that must survive pod restarts, which shapes most of this app's operational quirks below.

## Architecture at a glance
- **Depends on:** OpenRouter (LLM API), CNPG postgres `portfolio` database (own local secret copy — see Secrets), Telegram Bot API, the in-cluster webhook caller (Paperless-ngx), read-only Kubernetes API access (own ServiceAccount/RBAC, `rbac.yaml`) for its agent "terminal" tool, self-hosted Firecrawl (`kubernetes/apps/hermes-agent/firecrawl/`) as its web-scraping backend, and three sibling MCP tool servers reached over plain HTTP inside the namespace (`configmap.yaml`): `paperless-mcp.hermes-agent.svc.cluster.local:8000`, `nextcloud-mcp.hermes-agent.svc.cluster.local:8000`, and `mailu-mcp.hermes-agent.svc.cluster.local:8000` — see `docs/apps/paperless-mcp.md`, `docs/apps/nextcloud-mcp.md`, and `docs/apps/mailu-mcp.md` for each.
- **Depended on by:** Paperless-ngx's document-consumption workflow (webhook push, one-directional — Paperless doesn't wait on a response).

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/hermes-agent/hermes-agent/app/deployment.yaml` | Hand-rolled Deployment — 3 init containers + main container, extensively commented |
| `kubernetes/apps/hermes-agent/hermes-agent/app/configmap.yaml` | Bootstrap `config.yaml` — model, agent, terminal, security settings |
| `kubernetes/apps/hermes-agent/hermes-agent/app/externalsecret.yaml` | OpenRouter key, Telegram bot token/chat ID, webhook shared secret, portfolio DB password |
| `kubernetes/apps/hermes-agent/hermes-agent/app/service.yaml` | ClusterIP, port 8644, webhook-only |
| `kubernetes/apps/hermes-agent/hermes-agent/app/rbac.yaml` | Read-only Kubernetes API access for the agent's terminal tool |
| `kubernetes/apps/hermes-agent/hermes-agent/app/pvc.yaml` | `hermes-agent-data`, 5Gi, `zfs-nfs`, RWO |
| `kubernetes/apps/hermes-agent/hermes-agent/app/job-portfolio-schema.yaml` | One-off schema-init Job for the portfolio database |

## Secrets
| Key (in `hermes-agent-secrets`) | 1Password source | Consumed by |
| --- | --- | --- |
| `OPENROUTER_API_KEY` | `openrouter` item | main container envFrom (key name fixed by the app, unrelated to the 1Password field name) |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | `telegram` item | Telegram front-end; the same chat ID doubles as both `TELEGRAM_ALLOWED_USERS` and `TELEGRAM_HOME_CHANNEL` (single-user, self-chat pattern) |
| `WEBHOOK_SHARED_SECRET` | `hermes-agent` item | Validates the `X-Gitlab-Token`-shaped header Paperless-ngx's Workflow Webhook action sends — purely an internal, ad hoc-generated shared secret, no external account behind it |
| `PORTFOLIO_DB_PASSWORD` | `portfolio` item (same password as the CNPG-managed `portfolio-db-role` secret in the `database` namespace — duplicated here because K8s Secrets don't cross namespaces) | portfolio price-check cron script |

## Routing & access
- **ClusterIP only, deliberately no HTTPRoute** (`service.yaml`) — the webhook front-end (`config.yaml`'s `platforms.webhook`) is for in-cluster callers only; today that's exactly one caller (Paperless-ngx).
- CiliumNetworkPolicy (`ciliumnetworkpolicy.yaml`) restricts webhook ingress to the expected caller — not re-derived here in full; read that file directly before assuming who else can reach port 8644.
- No SSO — this app has no user-facing web UI to gate.

## Storage
`hermes-agent-data` (5Gi, `zfs-nfs`, `ReadWriteOnce`) — single-replica by design, since `HERMES_HOME` holds mutable state that can't be shared across concurrent writers. `strategy: Recreate` on the Deployment for the same reason (old and new pod must never both hold the volume at once).

## Known quirks
- **Hand-rolled Deployment, not the upstream Helm chart.** The `ultraworkers/hermes-agent-helm-chart` is young (3.5 months old at time of adoption) and had a wrong default image tag and a wrong ExternalSecret `apiVersion` on first real deploy — reverted to hand-rolled manifests instead of continuing to fight an immature chart for a Deployment simple enough not to need the abstraction.
- **Restored-PVC ownership fix, and why it must stay best-effort.** `fix-data-ownership` (first init container) does a one-time, non-recursive `chown 1000:1000 /opt/data`, needed because a Velero/Kopia-restored copy of this PVC keeps `/opt/data`'s own top-level ownership exactly as backed up (uid 10000, mode `0700`) — `fsGroup`/`fsGroupChangePolicy` cannot fix this, since those only ever adjust group ownership, never permission mode bits. This same fix broke production the first time it shipped, because production's own long-lived `/opt/data` mount rejects `chown` outright (even under the added `CAP_CHOWN`) once past its initial provisioning — the script must stay `|| echo ...; exit 0`, never `set -e`, for exactly this reason. Full incident: `docs/incidents/2026-08-16-hermes-agent-restore-pvc-chown-permission-denied.md`.
- **Two different "relative to home" conventions for cron scripts, undocumented upstream.** `hermes cron create --script` only accepts a path relative to `~/.hermes/scripts/` (an absolute path is rejected outright) — but a script's own `--no-agent` cron run resolves its `--script` path from `$HERMES_HOME/scripts/` instead. The portfolio price-check script and the cluster-health-check script are copied to *both* locations by `bootstrap-config` to satisfy each convention.
- **WhatsApp pairing writes its session to a different path than the gateway reads it from** — a real path inconsistency in the app itself. `bootstrap-config` symlinks `/opt/data/platforms/whatsapp → /opt/data/whatsapp` on every pod start to bridge it (`ln -sfn`, safe to re-run).
- **The bootstrap ConfigMap is a plain (non-hash-suffixed) resource** — editing `configmap.yaml` does **not** automatically roll the pod. Run `kubectl rollout restart deployment/hermes-agent -n hermes-agent` after any config change.
- **`tools-install` init container lands tools on the shared PVC, not the image**, because an init container's root filesystem isn't shared with the main container — only volume mounts are. This is how `kubectl` (for the agent's "terminal" tool) and `edge-tts`/`faster-whisper` (installed via `uv pip install --target=`, not into a venv, since a venv would live on the main container's own unshared rootfs and get wiped every restart) end up available to the running agent.
- **Model choice was changed for reliability, not cost**: moved off OpenRouter's free-tier shared pool (`mistralai/mistral-small-3.2-24b-instruct`) after it repeatedly hit 429s and returned garbled tool-call names under load, burning the full `agent.max_turns` budget on stuck retries — the LLM then filled the forced summary with invented excuses rather than reporting the real tool failure. Now on the paid `mistral-medium-3.1` tier.

## Common operations
- Change `config.yaml`: edit `configmap.yaml`, commit, push, then `kubectl rollout restart deployment/hermes-agent -n hermes-agent` (ConfigMap changes don't auto-roll this Deployment).
- Add a new tool for the agent's terminal: extend `tools-install`'s script, one reviewable diff at a time — land binaries under `/opt/data/tools/...`, never `/usr/*` (invisible to the main container otherwise).
- Re-pair WhatsApp: exec into the pod and run the `hermes whatsapp` pairing CLI; session lands at `/opt/data/whatsapp/session`, symlinked to where the gateway actually checks.

## TODOs / unknowns
- Exact CiliumNetworkPolicy ingress rule for the webhook port not restated here — read `ciliumnetworkpolicy.yaml` directly before adding a second webhook caller.
- Whether `hermes-agent-data`'s backup (Velero/Kopia) is on the same schedule as other app PVCs, or has any special handling given the restore-ownership quirk above, not verified from this repo directly for this doc.

---
_See also: `docs/incidents/2026-08-16-hermes-agent-restore-pvc-chown-permission-denied.md`._
