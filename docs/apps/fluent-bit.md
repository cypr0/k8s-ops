# Fluent Bit

> **Namespace**  monitoring
> **Source**     `fluent` Helm repo (https://fluent.github.io/helm-charts), chart `fluent-bit` v0.57.6
> **Hostname**   none (no HTTPRoute; reachable in-cluster and via a LoadBalancer VIP for Talos node logs)

## What it does here
A DaemonSet on every node (control-plane included) with exactly one job: receive Talos's own kernel and service logs and forward them to Loki. Talos ships these as raw JSON-lines over plain TCP — not syslog framing — which is the whole reason Fluent Bit is still in this cluster. Alloy already collects every pod log, but its `loki.source.syslog` expects RFC5424 and Talos offers no syslog format at all, so nothing else here can accept that stream. Talos requires the logs to go *somewhere* (CIS Talos 6.1.1/6.2.1); this is the somewhere.

It used to do considerably more — syslog ingestion from Proxmox and OPNsense, a container-log tail for four app namespaces, and a fan-out to both OpenSearch and Loki. All of that is gone (see **History** below). All config lives inline in the HelmRelease's `values.config` block — `kubernetes/apps/monitoring/fluent-bit/app/helmrelease.yaml` — there is no separate ConfigMap to look up.

## Architecture at a glance
- **Depends on:** nothing, deliberately. `kubernetes/apps/monitoring/fluent-bit/ks.yaml` carries no `dependsOn`: the only sink is Loki in this same namespace, and Fluent Bit buffers and retries if Loki is not up yet.
- **Depended on by:** the Talos nodes themselves, via `machine.logging.destinations` pointing at `tcp://192.168.10.110:5170` (`talos/patches/global/machine-logging.yaml`, and per-node in `talos/talconfig.yaml`). In-cluster, `kube-prometheus-stack`'s Prometheus scrapes its metrics endpoint (port 2020), and Loki's CiliumNetworkPolicy allow-lists ingress from it. Nothing reads *from* Fluent Bit — it is a pure forwarder.

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/monitoring/fluent-bit/app/helmrelease.yaml` | Chart version 0.57.6; the one INPUT, two FILTERs and one OUTPUT inline under `values.config` |
| `kubernetes/apps/monitoring/fluent-bit/app/helmrepository.yaml` | Points at the `fluent` Helm repo |
| `kubernetes/apps/monitoring/fluent-bit/app/service-syslog.yaml` | `LoadBalancer` Service on VIP 192.168.10.110, port 5170/TCP only |
| `kubernetes/apps/monitoring/fluent-bit/app/ciliumnetworkpolicy.yaml` | Ingress from `remote-node`/`host` on 5170 and Prometheus on 2020; egress to DNS and Loki |
| `kubernetes/apps/monitoring/fluent-bit/ks.yaml` | Flux Kustomization, no `dependsOn` |

## Secrets
None. The `fluent-bit-opensearch` ExternalSecret that once injected `${OPENSEARCH_PASSWORD}` into the OUTPUT blocks was removed along with those blocks.

## Routing & access
- No HTTPRoute/Gateway — Fluent Bit is not web-exposed. External reachability is via the `syslog-ingress` `LoadBalancer` Service (`kubernetes/apps/monitoring/fluent-bit/app/service-syslog.yaml`) on VIP **192.168.10.110:5170/TCP**.
  - The Service keeps its name and VIP even though only one port is left, because every node's machine config points at that address; changing it means a config patch plus a rolling reboot of the whole cluster.
  - Ports 514/UDP+TCP are gone. Proxmox and OPNsense (including its Suricata IDS) now report to the Wazuh manager on 192.168.10.111 instead, where events are decoded into alerts rather than stored as raw lines — see `docs/apps/wazuh.md`.
- No OIDC/SSO — this is an infrastructure DaemonSet, not a user-facing app.
- `CiliumNetworkPolicy` `fluent-bit`:
  - Ingress: `fromEntities: [remote-node, host]` on 5170/TCP — Talos log traffic originates from the node's own host network stack before CNI is up, so Cilium classifies it as host/remote-node rather than world; Prometheus (namespace `monitoring`) on 2020 for the metrics scrape.
  - Egress: DNS to `kube-system`/`kube-dns`; Loki (3100) to pods in `monitoring` labeled `app.kubernetes.io/name: loki`.

## Storage
No PVC. Runs as a DaemonSet with only the chart's default hostPath log mounts; with the container-log tail removed, nothing reads them any more. Stateless from a backup standpoint — nothing in `kubernetes/apps/velero/` references it.

## Known quirks
- **Talos is by far the loudest log source in the cluster** — roughly 175 records/second sustained, measured via `fluentbit_input_records_total`. That is more than every pod in the cluster combined. Worth revisiting what Talos is actually emitting before adding retention anywhere downstream.
- **`runAsNonRoot: false` is probably no longer needed.** It was set for the syslog listener, which is gone; port 5170 is unprivileged and there is no file tailing left. It is deliberately left as-is with a comment saying so — tightening it is a separate change that needs its own verification.
- **`extraPorts` needs an explicit `port`:** the chart's Service template fails Helm validation ("port: Invalid value: 0") if `extraPorts` entries omit `port` — fixed in `fa511c2`, documented inline just above the `extraPorts` block.
- **No explicit `chart.spec.sourceRef.namespace`, on purpose.** Spelling it out silently stops Renovate from detecting the chart for updates — the same trap that hid version bumps for authentik, falco and kubescape (see the note in the HelmRelease).
- **Custom `cri` parser was a footgun** (historical, no longer applicable): an earlier commit defined a custom `cri` parser that collided with Fluent Bit's built-in one, silently making it skip the rest of `custom_parsers.conf` so that later parsers like `talos_audit_type` never registered and the pod crashed. Fixed in `5b696a4`. Only `talos_audit_type` remains defined today, so the collision surface is gone — but reintroducing a container-log tail would bring it back.

## History
Fluent Bit was reduced to its current shape in the observability slim-down of 2026-09-20:

| Stream | Was | Now |
| --- | --- | --- |
| syslog :5514 from Proxmox | → OpenSearch + Loki | Wazuh agent on the host |
| syslog :5514 from OPNsense + Suricata | → OpenSearch + Loki | `os-wazuh-agent` plugin on the firewall |
| `kube.app` tail (4 namespaces) | → OpenSearch | Alloy, cluster-wide, no glob to maintain |
| `talos.log` :5170 | → OpenSearch + Loki | → Loki only |

That retired four custom parsers, eight filters, six of seven outputs, the OpenSearch credential, and eventually the `logging` namespace itself.

## TODOs / unknowns
- Exact chart-default volume mounts (hostPath paths for `/var/log`, `/var/lib/docker/containers`) were not verified against the chart's own `values.yaml` at v0.57.6 — no vendored copy exists in this repo.
- No PrometheusRule/alerting specific to Fluent Bit exists in `kubernetes/apps/monitoring/kube-prometheus-stack/`. Given it is now the sole path for Talos node logs, an alert on `fluentbit_output_retries_failed_total` would be reasonable and is not currently present.
- Whether Alloy could take over the Talos TCP listener is untested — it would remove the last reason to run Fluent Bit at all.

---
_Cite every non-obvious claim with a repo-root-relative file path (e.g. `kubernetes/apps/security/authentik/app/helmrelease.yaml`), not a bare filename — this doc lives under `docs/apps/`, so relative paths must resolve from there._
