# hermes-agent

> **Namespace**  hermes-agent
> **Source**     plain manifests, hand-rolled (not the upstream `ultraworkers/hermes-agent-helm-chart` — see Known quirks), image `nousresearch/hermes-agent:v2026.8.16.2` (`kubernetes/apps/hermes-agent/hermes-agent/app/deployment.yaml`)
> **Hostname**   `hermes.${SECRET_DOMAIN}` on `envoy-internal` only (LAN/VPN) — the Hermes Desktop backend; the webhook port stays ClusterIP-only

## What it does here
A personal LLM agent (OpenRouter-backed) that runs as a long-lived gateway process with multiple front-ends: Telegram, an in-cluster webhook receiver (currently just Paperless-ngx's "new document" workflow notification), WhatsApp pairing, scheduled cron scripts (a portfolio price check, a cluster-health check), and the Hermes Desktop app, which pairs with this pod as a thin client (see [Hermes Desktop](#hermes-desktop-thin-client)). Not a stateless request/response service — `HERMES_HOME` (`/opt/data`) holds mutable state (config, WhatsApp session, agent memory) that must survive pod restarts, which shapes most of this app's operational quirks below.

## Architecture at a glance
- **Depends on:** OpenRouter (LLM API), CNPG postgres `portfolio` database (own local secret copy — see Secrets), Telegram Bot API, the in-cluster webhook caller (Paperless-ngx), read-only Kubernetes API access (own ServiceAccount/RBAC, `rbac.yaml`) for its agent "terminal" tool, self-hosted Firecrawl (`kubernetes/apps/hermes-agent/firecrawl/`) as its web-scraping backend, and four sibling MCP tool servers reached over plain HTTP inside the namespace (`configmap.yaml`): `paperless-mcp`, `nextcloud-mcp`, `mailu-mcp` and `immich-mcp`, each at `<name>.hermes-agent.svc.cluster.local:8000` — see `docs/apps/paperless-mcp.md`, `docs/apps/nextcloud-mcp.md`, `docs/apps/mailu-mcp.md` and `docs/apps/immich-mcp.md` for each.
- **Depended on by:** the Hermes Desktop app (via `hermes.${SECRET_DOMAIN}`, Authentik OIDC — blueprint `10-hermes-oidc.yaml`), Paperless-ngx's document-consumption workflow (webhook push, one-directional — Paperless doesn't wait on a response), and Open WebUI's `hermes_agent` Tool, which posts to the `owui-task` webhook route so a chat user can hand this agent a task (`kubernetes/apps/open-webui/open-webui/app/tools/hermes_agent.py`). Both callers authenticate with the same single `WEBHOOK_SHARED_SECRET` — the webhook platform has no per-caller credential.

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/hermes-agent/hermes-agent/app/deployment.yaml` | Hand-rolled Deployment — 3 init containers + main container, extensively commented |
| `kubernetes/apps/hermes-agent/hermes-agent/app/configmap.yaml` | Bootstrap `config.yaml` — model routing per use case, agent, terminal, security, desktop backend OIDC (`dashboard:`) |
| `kubernetes/apps/hermes-agent/hermes-agent/app/externalsecret.yaml` | OpenRouter key, Telegram bot token/chat ID, webhook shared secret, portfolio DB password |
| `kubernetes/apps/hermes-agent/hermes-agent/app/service.yaml` | ClusterIP, port 8644 (webhook) + 9119 (desktop backend) |
| `kubernetes/apps/hermes-agent/hermes-agent/app/httproute.yaml` | `hermes.${SECRET_DOMAIN}` → :9119 on `envoy-internal` |
| `kubernetes/apps/security/authentik/app/blueprints/10-hermes-oidc.yaml` | Public OIDC client `hermes`, group `hermes-users` bound to the app |
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
- **Webhook (8644): ClusterIP only, no HTTPRoute** — the webhook front-end (`config.yaml`'s `platforms.webhook`) is for in-cluster callers only; today that's two callers, Paperless-ngx (route `paperless-document-added`) and Open WebUI (route `owui-task`), each with its own ingress block in `ciliumnetworkpolicy.yaml`.
- **Desktop backend (9119): `hermes.${SECRET_DOMAIN}` on `envoy-internal`** (`httproute.yaml`). No public DNS record, no Cloudflare Tunnel path; reachable from the LAN (k8s-gateway) or VPN only. The CNP admits 9119 only from the Envoy pods and Gatus.
- **SSO for the desktop backend is the app's own OIDC gate, not Envoy forward-auth.** `config.yaml`'s `dashboard.oauth.self_hosted` registers upstream's `self-hosted` OIDC plugin against Authentik (public client `hermes`, PKCE, scopes incl. `offline_access`). Authentik only lets members of `hermes-users` through (policy binding on the app). The desktop authenticates API and WebSocket calls with a Bearer token the backend brokered (RFC 8252), which a cookie-based outpost in front would break.
- A non-loopback bind always requires a registered auth provider (upstream hardening, June 2026): if the OIDC block is broken, the backend refuses to start rather than coming up open. Gatus checks `/api/status` for `auth_required == true` on top.
- CiliumNetworkPolicy (`ciliumnetworkpolicy.yaml`) restricts webhook ingress to the expected caller — not re-derived here in full; read that file directly before assuming who else can reach port 8644.

## Storage
`hermes-agent-data` (5Gi, `zfs-nfs`, `ReadWriteOnce`) — single-replica by design, since `HERMES_HOME` holds mutable state that can't be shared across concurrent writers. `strategy: Recreate` on the Deployment for the same reason (old and new pod must never both hold the volume at once).

## Known quirks
- **Hand-rolled Deployment, not the upstream Helm chart.** The `ultraworkers/hermes-agent-helm-chart` is young (3.5 months old at time of adoption) and had a wrong default image tag and a wrong ExternalSecret `apiVersion` on first real deploy — reverted to hand-rolled manifests instead of continuing to fight an immature chart for a Deployment simple enough not to need the abstraction.
- **Restored-PVC ownership fix, and why it must stay best-effort.** `fix-data-ownership` (first init container) does a one-time, non-recursive `chown 1000:1000 /opt/data`, needed because a Velero/Kopia-restored copy of this PVC keeps `/opt/data`'s own top-level ownership exactly as backed up (uid 10000, mode `0700`) — `fsGroup`/`fsGroupChangePolicy` cannot fix this, since those only ever adjust group ownership, never permission mode bits. This same fix broke production the first time it shipped, because production's own long-lived `/opt/data` mount rejects `chown` outright (even under the added `CAP_CHOWN`) once past its initial provisioning — the script must stay `|| echo ...; exit 0`, never `set -e`, for exactly this reason. Full incident: `docs/incidents/2026-08-16-hermes-agent-restore-pvc-chown-permission-denied.md`.
- **Two different "relative to home" conventions for cron scripts, undocumented upstream.** `hermes cron create --script` only accepts a path relative to `~/.hermes/scripts/` (an absolute path is rejected outright) — but a script's own `--no-agent` cron run resolves its `--script` path from `$HERMES_HOME/scripts/` instead. The portfolio price-check script and the cluster-health-check script are copied to *both* locations by `bootstrap-config` to satisfy each convention.
- **WhatsApp pairing writes its session to a different path than the gateway reads it from** — a real path inconsistency in the app itself. `bootstrap-config` symlinks `/opt/data/platforms/whatsapp → /opt/data/whatsapp` on every pod start to bridge it (`ln -sfn`, safe to re-run).
- **The bootstrap ConfigMap is a plain (non-hash-suffixed) resource** — editing `configmap.yaml` does **not** automatically roll the pod. Run `kubectl rollout restart deployment/hermes-agent -n hermes-agent` after any config change. This bites hardest on `platforms.webhook.extra.routes`: a newly declared route 404s until the pod restarts, and the caller's error will look like a typo rather than a stale pod.
- **The webhook platform is asynchronous, and that shapes what a caller can promise.** `POST /webhooks/<route>` validates the secret, renders the route's prompt template, hands it to the agent as a background lane and returns `{"status": ..., "delivery_id": ...}` immediately; the actual answer goes to the route's `deliver:` target (Telegram), never back over the HTTP call (`/opt/hermes/gateway/platforms/webhook.py`). A task can run for minutes and up to `agent.max_turns: 45`. Open WebUI's `hermes_agent` Tool docstring spells this out so the model reports a hand-over rather than waiting for — or inventing — a result.
- **The Paperless route's prompt was rewritten after the `paperless_full` v2.0.0 retrofit.** It used to carry a paragraph warning the model that 232 MCP tools were only reachable via `tool_search` and that `get_document` had four confusable near-namesakes. After the retrofit there are 21 Paperless tools and no such collision; the prompt now points at `bulk_edit()`/`raw_request()` and their `capabilities()` reference instead. If that paragraph reappears in a diff, it is stale.
- **`tools-install` init container lands tools on the shared PVC, not the image**, because an init container's root filesystem isn't shared with the main container — only volume mounts are. This is how `kubectl` (for the agent's "terminal" tool) and `edge-tts`/`faster-whisper` (installed via `uv pip install --target=`, not into a venv, since a venv would live on the main container's own unshared rootfs and get wiped every restart) end up available to the running agent.
- **Model routing per use case, all via OpenRouter** (`configmap.yaml`, block under `model:`): default `mistralai/mistral-medium-3.1` (unchanged, the unattended pipelines were tuned on it); aliases `alltag`/`code`/`tief`/`schnell` for per-session switching (`/model code` in the desktop or any chat); `fallback_providers` across vendors (Anthropic Haiku, then Gemini Flash Lite); subagents on Claude Haiku 5.5; side tasks (vision, page summaries, compression, titles) on Gemini 3.1 Flash Lite. `provider_routing.data_collection: deny` keeps OpenRouter on hosts that don't store or train on prompts — but upstream does **not** apply it to auxiliary calls, so each pinned auxiliary task repeats it in `extra_body`. A new auxiliary task added later needs the same, or it silently routes without the restriction. All five models were checked live with a tool call and `data_collection: deny` (2026-10-17).
- **Desktop sessions start expensive.** The system prompt plus tool schemas is ~23k tokens before the first word; on `code` (Sonnet 5.5) the first message of a session cost ~0.06 USD in the test, `tief` (Opus 5.5) roughly twice that. Prompt caching makes follow-up turns cheap.
- **The desktop backend runs with `HOME=/opt/data`, the gateway with `HOME=/opt/data/home`.** The image's s6 `dashboard` run script hard-codes `export HOME=/opt/data`. Anything a desktop session resolves relative to `~` therefore lands in a different tree than the gateway's — in practice only `hermes cron create --script` from a desktop session (validated against `/opt/data/.hermes/scripts/`, executed by the gateway from `/opt/data/home/.hermes/scripts/`). Create script-based cron jobs via `kubectl exec` instead.
- **The desktop backend is also an admin surface.** It can edit `config.yaml`, MCP servers, skills and API keys of this instance. `config.yaml` edits vanish on the next pod start (`bootstrap-config` overwrites it), but keys written to `/opt/data/.env` and skills persist on the PVC. That is why access is limited to `hermes-users`.
- **Model choice was changed for reliability, not cost**: moved off OpenRouter's free-tier shared pool (`mistralai/mistral-small-3.2-24b-instruct`) after it repeatedly hit 429s and returned garbled tool-call names under load, burning the full `agent.max_turns` budget on stuck retries — the LLM then filled the forced summary with invented excuses rather than reporting the real tool failure. Now on the paid `mistral-medium-3.1` tier.

## Hermes Desktop (thin client)
The desktop app is only a renderer: chats, agent loop, tools, MCP calls, memory and LLM calls all run in this pod (upstream: each `(gateway, profile)` pair runs on the machine that owns it; a remote gateway's files and tool output are fetched through it). Setup on a Mac:

1. Add your Authentik account to the group `hermes-users` (Directory → Groups → hermes-users → Users).
2. Install **only the desktop app** from `https://hermes-agent.nousresearch.com/desktop`. Do **not** let it install the local Hermes runtime on first launch (that would create `~/.hermes` with its own agent, tools and provider keys). Check afterwards: `ls ~/.hermes` must not exist and `command -v hermes` must print nothing.
3. Settings → Gateways → add **Remote gateway** `https://hermes.${SECRET_DOMAIN}` → Sign in. The system browser opens Authentik (password + MFA), then hands a code back to the app on `127.0.0.1`. Tokens are stored encrypted in the macOS keychain.
4. Set **Open on launch** to this gateway. Remove any **Hermes Cloud** entry. The app-managed **Local** entry cannot be removed (upstream), but without a runtime it has nothing to run.
5. Optional, enforces "nothing else" on the network: an outbound firewall rule for the app (LuLu / Little Snitch locally, MDM-pushed content filter in a company) that allows only `hermes.${SECRET_DOMAIN}:443` — Authentik runs in the system browser, not in the app. This also blocks the app's own update check; updates then go through whoever manages the machine.

Things the app does on the desktop itself and sends to the backend, all user-triggered: HUD window context (`read_window_below`), comment-mode screenshots, dragged-in file attachments, and dictation (which can call an STT provider directly — leave dictation off if that must not happen). Desktop plugins are not sandboxed; install none.

Verify from the LAN: `curl -s https://hermes.${SECRET_DOMAIN}/api/status | jq '.auth_required, .auth_providers, .auth_flows'` → `true`, `["self-hosted"]`, includes `"native_pkce"`.

## Common operations
- Change `config.yaml`: edit `configmap.yaml`, commit, push, then `kubectl rollout restart deployment/hermes-agent -n hermes-agent` (ConfigMap changes don't auto-roll this Deployment).
- Pin a cron job to a model lane: `hermes cron edit <job_id> --model google/gemini-3.1-flash-lite --provider openrouter` (via `kubectl exec`); `hermes cron list` shows the current pins.
- Add a model lane: add a `model_aliases` entry (any OpenRouter model id with tool support), confirm it with a one-shot `hermes -z "…" -m <alias> --usage-file /tmp/u.json` in the pod, then roll the pod.
- Add a new tool for the agent's terminal: extend `tools-install`'s script, one reviewable diff at a time — land binaries under `/opt/data/tools/...`, never `/usr/*` (invisible to the main container otherwise).
- Re-pair WhatsApp: exec into the pod and run the `hermes whatsapp` pairing CLI; session lands at `/opt/data/whatsapp/session`, symlinked to where the gateway actually checks.

## TODOs / unknowns
- Exact CiliumNetworkPolicy ingress rule for the webhook port not restated here — read `ciliumnetworkpolicy.yaml` directly before adding a second webhook caller.
- Whether `hermes-agent-data`'s backup (Velero/Kopia) is on the same schedule as other app PVCs, or has any special handling given the restore-ownership quirk above, not verified from this repo directly for this doc.

---
_See also: `docs/incidents/2026-08-16-hermes-agent-restore-pvc-chown-permission-denied.md`._
