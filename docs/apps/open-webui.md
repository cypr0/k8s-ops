# Open WebUI

> **Namespace**  `open-webui`
> **Source**     `oci://ghcr.io/bjw-s-labs/helm/app-template` (chart `app-template` v5.1.0) + image `ghcr.io/open-webui/open-webui:v0.11.0`
> **Hostname**   `ai.${SECRET_DOMAIN}` — internal-only (see [Routing & access](#routing--access))

## What it does here
The cluster's chat UI in front of OpenRouter-hosted LLMs, with OIDC SSO via Authentik, a pgvector-backed RAG pipeline (dedicated Tika instance for document text extraction), Firecrawl-backed web search, **five bundled "Tools"** (Python files under `tools/`) reaching this cluster's Paperless-ngx, Nextcloud, Mailu/Radicale, Immich and `hermes-agent`, and a **GitOps-managed workspace** (`workspace/`) that declares Open WebUI groups and workspace models — currently a locked-down "Lern-Buddy" model for the household's children. Local Ollama inference was deprovisioned — CPU-only inference on this cluster's worker VMs throttled to ~2 tok/s — so all chat now routes through OpenRouter (`kubernetes/apps/open-webui/open-webui/app/helmrelease.yaml:47-51`).

Four of the five tool source files are mirrored (not shared at runtime) into `kubernetes/apps/hermes-agent/{paperless,nextcloud,mailu,immich}-mcp/app/` to also serve as standalone MCP servers for `hermes-agent`. `hermes_agent.py` has no mirror — it points *at* hermes-agent, so a mirror would be a loop.

## Architecture at a glance
- **Depends on:**
  - CNPG cluster `postgres` (namespace `database`) — two managed `Database`s owned by role `openwebuiusr`: `openwebuidbapp` (app state, `kubernetes/apps/database/cloudnative-pg/databases/database-open-webui-app.yaml`) and `openwebuidbrag` (pgvector RAG store, `kubernetes/apps/database/cloudnative-pg/databases/database-open-webui-rag.yaml`)
  - Dragonfly (namespace `database`) — websocket session manager and general Redis cache (DB index 0), via `REDIS_URL`/`WEBSOCKET_REDIS_URL`
  - Authentik — OIDC SSO; blueprint `kubernetes/apps/security/authentik/app/blueprints/05-open-webui-oidc.yaml`, client credentials fed by `kubernetes/apps/security/authentik/app/externalsecret-open-webui-oidc.yaml` (same 1Password item this app's own `externalsecret.yaml` reads)
  - Firecrawl (`hermes-agent` namespace, Service `firecrawl-api:3002`) — web-search backend, replacing a flaky searxng sidecar (`kubernetes/apps/open-webui/open-webui/app/helmrelease.yaml:53-67`)
  - Tika (namespace `open-webui`, separate app dir `kubernetes/apps/open-webui/tika/`) — dedicated `apache/tika` instance for RAG document text extraction (`CONTENT_EXTRACTION_ENGINE: tika`)
  - Open Terminal (namespace `open-webui`, separate app dir `kubernetes/apps/open-webui/open-terminal/`) — multi-user shell sandbox; this app's backend proxies user terminal requests to it on `:8000` (`kubernetes/apps/open-webui/open-webui/app/ciliumnetworkpolicy.yaml:82-91`)
  - Paperless-ngx REST API, Nextcloud WebDAV/OCS API, Mailu (IMAP :993 + Radicale CalDAV/CardDAV via `front` :443), Immich REST API (:2283) and hermes-agent's webhook platform (:8644) — reached only when a user invokes the corresponding bundled Tool (see [Tools](#tools))
  - ExternalSecret → 1Password items `openwebui`, `openrouter`, `dragonfly`, `nextcloud`, `paperless`, `mailu-philipp`, `mailu-anna`, `hermes-agent`, `immich`
- **Depended on by:** none at the infrastructure level — this is a leaf, user-facing app. Four of its Tool source files are manually mirrored (not a live runtime dependency) into `hermes-agent`'s `paperless-mcp`, `nextcloud-mcp`, `mailu-mcp` and `immich-mcp` standalone MCP servers, kept in sync by hand (see `docs/apps/paperless-mcp.md`, `docs/apps/nextcloud-mcp.md`, `docs/apps/mailu-mcp.md`, `docs/apps/immich-mcp.md`). For `mailu_full.py` the mirror runs the *other* way round: it was written for `mailu-mcp` first and only later grew an Open WebUI counterpart.

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/open-webui/open-webui/app/helmrelease.yaml` | app-template chart values: image, env, probes, service, persistence |
| `kubernetes/apps/open-webui/open-webui/app/ocirepository.yaml` | `app-template` chart source (OCI, v5.1.0) |
| `kubernetes/apps/open-webui/open-webui/app/externalsecret.yaml` | Core secret: DB/pgvector URLs, Dragonfly URL, OpenRouter key, OIDC client id/secret, WebUI secret key |
| `kubernetes/apps/open-webui/open-webui/app/externalsecret-paperless-token.yaml` | Paperless API token for the `paperless_full` Tool |
| `kubernetes/apps/open-webui/open-webui/app/externalsecret-nextcloud-token.yaml` | Nextcloud admin credentials for the `nextcloud_full` Tool |
| `kubernetes/apps/open-webui/open-webui/app/externalsecret-mailu-credentials.yaml` | Both mailboxes' real Mailu credentials for the `mailu_full` Tool |
| `kubernetes/apps/open-webui/open-webui/app/externalsecret-hermes-token.yaml` | hermes-agent webhook shared secret for the `hermes_agent` Tool |
| `kubernetes/apps/open-webui/open-webui/app/externalsecret-immich-token.yaml` | Immich API key for the `immich_photos` Tool — **the one credential that must be created by hand**, see [Secrets](#secrets) |
| `kubernetes/apps/open-webui/open-webui/app/httproute.yaml` | Gateway routing (`envoy-internal` only) |
| `kubernetes/apps/open-webui/open-webui/app/ciliumnetworkpolicy.yaml` | Network policy for both `open-webui` and its co-located `tika` deployment |
| `kubernetes/apps/open-webui/open-webui/app/pvc.yaml` | `open-webui-data-pvc`, NFS-backed, 50Gi |
| `kubernetes/apps/open-webui/open-webui/app/tools/` | GitOps-managed Open WebUI "Tools" source + sync mechanism (see below) |
| `kubernetes/apps/open-webui/open-webui/app/tools/job-sync-tools.yaml` | Post-reconcile Job that writes the Tool source into the Open WebUI DB |
| `kubernetes/apps/open-webui/open-webui/app/tools/sync_tools.py` | The sync script the Job runs |
| `kubernetes/apps/open-webui/open-webui/app/tools/paperless_full.py` | Open WebUI Tool: full Paperless-ngx REST API access (21 methods) |
| `kubernetes/apps/open-webui/open-webui/app/tools/nextcloud_full.py` | Open WebUI Tool: full Nextcloud Client API access (22 methods) |
| `kubernetes/apps/open-webui/open-webui/app/tools/mailu_full.py` | Open WebUI Tool: Mailu IMAP (read-only) + Radicale CalDAV/CardDAV (read/write) (13 methods) |
| `kubernetes/apps/open-webui/open-webui/app/tools/immich_photos.py` | Open WebUI Tool: Immich photo search/albums/people, read-mostly (14 methods) |
| `kubernetes/apps/open-webui/open-webui/app/tools/hermes_agent.py` | Open WebUI Tool: hand a task to hermes-agent, fire-and-forget (3 methods) |
| `kubernetes/apps/open-webui/open-webui/app/workspace/` | GitOps-managed groups + workspace models (see [Workspace](#workspace-groups-and-models)) |
| `kubernetes/apps/open-webui/open-webui/app/workspace/workspace.yaml` | The declaration: group permission sets, the Lern-Buddy model and its system prompt |
| `kubernetes/apps/open-webui/open-webui/app/workspace/sync_workspace.py` | The sync script the workspace Job runs |
| `kubernetes/apps/open-webui/open-webui/app/workspace/job-sync-workspace.yaml` | Post-reconcile Job that writes groups/models into the Open WebUI DB |

## Secrets
| ExternalSecret | 1Password item / field(s) | Consumer |
| --- | --- | --- |
| `open-webui` (`externalsecret.yaml`) | item `openwebui`: `OPENWEBUI_DB_PASS` (→ `DATABASE_URL`, `PGVECTOR_DB_URL`), `OPENWEBUI_OIDC_CLIENT_ID`/`OPENWEBUI_OIDC_CLIENT_SECRET`, `OPENWEBUI_SECRET_KEY`; item `openrouter`: `OPENROUTER_OPENWEBUI_API_KEY` (→ `OPENAI_API_KEY`); item `dragonfly`: `DRAGONFLY_PASSWORD` (→ `REDIS_URL`, `WEBSOCKET_REDIS_URL`) | `envFrom` secret `open-webui-secret` on the `app` container; also `envFrom` on the tool-sync Job (`tools/job-sync-tools.yaml`) for `DATABASE_URL`/`WEBUI_SECRET_KEY` |
| `open-webui-paperless-token` (`externalsecret-paperless-token.yaml`) | item `paperless`: `PAPERLESS_API_TOKEN` — same item/field `kubernetes/apps/paperless/paperless-ngx/app/externalsecret.yaml` reads | `envFrom` secret `open-webui-paperless-secret`; auto-fills `tools/paperless_full.py`'s `Valves.API_TOKEN` default |
| `open-webui-nextcloud-token` (`externalsecret-nextcloud-token.yaml`) | item `nextcloud`: `NEXTCLOUD_ADMIN_USERNAME`/`NEXTCLOUD_ADMIN_PASSWORD` — same item/fields `kubernetes/apps/nextcloud/nextcloud/app/externalsecret.yaml` reads, templated here as `NEXTCLOUD_USERNAME`/`NEXTCLOUD_PASSWORD` | `envFrom` secret `open-webui-nextcloud-secret`; auto-fills `tools/nextcloud_full.py`'s `Valves` |
| `open-webui-mailu-credentials` (`externalsecret-mailu-credentials.yaml`) | items `mailu-philipp` / `mailu-anna`, fields `username`/`password` — the same two items `kubernetes/apps/hermes-agent/mailu-mcp/app/externalsecret.yaml` reads | `envFrom` secret `open-webui-mailu-secret`; auto-fills `tools/mailu_full.py`'s per-mailbox `Valves` |
| `open-webui-hermes-token` (`externalsecret-hermes-token.yaml`) | item `hermes-agent`: `HERMES_WEBHOOK_SHARED_SECRET` — the same field hermes-agent's own ExternalSecret reads, templated here as `HERMES_WEBHOOK_SECRET` | `envFrom` secret `open-webui-hermes-secret`; auto-fills `tools/hermes_agent.py`'s `Valves` |
| `open-webui-immich-token` (`externalsecret-immich-token.yaml`) | item `immich`: `IMMICH_API_KEY` — **a field that does not exist until someone creates it**, see below | `envFrom` secret `open-webui-immich-secret`, wired `optional: true`; auto-fills `tools/immich_photos.py`'s `Valves` |

**The Immich key is the one prerequisite this app cannot provision for itself.** Immich has no service-account concept: API keys are minted only by a signed-in user under *Account Settings → API Keys*, and that instance is OIDC-only with `passwordLogin.enabled: false`. Create one as the user whose photos the tool should see, scope it to `asset.read`/`asset.view`/`asset.update`/`album.read`/`album.create`/`album.update`/`person.read`/`server.about`, and add it as a new `IMMICH_API_KEY` field on the **existing** 1Password `immich` item. Its `envFrom` entry is marked `optional: true` on purpose (`helmrelease.yaml`): without that, a missing field would leave the Secret uncreated and the pod stuck in `CreateContainerConfigError` — the whole chat UI down because one of five tools has no credential. With it, the pod starts and `immich_photos` reports "No Immich API key configured" when used. `kubernetes/apps/hermes-agent/immich-mcp/` reads the same field and has no such fallback, because that pod has no other job.

Four of the five Tool credentials are **deliberately reused, not dedicated**: `paperless_full` uses the same Paperless API token `paperless-cronjob-fix-ownership` already uses (`kubernetes/apps/paperless/paperless-ngx/app/jobs.yaml`), `nextcloud_full` authenticates as the **real Nextcloud super-admin account**, `mailu_full` uses **both parents' actual mailbox passwords** (Mailu has no read-only or calendar-only credential mechanism, only per-mailbox master passwords), and `hermes_agent` uses hermes-agent's single global webhook secret (that platform has no per-caller credential). All documented as conscious blast-radius trade-offs in the respective ExternalSecret's header comment, deferred rather than fixed. This is exactly why the access grants were narrowed — see [Known quirks](#known-quirks).

Two more ExternalSecrets outside this app's directory read the same `openwebui` 1Password item: `kubernetes/apps/database/cloudnative-pg/databases/externalsecret-open-webui.yaml` (provisions the `openwebuiusr` DB role's password) and `kubernetes/apps/security/authentik/app/externalsecret-open-webui-oidc.yaml` (feeds the Authentik blueprint's OIDC client id/secret) — both must stay consistent with the values this app's own `externalsecret.yaml` templates.

## Routing & access
- **HTTPRoute** (`httproute.yaml`) attaches `ai.${SECRET_DOMAIN}` to `envoy-internal` **only** — there is no `envoy-external` attachment. `kubernetes/apps/network/cloudflare-tunnel/app/helmrelease.yaml` tunnels the wildcard `*.${SECRET_DOMAIN}` to `envoy-external`, and CoreDNS's split-horizon `hosts` block (`kubernetes/apps/kube-system/coredns/app/helmrelease.yaml:64-68`) only overrides `id.${SECRET_DOMAIN}` and `cloud.${SECRET_DOMAIN}` to resolve internally — `ai.${SECRET_DOMAIN}` has no such entry. Net effect (inferred from config, not independently traffic-tested): despite `CORS_ALLOW_ORIGIN`/`WEBUI_URL` being set to the public-shaped `https://ai.${SECRET_DOMAIN}`, this app is reachable only from inside the cluster/LAN via `envoy-internal`, not from the public internet.
- Long request/backend timeouts (`15m`) on the route — streaming LLM responses can run for minutes (`httproute.yaml:24-27`).
- **SSO:** OIDC via Authentik, `OAUTH_PROVIDER_NAME: Authentik`, issuer `https://id.${SECRET_DOMAIN}/application/o/open-webui/.well-known/openid-configuration`. Password login is disabled (`ENABLE_LOGIN_FORM: "false"`, `ENABLE_SIGNUP: "false"`); the app is OIDC-only. Group claims are enabled (`OAUTH_GROUP_CLAIM: groups`, `ENABLE_OAUTH_GROUP_MANAGEMENT`/`ENABLE_OAUTH_GROUP_CREATION: "true"`) via a custom scope mapping — blueprint `kubernetes/apps/security/authentik/app/blueprints/05-open-webui-oidc.yaml`.
- **CiliumNetworkPolicy** (`ciliumnetworkpolicy.yaml`, two policies — one for `open-webui`, one for the co-located `tika`):
  - Ingress allowed only from `envoy` pods (namespace `network`, port 8090) and Gatus health checks (namespace `monitoring`).
  - Egress allowed to: CoreDNS, Postgres/Redis in the `database` namespace, the in-namespace `tika` Service, Paperless (namespace `paperless`, port 80), Nextcloud (namespace `nextcloud`, port 80 post-DNAT), Immich (namespace `immich`, port 2283), Mailu (namespace `mail`, ports 993 + 443), hermes-agent (namespace `hermes-agent`, port 8644), Open Terminal (namespace `open-webui`, port 8000), Authentik via `envoy` on port 10443 (OIDC), Firecrawl's `api` Service (namespace `hermes-agent`, port 3002), and unrestricted `world` egress on 80/443 for OpenRouter **and for Open WebUI's own on-demand `pip install` of a Tool's `requirements:` frontmatter** (see [Known quirks](#known-quirks)).
  - The matching ingress rules live in the target apps' own policies: `kubernetes/apps/immich/immich/app/ciliumnetworkpolicy.yaml`, `kubernetes/apps/mail/mailu/app/ciliumnetworkpolicy.yaml` and `kubernetes/apps/hermes-agent/hermes-agent/app/ciliumnetworkpolicy.yaml` each grew a block for this app.

## Tools
Five Python files under `tools/`, synced into the DB by `tools/job-sync-tools.yaml`.

| Tool id | Reaches | Write scope |
| --- | --- | --- |
| `paperless_full` | Paperless-ngx REST API | Full, incl. destructive (delete goes to a recoverable trash) |
| `nextcloud_full` | Nextcloud WebDAV + OCS | Full, incl. destructive, as the **real super-admin** |
| `mailu_full` | Mailu IMAP + Radicale CalDAV/CardDAV | Mail **read-only**; calendar and contacts read/write — and Radicale has **no trash**, so those deletes are final |
| `immich_photos` | Immich REST API | Read-mostly: create album, add to album, favorite/description/rating. **No delete, and `raw_request` refuses the `DELETE` verb** |
| `hermes_agent` | hermes-agent webhook `:8644` | Hands over a task; fire-and-forget |

### Why they are shaped the way they are (token budget)
Every enabled tool's method signatures and docstrings are compiled into JSON schemas and injected into the model's context **on every chat turn**. `paperless_full` v1 spelled out one typed method per API operation — 139 of them — and `nextcloud_full` v1 had 85. Measured with Open WebUI's own `get_tool_specs()`:

| | Methods | Spec size |
| --- | --- | --- |
| `paperless_full` v1 → v2 | 139 → 21 | ~11,900 → ~2,600 tokens |
| `nextcloud_full` v1 → v2 | 85 → 22 | ~6,200 → ~2,300 tokens |
| **Old two tools, both enabled** | 224 | **~18,100 tokens** |
| **All five tools, all enabled** | 73 | **~9,300 tokens** |

So two and a half times the integrations for roughly half the context cost. The v2 shape, applied to all five:

1. **Keep the operations that carry real traffic typed**, with their v1 names and parameter names unchanged — hermes-agent's document-pipeline prompt calls `get_document`, `list_custom_fields`, `modify_documents_tags`, `set_documents_correspondent`, `update_document` and friends *by name*, and a rename there is a silent breakage, not a compile error.
2. **Collapse near-duplicate methods into one dispatcher.** Every `bulk_edit` variant that used to be its own method (`add_tag`, `set_storage_path`, `modify_custom_fields`, `reprocess`, `rotate`, `merge`, `split`, `edit_pdf`, …) now goes through one `bulk_edit(document_ids, method, parameters)`.
3. **Push the long tail behind the escape hatch** that v1 already had: `raw_request()` / `raw_ocs_request()` / `raw_webdav_request()`. Saved views, mail rules, share links, workflows, users, groups, tasks, trash, config, logs — all still reachable, none of them costing context.
4. **Add `capabilities(topic)` as the discovery path.** It returns the endpoint paths, `bulk_edit` method names, filter params and destructive-call warnings needed to drive the escape hatches — as a *return value*, so that reference material costs tokens only in the turn that actually asks for it.
5. **Slim the responses too, where volume is on the response side.** `immich_photos` projects each search hit down to id/filename/type/date/place/people instead of returning full EXIF-laden asset records (`verbose=True` opts back in).

This also fixed a second-order problem: hermes-agent saw **232** MCP tools across the mirrored servers, enough that its Paperless webhook prompt needed a whole paragraph warning the model about `get_document` vs `get_document_type`/`_metadata`/`_notes`/`_version`. That paragraph is now obsolete and was rewritten (`kubernetes/apps/hermes-agent/hermes-agent/app/configmap.yaml`).

### What was given up
Nothing in reach; some in convenience. A rarely-used operation now takes two calls (`capabilities()` then `raw_request()`) instead of one, and the model has to construct a path and body rather than filling a typed signature. That is the trade the numbers above buy.

## Workspace: groups and models
`workspace/workspace.yaml` declares Open WebUI **groups** (with their permission sets) and **workspace models** (base LLM + system prompt + params + who may use them); `workspace/job-sync-workspace.yaml` applies it, using `open_webui`'s own SQLAlchemy models the same way the tool sync does.

Two groups:

- **`owui-family`** — the adults. Sole principal for all five Tools. Permissions deliberately left undeclared, so the group keeps Open WebUI's instance defaults, including any future change to those defaults, instead of freezing today's copy into git.
- **`owui-kids`** — children aged 8–11. Every setting is a narrowing: no workspace model/prompt/tool creation or import, no sharing of anything, no API keys, no direct tool servers, no code interpreter, no `web_upload`. **No web search** — it is Firecrawl-backed and unfiltered, so it would put arbitrary scraped pages in front of an eight-year-old; the model's own answer is the surface this group is meant to have. `chat.system_prompt: false` is the load-bearing one: with it true, a child could override the Lern-Buddy's system prompt from the chat controls panel and talk to the raw base model. File upload stays **on** — photos of homework are the point.

One model: **`kids-assistant` ("Lern-Buddy")**, `anthropic/claude-haiku-4.5` plus a German system prompt that covers tone (short sentences, everyday examples, du), homework policy (help, never solve), honesty (say "I don't know" rather than invent), hard content limits, a no-personal-data rule, and an escalation path for bullying/fear/violence that points at a trusted adult and the *Nummer gegen Kummer* (116 111). Readable by `owui-kids` and by `owui-family`, so a parent can open the exact same model their child is talking to.

**Before any of this does anything, the Authentik side has to exist**, and it is not in this repo: create the groups `owui-family` and `owui-kids` in Authentik, create the child accounts, and put each account in the right group. Open WebUI mirrors the groups from the `groups` claim on first login (`ENABLE_OAUTH_GROUP_MANAGEMENT`/`ENABLE_OAUTH_GROUP_CREATION`) and matches them **by name**, so pre-creating them from this Job produces no duplicate. Until then: the Lern-Buddy exists but nobody is in `owui-kids`, and all five Tools are owner-only.

## Storage
- `open-webui-data-pvc` (`pvc.yaml`): `ReadWriteMany`, `zfs-nfs` StorageClass, 50Gi, mounted at `/data` with `subPath: data`. Also used for the HuggingFace/sentence-transformers embedding cache (`HF_HOME`/`SENTENCE_TRANSFORMERS_HOME: /data/cache/huggingface`).
- The `app` container runs as root (`runAsNonRoot: false`, `runAsUser/Group: 0`) so the NFS-backed PVC is writable — `csi-driver-nfs` doesn't enforce `fsGroup` (`helmrelease.yaml:140-145`).
- Single replica, `Recreate` strategy — no concurrent writers to the PVC.
- Covered by Velero: included in `includedNamespaces` on `schedule-daily.yaml`, `schedule-weekly.yaml`, and `schedule-monthly.yaml` (`kubernetes/apps/velero/schedules/`). Restores are no longer verified automatically — the restore-test CronJob was removed on 2026-09-20 (`docs/apps/velero.md`).

## Known quirks
- **Tools (and groups, and workspace models) live only in the DB — there's no file mount or env autoload for any of them.** `tools/job-sync-tools.yaml` is a post-reconcile Job (`kustomize.toolkit.fluxcd.io/force: "enabled"`) that runs `tools/sync_tools.py` **inside the Open WebUI image itself**, using `open_webui`'s own SQLAlchemy models, because this instance is OIDC-only and the admin HTTP API's password/API-key auth paths don't work here (`sync_tools.py:1-37`). The tool source is shipped via `configMapGenerator` (content-hashed name), so any edit to a tool `.py` or to `sync_tools.py` changes the Job spec and Flux re-runs the sync automatically on the next reconcile (`tools/kustomization.yaml`). `workspace/job-sync-workspace.yaml` is the same mechanism for groups and models, deliberately kept as a **second** Job with its own hash so a tool edit doesn't re-run the workspace sync or vice versa.
- **The tool-sync Job cannot `pip install`, so its dependencies come from an init container.** `load_tool_module_by_id()` actually *execs* each tool module to compute its specs, and before that it shells out to `pip install` for whatever the module's `requirements:` frontmatter lists — raising on failure (`open_webui/utils/plugin.py`, `install_frontmatter_requirements`). That works in the app pod (root, writable rootfs); it does not work in the sync Job (uid 1000, `readOnlyRootFilesystem`). `httpx` is already in the image, so the original two tools never exercised that path — `mailu_full.py`'s `icalendar` is the first requirement that isn't, which is what forced the `install-tool-deps` init container and `ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS: "False"` on that Job. **Adding a tool with a new third-party dependency means adding it to that init container**, otherwise the Job fails with a plain `ImportError`. The app pod is unaffected and still installs on demand — which does mean the `mailu_full` tool needs PyPI reachable the first time it is called after a pod restart.
- **The sync Job's owner user is hardcoded by email** (`TOOL_OWNER_EMAIL`, `tools/job-sync-tools.yaml:67-68`) to one specific, already-existing Authentik-provisioned admin account (address deliberately not restated here). `sync_tools.py`'s `main()` hard-fails the Job if that user doesn't exist or isn't `role=admin`.
- **Tool access is now group-scoped and fails CLOSED.** It used to be `[("user", "*")]` — every signed-in user — on the argument that this was a single-admin instance. Adding child accounts ends that argument: a kid must not be able to invoke a tool that authenticates as the Nextcloud super-admin, reads either parent's mailbox, or deletes Paperless documents. All five tools now grant to `("group_name", "owui-family")` only (`tools/sync_tools.py`). Because Open WebUI group **ids** are UUIDs that only exist once the group does — and these groups are created by Open WebUI itself from Authentik's `groups` claim — `sync_tools.py` resolves the group by NAME at sync time. **A name that matches no group yet resolves to nothing**, is logged at WARNING, and leaves that tool owner-only. That direction is deliberate: a typo'd or not-yet-created group must never degrade into "everyone".
- **Nothing in git manages group membership, and nothing should.** Open WebUI rebuilds it from the OIDC `groups` claim on every login, adding the user to every claimed group and removing them from every group not claimed (`open_webui/utils/oauth.py`). Membership is Authentik's job; this repo only decides what a group is *allowed* to do.
- **`FIRECRAWL_API_KEY: internal-unused`** in `helmrelease.yaml:64-67` is not a real secret — Firecrawl runs with `USE_DB_AUTHENTICATION=false`, so it never validates the value; Open WebUI just requires the field to be non-empty.
- **`CONTENT_EXTRACTION_ENGINE: tika` silently breaks web-search context extraction**: Tika returns empty for HTML, so if web-search results need to feed the RAG pipeline, this needs to be set to `""` to fall back to the built-in loader — noted as an open trade-off in `helmrelease.yaml:83-87`, not resolved.
- **`AIOHTTP_CLIENT_TIMEOUT`/`AIOHTTP_CLIENT_TIMEOUT_MODEL` are deliberately empty**, not omitted — a numeric value cuts streaming LLM responses mid-stream with an `aiohttp` `TransferEncodingError` (`helmrelease.yaml:95-98`).
- **`forceRename: open-webui-app`** on the Service (`helmrelease.yaml:150-155`) exists because `app-template` only suffixes a Service name when a release has more than one Service; after the searxng sidecar's Service was removed, `open-webui` would otherwise silently rename from `open-webui-app` to bare `open-webui`, breaking `httproute.yaml`'s `backendRef`.
- **Open WebUI "Computer" (`cptr`) was added then reverted** — a workstation/desktop feature behind Authentik, added in `8551666` and removed in `c9fb038` ("run locally instead"); per this operator's auto-memory (not independently re-verified here), the two reusable lessons from that attempt were an envoy-proxy egress allow-list needed per new backend, and that Authentik's embedded-outpost provider list is a full-replace, not additive.
- **Local Ollama was fully deprovisioned** (no `ollama` app directory remains in the repo) after CPU-only inference throttled to ~2 tok/s on this cluster's overcommitted worker VMs; `ENABLE_OLLAMA_API: "false"` and all chat routes through OpenRouter instead (`helmrelease.yaml:46-51`).
- A transient Kopia/Velero backup job error for open-webui during the 2026-08-16 CoreDNS AAAA/NXDOMAIN incident was only temporally correlated, not confirmed with direct log evidence — see `docs/incidents/2026-08-16-coredns-aaaa-nxdomain-breaks-internal-dns.md`.

## Common operations
- Upgrade chart version: edit `helmrelease.yaml` (`chartRef`/`ocirepository.yaml` for the chart, or the `image.tag` for the app image — kept in sync with `tools/job-sync-tools.yaml`'s image tag, per its comment), commit, push; Flux reconciles within `interval: 1h` (or force with `flux reconcile helmrelease open-webui -n open-webui`).
- Edit or add a Tool: edit/add a `.py` under `tools/`, add it to `tools/kustomization.yaml`'s `configMapGenerator` file list if new, add its access grant to `TOOL_ACCESS_GRANTS` in `tools/sync_tools.py`, and — if it needs a third-party package that isn't in the Open WebUI image — add that package to the `install-tool-deps` init container in `tools/job-sync-tools.yaml`. If the tool has an MCP mirror under `kubernetes/apps/hermes-agent/*-mcp/app/`, copy it there too. A commit to any tool file or to `sync_tools.py` changes the sync job's spec, so Flux re-runs the DB sync automatically on the next reconcile — no manual trigger needed.
- Change the kids' system prompt, a group permission or the Lern-Buddy's base model: edit `workspace/workspace.yaml`, commit, push. Same automatic re-run, via the second Job. Check it landed with `kubectl logs -n open-webui job/sync-openwebui-workspace`.
- Check who can actually use a tool: `kubectl logs -n open-webui job/sync-openwebui-tools` — it logs a WARNING per tool whose group doesn't exist yet, which is the usual reason a tool is invisible to everyone but the owner.
- Rotate a secret: update the relevant 1Password item, then `kubectl annotate externalsecret <name> -n open-webui force-sync=$(date +%s)` (or wait out the `1h` refresh interval); the `reloader.stakater.com` annotations on the controller restart the pod automatically once `open-webui-secret` changes.
- Pause reconciliation: `flux suspend kustomization open-webui -n flux-system` / `flux suspend helmrelease open-webui -n open-webui`.

## TODOs / unknowns
- The open-webui Kopia backup job error during the 2026-08-16 CoreDNS incident was never confirmed as DNS-caused versus coincidental (tracked in the incident doc's own TODO list).
- Exact behavior/error surfaced to a user when the RAG pipeline tries to embed a web-search HTML result under `CONTENT_EXTRACTION_ENGINE: tika` (silently empty context vs. a visible error) was not observed/tested from this repo.

---
_Cite every non-obvious claim with a repo-root-relative file path (e.g. `kubernetes/apps/security/authentik/app/helmrelease.yaml`), not a bare filename — this doc lives under `docs/apps/`, so relative paths must resolve from there._
