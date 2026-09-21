# paperless-mcp

> **Namespace**  hermes-agent
> **Source**     plain manifests, hand-rolled (no HelmRelease/OCIRepository) — stock `python:3.14-slim` image, no custom build (`kubernetes/apps/hermes-agent/paperless-mcp/app/deployment.yaml`)
> **Hostname**   none — ClusterIP only, no external exposure

## What it does here
A standalone MCP (Model Context Protocol) server that gives `hermes-agent` full tool access to this cluster's Paperless-ngx REST API over streamable-HTTP. It's a generic bridge (`owui_tool_mcp_bridge.py`) wrapped around the exact same `Tools` class Open WebUI already runs as a chat tool (`paperless_full.py`, a byte-for-byte mirror of `kubernetes/apps/open-webui/open-webui/app/tools/paperless_full.py`) — every public method on that class is auto-registered as an MCP tool by introspection, no code changes needed to go from "Open WebUI Tool" to "standalone MCP server" (`kubernetes/apps/hermes-agent/paperless-mcp/app/owui_tool_mcp_bridge.py`). Its own deployment manifest states it was "built for hermes-agent's remote HTTP MCP client support" (`kubernetes/apps/hermes-agent/paperless-mcp/app/deployment.yaml`), and the CiliumNetworkPolicy's ingress rule only allows the `hermes-agent` pod in — confirming hermes-agent is this server's sole consumer today (`kubernetes/apps/hermes-agent/paperless-mcp/app/ciliumnetworkpolicy.yaml`). Concretely, this is the tool interface behind hermes-agent's Paperless document-processing workflow: Paperless-ngx's Workflow "Webhook" action (trigger: Document Added) calls hermes-agent's webhook front-end, and hermes-agent then calls back into this server's tools to read the new document and assign tags/correspondent/document type/title/custom fields — see `docs/apps/hermes-agent.md` for the workflow itself; this doc only covers the tool-serving side.

## Architecture at a glance
- **Depends on:** Paperless-ngx REST API (namespace `paperless`, in-cluster Service DNS `paperless.paperless.svc.cluster.local`, port 80 — `kubernetes/apps/hermes-agent/paperless-mcp/app/paperless_full.py`'s `Valves.PAPERLESS_BASE_URL` default); ExternalSecret → 1Password item `paperless` (`kubernetes/apps/hermes-agent/paperless-mcp/app/externalsecret.yaml`); PyPI, reached on every pod start to `pip install` its own runtime dependencies into an ephemeral volume — no persistent storage, no prebuilt image.
- **Depended on by:** `hermes-agent`, as a remote HTTP MCP client — its `mcp_servers.paperless` entry points at `http://paperless-mcp.hermes-agent.svc.cluster.local:8000/mcp` (`kubernetes/apps/hermes-agent/hermes-agent/app/configmap.yaml`).

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/hermes-agent/paperless-mcp/app/deployment.yaml` | Init container pip-installs the `mcp`/`httpx`/`pydantic` deps to a shared `emptyDir`; main container runs the bridge script against the mounted Tool source |
| `kubernetes/apps/hermes-agent/paperless-mcp/app/paperless_full.py` | The Paperless-ngx `Tools` class (21 named methods + `bulk_edit`/`raw_request` escape hatches + `capabilities()`) — mirrored from the Open WebUI Tool of the same name |
| `kubernetes/apps/hermes-agent/paperless-mcp/app/owui_tool_mcp_bridge.py` | Generic OWUI-Tool-to-MCP bridge; also duplicated identically in `nextcloud-mcp/app/` |
| `kubernetes/apps/hermes-agent/paperless-mcp/app/externalsecret.yaml` | Paperless API token, pulled from the same 1Password item other Paperless-consuming apps already use |
| `kubernetes/apps/hermes-agent/paperless-mcp/app/service.yaml` | ClusterIP, port 8000 |
| `kubernetes/apps/hermes-agent/paperless-mcp/app/ciliumnetworkpolicy.yaml` | Ingress restricted to `hermes-agent`; egress to DNS, Paperless-ngx, and PyPI only |
| `kubernetes/apps/hermes-agent/paperless-mcp/app/kustomization.yaml` | Ships `paperless_full.py`/`owui_tool_mcp_bridge.py` via `configMapGenerator` (content-hash suffixed) |
| `kubernetes/apps/hermes-agent/paperless-mcp/ks.yaml` | Flux Kustomization — `dependsOn` `external-secrets-stores` (security) and `paperless` (paperless) |

## Secrets
| Key (in `paperless-mcp-secret`) | 1Password source | Consumed by |
| --- | --- | --- |
| `PAPERLESS_API_TOKEN` | `paperless` item — the same item and field `kubernetes/apps/paperless/paperless-ngx/app/externalsecret.yaml` and `kubernetes/apps/open-webui/open-webui/app/externalsecret-paperless-token.yaml` already read | `mcp-server` container, via `envFrom` (`kubernetes/apps/hermes-agent/paperless-mcp/app/deployment.yaml`); used as an `Authorization: Token <..>` header (`paperless_full.py`'s `_auth_header`) |

No new Paperless credential is provisioned for this server — it's the same shared token the `paperless-cronjob-fix-ownership` CronJob and Open WebUI's `paperless_full` Tool already use. That token's underlying Paperless account is **not** staff: methods like `get_system_status()`/`list_users()` exist on this server but currently 403 (confirmed live per the module docstring).

## Routing & access
- ClusterIP only, no HTTPRoute present — port 8000, MCP served over streamable-HTTP at path `/mcp`.
- CiliumNetworkPolicy ingress allows only pods labeled `app.kubernetes.io/name: hermes-agent` in the `hermes-agent` namespace to reach port 8000 (`kubernetes/apps/hermes-agent/paperless-mcp/app/ciliumnetworkpolicy.yaml`).
- Egress is limited to: kube-dns (53/UDP+TCP), the `paperless` namespace's `paperless` Service on port 80, and `world` on 443 (PyPI, for the `install-deps` init container's `pip install` on every pod start — same "ephemeral deps re-fetched on restart" tradeoff as hermes-agent's own `tools-install` init container).
- No SSO — no user-facing UI; the only client is hermes-agent's MCP-client machinery.

## Storage
None. No PVC. `/pylibs` (pip-installed deps) and `/tmp` are both `emptyDir`, and the Tool source (`paperless_full.py`, `owui_tool_mcp_bridge.py`) is mounted read-only from a `configMapGenerator`-produced ConfigMap. Because that ConfigMap gets a content-hash suffix, any edit to either source file changes the ConfigMap name, which changes the Deployment's volume reference and triggers kustomize's standard "config change forces rollout" — no manual restart needed, unlike hermes-agent's own plain (non-hashed) bootstrap ConfigMap. Both `emptyDir`s are excluded from Velero's fs-backup (`backup.velero.io/backup-volumes-excludes: pylibs,tmp`, `deployment.yaml`) since `hermes-agent` is in Velero's GFS schedules and neither volume holds anything worth restoring.

## Known quirks
- **No prebuilt image, no CI/registry for this app.** It's a stock `python:3.14-slim` base plus a ConfigMap-mounted script plus an init container that `pip install`s dependencies fresh into a shared `emptyDir` on every pod start — the same pattern hermes-agent's own `tools-install` init container and the Open WebUI tool-sync Job already use elsewhere in this repo.
- **The deployment's own header comment says `python:3.13-slim`, but the actual `image:` fields (both the init container and the main container) are `python:3.14-slim`** — a stale comment, not the running version; don't trust the comment over the manifest if bumping the base image.
- **The `mcp` SDK is pinned `<2`.** The upstream SDK's 2.0.0 release (2026-08) restructured its module layout and dropped `mcp.server.fastmcp.FastMCP` from where `owui_tool_mcp_bridge.py` imports it — confirmed live as a `ModuleNotFoundError` on 2.0.0, working on 1.9.4. Don't lift the pin without updating the bridge script for whatever 2.x's replacement API looks like.
- **`owui_tool_mcp_bridge.py` is duplicated byte-for-byte in both `paperless-mcp/app/` and `nextcloud-mcp/app/`** because kustomize's `configMapGenerator` refuses file paths that escape its own kustomization directory — a single shared copy isn't possible; keep both in sync when editing.
- **`paperless_full.py` is likewise a mirror of the Open WebUI Tool of the same name** (`kubernetes/apps/open-webui/open-webui/app/tools/paperless_full.py`) — same file, edited in two places, kept in sync manually.
- **The tool was cut from 139 methods to 21 in v2.0.0, without losing reach.** Every method's signature and docstring is compiled into a JSON schema and injected into the consuming model's context, so 139 + 85 methods across this server and `nextcloud-mcp` meant hermes-agent facing **232** MCP tools — enough that its Paperless webhook prompt needed a paragraph warning the model about near-identical names. The v2 shape keeps the high-traffic operations typed (with their v1 names and parameters unchanged, because that prompt calls them by name), collapses the near-duplicates into one dispatcher, pushes the long tail behind the `raw_*` escape hatches v1 already had, and adds `capabilities(topic)` as an on-demand reference for driving them. Measured with Open WebUI's own `get_tool_specs()`: ~11,900 → ~2,600 tokens. Full reasoning in `docs/apps/open-webui.md`.
- **The long tail is reachable, not gone.** If something used to have a named method and no longer does, it is behind ``bulk_edit()` or `raw_request()`` — call `capabilities()` first for the paths and body shapes.
- **`/api/profile/` echoes the configured token's own `auth_token` field back verbatim.** v1 had a `get_profile()` method with a warning in its docstring; v2 reaches it only through `raw_request("GET", "/api/profile/")`, and `capabilities("danger")` carries the warning instead — along with the reminder never to call `/api/profile/generate_auth_token/`, which would rotate the very token this server authenticates with (and which `paperless-cronjob-fix-ownership` also uses), locking out both.
- **Deliberately unimplemented:** `profile`/`generate_auth_token` (would rotate the very token this server authenticates with, and that the `paperless-cronjob-fix-ownership` CronJob also depends on), TOTP/2FA setup, social-account-provider management, and raw document bytes beyond `MAX_INLINE_DOWNLOAD_BYTES` (3MB default) — oversized downloads return an error instead of flooding the caller with base64.

## Common operations
- Change the exposed Paperless tool surface: edit `kubernetes/apps/hermes-agent/paperless-mcp/app/paperless_full.py` (and its Open WebUI mirror, to keep them in sync), commit, push — `configMapGenerator`'s content-hash suffix forces an automatic rollout, no manual restart needed.
- Rotate the Paperless token: update the `PAPERLESS_API_TOKEN` field on the `paperless` 1Password item, then `kubectl annotate externalsecret paperless-mcp-token -n hermes-agent force-sync=$(date +%s)` (or wait out the 1h `refreshInterval`), then roll the pod — `envFrom` env vars aren't hot-reloaded on secret change.
- Pause reconciliation: `flux suspend kustomization paperless-mcp -n flux-system`.

## TODOs / unknowns
- The exact Paperless Workflow trigger condition (tag-scoped vs. plain "Document Added") lives in Paperless's own database, not this repo — the detailed tag/correspondent/document-type auto-assignment logic that runs afterward is hermes-agent's own system prompt, not something this MCP server's files define — see `docs/apps/hermes-agent.md` for that side.
- No `docs/incidents/` entry currently references `paperless-mcp` by name.
- Whether the Paperless account behind `PAPERLESS_API_TOKEN` should be widened to staff (to unlock `get_system_status`/`list_users`/etc., which currently 403) was not decided or verified from this repo.

---
_See also: `docs/apps/hermes-agent.md` for the webhook/agent side of the document-processing workflow this server provides tools for._
