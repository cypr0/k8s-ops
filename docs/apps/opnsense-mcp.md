# opnsense-mcp

> **Namespace**  hermes-agent
> **Source**     plain manifests, same pattern as `immich-mcp`: generic `python:3.15-slim` image, init-container `pip install`, tool module from a ConfigMap served by `owui_tool_mcp_bridge.py` (`kubernetes/apps/hermes-agent/opnsense-mcp/app/`)
> **Hostname**   none; ClusterIP only, ingress restricted to `hermes-agent` pods (`ciliumnetworkpolicy.yaml`)

## What it does here
A read-only MCP server that gives hermes-agent's `netzwerk` profile access to the OPNsense API. Tools: `status`, `interfaces_and_gateways`, `firewall_rules`, `dhcp_leases` (ISC or Kea), `vpn_status` (WireGuard), `ids_alerts` (Suricata), `raw_get` and `capabilities`.

OPNsense has **no read-only API role**: an API key carries its user's page privileges, and the same pages accept the POST calls that change things. So the guarantee is in the code. Only `GET` is ever sent, and any path whose segment is an action verb (`set*`, `add*`, `del*`, `toggle*`, `apply`, `reconfigure`, `start`/`stop`/`restart`, `update`/`upgrade`, …) is refused before the request goes out. Give the `hermes` user only the pages it should read anyway.

## Architecture at a glance
- **Depends on:** The OPNsense web GUI/API at `OPNSENSE_URL` on the LAN (`world` for Cilium, 443).
- **Depended on by:** hermes-agent's `netzwerk` profile only (`kubernetes/apps/hermes-agent/hermes-agent/app/profiles/netzwerk/config.yaml`, key `opnsense`, `http://opnsense-mcp.hermes-agent.svc.cluster.local:8000/mcp`). The default profile and the other specialists don't list it.

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/hermes-agent/opnsense-mcp/app/opnsense_net.py` | The `Tools` class. Config comes from env-var-backed `Valves` |
| `kubernetes/apps/hermes-agent/opnsense-mcp/app/owui_tool_mcp_bridge.py` | Generic bridge, byte-identical to the other `*-mcp` apps |
| `kubernetes/apps/hermes-agent/opnsense-mcp/app/deployment.yaml` | Deployment (hardened, read-only root FS, non-root) |
| `kubernetes/apps/hermes-agent/opnsense-mcp/app/externalsecret.yaml` | Credentials from 1Password |
| `kubernetes/apps/hermes-agent/opnsense-mcp/app/ciliumnetworkpolicy.yaml` | Ingress from `hermes-agent` only; egress to DNS, world:443 (pip) and the backend |
| `kubernetes/apps/hermes-agent/opnsense-mcp/ks.yaml` | Flux Kustomization |

## Secrets
| Key (in `opnsense-mcp-secret`) | 1Password source |
| --- | --- |
| `OPNSENSE_URL` | `hermes-opnsense` item |
| `OPNSENSE_API_KEY` | `hermes-opnsense` item |
| `OPNSENSE_API_SECRET` | `hermes-opnsense` item |
