# hermes-agent

> **Namespace**  hermes-agent
> **Source**     plain manifests, hand-rolled (not the upstream `ultraworkers/hermes-agent-helm-chart` — see Known quirks), image `nousresearch/hermes-agent:v0.23.3` (upstream switched from CalVer to SemVer tags; `.renovaterc.json5` pins Renovate to `v0.x`) (`kubernetes/apps/hermes-agent/hermes-agent/app/deployment.yaml`)
> **Hostname**   `hermes.${SECRET_DOMAIN}` on `envoy-internal` only (LAN/VPN) — the Hermes Desktop backend; the webhook port stays ClusterIP-only

## What it does here
A personal LLM agent (OpenRouter-backed) that runs as a long-lived gateway process with multiple front-ends: Telegram, an in-cluster webhook receiver (currently just Paperless-ngx's "new document" workflow notification), WhatsApp pairing, scheduled cron scripts (a portfolio price check, a cluster-health check), and the Hermes Desktop app, which pairs with this pod as a thin client (see [Hermes Desktop](#hermes-desktop-thin-client)). Not a stateless request/response service — `HERMES_HOME` (`/opt/data`) holds mutable state (config, WhatsApp session, agent memory) that must survive pod restarts, which shapes most of this app's operational quirks below.

## Architecture at a glance
- **Depends on:** OpenRouter (LLM API), CNPG postgres `portfolio` database (own local secret copy — see Secrets), Telegram Bot API, the in-cluster webhook caller (Paperless-ngx), read-only Kubernetes API access (own ServiceAccount/RBAC, `rbac.yaml`) for its agent "terminal" tool, self-hosted Firecrawl (`kubernetes/apps/hermes-agent/firecrawl/`) as its web-scraping backend, and four sibling MCP tool servers reached over plain HTTP inside the namespace (`configmap.yaml`): `paperless-mcp`, `nextcloud-mcp`, `mailu-mcp` and `immich-mcp`, each at `<name>.hermes-agent.svc.cluster.local:8000` — see `docs/apps/paperless-mcp.md`, `docs/apps/nextcloud-mcp.md`, `docs/apps/mailu-mcp.md` and `docs/apps/immich-mcp.md` for each.
- **Depended on by:** the Hermes Desktop app (via `hermes.${SECRET_DOMAIN}`, Authentik OIDC — blueprint `10-hermes-oidc.yaml`), Paperless-ngx's document-consumption workflow (webhook push, one-directional — Paperless doesn't wait on a response), and Open WebUI's `hermes_agent` Tool, which posts to the `owui-task` webhook route so a chat user can hand this agent a task (`kubernetes/apps/open-webui/open-webui/app/tools/hermes_agent.py`). Both callers authenticate with the same single `WEBHOOK_SHARED_SECRET` — the webhook platform has no per-caller credential.

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/hermes-agent/hermes-agent/app/deployment.yaml` | Hand-rolled Deployment — 2 init containers + main container + kubeconfig sidecar, extensively commented |
| `kubernetes/apps/hermes-agent/hermes-agent/app/configmap.yaml` | Bootstrap `config.yaml` — model routing per use case, agent, terminal, security, desktop backend OIDC (`dashboard:`) |
| `kubernetes/apps/hermes-agent/hermes-agent/app/externalsecret.yaml` | OpenRouter key, Telegram bot token/chat ID, webhook shared secret, portfolio DB password |
| `kubernetes/apps/hermes-agent/hermes-agent/app/service.yaml` | ClusterIP, port 8644 (webhook) + 9119 (desktop backend) |
| `kubernetes/apps/hermes-agent/hermes-agent/app/httproute.yaml` | `hermes.${SECRET_DOMAIN}` → :9119 on `envoy-internal` |
| `kubernetes/apps/security/authentik/app/blueprints/10-hermes-oidc.yaml` | Public OIDC client `hermes`, group `hermes-users` bound to the app |
| `kubernetes/apps/hermes-agent/hermes-agent/app/rbac.yaml` | Read-only Kubernetes API access for the agent's terminal tool |
| `kubernetes/apps/hermes-agent/hermes-agent/app/pvc.yaml` | `hermes-agent-data`, 5Gi, `zfs-nfs`, RWO |
| `kubernetes/apps/hermes-agent/hermes-agent/app/job-portfolio-schema.yaml` | One-off schema-init Job for the portfolio database |
| `kubernetes/apps/hermes-agent/hermes-agent/app/profiles/` | Specialist profiles (`<name>/config.yaml` + `SOUL.md`), the Chief of Staff's `chief-of-staff.SOUL.md`, and `bootstrap_profiles.sh`, which seeds them on every start |

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
- **Client IP is set by Envoy, not by the client (since 2026-10-17).** The route's `RequestHeaderModifier` overwrites `X-Forwarded-For` and `X-Real-IP` with `%DOWNSTREAM_REMOTE_ADDRESS_WITHOUT_PORT%`. The dashboard's auth code (`dashboard_auth` `_client_ip`) takes the raw leftmost XFF entry with no trust check. Without the overwrite, a LAN client chose the IP in auth audit logs and in the per-IP caps on password login (unused here, OIDC only) and on pending native sign-ins. uvicorn itself does not trust Envoy (`forwarded_allow_ips` defaults to `127.0.0.1`), so `request.client.host` stays the Envoy pod IP. Background: `docs/apps/envoy-gateway.md` (Client IP).

## Storage
`hermes-agent-data` (5Gi, `zfs-nfs`, `ReadWriteOnce`) — single-replica by design, since `HERMES_HOME` holds mutable state that can't be shared across concurrent writers. `strategy: Recreate` on the Deployment for the same reason (old and new pod must never both hold the volume at once).

## Known quirks
- **Hand-rolled Deployment, not the upstream Helm chart.** The `ultraworkers/hermes-agent-helm-chart` is young (3.5 months old at time of adoption) and had a wrong default image tag and a wrong ExternalSecret `apiVersion` on first real deploy — reverted to hand-rolled manifests instead of continuing to fight an immature chart for a Deployment simple enough not to need the abstraction.
- **No `fsGroup`, and `/opt/data` must not be group-writable.** hermes v0.23 refuses its gateway control socket (`/opt/data/gateway.sock`) when `HERMES_HOME` is writable by group or other (`unsafe_control_permissions`). Every client that discovers the gateway that way fails, including Kanban workers, with "Gateway starting: deadline". With `fsGroup` + `OnRootMismatch` the kubelet set `2775` on every start (and walked the whole NFS volume for 5 to 10 minutes). So the pod has no `fsGroup`, and `bootstrap-config` runs `chmod go-w /opt/data`. Check with `stat -c %a /opt/data`; it must show no group or other write bit.
- **Init containers run as uid 10000, the image's runtime user.** The image's boot hook chowns `/opt/data` to `hermes` (uid 10000) and hermes chmods it to `0700` whenever it writes a secret file, so only uid 10000 can ever enter it. `bootstrap-config` and `tools-install` therefore run as `runAsUser: 10000` (gid 1000 unchanged, which owns everything below with group-write). This holds in steady state, after a Velero/Kopia restore and on a fresh volume. The former `fix-data-ownership` init container (chown to 1000) was removed: the image reverted it on every boot. Background: `docs/incidents/2026-11-16-hermes-agent-init-uid-permission-denied.md` (six-day outage) and the older `docs/incidents/2026-08-16-hermes-agent-restore-pvc-chown-permission-denied.md`.
- **Two different "relative to home" conventions for cron scripts, undocumented upstream.** `hermes cron create --script` only accepts a path relative to `~/.hermes/scripts/` (an absolute path is rejected outright) — but a script's own `--no-agent` cron run resolves its `--script` path from `$HERMES_HOME/scripts/` instead. The portfolio price-check script and the cluster-health-check script are copied to *both* locations by `bootstrap-config` to satisfy each convention.
- **WhatsApp pairing writes its session to a different path than the gateway reads it from** — a real path inconsistency in the app itself. `bootstrap-config` symlinks `/opt/data/platforms/whatsapp → /opt/data/whatsapp` on every pod start to bridge it (`ln -sfn`, safe to re-run).
- **The bootstrap ConfigMap is a plain (non-hash-suffixed) resource** — editing `configmap.yaml` does **not** automatically roll the pod. Run `kubectl rollout restart deployment/hermes-agent -n hermes-agent` after any config change. This bites hardest on `platforms.webhook.extra.routes`: a newly declared route 404s until the pod restarts, and the caller's error will look like a typo rather than a stale pod.
- **The webhook platform is asynchronous, and that shapes what a caller can promise.** `POST /webhooks/<route>` validates the secret, renders the route's prompt template, hands it to the agent as a background lane and returns `{"status": ..., "delivery_id": ...}` immediately; the actual answer goes to the route's `deliver:` target (Telegram), never back over the HTTP call (`/opt/hermes/gateway/platforms/webhook.py`). A task can run for minutes and up to `agent.max_turns: 45`. Open WebUI's `hermes_agent` Tool docstring spells this out so the model reports a hand-over rather than waiting for — or inventing — a result.
- **The Paperless route's prompt was rewritten after the `paperless_full` v2.0.0 retrofit.** It used to carry a paragraph warning the model that 232 MCP tools were only reachable via `tool_search` and that `get_document` had four confusable near-namesakes. After the retrofit there are 21 Paperless tools and no such collision; the prompt now points at `bulk_edit()`/`raw_request()` and their `capabilities()` reference instead. If that paragraph reappears in a diff, it is stale.
- **`tools-install` init container lands `kubectl` on the shared PVC, not the image**, because an init container's root filesystem isn't shared with the main container — only volume mounts are. `edge-tts`/`faster-whisper` are no longer installed here: since v0.23 the image installs them itself on first use (lazy install under `$HERMES_HOME`). The old `/opt/data/tools/pip` target held Python 3.13 wheels and is deleted by `tools-install`.
- **Model routing per use case, all via OpenRouter** (`configmap.yaml`, block under `model:`): default `mistralai/mistral-medium-3.1` (unchanged, the unattended pipelines were tuned on it); aliases `alltag`/`code`/`tief`/`schnell` for per-session switching (`/model code` in the desktop or any chat); `fallback_providers` across vendors (Claude Haiku 5.5, then Mistral Small 2603); subagents and side tasks (vision, page summaries, compression, titles) on Claude Haiku 5.5. `provider_routing.data_collection: deny` keeps OpenRouter on hosts that don't train on prompts — but upstream does **not** apply it to auxiliary calls, so each pinned auxiliary task repeats it in `extra_body`, together with `zdr: true` (zero data retention; `provider_routing` itself has no `zdr` key). A new auxiliary task added later needs the same, or it silently routes without the restriction. **`mistral-medium-3.1` has no ZDR endpoint** (live probe 2026-11-22: HTTP 400 under `provider.zdr: true`), so do not enforce ZDR account-wide on OpenRouter while it is still the default. Haiku 5.5, Sonnet 5.5, Mistral Large 4 and Mistral Small 2603 all returned a real tool call under `zdr: true` on 2026-11-22.
- **Desktop sessions start expensive.** The system prompt plus tool schemas is ~23k tokens before the first word; on `code` (Sonnet 5.5) the first message of a session cost ~0.06 USD in the test, `tief` (Opus 5.5) roughly twice that. Prompt caching makes follow-up turns cheap.
- **The desktop backend runs with `HOME=/opt/data`, the gateway with `HOME=/opt/data/home`.** The image's s6 `dashboard` run script hard-codes `export HOME=/opt/data`. Anything a desktop session resolves relative to `~` therefore lands in a different tree than the gateway's — in practice only `hermes cron create --script` from a desktop session (validated against `/opt/data/.hermes/scripts/`, executed by the gateway from `/opt/data/home/.hermes/scripts/`). Create script-based cron jobs via `kubectl exec` instead.
- **The desktop backend is also an admin surface.** It can edit `config.yaml`, MCP servers, skills and API keys of this instance. `config.yaml` edits vanish on the next pod start (`bootstrap-config` overwrites it), but keys written to `/opt/data/.env` and skills persist on the PVC. That is why access is limited to `hermes-users`.
- **Model choice was changed for reliability, not cost**: moved off OpenRouter's free-tier shared pool (`mistralai/mistral-small-3.2-24b-instruct`) after it repeatedly hit 429s and returned garbled tool-call names under load, burning the full `agent.max_turns` budget on stuck retries — the LLM then filled the forced summary with invented excuses rather than reporting the real tool failure. Now on the paid `mistral-medium-3.1` tier.

## Chief of Staff and specialist profiles
The default profile is the **Chief of Staff**: it talks to the owner on Telegram, WhatsApp and the desktop app and hands domain work to specialist profiles as Kanban tasks. One gateway process serves every profile (`gateway.multiplex_profiles`), including the Kanban dispatcher (`kanban.dispatch_in_gateway`, 20 s tick).

| Profile | Domain | Model | MCP tools | Disabled toolsets |
| --- | --- | --- | --- | --- |
| `default` (Chief of Staff) | Triage, delegation, summaries | `mistral-large-4-0` (Medium 3.1 was retired by Mistral; the Paperless pipeline still runs here) | all four servers | none; `kanban` enabled per platform |
| `assistenz` | Mail, calendar, Nextcloud files | `mistral-small-2603` (alias `gross` → Large 4) | mailu without `delete_*`; nextcloud read + upload only | terminal, code_execution, file, web, search, browser, cronjob, delegation, … |
| `finanzen` | Invoices, contracts, deadlines | `mistral-large-4-0` | paperless without `delete_documents` | same as assistenz |
| `steuern` | Tax view on Paperless | `mistral-large-4-0` | paperless, read-only allow-list (11 tools) | same as assistenz |
| `kurator` | Immich curation | `mistral-small-2603` (vision stays on Mistral) | immich without `raw_request` | same as assistenz |
| `devops` | Cluster diagnosis | `claude-haiku-5.5` (alias `analyse` → Sonnet 5.5) | flux-operator-mcp (read-only) | browser, cronjob, delegation, … (keeps terminal + web) |
| `sicherheit` | SOC: Wazuh alerts (Proxmox, Mac, OPNsense/Suricata), vulnerabilities, Trivy/Kyverno | `claude-haiku-5.5` (alias `analyse`) | wazuh-mcp, flux-operator-mcp | same as assistenz (log content is attacker-controlled) |
| `isms` | Compliance as code in GitLab `isms/compliance-as-code`, MRs only | `claude-sonnet-5.5` (alias `schnell`) | flux-operator-mcp; GitLab via git/REST with its own token | browser, cronjob, delegation, … (keeps terminal + file + web) |
| `infrastruktur` | Proxmox host | `claude-haiku-5.5` (alias `analyse`) | proxmox-mcp (PVEAuditor) | same as assistenz |
| `netzwerk` | OPNsense | `claude-haiku-5.5` (alias `analyse`) | opnsense-mcp (GET only) | same as assistenz |
| `depot` | Securities: daily price check, assessments, watch candidates | `mistral-large-4-0` | portfolio-mcp (watch candidates + assessment log only, no positions, no free SQL) | terminal, code_execution, file, browser, … (keeps web: news and ticker lookup) |

Why it's built this way:
- **Kanban, not `delegate_task`.** A Kanban task runs entirely in the assignee's profile: its own model, `mcp_servers`, `agent.disabled_toolsets`, `.env` and memory. `delegate_task` children (including the `advanced-delegation` plugin's `profile_routes`) keep the **parent's** toolsets and MCP servers, so they give no isolation.
- **Profiles don't inherit config.** Each `profiles/<name>/config.yaml` is complete. Served profiles don't see the container environment either (`agent/secret_scope.py`), so `bootstrap_profiles.sh` writes each profile's own `.env` with the OpenRouter key, read from a mounted Secret file.
- **Untrusted input → no exfiltration channel.** Profiles that read mail or documents have no shell, no local files, no web and no browser. Prompt-injection text in a PDF has nothing to send data out with.
- **Not a security boundary between profiles.** All profiles share one pod and uid 10000. `devops` (terminal + file) could technically read the other profiles' `.env`; accepted because it only processes cluster data. Real separation would mean one pod per profile.
- **Profile-only secrets** use keys `<profile>__<VAR>` in `externalsecret-profile-env.yaml`. `bootstrap_profiles.sh` writes each one only into that profile's `.env`, e.g. `isms__GITLAB_TOKEN`. The Secret volume is `optional`, so a missing 1Password item doesn't stop the pod.
- **Personal names are placeholders.** SOULs and prompts use `${SECRET_HERMES_OWNER_NAME}`, `${SECRET_HERMES_PARTNER_NAME}`, `${SECRET_HERMES_OWNER_MAILBOX}` and `${SECRET_HERMES_PARTNER_MAILBOX}` from the SOPS-encrypted `cluster-secrets`. The profiles ConfigMap goes through Flux substitution, so **no other `$` may appear in those files**. `bootstrap_profiles.sh` sits in its own ConfigMap with substitution disabled.
- **Telegram delivery from a specialist** (its cron jobs, a webhook route with `profile: <name>`) only works into a chat/topic that `gateway.profile_routes` maps to that profile; the shared bot refuses anything else (502). Deliver targets are `telegram:<group>:<topic>`; create them via `kubectl exec` with `--deliver "telegram:$TELEGRAM_HOME_CHANNEL:<topic>"` so the group id never lands in the repo.

### Scheduled jobs
Each job runs in the profile that owns the domain and posts into that profile's topic. The prompts live only in each profile's `cron/jobs.json`, which is pod state on the PVC and not in git. Change a prompt with `hermes -p <profile> cron edit <name> --prompt …`. All jobs use `--no-wrap-response`. Formatting rules (bold heading with one emoji, bullets, German dates, no IDs or tables) are in every SOUL's "Ausgabe in Telegram" section. A job that has nothing to report answers `[SILENT]`, and nothing gets posted.

| Job | Profile | Schedule | Topic | Notes |
| --- | --- | --- | --- | --- |
| `morgen-briefing` | default | 07:00 daily | 1 | calendar, mail, new Paperless documents, deadlines |
| `politik-radar` | default | 12:34 daily | 1 | web search; memory entry "Politik-Radar" holds what was already reported |
| `sicherheits-radar` | isms | 12:40 daily | 36 | web search, weighted to the software running here; memory entry "Sicherheits-Radar" |
| `fristen` | finanzen | 08:00 daily | 3 | one `list_documents` call with a fixed `custom_field_query` (field names + `range`) |
| `monatsuebersicht` | finanzen | 09:00 on the 1st | 3 | |
| `depot-check` | depot | 19:00 daily | 44 | `--script portfolio_price_check.py` (asyncpg from the image's venv), then assessment + web news |
| `homelab-waechter` | devops | :17 every 2 h | 6 | `--no-agent --script cluster_health_check.py`, silent when healthy |

The pre-profile jobs in the default profile (`daily-digest`, `vertrags-abo-monitoring`, `finanzuebersicht-monatlich`, `portfolio-price-check`, `homelab-waechter`, `themen-radar`) are paused, not removed, so they can be restored with `hermes cron resume <name>`.

## Telegram and Open WebUI
- **Telegram:** The forum group "Hermes" (id in `SECRET_HERMES_TELEGRAM_GROUP`) is the home.
  - Topic 1 (General) belongs to the Chief of Staff. It is `TELEGRAM_HOME_CHANNEL` + `TELEGRAM_HOME_CHANNEL_THREAD_ID=1`, so the default profile's cron results and webhook deliveries land there.
  - Topics routed to specialists (`gateway.profile_routes`): 2 assistenz, 3 finanzen, 4 steuern, 5 kurator, 6 devops, 35 sicherheit, 36 isms, 37 infrastruktur, 38 netzwerk, 44 depot. A topic's id is its root message id. Read it from the `thread_id` column of `sessions` in `/opt/data/state.db` after writing one message into the topic.
  - `telegram.free_response_chats` lifts the @mention requirement for this group only.
  - The old DM still works; only the owner's user id is allowed anywhere (`TELEGRAM_ALLOWED_USERS`).
- **Open WebUI:** The OpenAI-compatible API server (`API_SERVER_*` env vars, port 8642, Service port `api`) is reachable only from the open-webui pod (CNP), and requests carry Bearer `API_SERVER_KEY` (1Password `hermes-agent` / `HERMES_API_SERVER_KEY`).
  - `/v1` is the Chief of Staff; `/p/<profile>/v1` is a specialist.
  - A served profile only accepts the key from its own `.env`, so `bootstrap_profiles.sh` writes it into every profile.
  - Open WebUI's workspace sync (`kubernetes/apps/open-webui/open-webui/app/workspace/workspace.yaml`, `openai_connections`) creates the ten connections with prefix `hermes.`.
  - Models without a row in Open WebUI's `models` table are **admin-only**, which is why no access grant exists for them.

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
- Pin a cron job to a model lane: `hermes cron edit <job_id> --model anthropic/claude-haiku-5.5 --provider openrouter` (via `kubectl exec`); `hermes cron list` shows the current pins.
- Add a model lane: add a `model_aliases` entry (any OpenRouter model id with tool support), confirm it with a one-shot `hermes -z "…" -m <alias> --usage-file /tmp/u.json` in the pod, then roll the pod.
- Add a new tool for the agent's terminal: extend `tools-install`'s script, one reviewable diff at a time — land binaries under `/opt/data/tools/...`, never `/usr/*` (invisible to the main container otherwise).
- Re-pair WhatsApp: exec into the pod and run the `hermes whatsapp` pairing CLI; session lands at `/opt/data/whatsapp/session`, symlinked to where the gateway actually checks.

## TODOs / unknowns
- Exact CiliumNetworkPolicy ingress rule for the webhook port not restated here — read `ciliumnetworkpolicy.yaml` directly before adding a second webhook caller.
- Whether `hermes-agent-data`'s backup (Velero/Kopia) is on the same schedule as other app PVCs, or has any special handling given the restore-ownership quirk above, not verified from this repo directly for this doc.

---
_See also: `docs/incidents/2026-08-16-hermes-agent-restore-pvc-chown-permission-denied.md`._
