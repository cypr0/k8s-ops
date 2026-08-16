# Open WebUI

> **Namespace**  `open-webui`
> **Source**     `oci://ghcr.io/bjw-s-labs/helm/app-template` (chart `app-template` v5.0.1) + image `ghcr.io/open-webui/open-webui:v0.11.0`
> **Hostname**   `ai.${SECRET_DOMAIN}` — internal-only (see [Routing & access](#routing--access))

## What it does here
The cluster's chat UI in front of OpenRouter-hosted LLMs, with OIDC SSO via Authentik, a pgvector-backed RAG pipeline (dedicated Tika instance for document text extraction), Firecrawl-backed web search, and two bundled "Tools" (Python files under `tools/`) that give chat users full read/write access to this cluster's Nextcloud and Paperless-ngx instances via their own credentials — the same `nextcloud_full.py`/`paperless_full.py` source files are mirrored (not shared at runtime) into `kubernetes/apps/hermes-agent/nextcloud-mcp/app/nextcloud_full.py` and `kubernetes/apps/hermes-agent/paperless-mcp/app/paperless_full.py` to also serve as standalone MCP servers for `hermes-agent`. Local Ollama inference was deprovisioned — CPU-only inference on this cluster's worker VMs throttled to ~2 tok/s — so all chat now routes through OpenRouter (`kubernetes/apps/open-webui/open-webui/app/helmrelease.yaml:47-51`).

## Architecture at a glance
- **Depends on:**
  - CNPG cluster `postgres` (namespace `database`) — two managed `Database`s owned by role `openwebuiusr`: `openwebuidbapp` (app state, `kubernetes/apps/database/cloudnative-pg/databases/database-open-webui-app.yaml`) and `openwebuidbrag` (pgvector RAG store, `kubernetes/apps/database/cloudnative-pg/databases/database-open-webui-rag.yaml`)
  - Dragonfly (namespace `database`) — websocket session manager and general Redis cache (DB index 0), via `REDIS_URL`/`WEBSOCKET_REDIS_URL`
  - Authentik — OIDC SSO; blueprint `kubernetes/apps/security/authentik/app/blueprints/05-open-webui-oidc.yaml`, client credentials fed by `kubernetes/apps/security/authentik/app/externalsecret-open-webui-oidc.yaml` (same 1Password item this app's own `externalsecret.yaml` reads)
  - Firecrawl (`hermes-agent` namespace, Service `firecrawl-api:3002`) — web-search backend, replacing a flaky searxng sidecar (`kubernetes/apps/open-webui/open-webui/app/helmrelease.yaml:53-67`)
  - Tika (namespace `open-webui`, separate app dir `kubernetes/apps/open-webui/tika/`) — dedicated `apache/tika` instance for RAG document text extraction (`CONTENT_EXTRACTION_ENGINE: tika`)
  - Open Terminal (namespace `open-webui`, separate app dir `kubernetes/apps/open-webui/open-terminal/`) — multi-user shell sandbox; this app's backend proxies user terminal requests to it on `:8000` (`kubernetes/apps/open-webui/open-webui/app/ciliumnetworkpolicy.yaml:82-91`)
  - Paperless-ngx REST API and Nextcloud WebDAV/OCS API — reached only when a user invokes the bundled `paperless_full`/`nextcloud_full` Tools, using the real Paperless API token and the real Nextcloud super-admin account respectively (see [Secrets](#secrets))
  - ExternalSecret → 1Password items `openwebui`, `openrouter`, `dragonfly`, `nextcloud`, `paperless`
- **Depended on by:** none at the infrastructure level — this is a leaf, user-facing app. Its two Tool source files are manually mirrored (not a live runtime dependency) into `hermes-agent`'s `nextcloud-mcp` and `paperless-mcp` standalone MCP servers, kept in sync by hand (see `docs/apps/nextcloud-mcp.md`, `docs/apps/paperless-mcp.md`).

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/open-webui/open-webui/app/helmrelease.yaml` | app-template chart values: image, env, probes, service, persistence |
| `kubernetes/apps/open-webui/open-webui/app/ocirepository.yaml` | `app-template` chart source (OCI, v5.0.1) |
| `kubernetes/apps/open-webui/open-webui/app/externalsecret.yaml` | Core secret: DB/pgvector URLs, Dragonfly URL, OpenRouter key, OIDC client id/secret, WebUI secret key |
| `kubernetes/apps/open-webui/open-webui/app/externalsecret-paperless-token.yaml` | Paperless API token for the `paperless_full` Tool |
| `kubernetes/apps/open-webui/open-webui/app/externalsecret-nextcloud-token.yaml` | Nextcloud admin credentials for the `nextcloud_full` Tool |
| `kubernetes/apps/open-webui/open-webui/app/httproute.yaml` | Gateway routing (`envoy-internal` only) |
| `kubernetes/apps/open-webui/open-webui/app/ciliumnetworkpolicy.yaml` | Network policy for both `open-webui` and its co-located `tika` deployment |
| `kubernetes/apps/open-webui/open-webui/app/pvc.yaml` | `open-webui-data-pvc`, NFS-backed, 50Gi |
| `kubernetes/apps/open-webui/open-webui/app/tools/` | GitOps-managed Open WebUI "Tools" source + sync mechanism (see below) |
| `kubernetes/apps/open-webui/open-webui/app/tools/job-sync-tools.yaml` | Post-reconcile Job that writes the Tool source into the Open WebUI DB |
| `kubernetes/apps/open-webui/open-webui/app/tools/sync_tools.py` | The sync script the Job runs |
| `kubernetes/apps/open-webui/open-webui/app/tools/nextcloud_full.py` | Open WebUI Tool: full Nextcloud Client API access |
| `kubernetes/apps/open-webui/open-webui/app/tools/paperless_full.py` | Open WebUI Tool: full Paperless-ngx REST API access |

## Secrets
| ExternalSecret | 1Password item / field(s) | Consumer |
| --- | --- | --- |
| `open-webui` (`externalsecret.yaml`) | item `openwebui`: `OPENWEBUI_DB_PASS` (→ `DATABASE_URL`, `PGVECTOR_DB_URL`), `OPENWEBUI_OIDC_CLIENT_ID`/`OPENWEBUI_OIDC_CLIENT_SECRET`, `OPENWEBUI_SECRET_KEY`; item `openrouter`: `OPENROUTER_OPENWEBUI_API_KEY` (→ `OPENAI_API_KEY`); item `dragonfly`: `DRAGONFLY_PASSWORD` (→ `REDIS_URL`, `WEBSOCKET_REDIS_URL`) | `envFrom` secret `open-webui-secret` on the `app` container; also `envFrom` on the tool-sync Job (`tools/job-sync-tools.yaml`) for `DATABASE_URL`/`WEBUI_SECRET_KEY` |
| `open-webui-paperless-token` (`externalsecret-paperless-token.yaml`) | item `paperless`: `PAPERLESS_API_TOKEN` — same item/field `kubernetes/apps/paperless/paperless-ngx/app/externalsecret.yaml` reads | `envFrom` secret `open-webui-paperless-secret`; auto-fills `tools/paperless_full.py`'s `Valves.API_TOKEN` default |
| `open-webui-nextcloud-token` (`externalsecret-nextcloud-token.yaml`) | item `nextcloud`: `NEXTCLOUD_ADMIN_USERNAME`/`NEXTCLOUD_ADMIN_PASSWORD` — same item/fields `kubernetes/apps/nextcloud/nextcloud/app/externalsecret.yaml` reads, templated here as `NEXTCLOUD_USERNAME`/`NEXTCLOUD_PASSWORD` | `envFrom` secret `open-webui-nextcloud-secret`; auto-fills `tools/nextcloud_full.py`'s `Valves` |

Both Tool credentials are **deliberately reused, not dedicated**: `paperless_full` uses the same Paperless API token `paperless-cronjob-fix-ownership` already uses (`kubernetes/apps/paperless/paperless-ngx/app/jobs.yaml`), and `nextcloud_full` authenticates as the **real Nextcloud super-admin account** — both documented as a conscious blast-radius trade-off in the respective ExternalSecret's header comment, deferred rather than fixed (`externalsecret-nextcloud-token.yaml:8-14`, `externalsecret-paperless-token.yaml:8-17`).

Two more ExternalSecrets outside this app's directory read the same `openwebui` 1Password item: `kubernetes/apps/database/cloudnative-pg/databases/externalsecret-open-webui.yaml` (provisions the `openwebuiusr` DB role's password) and `kubernetes/apps/security/authentik/app/externalsecret-open-webui-oidc.yaml` (feeds the Authentik blueprint's OIDC client id/secret) — both must stay consistent with the values this app's own `externalsecret.yaml` templates.

## Routing & access
- **HTTPRoute** (`httproute.yaml`) attaches `ai.${SECRET_DOMAIN}` to `envoy-internal` **only** — there is no `envoy-external` attachment. `kubernetes/apps/network/cloudflare-tunnel/app/helmrelease.yaml` tunnels the wildcard `*.${SECRET_DOMAIN}` to `envoy-external`, and CoreDNS's split-horizon `hosts` block (`kubernetes/apps/kube-system/coredns/app/helmrelease.yaml:64-68`) only overrides `id.${SECRET_DOMAIN}` and `cloud.${SECRET_DOMAIN}` to resolve internally — `ai.${SECRET_DOMAIN}` has no such entry. Net effect (inferred from config, not independently traffic-tested): despite `CORS_ALLOW_ORIGIN`/`WEBUI_URL` being set to the public-shaped `https://ai.${SECRET_DOMAIN}`, this app is reachable only from inside the cluster/LAN via `envoy-internal`, not from the public internet.
- Long request/backend timeouts (`15m`) on the route — streaming LLM responses can run for minutes (`httproute.yaml:24-27`).
- **SSO:** OIDC via Authentik, `OAUTH_PROVIDER_NAME: Authentik`, issuer `https://id.${SECRET_DOMAIN}/application/o/open-webui/.well-known/openid-configuration`. Password login is disabled (`ENABLE_LOGIN_FORM: "false"`, `ENABLE_SIGNUP: "false"`); the app is OIDC-only. Group claims are enabled (`OAUTH_GROUP_CLAIM: groups`, `ENABLE_OAUTH_GROUP_MANAGEMENT`/`ENABLE_OAUTH_GROUP_CREATION: "true"`) via a custom scope mapping — blueprint `kubernetes/apps/security/authentik/app/blueprints/05-open-webui-oidc.yaml`.
- **CiliumNetworkPolicy** (`ciliumnetworkpolicy.yaml`, two policies — one for `open-webui`, one for the co-located `tika`):
  - Ingress allowed only from `envoy` pods (namespace `network`, port 8090) and Gatus health checks (namespace `monitoring`).
  - Egress allowed to: CoreDNS, Postgres/Redis in the `database` namespace, the in-namespace `tika` Service, Paperless (namespace `paperless`, port 80), Nextcloud (namespace `nextcloud`, port 80 post-DNAT), Open Terminal (namespace `open-webui`, port 8000), Authentik via `envoy` on port 10443 (OIDC), Firecrawl's `api` Service (namespace `hermes-agent`, port 3002), and unrestricted `world` egress on 80/443 for OpenRouter.

## Storage
- `open-webui-data-pvc` (`pvc.yaml`): `ReadWriteMany`, `zfs-nfs` StorageClass, 50Gi, mounted at `/data` with `subPath: data`. Also used for the HuggingFace/sentence-transformers embedding cache (`HF_HOME`/`SENTENCE_TRANSFORMERS_HOME: /data/cache/huggingface`).
- The `app` container runs as root (`runAsNonRoot: false`, `runAsUser/Group: 0`) so the NFS-backed PVC is writable — `csi-driver-nfs` doesn't enforce `fsGroup` (`helmrelease.yaml:140-145`).
- Single replica, `Recreate` strategy — no concurrent writers to the PVC.
- Covered by Velero: included in `includedNamespaces` on `schedule-daily.yaml`, `schedule-weekly.yaml`, and `schedule-monthly.yaml` (`kubernetes/apps/velero/schedules/`), and in the restore-verification cronjob's namespace list (`kubernetes/apps/velero/restore-test/cronjob.yaml:52`).

## Known quirks
- **Tools live only in the DB — there's no file mount or env autoload for Open WebUI "Tools."** `tools/job-sync-tools.yaml` is a post-reconcile Job (`kustomize.toolkit.fluxcd.io/force: "enabled"`) that runs `tools/sync_tools.py` **inside the Open WebUI image itself**, using `open_webui`'s own SQLAlchemy models, because this instance is OIDC-only and the admin HTTP API's password/API-key auth paths don't work here (`sync_tools.py:1-37`). The tool source is shipped via `configMapGenerator` (content-hashed name), so any edit to a tool `.py` or to `sync_tools.py` changes the Job spec and Flux re-runs the sync automatically on the next reconcile (`tools/kustomization.yaml:8-14`).
- **The sync Job's owner user is hardcoded by email** (`TOOL_OWNER_EMAIL`, `tools/job-sync-tools.yaml:67-68`) to one specific, already-existing Authentik-provisioned admin account (address deliberately not restated here). `sync_tools.py`'s `main()` hard-fails the Job if that user doesn't exist or isn't `role=admin`.
- **Tool access is currently wide open by design, not oversight**: `sync_tools.py`'s `TOOL_ACCESS_GRANTS` grants both `paperless_full` and `nextcloud_full` to `[("user", "*")]` — every signed-in user — with an inline comment noting this is a single-admin-user instance today and should be narrowed to a specific Open WebUI group once more users are added (`sync_tools.py:57-74`).
- **`FIRECRAWL_API_KEY: internal-unused`** in `helmrelease.yaml:64-67` is not a real secret — Firecrawl runs with `USE_DB_AUTHENTICATION=false`, so it never validates the value; Open WebUI just requires the field to be non-empty.
- **`CONTENT_EXTRACTION_ENGINE: tika` silently breaks web-search context extraction**: Tika returns empty for HTML, so if web-search results need to feed the RAG pipeline, this needs to be set to `""` to fall back to the built-in loader — noted as an open trade-off in `helmrelease.yaml:83-87`, not resolved.
- **`AIOHTTP_CLIENT_TIMEOUT`/`AIOHTTP_CLIENT_TIMEOUT_MODEL` are deliberately empty**, not omitted — a numeric value cuts streaming LLM responses mid-stream with an `aiohttp` `TransferEncodingError` (`helmrelease.yaml:95-98`).
- **`forceRename: open-webui-app`** on the Service (`helmrelease.yaml:150-155`) exists because `app-template` only suffixes a Service name when a release has more than one Service; after the searxng sidecar's Service was removed, `open-webui` would otherwise silently rename from `open-webui-app` to bare `open-webui`, breaking `httproute.yaml`'s `backendRef`.
- **Open WebUI "Computer" (`cptr`) was added then reverted** — a workstation/desktop feature behind Authentik, added in `8551666` and removed in `c9fb038` ("run locally instead"); per this operator's auto-memory (not independently re-verified here), the two reusable lessons from that attempt were an envoy-proxy egress allow-list needed per new backend, and that Authentik's embedded-outpost provider list is a full-replace, not additive.
- **Local Ollama was fully deprovisioned** (no `ollama` app directory remains in the repo) after CPU-only inference throttled to ~2 tok/s on this cluster's overcommitted worker VMs; `ENABLE_OLLAMA_API: "false"` and all chat routes through OpenRouter instead (`helmrelease.yaml:46-51`).
- A transient Kopia/Velero backup job error for open-webui during the 2026-08-16 CoreDNS AAAA/NXDOMAIN incident was only temporally correlated, not confirmed with direct log evidence — see `docs/incidents/2026-08-16-coredns-aaaa-nxdomain-breaks-internal-dns.md`.

## Common operations
- Upgrade chart version: edit `helmrelease.yaml` (`chartRef`/`ocirepository.yaml` for the chart, or the `image.tag` for the app image — kept in sync with `tools/job-sync-tools.yaml`'s image tag, per its comment), commit, push; Flux reconciles within `interval: 1h` (or force with `flux reconcile helmrelease open-webui -n open-webui`).
- Edit or add a Tool: edit/add a `.py` under `tools/`, add it to `tools/kustomization.yaml`'s `configMapGenerator` file list if new, and (for a new tool) add its access grant to `TOOL_ACCESS_GRANTS` in `tools/sync_tools.py`. A commit to any tool file or to `sync_tools.py` changes the sync job's spec, so Flux re-runs the DB sync automatically on the next reconcile — no manual trigger needed.
- Rotate a secret: update the relevant 1Password item, then `kubectl annotate externalsecret <name> -n open-webui force-sync=$(date +%s)` (or wait out the `1h` refresh interval); the `reloader.stakater.com` annotations on the controller restart the pod automatically once `open-webui-secret` changes.
- Pause reconciliation: `flux suspend kustomization open-webui -n flux-system` / `flux suspend helmrelease open-webui -n open-webui`.

## TODOs / unknowns
- The open-webui Kopia backup job error during the 2026-08-16 CoreDNS incident was never confirmed as DNS-caused versus coincidental (tracked in the incident doc's own TODO list).
- Exact behavior/error surfaced to a user when the RAG pipeline tries to embed a web-search HTML result under `CONTENT_EXTRACTION_ENGINE: tika` (silently empty context vs. a visible error) was not observed/tested from this repo.

---
_Cite every non-obvious claim with a repo-root-relative file path (e.g. `kubernetes/apps/security/authentik/app/helmrelease.yaml`), not a bare filename — this doc lives under `docs/apps/`, so relative paths must resolve from there._
