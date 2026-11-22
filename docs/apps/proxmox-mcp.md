# proxmox-mcp

> **Namespace**  hermes-agent
> **Source**     plain manifests, same pattern as `immich-mcp`: generic `python:3.15-slim` image, init-container `pip install`, tool module from a ConfigMap served by `owui_tool_mcp_bridge.py` (`kubernetes/apps/hermes-agent/proxmox-mcp/app/`)
> **Hostname**   none; ClusterIP only, ingress restricted to `hermes-agent` pods (`ciliumnetworkpolicy.yaml`)

## What it does here
A read-only MCP server that gives hermes-agent's `infrastruktur` profile access to the Proxmox VE API of the host the cluster runs on. Tools: `overview` (node, guests), `storage` (storages, ZFS, SMART), `problems` (failed tasks, pending updates, guests without backup), `raw_get` and `capabilities`.

The API token `hermes@pve!readonly` uses Privilege Separation and holds only the `PVEAuditor` role on `/`, which can read everything and change nothing. The module also only sends `GET`. Changes to the host belong in the proxmox-ansible playbook (`kubernetes/apps/automation/proxmox-ansible/`).

## Architecture at a glance
- **Depends on:** The Proxmox VE API at `PROXMOX_URL` (port 8006, TLS verified against the host's ACME certificate). The host sits on the LAN, which is `world` for Cilium.
- **Depended on by:** hermes-agent's `infrastruktur` profile only (`kubernetes/apps/hermes-agent/hermes-agent/app/profiles/infrastruktur/config.yaml`, key `proxmox`, `http://proxmox-mcp.hermes-agent.svc.cluster.local:8000/mcp`). The default profile and the other specialists don't list it.

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/hermes-agent/proxmox-mcp/app/proxmox_infra.py` | The `Tools` class. Config comes from env-var-backed `Valves` |
| `kubernetes/apps/hermes-agent/proxmox-mcp/app/owui_tool_mcp_bridge.py` | Generic bridge, byte-identical to the other `*-mcp` apps |
| `kubernetes/apps/hermes-agent/proxmox-mcp/app/deployment.yaml` | Deployment (hardened, read-only root FS, non-root) |
| `kubernetes/apps/hermes-agent/proxmox-mcp/app/externalsecret.yaml` | Credentials from 1Password |
| `kubernetes/apps/hermes-agent/proxmox-mcp/app/ciliumnetworkpolicy.yaml` | Ingress from `hermes-agent` only; egress to DNS, world:443 (pip) and the backend |
| `kubernetes/apps/hermes-agent/proxmox-mcp/ks.yaml` | Flux Kustomization |

## Secrets
| Key (in `proxmox-mcp-secret`) | 1Password source |
| --- | --- |
| `PROXMOX_URL` | `hermes-proxmox` item |
| `PROXMOX_API_TOKEN_ID` | `hermes-proxmox` item (`hermes@pve!readonly`) |
| `PROXMOX_API_TOKEN_SECRET` | `hermes-proxmox` item |
