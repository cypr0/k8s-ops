# mailu-mcp

> **Namespace**  hermes-agent
> **Source**     plain manifests, hand-rolled — generic `python:3.14-slim` image + ConfigMap-mounted script + init-container `pip install` (`kubernetes/apps/hermes-agent/mailu-mcp/app/deployment.yaml`), same pattern as sibling apps `paperless-mcp` and `nextcloud-mcp` in this namespace
> **Hostname**   none — ClusterIP only, ingress restricted to `hermes-agent` pods + Gatus (`kubernetes/apps/hermes-agent/mailu-mcp/app/ciliumnetworkpolicy.yaml`)

## What it does here
A standalone MCP (Model Context Protocol) server that gives the `hermes-agent` app tool access to the owner's ("philipp") and Ann's ("ann") real, personal mailboxes on the cluster's own in-cluster Mailu instance (`kubernetes/apps/mail/mailu/`). It exposes two capability groups, both parameterized by a `mailbox: "philipp" | "ann"` argument: read/write CalDAV calendar access (list calendars, list events in a range, create one-shot events) and read-only IMAP mailbox access (list folders, search, read a message). Replaces the former `sogo-mcp`, which pointed at the same kind of account on Netcup's now-retired SOGo hosting — see git history; this is a straight swap of backend (Mailu's bundled Radicale instead of Netcup's SOGo), same scope/capabilities/tradeoffs, now covering two mailboxes instead of one.

Built specifically for hermes-agent's document-pipeline / contract-monitoring use cases — e.g. creating a calendar reminder on the owner's calendar for an invoice due date, or letting the agent search either mailbox on request (`mailu_full.py`'s module docstring).

## Architecture at a glance
- **Depends on:** the in-cluster Mailu instance (`kubernetes/apps/mail/mailu/`) — CalDAV over HTTPS (Radicale, via `front`'s `/webdav/` path) + IMAP (Dovecot, via `front`, port 993) — reached at `mail.${SECRET_DOMAIN}`, which CoreDNS resolves straight to `mailu-front`'s ClusterIP for in-cluster clients (the same trick `paperless-ngx`'s own Mailu IMAP integration uses; needed here too because Mailu's TLS cert only covers that public hostname, not `mailu-front.mail.svc.cluster.local`). No in-cluster database, cache, or object storage dependency of its own; the deployment is stateless.
- **Depended on by:** `hermes-agent` only, as an MCP client — registered under the `mailu` key in `kubernetes/apps/hermes-agent/hermes-agent/app/configmap.yaml`'s `mcp_servers` section, pointed at `http://mailu-mcp.hermes-agent.svc.cluster.local:8000/mcp`. Also used directly by hermes-agent's Paperless-ngx "document added" webhook prompt, which creates all-day reminder events on the owner's ("philipp") calendar for detected invoice due dates / contract cancellation deadlines.

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/hermes-agent/mailu-mcp/app/deployment.yaml` | Init container `pip install`s the `mcp`/`httpx`/`pydantic`/`icalendar` SDKs into an `emptyDir`; main container runs a generic bridge script against the Mailu-specific tool module |
| `kubernetes/apps/hermes-agent/mailu-mcp/app/owui_tool_mcp_bridge.py` | Generic bridge: loads an Open-WebUI-"Tools"-shaped `.py` file and republishes every public method as an MCP tool over streamable-HTTP (`/mcp`) — byte-identical copy also lives in `paperless-mcp`/`nextcloud-mcp` (kustomize's `configMapGenerator` can't reference files outside its own directory, so a shared copy isn't possible) |
| `kubernetes/apps/hermes-agent/mailu-mcp/app/mailu_full.py` | The actual Mailu `Tools` class — CalDAV + IMAP methods, every method takes a `mailbox` argument, config via `Valves` (env-var-backed) |
| `kubernetes/apps/hermes-agent/mailu-mcp/app/externalsecret.yaml` | Pulls both mailboxes' credentials from two separate 1Password items |
| `kubernetes/apps/hermes-agent/mailu-mcp/app/service.yaml` | ClusterIP, port 8000 |
| `kubernetes/apps/hermes-agent/mailu-mcp/app/ciliumnetworkpolicy.yaml` | Ingress from `hermes-agent` pods + Gatus; egress to DNS + Mailu (`mail` namespace) + world:443 (pip install) |
| `kubernetes/apps/hermes-agent/mailu-mcp/app/kustomization.yaml` | Content-hash-suffixed `configMapGenerator` for the two `.py` files — any script change renames the ConfigMap, which changes the Deployment's volume ref and triggers a real rollout automatically |
| `kubernetes/apps/hermes-agent/mailu-mcp/ks.yaml` | Flux Kustomization — note the `postBuild.substituteFrom: cluster-secrets`, needed so `${SECRET_DOMAIN}` in `deployment.yaml`'s `MAILU_HOST` env var actually gets substituted (the former `sogo-mcp` never needed this, since none of its config came from `${SECRET_*}` vars) |

## Secrets
| Key (in `mailu-mcp-secret`) | 1Password source | Consumed by |
| --- | --- | --- |
| `PHILIPP_USERNAME` / `PHILIPP_PASSWORD` | `mailu-philipp` item (`username`/`password` fields) | The owner's real Mailu mailbox+calendar account — both CalDAV (HTTP Basic Auth) and IMAP (`LOGIN`) |
| `ANN_USERNAME` / `ANN_PASSWORD` | `mailu-anna` item (`username`/`password` fields) | Ann's real Mailu mailbox+calendar account, same auth model |

**Deliberately kept out of this public repo entirely:** unlike `${SECRET_DOMAIN}`-style substitution variables (which resolve from the cluster's own SOPS-backed secret and are non-sensitive placeholders in git), the full mailbox addresses (and passwords) have no representation in git at all — not even as a `${SECRET_*}` reference — since they're real personal email addresses with no narrower-scope alternative. They exist only in the two 1Password items above and the resulting runtime `mailu-mcp-secret`. `MAILU_HOST` and `MAILU_IMAP_PORT` are the only non-secret config, set directly as plain env vars in `deployment.yaml` (the former via `${SECRET_DOMAIN}` substitution, the latter hardcoded `"993"`).

## Routing & access
- **ClusterIP only** (`service.yaml`), no HTTPRoute — this is an internal tool backend.
- **Ingress:** port 8000 from pods labeled `app.kubernetes.io/name: hermes-agent` in the `hermes-agent` namespace, and from Gatus (`monitoring` namespace) for health checks.
- **Egress:** DNS to `kube-dns`; `mail` namespace (`app.kubernetes.io/name: mailu`) on ports 993 (IMAP) and 443 (CalDAV, `front`'s `/webdav/` path) — a same-cluster `toEndpoints` rule, not `toEntities: world`, since `mail.${SECRET_DOMAIN}` resolves in-cluster to `mailu-front`'s ClusterIP (see `mailu`'s own CiliumNetworkPolicy comment on the equivalent `paperless-ngx` rule); `toEntities: world` on 443 covers the init container's `pip install`.
- No SSO — no user-facing surface to gate; the only "user" is hermes-agent's MCP client.

## Storage
None. Fully stateless — config arrives via env vars each pod start, `pylibs`/`tmp`/`app-src` are all `emptyDir`/`configMap` volumes, nothing to back up. `pylibs`/`tmp` are explicitly excluded from Velero's fs-backup (`backup.velero.io/backup-volumes-excludes: pylibs,tmp`) since `hermes-agent` is in Velero's GFS schedules — same pattern as `paperless-mcp`/`nextcloud-mcp`/the former `sogo-mcp`.

## Known quirks
- **This wields the owner's and Ann's full real mailbox and calendar accounts, not service accounts** — Mailu, like most mail servers, has no read-only or calendar-only credential mechanism, only per-mailbox master passwords. Same tradeoff the former `sogo-mcp` made against Netcup's SOGo hosting.
- **CalDAV is intentionally narrow: list/create events only, no update/delete/recurrence-rule handling** — a `delete_event` tool against someone's real personal calendar isn't worth the risk for what this integration is for; fixing a wrongly-created reminder by hand is a two-second job in any calendar client.
- **IMAP is read-only by design** (list/search/read only, no send/delete/flag-mutate) — this exists so the agent can look something up on request, not act as a mail client.
- **Calendars are auto-detected, not named.** Unlike the former SOGo integration's fixed `"personal"` calendar id, Mailu's Radicale assigns each collection a server-generated UUID with no stable friendly name in the CalDAV path itself. `list_events`/`create_event` default to auto-detecting the mailbox's single calendar (both accounts currently have exactly one) via a resourcetype-filtered `PROPFIND`, erroring with the discovered id list if there's ever more than one — pass `calendar=` explicitly in that case.
- **Each mailbox's `/webdav/` home also lists an addressbook (VADDRESSBOOK) collection side by side with its calendar** (confirmed live via `kubectl exec` into `mailu-webdav` and reading each collection's `.Radicale.props` — both accounts have exactly one calendar + one addressbook). `list_calendars`/the auto-detect logic filter specifically on the CalDAV `{urn:ietf:params:xml:ns:caldav}calendar` resourcetype element, not just "is this a collection" (which the former SOGo integration used safely, since SOGo's calendar home had no addressbooks mixed in) — a generic collection check here would wrongly include the addressbook too. Contacts/addressbook access itself isn't implemented (out of scope for this integration).
- **Radicale's own auth (`radicale.conf`: `auth.type = http_x_remote_user`) never sees the password** — `front`'s `/webdav` location does `auth_request /internal/auth/basic` (validating Basic Auth against Mailu's actual user database) and forwards only the validated username as an `X-Remote-User` header. Confirmed live by reading `front`'s nginx conf directly (`kubectl exec` into a `mailu-front` pod) rather than assumed from Radicale's docs, since this determines where the real credential check happens.
- **`MAILU_HOST` must be the public hostname, not the internal Service name.** Mailu's TLS cert (`kubernetes/apps/mail/mailu/app/certificate.yaml`) only lists `mail.${SECRET_DOMAIN}`/`webmail.${SECRET_DOMAIN}` as SANs — connecting to `mailu-front.mail.svc.cluster.local` directly would fail TLS hostname verification. Using the public hostname works in-cluster because of the CoreDNS override (see Architecture above).
- **No CI/registry for this image** — generic `python:3.14-slim` + init-container `pip install` at pod start is a deliberate greenfield tradeoff, same as `paperless-mcp`/`nextcloud-mcp`; a `pip install` failure at pod start (registry hiccup, dependency yank) blocks the whole pod from becoming ready.

## Common operations
- Change tool behavior or add a method: edit `mailu_full.py`, commit, push — the `configMapGenerator` content hash changes automatically, which changes the Deployment's volume reference and triggers a real rollout, no manual restart or force annotation needed.
- Rotate a mailbox's password: update the `mailu-philipp` or `mailu-anna` 1Password item; `externalsecret.yaml`'s `refreshInterval: 1h` picks it up, or force it sooner with `kubectl annotate externalsecret mailu-mcp -n hermes-agent force-sync=$(date +%s)`. The running pod won't pick up a changed `mailu-mcp-secret` until restarted (env vars are injected at container start, not live-reloaded) — `kubectl rollout restart deployment/mailu-mcp -n hermes-agent` after rotating.
- Bump the SDK pin: edit the `pip install` line's version constraints in `deployment.yaml`; the `mcp` SDK is deliberately pinned `<2` because 2.0.0 restructured its module layout and dropped `mcp.server.fastmcp.FastMCP` from where `owui_tool_mcp_bridge.py` imports it.
- Pause reconciliation: `flux suspend kustomization mailu-mcp -n flux-system`.

## TODOs / unknowns
- No backup/DR story documented here because there's nothing stateful to back up in-cluster — Mailu's own Radicale data (`/data/collection-root` inside `mailu-webdav`) is the actual calendar/contact store and isn't covered by this doc; check `docs/apps/mailu.md`/the cluster's Velero schedules for that.
- Contacts (addressbook/CardDAV) access isn't implemented — only calendars. Revisit if hermes-agent ever needs contact lookups.

---
_See also: `docs/apps/hermes-agent.md` for the MCP-client side of this integration, `docs/apps/mailu.md` for the Mailu instance itself._
