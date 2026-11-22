# wazuh-mcp

> **Namespace**  hermes-agent
> **Source**     plain manifests, same pattern as `immich-mcp`: generic `python:3.15-slim` image, init-container `pip install`, tool module from a ConfigMap served by `owui_tool_mcp_bridge.py` (`kubernetes/apps/hermes-agent/wazuh-mcp/app/`)
> **Hostname**   none; ClusterIP only, ingress restricted to `hermes-agent` pods (`ciliumnetworkpolicy.yaml`)

## What it does here
A read-only MCP server that gives hermes-agent's `sicherheit` (SOC) profile access to the Wazuh SIEM (`kubernetes/apps/security/wazuh/`). Wazuh watches the hosts outside the cluster: the Proxmox host, the MacBook, and OPNsense including its Suricata IDS via syslog. Tools: `alert_statistics`, `search_alerts`, `get_alert`, `agents_overview`, `vulnerabilities`, `sca_results`, `raw_get` and `capabilities`.

Read-only twice over. The credentials are a Wazuh API user `hermes-soc` with the built-in `readonly` RBAC role and an indexer internal user `hermes-soc` mapped to the static `readall` role. On top of that, the module only sends `GET` to the manager and `POST …/_search` to the indexer, and `raw_get` is GET-only. Alert text is attacker-controlled (log lines, user agents), so nothing a prompt injection says can turn into a write.

## Architecture at a glance
- **Depends on:** Wazuh manager API `https://wazuh.wazuh.svc.cluster.local:55000` and indexer `https://indexer.wazuh.svc.cluster.local:9200`. Matching ingress rules: `kubernetes/apps/security/wazuh/app/ciliumnetworkpolicy.yaml`. TLS verification is off (`WAZUH_VERIFY_TLS=false`): the manager certificate is self-signed by Wazuh, and the indexer CA only exists as a Secret in the `wazuh` namespace. Reachability is limited by CNP on both sides.
- **Depended on by:** hermes-agent's `sicherheit` profile only (`kubernetes/apps/hermes-agent/hermes-agent/app/profiles/sicherheit/config.yaml`, key `wazuh`, `http://wazuh-mcp.hermes-agent.svc.cluster.local:8000/mcp`). The default profile and the other specialists don't list it.

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/hermes-agent/wazuh-mcp/app/wazuh_soc.py` | The `Tools` class. Config comes from env-var-backed `Valves` |
| `kubernetes/apps/hermes-agent/wazuh-mcp/app/owui_tool_mcp_bridge.py` | Generic bridge, byte-identical to the other `*-mcp` apps |
| `kubernetes/apps/hermes-agent/wazuh-mcp/app/deployment.yaml` | Deployment (hardened, read-only root FS, non-root) |
| `kubernetes/apps/hermes-agent/wazuh-mcp/app/externalsecret.yaml` | Credentials from 1Password |
| `kubernetes/apps/hermes-agent/wazuh-mcp/app/ciliumnetworkpolicy.yaml` | Ingress from `hermes-agent` only; egress to DNS, world:443 (pip) and the backend |
| `kubernetes/apps/hermes-agent/wazuh-mcp/ks.yaml` | Flux Kustomization |

## Secrets
| Key (in `wazuh-mcp-secret`) | 1Password source |
| --- | --- |
| `WAZUH_API_PASSWORD` | `wazuh` item, field `HERMES_WAZUH_API_PASSWORD` |
| `WAZUH_INDEXER_PASSWORD` | `wazuh` item, field `HERMES_INDEXER_PASSWORD` |
## One-time setup
- **Indexer user:** `hermes-soc` lives in `internal_users.yml` (`externalsecret-securityconfig.yaml`, hash from the `wazuh` item field `HERMES_INDEXER_HASH`, generated with `htpasswd -bnBC 12 "" '<pw>' | tr -d ':\n'`). It is mapped to `readall` in `roles_mapping.yml`. The setup Job (`config/job-setup.yaml`, `setup-version` annotation) pushes it with securityadmin.
- **API user:** the Wazuh server API keeps its users in the manager's own RBAC database, not in a file. Create `hermes-soc` once with the `wazuh-wui` admin account: `POST /security/users` with `{username, password}`, then `POST /security/users/{id}/roles?role_ids=<id of "readonly">`. Re-do this after a manager volume loss; a Velero restore keeps it.
- Only the two hermes-soc fields are pulled from the shared `wazuh` item (explicit `data`, not `dataFrom: extract`), so the admin and dashboard credentials never reach the `hermes-agent` namespace.
