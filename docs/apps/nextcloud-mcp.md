# nextcloud-mcp

> **Namespace**  hermes-agent
> **Source**     plain manifests, hand-rolled (no HelmRelease) — stock `python:3.14-slim` image, no dedicated app image built for it (`kubernetes/apps/hermes-agent/nextcloud-mcp/app/deployment.yaml`)
> **Hostname**   none — ClusterIP only, no external exposure

## What it does here
A standalone MCP (Model Context Protocol) server that gives `hermes-agent` (and any other in-cluster MCP client) full read/write tool access to this cluster's Nextcloud instance — 85 tools covering WebDAV files and the OCS APIs (sharing, provisioning, notifications, activity, capabilities), confirmed live in the pod's startup log (`Registered 85 tool(s) ... as MCP server 'nextcloud'`). It isn't a purpose-built MCP app: it's a generic bridge (`owui_tool_mcp_bridge.py`) that loads an existing Open WebUI "Tools"-class Python file (`nextcloud_full.py`) and re-exposes every public method as an MCP tool over streamable-HTTP — the same source file already serves as an Open WebUI Tool at `kubernetes/apps/open-webui/open-webui/app/tools/nextcloud_full.py`.

## Architecture at a glance
- **Depends on:** Nextcloud (`nextcloud.nextcloud.svc.cluster.local:8080`, default in `nextcloud_full.py`'s `Valves.NEXTCLOUD_BASE_URL`, not overridden in `deployment.yaml`), ExternalSecret → 1Password `nextcloud` item (admin credentials), PyPI (egress at every pod start, for the init container's `pip install`). Flux-level `dependsOn`: `external-secrets-stores` (security ns) and the `nextcloud` Kustomization (`kubernetes/apps/hermes-agent/nextcloud-mcp/ks.yaml`).
- **Depended on by:** `hermes-agent`, as a remote HTTP MCP client — `mcp_servers.nextcloud.url: http://nextcloud-mcp.hermes-agent.svc.cluster.local:8000/mcp` in `kubernetes/apps/hermes-agent/hermes-agent/app/configmap.yaml`. No other in-repo consumer found (grepped for the Service DNS name).

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/hermes-agent/nextcloud-mcp/ks.yaml` | Flux Kustomization — `dependsOn` nextcloud + external-secrets-stores, `targetNamespace: hermes-agent` |
| `kubernetes/apps/hermes-agent/nextcloud-mcp/app/kustomization.yaml` | Resource list + `configMapGenerator` for the two Python source files (hash-suffixed — see Known quirks) |
| `kubernetes/apps/hermes-agent/nextcloud-mcp/app/deployment.yaml` | Init container (`pip install` to shared emptyDir) + main container (runs the bridge script) |
| `kubernetes/apps/hermes-agent/nextcloud-mcp/app/nextcloud_full.py` | The actual Nextcloud tool implementation (mirrored from Open WebUI's copy) |
| `kubernetes/apps/hermes-agent/nextcloud-mcp/app/owui_tool_mcp_bridge.py` | Generic OWUI-Tools-class → MCP server bridge (mirrored from `paperless-mcp`'s copy) |
| `kubernetes/apps/hermes-agent/nextcloud-mcp/app/externalsecret.yaml` | Nextcloud admin credential, reused from the same 1Password item Nextcloud itself uses |
| `kubernetes/apps/hermes-agent/nextcloud-mcp/app/service.yaml` | ClusterIP, port 8000 |
| `kubernetes/apps/hermes-agent/nextcloud-mcp/app/ciliumnetworkpolicy.yaml` | Ingress from `hermes-agent` only; egress to DNS, Nextcloud, and PyPI |

## Secrets
| ExternalSecret | 1Password source | Consumed by |
| --- | --- | --- |
| `nextcloud-mcp-token` → `nextcloud-mcp-secret` | `nextcloud` item, fields `NEXTCLOUD_ADMIN_USERNAME`/`NEXTCLOUD_ADMIN_PASSWORD` (`kubernetes/apps/hermes-agent/nextcloud-mcp/app/externalsecret.yaml`) — the **same** item and real admin credential that `kubernetes/apps/nextcloud/nextcloud/app/externalsecret.yaml` and Open WebUI's `externalsecret-nextcloud-token.yaml` already read | `mcp-server` container, `envFrom.secretRef` in `deployment.yaml`, as `NEXTCLOUD_USERNAME`/`NEXTCLOUD_PASSWORD` |

## Routing & access
- No HTTPRoute — ClusterIP only (`service.yaml`), port 8000, MCP path `/mcp` (streamable-HTTP).
- **Auth model is the real Nextcloud super-admin account**, HTTP Basic — same deliberate choice documented in `nextcloud_full.py`'s module docstring (lines ~30–45): every OCS provisioning endpoint genuinely works this way, at the cost of no separate revoke path just for this tool. This tool is **not read-only**: file deletes land in Nextcloud's trashbin (recoverable), but deletes of users/groups/shares/tags/trashbin-itself are immediate and irreversible, with no confirmation step beyond whatever the calling MCP client's own tool-approval UI provides.
- `ciliumnetworkpolicy.yaml`: ingress restricted to the `hermes-agent` pod (label-scoped, same namespace still requires an explicit rule — Cilium policy here isn't namespace-scoped). Egress allows DNS (`kube-dns`), Nextcloud on container port 80 (Service is 8080→80; Cilium enforces post-DNAT, same note as Nextcloud's own CNP), and `world:443` for the init container's PyPI fetch on every pod start.
- No SSO — no user-facing UI to gate.

## Storage
None — no PVC. All volumes are ephemeral `emptyDir` (`pylibs` for the pip-installed MCP SDK, `tmp`, and `app-src` mounted from the `nextcloud-mcp-src` ConfigMap). Dependencies are re-fetched from PyPI on every pod restart — an accepted ephemeral-dependency tradeoff, the same pattern `hermes-agent`'s own `tools-install` init container uses. Since `hermes-agent` (the namespace) is in Velero's GFS backup schedules, `pylibs`/`tmp` are explicitly excluded from fs-backup via `backup.velero.io/backup-volumes-excludes: pylibs,tmp` (`deployment.yaml`) — nothing in them is worth backing up given the pod re-fetches on every restart anyway.

## Known quirks
- **Full-admin blast radius, shared across three copies of the same credential.** This app, `kubernetes/apps/nextcloud/nextcloud/`, and Open WebUI's `nextcloud_full` Tool all authenticate as the same real Nextcloud admin account from the same 1Password item — a deliberate choice (over a dedicated restricted account) documented in `nextcloud_full.py`'s docstring, reconsider if this tool's blast radius ever needs to shrink.
- **Source is mirrored, not shared, across three locations** — `nextcloud_full.py` here is byte-for-byte identical to Open WebUI's copy, and `owui_tool_mcp_bridge.py` here is byte-for-byte identical to `paperless-mcp`'s copy, because kustomize's `configMapGenerator` refuses file paths that escape its own kustomization directory. Editing one copy without the other is a real drift risk.
- **`mcp` Python SDK pinned `<2` for a real breaking-change reason**, not caution: the SDK's 2.0.0 release (2026-08) restructured its module layout and dropped `mcp.server.fastmcp.FastMCP` from where `owui_tool_mcp_bridge.py` imports it — confirmed live as a `ModuleNotFoundError` on 2.0.0, working on 1.9.4. Needs the bridge script updated before this pin can move.
- **Greenfield app, no built image** — runs stock `python:3.14-slim` (bumped from `3.13-slim` by Renovate, unrelated to any incident) with dependencies `pip install`-ed by an init container at every pod start, rather than a prebuilt/CI-published image.
- **Triggers a Kyverno CIS 5.4.1 Audit-mode policy violation** (`prefer-secrets-as-files`, `validationFailureAction: Audit` in `kubernetes/apps/security/kyverno/policies/clusterpolicy-secrets-as-files.yaml`) on every rollout, because the Nextcloud credential is consumed via `envFrom.secretRef` rather than a mounted volume — visible as a `PolicyViolation` event on `kubectl describe deployment`, non-blocking (Audit, not Enforce).
- **ConfigMap is hash-suffixed, unlike `hermes-agent`'s own bootstrap ConfigMap** — `kustomization.yaml`'s `configMapGenerator` gives `nextcloud-mcp-src` a content-hash suffix, so any change to either Python source file changes the ConfigMap name, which changes the Deployment's volume reference and forces a real rollout automatically — no manual `kubectl rollout restart` needed here, in contrast to `hermes-agent`'s plain ConfigMap.

## Common operations
- Update tool code: edit `nextcloud_full.py` or `owui_tool_mcp_bridge.py` under `app/`, commit, push — the `configMapGenerator` hash change forces an automatic rollout (see Known quirks).
- Force reconcile: `flux reconcile kustomization nextcloud-mcp -n hermes-agent`.
- Check it's actually serving tools: `kubectl logs -n hermes-agent deploy/nextcloud-mcp` — startup line logs `Registered N tool(s) from /app/nextcloud_full.py as MCP server 'nextcloud'`.
- Rotate the credential: update the `nextcloud` 1Password item, then `kubectl annotate externalsecret nextcloud-mcp-token -n hermes-agent force-sync=$(date +%s)` (or wait for the 1h `refreshInterval`).

## TODOs / unknowns
- No other MCP client for this server was found in-repo besides `hermes-agent` (grepped the Service DNS name across the repo) — could change if another agent/tool is wired up later.
- Whether the 85-tool count logged at startup still matches `nextcloud_full.py`'s current method set wasn't independently re-derived here (would require enumerating every public method) — treat the log line itself as the source of truth, not this number.
- No PVC, so no backup-coverage question applies to this app directly.

---
_See also: `docs/apps/hermes-agent.md` (the sole known consumer) and `docs/apps/nextcloud.md` (the backing service)._
