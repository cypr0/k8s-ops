# Fluent Bit

> **Namespace**  logging
> **Source**     `fluent` Helm repo (https://fluent.github.io/helm-charts), chart `fluent-bit` v0.57.6
> **Hostname**   none (no HTTPRoute; reachable in-cluster and via a LoadBalancer VIP for external syslog/log senders)

## What it does here
Cluster-wide log shipper running as a DaemonSet on every node (control-plane included). It has three separate jobs: (1) receives syslog from external infra (Proxmox, OPNsense/Suricata) over a dedicated LoadBalancer VIP, (2) receives Talos's own kernel/service logs (shipped as raw JSON over TCP, per Talos's CIS 6.1.1/6.2.1 requirement that audit logs go somewhere), and (3) tails container logs for four specific app namespaces from each node's `/var/log/containers/*.log`. Every stream is fanned out to both OpenSearch (for SIEM/per-app dashboards) and Loki (for Grafana), making it the single ingestion point that feeds both logging backends in this cluster. All config lives inline in the HelmRelease's `values.config` block — `kubernetes/apps/logging/fluent-bit/app/helmrelease.yaml` — there is no separate ConfigMap to look up.

## Architecture at a glance
- **Depends on:** `opensearch-cluster` Kustomization (namespace `logging`) and `external-secrets-stores` (namespace `security`) as hard Flux `dependsOn` — `kubernetes/apps/logging/fluent-bit/ks.yaml`. Also pushes to Loki at `loki.monitoring.svc.cluster.local:3100` (soft dependency), and to OpenSearch at `opensearch.logging.svc.cluster.local:9200`, the Service fronting the `opensearch` OpenSearchCluster CR.
- **Depended on by:** External syslog senders — Proxmox and OPNsense — configured to log to the cluster's syslog LoadBalancer VIP, and Talos nodes themselves for kernel/audit logging (all three documented in `kubernetes/apps/logging/fluent-bit/app/service-syslog.yaml` comments). In-cluster, `kube-prometheus-stack`'s Prometheus scrapes its metrics endpoint (port 2020), and Loki's CiliumNetworkPolicy explicitly allow-lists ingress from fluent-bit. Nothing in-cluster reads *from* fluent-bit directly — it's a pure forwarder.

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/logging/fluent-bit/app/helmrelease.yaml` | Chart version 0.57.6; all SERVICE/PARSER/INPUT/FILTER/OUTPUT config inline under `values.config` |
| `kubernetes/apps/logging/fluent-bit/app/helmrepository.yaml` | Points at the `fluent` Helm repo |
| `kubernetes/apps/logging/fluent-bit/app/externalsecret.yaml` | Pulls the OpenSearch admin password used by all OUTPUT blocks |
| `kubernetes/apps/logging/fluent-bit/app/service-syslog.yaml` | Extra `LoadBalancer` Service exposing syslog (514/UDP+TCP) and Talos log (5170/TCP) to the LAN |
| `kubernetes/apps/logging/fluent-bit/app/ciliumnetworkpolicy.yaml` | Ingress from `world`/`remote-node`/`host` for external log sources, Prometheus scrape; egress to DNS, OpenSearch, Loki |
| `kubernetes/apps/logging/fluent-bit/ks.yaml` | Flux Kustomization; `dependsOn` opensearch-cluster + external-secrets-stores |

## Secrets
| ExternalSecret | 1Password item / field | Consumed by |
| --- | --- | --- |
| `fluent-bit-opensearch` (`kubernetes/apps/logging/fluent-bit/app/externalsecret.yaml`) | Item `opensearch`, field `OPENSEARCH_ADMIN_PASSWORD` → templated into target key `OPENSEARCH_PASSWORD` | Injected cluster-wide into the fluent-bit pod via `envFrom: secretRef: fluent-bit-opensearch`; referenced as `${OPENSEARCH_PASSWORD}` in every `[OUTPUT]` block of type `opensearch` |

The `ClusterSecretStore` used is `onepassword`. This is the same `opensearch` 1Password item that backs the OpenSearch cluster's own admin/dashboards ExternalSecrets — one shared credential, not a fluent-bit-specific one.

## Routing & access
- No HTTPRoute/Gateway — fluent-bit is not web-exposed. External reachability is instead via the `syslog-ingress` `LoadBalancer` Service (`kubernetes/apps/logging/fluent-bit/app/service-syslog.yaml`), forwarding UDP/TCP 514 → pod port 5514 (syslog) and TCP 5170 (Talos JSON logs). That file's header comments record what's configured to send here: Proxmox (`System > Syslog`), OPNsense (`System > Log Files > Settings > Remote Logging`), and Talos (`machine.logging.destinations` / `install.extraKernelArgs`).
- No OIDC/SSO — this is an infrastructure DaemonSet, not a user-facing app.
- `CiliumNetworkPolicy` `fluent-bit`:
  - Ingress: `fromEntities: [world]` on 5514/TCP+UDP (syslog from LAN devices); `fromEntities: [remote-node, host]` on 5170/TCP — the comment notes Talos log traffic originates from the node's own host network stack before CNI is up, so it's classified as host/remote-node rather than world; Prometheus (namespace `monitoring`) on 2020 for metrics scrape.
  - Egress: DNS to `kube-system`/`kube-dns`; OpenSearch REST API (9200) to pods labeled `opensearch.org/opensearch-cluster: opensearch`; Loki (3100) to pods in `monitoring` labeled `app.kubernetes.io/name: loki`.

## Storage
No PVC — runs as a DaemonSet with no volumes declared in this repo beyond the chart's own defaults (hostPath log mounts implied by tailing `/var/log/containers/*`). Position-tracking DBs (e.g. `DB /var/log/flb_kube_apps.db` in the `kube.app` tail input) are node-local state, not covered by Velero — fluent-bit is stateless from a backup standpoint; nothing in `kubernetes/apps/velero/` references this namespace.

## Known quirks
- **Root required for syslog:** `podSecurityContext.runAsNonRoot: false` is set explicitly with the comment "needs root for syslog socket". This is also why the namespace is one of the few allow-listed for privileged/hostPath workloads in the cluster's baseline Kyverno policy (`kubernetes/apps/security/kyverno/policies/clusterpolicy-pod-security.yaml`, comment lists `fluent-bit` alongside Cilium, Falco, OpenSearch as verified-legitimate exceptions).
- **Custom `cri` parser was a footgun:** an earlier commit defined a custom `cri` parser that collided with fluent-bit's *built-in* one, silently making fluent-bit skip the rest of `custom_parsers.conf` — parsers defined after it (like `talos_audit_type`) never registered and the pod crashed. Fixed in `5b696a4` ("drop custom 'cri' parser — collides with built-in"); the built-in `cri` parser is now referenced directly in the `kube.app` tail input instead, with an inline `# NOTE:` in the HelmRelease warning against reintroducing it.
- **`extraPorts` needs an explicit `port`:** the chart's Service template fails Helm validation ("port: Invalid value: 0") if `extraPorts` entries omit `port` — fixed in `fa511c2`, now documented inline in the HelmRelease just above the `extraPorts` block.
- **RFC5424 timestamp handling took several iterations:** commits `0deb81b`, `1e4f427`, `2055501` show repeated fixes for Proxmox/OPNsense syslog parsing before landing on the current `syslog-rfc5424-nofrac` / `syslog-rfc5424-simple` pair in `custom_parsers.conf`.
- **App-log OpenSearch shipping is intentionally scoped, not cluster-wide:** the `kube.app` tail input only globs four namespaces (`nextcloud`, `paperless`, `open-webui`, `hermes-agent`) rather than all containers, per the inline comment in `helmrelease.yaml` — "so we don't ingest every container in the cluster." Adding a new app's logs to OpenSearch means editing this glob, not just deploying the app. Commit `5b8b47f` shows this list needed updating when the `openclaw` namespace was renamed to `hermes-agent`.
- **Namespace/pod metadata comes from the log filename, not the Kubernetes API filter** — deliberately, to avoid needing RBAC (`k8s_file_meta` parser, regex on `/var/log/containers/<pod>_<ns>_<container>-<id>.log`). If Kubelet's container log filename format ever changes, this parser (and thus all `kube.app`-derived fields) breaks silently.
- **OpenSearch TLS verification is disabled** (`tls On`, `tls.verify Off`) on every `opensearch` OUTPUT block — acceptable for in-cluster ClusterIP traffic but worth knowing if the OpenSearch endpoint or its certs ever change.

## Common operations
- Upgrade chart version: edit `kubernetes/apps/logging/fluent-bit/app/helmrelease.yaml` (`spec.chart.spec.version`), commit, push; Flux reconciles within the 1h `interval` or force with `flux reconcile helmrelease fluent-bit -n logging`.
- Change parsing/routing behavior: edit the relevant `values.config.*` block (`service`/`customParsers`/`inputs`/`filters`/`outputs`) in the same HelmRelease — there's no separate ConfigMap.
- Rotate the OpenSearch password: update the `opensearch` 1Password item, then `kubectl annotate externalsecret fluent-bit-opensearch -n logging force-sync=$(date +%s)` (or wait for the refresh interval) — note this password is shared with the OpenSearch cluster's own ExternalSecrets, so check other consumers before rotating.
- Pause reconciliation: `flux suspend kustomization fluent-bit -n logging` / `flux suspend helmrelease fluent-bit -n logging`.
- Check ingestion health: HTTP metrics/status on port 2020 (`HTTP_Server On`), scraped by Prometheus per the ServiceMonitor (`serviceMonitor.enabled: true`).

## TODOs / unknowns
- Exact chart-default volume mounts (hostPath paths for `/var/log`, `/var/lib/docker/containers`, position DB storage) were not verified against the fluent-bit chart's own `values.yaml` at v0.57.6 — no vendored/rendered copy of the chart exists in this repo to confirm.
- Whether Suricata Eve-JSON actually arrives via OPNsense's syslog forwarding (as the `rewrite_tag` filter matching `$ident ^suricata$` assumes) or via some other path was not independently confirmed beyond the fluent-bit config and the OpenSearch-side ingest pipeline — plausible from both files but not traced end-to-end.
- No PrometheusRule/alerting specific to fluent-bit was found in `kubernetes/apps/monitoring/kube-prometheus-stack/` — if there's alerting on ingestion failures or dropped records, it wasn't located.

---
_Cite every non-obvious claim with a repo-root-relative file path (e.g. `kubernetes/apps/security/authentik/app/helmrelease.yaml`), not a bare filename — this doc lives under `docs/apps/`, so relative paths must resolve from there._
