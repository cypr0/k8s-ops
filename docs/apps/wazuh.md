# Wazuh

> **Namespace**  wazuh
> **Source**     plain manifests, derived from [wazuh/wazuh-kubernetes](https://github.com/wazuh/wazuh-kubernetes) tag `v4.14.7`
> **Hostname**   `wazuh.${SECRET_DOMAIN}` (internal only), agents connect to `192.168.10.111:1514/1515`

## What it does here

Host-based SIEM/XDR for machines *outside* the cluster: the MacBook, the
Proxmox host the Talos cluster runs on, and any VMs added later. It gives those
hosts file integrity monitoring, rootcheck, software inventory and vulnerability
detection, and correlates the resulting alerts — none of which the syslog feed
into OpenSearch provides.

It deliberately does not watch the Kubernetes nodes. Talos has no package
manager and no writable root filesystem, so a Wazuh agent cannot run there;
that layer is covered by Falco, Trivy and Kyverno instead.

## Architecture at a glance

Three workloads, all single-replica:

| Workload | Image | Role |
| --- | --- | --- |
| `wazuh-manager-master` (StatefulSet) | `wazuh/wazuh-manager:4.14.7` | Agent enrollment + event reception, rule engine, Wazuh API |
| `wazuh-indexer` (StatefulSet) | `wazuh/wazuh-indexer:4.14.7` | OpenSearch 2.19 fork; stores alerts and vulnerability state |
| `wazuh-dashboard` (Deployment) | `wazuh/wazuh-dashboard:4.14.7` | OpenSearch Dashboards fork with the Wazuh plugin |

- **Depends on:** cert-manager (internal PKI), external-secrets → 1Password item
  `wazuh`, Authentik (OIDC login), Envoy Gateway (`envoy-internal`), Cilium LB
  IPAM (the agent VIP), `zfs-nfs` StorageClass.
- **Depended on by:** nothing. Wazuh being down costs visibility, not service.

### Why a separate indexer, given the OpenSearch cluster in `logging`

Two independent reasons:

1. Wazuh's [OpenSearch integration](https://documentation.wazuh.com/current/integrations-guide/opensearch/index.html)
   is a *forwarder* (Logstash reading from the Wazuh indexer and writing
   elsewhere). It does not replace the Wazuh indexer — that stays mandatory.
2. Wazuh 4.14 is built against OpenSearch 2.19; the cluster in `logging` runs
   3.8.0 (`kubernetes/apps/logging/opensearch-cluster/app/cluster.yaml`).

Pointing Wazuh at the existing cluster would also mean editing that cluster's
working `securityconfig` — historically the most fragile part of this setup.

## Repo layout

| File | Purpose |
| --- | --- |
| `kubernetes/apps/security/wazuh/ks.yaml` | Two Flux Kustomizations: `wazuh` (app) and `wazuh-config` (post-install setup, `force: true`) |
| `kubernetes/apps/security/wazuh/app/certificate.yaml` | Self-signed CA + node/admin/filebeat/dashboard leaf certs |
| `kubernetes/apps/security/wazuh/app/configmap-indexer.yaml` | `opensearch.yml` — TLS paths, DN matching, discovery |
| `kubernetes/apps/security/wazuh/app/configmap-manager.yaml` | `ossec.conf` — remoted, authd, indexer connection, FIM |
| `kubernetes/apps/security/wazuh/app/configmap-dashboard.yaml` | `opensearch_dashboards.yml` — OIDC, plain-HTTP backend |
| `kubernetes/apps/security/wazuh/app/externalsecret*.yaml` | Credentials, security plugin config, OIDC client |
| `kubernetes/apps/security/wazuh/app/service-agents.yaml` | The LoadBalancer agents connect to |
| `kubernetes/apps/security/wazuh/app/httproute.yaml` | Dashboard on `envoy-internal` |
| `kubernetes/apps/security/wazuh/app/ciliumnetworkpolicy.yaml` | Four policies (manager, indexer, dashboard, setup jobs) |
| `kubernetes/apps/security/wazuh/config/job-setup.yaml` | Pushes security config, creates the retention policy |
| `kubernetes/apps/security/authentik/app/blueprints/09-wazuh-oidc.yaml` | Authentik provider, application, groups |

## Secrets

All from the single 1Password item **`wazuh`**, via `ClusterSecretStore/onepassword`.

| ExternalSecret | Fields pulled | Consumed by |
| --- | --- | --- |
| `wazuh-api-cred` | `WAZUH_API_PASSWORD` | manager (creates the `wazuh-wui` user), dashboard (`API_PASSWORD`) |
| `wazuh-authd-pass` | `WAZUH_AUTHD_PASS` | manager, mounted at `/wazuh-config-mount/etc/authd.pass`; this is the agent enrollment password |
| `wazuh-cluster-key` | `WAZUH_CLUSTER_KEY` | manager env `WAZUH_CLUSTER_KEY` (unused while clustering is off) |
| `indexer-cred` | `WAZUH_INDEXER_PASSWORD` | manager + dashboard env, and the setup Job |
| `dashboard-cred` | `WAZUH_DASHBOARD_PASSWORD` | dashboard env (`kibanaserver` account) |
| `wazuh-securityconfig` | `WAZUH_INDEXER_HASH`, `WAZUH_DASHBOARD_HASH` | indexer, as `internal_users.yml` / `config.yml` / `roles_mapping.yml` |
| `wazuh-dashboard-oidc` | `WAZUH_OIDC_CLIENT_ID`, `WAZUH_OIDC_CLIENT_SECRET`, `WAZUH_COOKIE_SECRET` | dashboard, via `envFrom` |
| `authentik-wazuh-oidc` (security ns) | `WAZUH_OIDC_CLIENT_ID`, `WAZUH_OIDC_CLIENT_SECRET` | Authentik, so blueprint `!Env` lookups resolve |

**The two `*_HASH` fields are not optional.** Unlike the operator-managed
cluster in `logging`, a bare `wazuh-indexer` does not hash passwords — it wants
finished bcrypt in `internal_users.yml`. Generate with:

```sh
htpasswd -bnBC 12 "" '<password>' | tr -d ':\n'
```

and store the hash next to the plaintext in the same 1Password item.

## Routing & access

- **Dashboard:** `wazuh.${SECRET_DOMAIN}` → `envoy-internal` → `dashboard:5601`.
  LAN clients resolve it through k8s-gateway's split DNS; from outside, over the
  VPN. Never exposed through Cloudflare Tunnel.
- **SSO:** Authentik OIDC, terminated by the OpenSearch security plugin *on the
  indexer*, not by a proxy in front. Blueprint:
  `kubernetes/apps/security/authentik/app/blueprints/09-wazuh-oidc.yaml`.
  Membership in the `Wazuh Admins` group maps to the indexer's `all_access`
  role; `Wazuh Users` maps to read-only. Groups are created by the blueprint but
  members have to be added by hand in Authentik.
- **Agents:** `192.168.10.111`, its own Cilium VIP, ports 1514 (events) and 1515
  (enrollment). LAN and VPN only — no port forward on OPNsense.
- **Wazuh API (55000):** cluster-internal only; the dashboard is the only client.

### Certificates — two separate layers

Worth being explicit about, because it looks like duplication and is not:

- Browsers get the **Let's Encrypt wildcard**, terminated by Envoy. The
  dashboard pod itself runs with `SERVER_SSL_ENABLED=false`, since Envoy's
  HTTPRoute model expects a plain-HTTP backend.
- The **cert-manager CA in this namespace** exists only for the OpenSearch
  security plugin, which authenticates the transport layer and `securityadmin`
  with client certificates whose Subject DN it matches against
  `plugins.security.nodes_dn` / `admin_dn`. A public CA cannot issue those.
  Nothing from this CA is ever presented to a browser or an agent.
- **Agents** do not verify the manager's certificate at all
  (`ssl_verify_host: no`); enrollment is authenticated by the shared password.

## Storage

| PVC | Size | Contents |
| --- | --- | --- |
| `wazuh-indexer-wazuh-indexer-0` | 20 Gi | Alert indices, vulnerability state |
| `wazuh-manager-master-wazuh-manager-master-0` | 10 Gi | `/var/ossec/{etc,logs,queue,…}`, filebeat state |

Both on `zfs-nfs`. **Not covered by Velero**, matching the treatment of
`logging` (`kubernetes/apps/velero/schedules/schedule-daily.yaml`): alert data is
reproducible and configuration lives in Git. The one genuinely
non-reproducible file is `/var/ossec/etc/client.keys` — losing the manager PVC
means re-enrolling every agent.

Retention is an ISM policy (`wazuh-retention`, created by the setup Job):
`wazuh-alerts-*`, `wazuh-archives-*` and `wazuh-states-*` are deleted after 90
days and pinned to zero replicas.

## Known quirks

- **`replica_count` comes from ISM, not an index template.** Composable index
  templates do not merge — the single highest-priority match wins outright, so a
  template carrying only `number_of_replicas` would shadow Wazuh's own templates
  and take their mappings with it. See the comment in
  `kubernetes/apps/security/wazuh/config/configmap-setup.yaml`.
- **`securityadmin.sh` talks to port 9200, not 9300.** The transport client was
  removed in OpenSearch 2.0; plenty of older documentation still says 9300.
- **`allow_default_init_securityindex` only seeds the FIRST boot.** After that
  the `.opendistro_security` index ignores the mounted files, which is why
  `config/job-setup.yaml` exists. Bump its `setup-version` annotation to re-run
  it; the `wazuh-config` Kustomization sets `force: true` because a Job's pod
  template is immutable.
- **`"admin"` must stay in `all_access.backend_roles`.** Replacing it with only
  OIDC groups locks the internal admin account out of the REST API entirely —
  the same mistake once made on the logging cluster.
- **`RUN_AS=false` is load-bearing, not a default.** The dashboard image
  defaults it to `true`, which makes the Wazuh app call the API as the
  logged-in user and map them onto a Wazuh RBAC role via an
  authorization-context rule. With no such rule the user logs in fine, holds
  `all_access` on the indexer, and still has no administrable anything — the
  two permission systems are independent. `false` makes everyone act as
  `wazuh-wui` (`administrator`), which is the deliberate trade-off here: OIDC
  already gates who may log in at all, and this cluster has one admin.
- **A new Authentik OIDC provider needs an explicit `grant_types`.** The
  providers that predate the field were backfilled with the full list by a
  migration; one newly created from a blueprint gets an empty list and permits
  nothing. Authentik then answers every authorize request with
  `invalid_request` / "The request is otherwise malformed", the dashboard
  retries, and the browser shows `ERR_TOO_MANY_REDIRECTS`. The reason appears
  only in Authentik's own log, as "Invalid grant_type for provider".
- **Three separate hops need an Envoy network-policy entry, not one.** The
  dashboard reaching Authentik, the *indexer* reaching Authentik, and Envoy
  reaching the dashboard are three different rules, and Envoy's ingress
  allow-list names each client individually. They fail in ways that do not look
  like networking at all:

  | Missing rule | Symptom |
  | --- | --- |
  | dashboard → Envoy :10443 | container exits on the securityDashboards 30s setup timeout |
  | indexer → Envoy :10443 | login succeeds, then every request is a bare 401 |
  | Envoy → dashboard :5601 | 503 from the gateway |

  The token is validated by the **indexer**, not the dashboard — that is why the
  indexer needs its own path to Authentik for the JWKS. Always use the
  post-DNAT container port (10443), never the Service port 443.
- **No privileged init container.** Upstream ships one that sets
  `vm.max_map_count`. Instead the value is raised to 262144 on every node via
  `talos/patches/global/machine-sysctls.yaml`, which is what lets this namespace
  keep a `baseline` Pod Security level. The kernel default of 65530 fails
  OpenSearch's bootstrap check outright — the indexer will not start.
  Careful: reading `/proc/sys/vm/max_map_count` from inside an arbitrary pod
  proves nothing about the cluster, because the opensearch-cluster operator
  raises it per-node with its own privileged init container. Check with
  `talosctl -n <ip> read /proc/sys/vm/max_map_count` across all nodes.
- **`zfs-nfs` keys directories by PVC *name*, not UID.** Deleting and recreating
  the indexer PVC under the same name inherits the old cluster state. On a
  genuine rebuild, clear `/data/nodes/0/_state/` on the NFS server first.

## Common operations

- **Enroll an agent** (Debian/Proxmox):
  ```sh
  curl -sO https://packages.wazuh.com/4.x/apt/pool/main/w/wazuh-agent/wazuh-agent_4.14.7-1_amd64.deb
  WAZUH_MANAGER='192.168.10.111' WAZUH_REGISTRATION_PASSWORD='<WAZUH_AUTHD_PASS>' \
    dpkg -i ./wazuh-agent_4.14.7-1_amd64.deb
  systemctl enable --now wazuh-agent
  ```
- **Enroll an agent** (macOS, Apple Silicon — use `intel64` instead of `arm64`
  on an Intel Mac):
  ```sh
  curl -O https://packages.wazuh.com/4.x/macos/wazuh-agent-4.14.7-1.arm64.pkg
  echo "WAZUH_MANAGER='192.168.10.111'
  WAZUH_AGENT_NAME='macbook.cisotop.de'
  WAZUH_REGISTRATION_PASSWORD='<WAZUH_AUTHD_PASS>'" > /tmp/wazuh_envs
  sudo installer -pkg wazuh-agent-4.14.7-1.arm64.pkg -target /
  sudo launchctl bootstrap system /Library/LaunchDaemons/com.wazuh.agent.plist
  ```
  Two things worth knowing before you do this:

  - The manager is reachable on the LAN and over the VPN only, so a roaming
    MacBook goes quiet while disconnected and catches up on reconnect. Expect
    `disconnected` in the agent list to be the normal state, not an alert.
  - macOS needs **Full Disk Access** for `/Library/Ossec/bin/wazuh-agentd` under
    *System Settings → Privacy & Security*, otherwise FIM and log collection
    silently return nothing for `~/Library`, `/Users` and the unified log. The
    agent does not warn about this — it simply reports an empty scan.

### Agent naming

Wazuh registers an agent under the host's **short** hostname unless told
otherwise, which is why the firewall arrived as `secsrv.cisotop.de` (its
hostname *is* the FQDN) while Proxmox arrived as bare `proxmox`. Set the name
explicitly at enrollment instead:

| Host | Mechanism |
| --- | --- |
| Proxmox | `agent-auth -A proxmox.cisotop.de`, driven by the Ansible playbook |
| macOS | `WAZUH_AGENT_NAME` in `/tmp/wazuh_envs` before `installer` |
| OPNsense | nothing to do — already enrolls as its FQDN |

Do **not** rename the Proxmox node to fix this. A PVE node rename rewrites
`/etc/pve/nodes/<name>` and every guest, storage and replication entry beneath
it — far too much blast radius for a display name. Editing `/etc/hosts` would
not help either: the agent reads the kernel hostname, not the resolver's FQDN.

Renaming produces a **new** agent ID; the old entry lingers as `disconnected`
and has to be removed by hand:

```sh
kubectl exec -n wazuh wazuh-manager-master-0 -- \
  /var/ossec/bin/manage_agents -r <old-id>
```
- **Re-run the setup Job:** bump `setup-version` in
  `kubernetes/apps/security/wazuh/config/job-setup.yaml`, commit, push.
- **Rotate a credential:** update the 1Password item, then
  `kubectl annotate externalsecret <name> -n wazuh force-sync=$(date +%s)`.
  Changing an indexer password also means regenerating its bcrypt hash and
  re-running the setup Job.
- **Pause reconciliation:** `flux suspend kustomization wazuh -n security`.
- **Check ingestion:**
  ```sh
  kubectl exec -n wazuh wazuh-indexer-0 -- \
    curl -sk -u admin:<pw> https://localhost:9200/_cat/indices/wazuh-*?v
  ```

## TODOs / unknowns

- Forwarding alerts into the OpenSearch cluster in `logging` (for a single pane
  of glass) is possible with Fluent Bit or Alloy, both already in the cluster.
  Not built.
- `client.keys` is not backed up; see Storage.
- Wazuh 5.0 is still in beta as of 2026-09. The upgrade path from 4.14 will need
  its own review — 5.x agents enroll over a single HTTPS channel on port 1517,
  which changes `service-agents.yaml`.
