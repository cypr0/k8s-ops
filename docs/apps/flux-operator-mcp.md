# flux-operator-mcp

> **Namespace**  flux-system
> **Source**     OCI Helm chart `oci://ghcr.io/controlplaneio-fluxcd/charts/flux-operator-mcp`, tag `0.57.0` (`kubernetes/apps/flux-system/flux-operator-mcp/app/ocirepository.yaml`)
> **Hostname**   none — internal-only, no HTTPRoute; reached at `flux-operator-mcp.flux-system.svc.cluster.local:9090`

## What it does here
A read-only MCP (Model Context Protocol) server that exposes Flux — and, via its RBAC binding, general Kubernetes — state as tools an LLM agent can call. Every mutating MCP tool (reconcile/suspend/resume/…) is explicitly disabled (`values.readonly: true`, `kubernetes/apps/flux-system/flux-operator-mcp/app/helmrelease.yaml:15`); the comment on that line states the intent directly: an LLM client "should only ever be able to inspect Flux state, never change it." This is a deliberate security design choice, not an oversight — see Known quirks for how it's belt-and-suspenders enforced.

It was built as one of several MCP backends for OpenClaw, the cluster's original LLM chat gateway (`git log --oneline -- kubernetes/apps/flux-system/flux-operator-mcp/` → `306a42c feat(openclaw): read-only MCP integrations + Telegram/OpenRouter`). OpenClaw was decommissioned a week later and replaced by `hermes-agent` (commit `445ed29`, 2026-07-19), and nothing currently consumes this app — see Known quirks.

## Architecture at a glance
- **Depends on:** the Kustomization declares `dependsOn: flux-instance` (`kubernetes/apps/flux-system/flux-operator-mcp/ks.yaml:7-8`) — it needs the actual FluxInstance/controllers running (`kubernetes/apps/flux-system/flux-instance/`) to have any Flux state to expose. Egress is restricted to `kube-dns` (namespace `kube-system`) and the `kube-apiserver` entity (`kubernetes/apps/flux-system/flux-operator-mcp/app/ciliumnetworkpolicy.yaml:22-34`).
- **RBAC:** the chart's auto-created ServiceAccount `flux-operator-mcp` is bound to the built-in read-only `view` ClusterRole (`kubernetes/apps/flux-system/flux-operator-mcp/app/rbac.yaml`), instead of the chart's own default of granting its ServiceAccount cluster-admin (`rbac.create: false` in `helmrelease.yaml:16-19`, comment confirms the chart default being overridden).
- **Depended on by:** nothing at present (see Known quirks) — no other app in the repo references its Service DNS name (`grep -rn "flux-operator-mcp.flux-system\|flux-operator-mcp:9090" kubernetes/` returns no hits outside its own directory).

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/flux-system/flux-operator-mcp/ks.yaml` | Flux Kustomization; `dependsOn: flux-instance`, `targetNamespace: flux-system` |
| `kubernetes/apps/flux-system/flux-operator-mcp/app/ocirepository.yaml` | Chart source: `oci://ghcr.io/controlplaneio-fluxcd/charts/flux-operator-mcp`, tag `0.57.0` |
| `kubernetes/apps/flux-system/flux-operator-mcp/app/helmrelease.yaml` | `transport: http`, `readonly: true`, `rbac.create: false`, `networkPolicy.create: false` |
| `kubernetes/apps/flux-system/flux-operator-mcp/app/rbac.yaml` | Binds the chart's ServiceAccount to the built-in `view` ClusterRole (overrides chart's default cluster-admin) |
| `kubernetes/apps/flux-system/flux-operator-mcp/app/ciliumnetworkpolicy.yaml` | Ingress/egress policy — see Routing & access |

No ExternalSecret exists in this app directory — the app has no credentials of its own.

## Secrets
None. `ls kubernetes/apps/flux-system/flux-operator-mcp/app/` contains no `externalsecret*.yaml` — this app authenticates to the Kubernetes API purely via its ServiceAccount token / RBAC binding, not a stored credential.

## Routing & access
- No HTTPRoute — internal-only, `http` transport on port 9090 (`ciliumnetworkpolicy.yaml:19`).
- CiliumNetworkPolicy ingress allows only pods labeled `app.kubernetes.io/name: openclaw` in namespace `openclaw`, port 9090/TCP (`kubernetes/apps/flux-system/flux-operator-mcp/app/ciliumnetworkpolicy.yaml:13-21`). That namespace and workload no longer exist in the cluster (decommissioned in commit `445ed29`) — see Known quirks.
- Egress: `kube-dns` in `kube-system` (DNS) and the `kube-apiserver` entity only (`ciliumnetworkpolicy.yaml:22-34`) — consistent with a tool that only needs to talk to the API server, nothing else.
- No OIDC/Authentik integration — this is a machine-to-machine MCP endpoint, not a human-facing app.

## Storage
None. Stateless MCP server, no PVC in the app directory.

## Known quirks
- **Deliberate design, not an oversight:** the combination of `readonly: true`, `rbac.create: false` + a hand-written `view`-only ClusterRoleBinding, and a scoped CiliumNetworkPolicy is explicitly there to make sure an LLM-consumable interface to the cluster can only ever *inspect* Flux/Kubernetes state — never reconcile, suspend, resume, or otherwise mutate anything. The comments in `kubernetes/apps/flux-system/flux-operator-mcp/app/helmrelease.yaml:12-19` and `kubernetes/apps/flux-system/flux-operator-mcp/app/rbac.yaml:1-4` state this intent directly, and it mirrors the same "why" comment repeated verbatim in `kubernetes/apps/hermes-agent/hermes-agent/app/rbac.yaml:20-25` for the successor app.
- **Currently an orphan.** This app was added for OpenClaw (`git log`, commit `306a42c`, 2026-07-12). OpenClaw was decommissioned one week later (commit `445ed29`, 2026-07-19: "chore(openclaw): decommission OpenClaw" — "Replaced by hermes-agent"), and its replacement did not take over the MCP client relationship: `hermes-agent`'s `mcp_servers` block (`kubernetes/apps/hermes-agent/hermes-agent/app/configmap.yaml:268-274`) wires up `paperless-mcp`, `nextcloud-mcp`, and `sogo-mcp` as remote HTTP MCP servers, but omits `flux-operator-mcp` entirely. Instead, `hermes-agent` gets its own direct `view` + node-view ClusterRoleBindings (`kubernetes/apps/hermes-agent/hermes-agent/app/rbac.yaml`) and runs `kubectl` in-pod for the equivalent "what's cluster status" capability. The net effect: the CiliumNetworkPolicy here still only admits traffic from the (now-nonexistent) `openclaw` namespace/label, so as things stand **nothing can reach this Service** — it reconciles cleanly but is unreachable. Worth a decision: rewire the CNP for `hermes-agent` (or another consumer), or decommission this app the same way OpenClaw was.

## Common operations
- Upgrade chart version: bump `tag` in `kubernetes/apps/flux-system/flux-operator-mcp/app/ocirepository.yaml`, commit, push, Flux reconciles within the OCIRepository's 15m interval or the HelmRelease's 1h interval (or force with `flux reconcile helmrelease flux-operator-mcp -n flux-system`).
- Pause reconciliation: `flux suspend kustomization flux-operator-mcp -n flux-system` / `flux suspend helmrelease flux-operator-mcp -n flux-system`.
- Re-enable a consumer: add an ingress rule to `ciliumnetworkpolicy.yaml` for the new client's namespace/label, matching the existing `fromEndpoints` block shape.

## TODOs / unknowns
- Decide and act on the orphan status above: rewire for `hermes-agent` consumption, wire up a new consumer, or decommission like OpenClaw. Not fixed in this doc per the campaign's "no manifest fixes on this branch" rule (§2.7 / §6) — surfacing it here and in the closing summary only.
- The chart's own `values.yaml` defaults (beyond what's overridden in `helmrelease.yaml`) were not fetched/read for this doc — anything not explicitly listed under Repo layout above is unverified chart-default behavior, not a repo fact.

---
_Cite every non-obvious claim with a repo-root-relative file path (e.g. `kubernetes/apps/security/authentik/app/helmrelease.yaml`), not a bare filename — this doc lives under `docs/apps/`, so relative paths must resolve from there._
