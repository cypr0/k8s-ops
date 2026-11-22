# portfolio-mcp

> **Namespace**  hermes-agent  
> **Source**     plain manifests  
> **Hostname**   none (internal service only)

## What it does here
Exposes the depot's portfolio database (holdings, WATCH candidates) as an MCP server for the `hermes-agent` LLM profile. Serves read-only portfolio queries and write operations limited to adding/retiring WATCH candidates. Consumed exclusively by `hermes-agent` over HTTP on port 8000.

## Architecture at a glance
- **Depends on:** CNPG cluster `postgres` (namespace `database`), database `portfolio` with role `portfoliousr`; ExternalSecret → 1Password item `portfolio`; DNS (kube-dns).
- **Depended on by:** `hermes-agent` (same namespace) — the only permitted caller per CiliumNetworkPolicy.

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/hermes-agent/portfolio-mcp/ks.yaml` | Flux Kustomization, depends on `external-secrets-stores` |
| `kubernetes/apps/hermes-agent/portfolio-mcp/app/deployment.yaml` | Deployment: python:3.15-slim, init container installs MCP/psycopg deps, runs `owui_tool_mcp_bridge.py` |
| `kubernetes/apps/hermes-agent/portfolio-mcp/app/service.yaml` | ClusterIP service on port 8000 |
| `kubernetes/apps/hermes-agent/portfolio-mcp/app/externalsecret.yaml` | Pulls `PGPASSWORD` from 1Password |
| `kubernetes/apps/hermes-agent/portfolio-mcp/app/ciliumnetworkpolicy.yaml` | Ingress from `hermes-agent` only; egress to DNS, PyPI (443), and CNPG postgres |
| `kubernetes/apps/hermes-agent/portfolio-mcp/app/kustomization.yaml` | ConfigMapGenerator for `portfolio_depot.py` and `owui_tool_mcp_bridge.py` (hash-suffixed to roll on change) |

## Secrets
| ExternalSecret | 1Password item → field | Consumed by |
| --- | --- | --- |
| `portfolio-mcp` | `portfolio` → `PORTFOLIO_POSTGRESQL_PASSWORD` | Deployment env var `PGPASSWORD` (container `mcp-server`) |

The same credential is shared with `hermes-agent` (its `hermes-agent-secrets` ExternalSecret pulls the same field) and matches the CNPG-managed role `portfoliousr` in the `postgres` cluster.

## Routing & access
- **Internal only:** ClusterIP service `portfolio-mcp.hermes-agent.svc.cluster.local:8000`, no HTTPRoute or external exposure.
- **Network policy:** `kubernetes/apps/hermes-agent/portfolio-mcp/app/ciliumnetworkpolicy.yaml` restricts ingress to pods labeled `app.kubernetes.io/name: hermes-agent` in namespace `hermes-agent`; egress to kube-dns (UDP/TCP 53), world:443 (PyPI for init container), and `cnpg.io/cluster: postgres` in namespace `database` on port 5432.
- **SSO:** not applicable (no web UI, internal service).

## Storage
No PVCs. Two emptyDir volumes (`pylibs`, `tmp`) for ephemeral pip-installed dependencies and scratch space; both excluded from Velero backup via pod annotation `backup.velero.io/backup-volumes-excludes: pylibs,tmp`.

## Known quirks
- **Init container pattern:** Dependencies (`mcp>=1.6,<2`, `psycopg[binary]>=3.2,<4`, `pydantic`) are installed at pod start into `/pylibs` emptyDir and added to `PYTHONPATH`. This avoids baking a custom image but means every pod restart re-downloads from PyPI (egress to world:443 required).
- **ConfigMap hash-suffix roll:** The tool module (`portfolio_depot.py`) and bridge (`owui_tool_mcp_bridge.py`) are mounted from a ConfigMap generated with `disableNameSuffixHash: false` in `kubernetes/apps/hermes-agent/portfolio-mcp/app/kustomization.yaml`. Any change to these files triggers a new ConfigMap name, which rolls the Deployment automatically.
- **Shared CNPG role:** The `portfoliousr` role and `portfolio` database are managed by the CNPG `postgres` cluster in namespace `database`. This app does not create the database or role — it assumes they exist (likely provisioned by a CNPG `Database` and `Role` resource or manual setup; not visible in the provided manifests).
- **Strategy: Recreate:** `kubernetes/apps/hermes-agent/portfolio-mcp/app/deployment.yaml` uses `strategy.type: Recreate` (not RollingUpdate), so updates cause brief downtime.

## Common operations
- **Update tool module:** Edit `portfolio_depot.py` or `owui_tool_mcp_bridge.py` in `kubernetes/apps/hermes-agent/portfolio-mcp/app/`, commit, push. Flux reconciles within 1h (or force with `flux reconcile kustomization portfolio-mcp -n flux-system`). The ConfigMap hash changes, triggering a pod rollout.
- **Rotate database password:** Update the `portfolio` 1Password item's `PORTFOLIO_POSTGRESQL_PASSWORD` field, then force ExternalSecret refresh: `kubectl annotate externalsecret portfolio-mcp -n hermes-agent force-sync=$(date +%s)`. Restart the pod to pick up the new secret: `kubectl rollout restart deployment portfolio-mcp -n hermes-agent`.
- **Pause reconciliation:** `flux suspend kustomization portfolio-mcp -n flux-system`.
- **Check logs:** `kubectl logs -n hermes-agent deployment/portfolio-mcp -c mcp-server` (or `-c install-deps` for init container).

## TODOs / unknowns
- **Database/role provisioning:** The manifests reference CNPG cluster `postgres`, database `portfolio`, and role `portfoliousr`, but no `Database` or `Role` CRD is present in the provided files. Verify whether these are managed by CNPG resources elsewhere in the repo or were manually created.
- **Backup coverage:** No explicit Velero schedule reference found. Confirm whether the `portfolio` database is covered by a CNPG backup schedule or a separate Velero/Kopia job.
- **Tool module source:** `portfolio_depot.py` and `owui_tool_mcp_bridge.py` are referenced in `kustomization.yaml` but not provided in the manifests. Their behavior (what queries/writes are exposed) cannot be documented without reading them.

---
**Secret/IP scan:** clean. The ExternalSecret resource and 1Password item/field name are cited; no resolved password values, real public IPs, or account-identifying paths are present.

_All file paths are relative to repo root (e.g. `kubernetes/apps/hermes-agent/portfolio-mcp/app/deployment.yaml`)._
