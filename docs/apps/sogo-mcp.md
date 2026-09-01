# sogo-mcp

> **Namespace**  hermes-agent
> **Source**     plain manifests, hand-rolled — generic `python:3.14-slim` image + ConfigMap-mounted script + init-container `pip install` (`kubernetes/apps/hermes-agent/sogo-mcp/app/deployment.yaml`), same pattern as sibling apps `paperless-mcp` and `nextcloud-mcp` in this namespace
> **Hostname**   none — ClusterIP only, ingress restricted to `hermes-agent` pods (`kubernetes/apps/hermes-agent/sogo-mcp/app/ciliumnetworkpolicy.yaml`)

## What it does here
A standalone MCP (Model Context Protocol) server that gives the `hermes-agent` app tool access to the owner's real, personal SOGo groupware account — **hosted externally by Netcup, not in this cluster** (`kubernetes/apps/hermes-agent/sogo-mcp/app/sogo_full.py:9-21`). It exposes two capability groups: read/write CalDAV calendar access (list calendars, list events in a range, create one-shot events) and read-only IMAP mailbox access (list folders, search, read a message). Built specifically for hermes-agent's document-pipeline / contract-monitoring use cases — e.g. creating a calendar reminder for an invoice due date, or letting the agent search the owner's mailbox on request (`sogo_full.py:15-21`).

## Architecture at a glance
- **Depends on:** an external SOGo instance (CalDAV over HTTPS + IMAP), reached over the internet, not `*.cluster.local` — see Routing & access. No in-cluster database, cache, or object storage dependency; the deployment is stateless.
- **Depended on by:** `hermes-agent` only, as an MCP client — registered under the `sogo` key in `kubernetes/apps/hermes-agent/hermes-agent/app/configmap.yaml`'s `mcp_servers` section, pointed at `http://sogo-mcp.hermes-agent.svc.cluster.local:8000/mcp` (`configmap.yaml:273-274`).

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/hermes-agent/sogo-mcp/app/deployment.yaml` | Init container `pip install`s the `mcp`/`httpx`/`pydantic`/`icalendar` SDKs into an `emptyDir`; main container runs a generic bridge script against the SOGo-specific tool module |
| `kubernetes/apps/hermes-agent/sogo-mcp/app/owui_tool_mcp_bridge.py` | Generic bridge: loads an Open-WebUI-"Tools"-shaped `.py` file and republishes every public method as an MCP tool over streamable-HTTP (`/mcp`) — byte-identical copy also lives in `paperless-mcp`/`nextcloud-mcp` (kustomize's `configMapGenerator` can't reference files outside its own directory, so a shared copy isn't possible; see `owui_tool_mcp_bridge.py:13-17`) |
| `kubernetes/apps/hermes-agent/sogo-mcp/app/sogo_full.py` | The actual SOGo `Tools` class — CalDAV + IMAP methods, config via `Valves` (env-var-backed) |
| `kubernetes/apps/hermes-agent/sogo-mcp/app/externalsecret.yaml` | Pulls SOGo account credentials from 1Password |
| `kubernetes/apps/hermes-agent/sogo-mcp/app/service.yaml` | ClusterIP, port 8000 |
| `kubernetes/apps/hermes-agent/sogo-mcp/app/ciliumnetworkpolicy.yaml` | Ingress from `hermes-agent` pods only; egress to DNS + the external SOGo host |
| `kubernetes/apps/hermes-agent/sogo-mcp/app/kustomization.yaml` | Content-hash-suffixed `configMapGenerator` for the two `.py` files — any script change renames the ConfigMap, which changes the Deployment's volume ref and triggers a real rollout automatically |

## Secrets
| Key (in `sogo-mcp-secret`) | 1Password source | Consumed by |
| --- | --- | --- |
| `SOGO_BASE_URL` | `sogo` item | CalDAV base URL, consumed by `sogo_full.py`'s `Valves.SOGO_BASE_URL` |
| `SOGO_USERNAME` / `SOGO_PASSWORD` | `sogo` item | Both CalDAV (HTTP Basic Auth) and IMAP (`LOGIN`) — the **same real personal account credentials** used for both protocols, no narrower-scoped alternative exists on Netcup's SOGo hosting (`kubernetes/apps/hermes-agent/sogo-mcp/app/externalsecret.yaml:3-6`, `sogo_full.py:22-29`) |
| `SOGO_IMAP_HOST` / `SOGO_IMAP_PORT` | `sogo` item | IMAP connection target for the read-only mailbox tools |

All five keys come from the single `sogo` 1Password item via `dataFrom.extract` (`externalsecret.yaml:27-29`); none are defaulted or hardcoded — the module docstring notes the CalDAV base path is a fixed `/SOGo/dav` per Netcup's own documentation, but the account subdomain itself is only ever supplied via this secret (`sogo_full.py:41-46`).

## Routing & access
- **ClusterIP only** (`service.yaml`), no HTTPRoute — this is an internal tool backend for exactly one caller.
- **Ingress:** `ciliumnetworkpolicy.yaml:12-21` allows port 8000 only from pods labeled `app.kubernetes.io/name: hermes-agent` in the `hermes-agent` namespace.
- **Egress:** DNS to `kube-dns` (`ciliumnetworkpolicy.yaml:23-33`), plus `toEntities: world` on ports 443/143/993 (`ciliumnetworkpolicy.yaml:43-52`) — CalDAV (HTTPS) and IMAP (143/STARTTLS on this account, 993/implicit-TLS opened defensively in case that's ever switched) both go to the external Netcup host. The policy comment explains why this is a broad `world` rule rather than a pinned `toFQDNs` (the pattern `paperless-ngx`'s own external-IMAP rule uses): a `toFQDNs` rule needs the hostname available as a Flux `$${VAR}` substitution from the cluster's SOPS secrets, but this hostname only exists in 1Password, not statically in-repo (`ciliumnetworkpolicy.yaml:34-42`). The same `world` egress also covers the init container's `pip install`.
- No SSO — no user-facing surface to gate; the only "user" is hermes-agent's MCP client.

## Storage
None. Fully stateless — config arrives via env vars each pod start, `pylibs`/`tmp`/`app-src` are all `emptyDir`/`configMap` volumes (`deployment.yaml:122-129`), nothing to back up. `pylibs`/`tmp` are explicitly excluded from Velero's fs-backup (`backup.velero.io/backup-volumes-excludes: pylibs,tmp`, `deployment.yaml`) since `hermes-agent` is in Velero's GFS schedules — same pattern as `paperless-mcp`/`nextcloud-mcp`.

## Known quirks
- **This wields the owner's full real mailbox and calendar account, not a service account** — Netcup's SOGo hosting has no read-only or calendar-only credential option, a deliberate scope tradeoff documented in the module docstring (`sogo_full.py:22-29`).
- **CalDAV is intentionally narrow: list/create events only, no update/delete/recurrence-rule handling.** The docstring reasons that a `delete_event` tool against someone's real personal calendar isn't worth the risk for what this integration is for — fixing a wrongly-created reminder by hand is a two-second job in any calendar client (`sogo_full.py:31-36`).
- **IMAP is read-only by design** (list/search/read only, no send/delete/flag-mutate) — this exists so the agent can look something up on request, not act as a mail client (`sogo_full.py:36-39`).
- **Recurring-event query results can be misleading.** Confirmed live against this SOGo server: a YEARLY-recurring event can be returned for a date range that doesn't actually contain one of its occurrences (the server appears to filter loosely on recurring components rather than expanding the RRULE), and the returned start/end are always the *original* occurrence's dates, not the one in range. `list_events` doesn't parse/expand RRULE at all — treat a recurring hit as "this series might be relevant," not proof of an in-range occurrence (`sogo_full.py:204-212`).
- **IMAP port/TLS mode is account-specific and was wrong at first.** This Netcup account uses plaintext-then-`STARTTLS` on port 143, not implicit TLS on 993 — `IMAP4_SSL` against port 143 just hangs at the handshake. Fixed across three commits: `25d917f` (open egress 143), `1b2e547` (use STARTTLS not implicit TLS), still branches on `SOGO_IMAP_PORT == 993` vs STARTTLS at runtime (`sogo_full.py:356-367`).
- **IMAP `LIST` response parsing has to handle mixed quoted/bare mailbox names in the same response** (e.g. `"Sent Messages"` vs bare `INBOX` from the same server) — a naive quote-count split breaks on the bare-atom entries; fixed in `ff044a5`, regex at `sogo_full.py:74-77`.
- **`SOGO_BASE_URL` is tolerated with or without a trailing `/SOGo`** — Netcup's own docs show the URL *with* the suffix already; `_base_url()` strips it if present rather than assuming one specific input shape (`sogo_full.py:113-121`, fixed in `e8dd35d`).
- **`search_messages`'s `query` was made optional after the fact** (`ee47a35`) so an empty query just lists a folder's messages instead of erroring.
- **Config was silently not read from env on first ship** (`b152c70`) — an early bug where `Valves` didn't actually pick up the env vars; worth remembering if a future refactor of the `Valves`/`os.getenv` pattern reintroduces the same class of bug across all three MCP servers in this namespace.
- **No CI/registry for this image** — generic `python:3.14-slim` + init-container `pip install` at pod start is a deliberate greenfield tradeoff, same as `paperless-mcp`/`nextcloud-mcp` (`deployment.yaml:1-12`); a `pip install` failure at pod start (registry hiccup, dependency yank) blocks the whole pod from becoming ready.

## Common operations
- Change tool behavior or add a method: edit `sogo_full.py`, commit, push — the `configMapGenerator` content hash changes automatically, which changes the Deployment's volume reference and triggers a real rollout (`kustomization.yaml:11-14`), no manual restart or force annotation needed.
- Rotate the SOGo account password: update the `sogo` 1Password item; `externalsecret.yaml`'s `refreshInterval: 1h` picks it up, or force it sooner with `kubectl annotate externalsecret sogo-mcp -n hermes-agent force-sync=$(date +%s)`. Note the running pod won't pick up a changed `sogo-mcp-secret` until restarted (env vars are injected at container start, not live-reloaded) — `kubectl rollout restart deployment/sogo-mcp -n hermes-agent` after rotating.
- Bump the SDK pin: edit the `pip install` line's version constraints in `deployment.yaml:51`; the `mcp` SDK is deliberately pinned `<2` because 2.0.0 restructured its module layout and dropped `mcp.server.fastmcp.FastMCP` from where `owui_tool_mcp_bridge.py` imports it (`deployment.yaml:47-51`).
- Pause reconciliation: `flux suspend kustomization sogo-mcp -n flux-system`.

## TODOs / unknowns
- No backup/DR story documented here because there's nothing stateful to back up in-cluster — but there is also no documented backup posture for the *external* Netcup SOGo account itself (calendar/mail data) from this repo; out of scope for this doc but worth flagging.

---
_See also: `docs/apps/hermes-agent.md` for the MCP-client side of this integration._
