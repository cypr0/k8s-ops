# Loki

> **Namespace**  monitoring
> **Source**     Grafana Helm chart repo `https://grafana.github.io/helm-charts` (`kubernetes/apps/monitoring/loki/app/helmrepository.yaml`), chart `loki` v7.0.0 (`kubernetes/apps/monitoring/loki/app/helmrelease.yaml`)
> **Hostname**   none — internal only, no HTTPRoute; reached at `loki.monitoring.svc.cluster.local:3100`

## What it does here
Cluster-wide log aggregator behind Grafana's Explore view and several custom dashboards — not a namespace-scoped or secondary store. It ingests from three independent shippers: Alloy (all pod logs cluster-wide, unfiltered by namespace, plus Kubernetes events), Fluent Bit (syslog from Proxmox/OPNsense, Suricata Eve-JSON, and Talos kernel/service/audit logs), and Falco Sidekick (security alerts at warning+ priority). It runs `deploymentMode: SingleBinary` (`kubernetes/apps/monitoring/loki/app/helmrelease.yaml:27`) — no distributed read/write/ingester tier — with local filesystem storage on one NFS-backed PVC, sized for a homelab rather than a distributed production deployment.

This deliberately overlaps with OpenSearch (namespace `logging`) for specific streams rather than being redundant by accident: Fluent Bit ships syslog, Suricata, and Talos logs to *both* Loki and OpenSearch, and the same for four named apps' container logs (nextcloud, paperless, open-webui, hermes-agent) to OpenSearch — the fluent-bit HelmRelease's own comment states the reasoning: "App logs already go to Loki via Alloy; this adds them to OpenSearch for the per-app OpenSearch dashboards". OpenSearch is the SIEM/per-app-dashboard store; Loki is the general Grafana-facing log store for everything, including the other ~40 namespaces that OpenSearch's Fluent Bit config doesn't touch at all.

## Architecture at a glance
- **Depends on:** Flux Kustomization `kube-prometheus-stack` (namespace `monitoring`) via `dependsOn` (`kubernetes/apps/monitoring/loki/ks.yaml:12-14`); `zfs-nfs` StorageClass for its PVC.
- **Depended on by:** Grafana — Loki datasource plus the `loki-logs`, `talos-logs`, and `apps-overview` dashboards; Alloy, as its only log-write target; Fluent Bit, for syslog/Suricata/Talos streams; Falco Sidekick, for security alerts — the `falco` Flux Kustomization itself `dependsOn` `loki` for this reason; Gatus, for a `/ready` health check.

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/monitoring/loki/ks.yaml` | Flux Kustomization — `dependsOn kube-prometheus-stack`, targets `monitoring` namespace |
| `kubernetes/apps/monitoring/loki/app/helmrepository.yaml` | Grafana chart repo source |
| `kubernetes/apps/monitoring/loki/app/helmrelease.yaml` | Chart v7.0.0, SingleBinary-mode values |
| `kubernetes/apps/monitoring/loki/app/ciliumnetworkpolicy.yaml` | Per-pod ingress/egress rules |
| `kubernetes/apps/monitoring/loki/app/kustomization.yaml` | Resource list |

## Secrets
None. There is no `externalsecret*.yaml` in `kubernetes/apps/monitoring/loki/app/` — `loki.auth_enabled: false` (`kubernetes/apps/monitoring/loki/app/helmrelease.yaml:30`) means no multi-tenant auth and nothing to credential.

## Routing & access
- No HTTPRoute — internal only, reached at `loki.monitoring.svc.cluster.local:3100`. `gateway.enabled: false`, i.e. no chart-provided reverse proxy in front of the single-binary pod either.
- No SSO — no user-facing surface of its own; queried through Grafana, which has its own Authentik OIDC integration.
- CiliumNetworkPolicy allows ingress to port 3100 only from: Gatus (health check), Alloy, Fluent Bit (namespace `logging`), Grafana, Prometheus (scraping), the loki-canary component, and the kubelet (`fromEntities: host`, for readiness/liveness probes). Memberlist gossip (7946 TCP/UDP) is allowed between Loki pods, which is vestigial at `replication_factor: 1` but harmless.
- Egress is scoped to DNS, memberlist, the canary component, `kube-apiserver` (needed by the `loki-sc-rules` sidecar — see Known quirks), and `toEntities: world` on 443.

## Storage
- One 20Gi PVC on the `zfs-nfs` StorageClass (`singleBinary.persistence`); `singleBinary.replicas: 1`.
- `loki.storage.type: filesystem` — chunks and the TSDB index live on that one PVC. `replication_factor: 1` plus a single replica means no redundancy at the Loki layer itself.
- Retention: `limits_config.retention_period: 30d`, compactor `delete_request_store: filesystem` — old chunks age out on-disk rather than being pruned via any external store.
- Not covered by Velero: all three backup schedules restrict `includedNamespaces` to `nextcloud, paperless, open-webui, hermes-agent` — `monitoring` is absent. A lost PVC means lost log history, bounded only by the 30d retention window already in place.

## Known quirks
- **`world:443` egress may be stale.** Commit `8ab3792` added `toEntities: world` egress specifically so "Loki can write/read chunks from S3 object store," and the CNP still labels it that way, but the current HelmRelease has `loki.storage.type: filesystem` with no S3 backend configured. Not verified whether the chart needs outbound HTTPS for something else (e.g. a version check) or whether this rule is simply left over from an earlier config — flagged, not resolved, here.
- **`kube-apiserver` egress must not be port-scoped.** Cilium's kube-proxy replacement DNATs ClusterIP:443 to the real apiserver port (6443) before policy enforcement, so a `toPorts`-scoped rule on 443 never matches. Fixed by dropping `toPorts` entirely on the `kube-apiserver` egress entity (commit `3cb5645`). Without this, the `loki-sc-rules` k8s-sidecar (watches ConfigMaps/Secrets for rule reloads) crash-loops — 1800+ restarts observed before the fix (commit `d4b2793`).
- **Memcached caches deliberately disabled.** `chunksCache`/`resultsCache` default to 8Gi memory requests upstream, which no node here can satisfy; both set `enabled: false` (commit `fa5b329`). Query performance is traded off against homelab memory limits.
- **All microservice-mode components are explicitly zeroed** (`backend`, `read`, `write`, `ingester`, `querier`, `queryFrontend`, `queryScheduler`, `distributor`, `compactor`, `indexGateway`, `bloomCompactor`, `bloomGateway`) because `deploymentMode: SingleBinary` requires it, not because they were disabled by choice — don't be surprised these show `replicas: 0`.
- **`monitoring.lokiCanary.enabled: false`** even though the CiliumNetworkPolicy still carries canary-labeled ingress/egress rules — currently unused; not confirmed whether that's intentional headroom for re-enabling the canary later or leftover from when it was on.

## Common operations
- Upgrade chart version: edit `spec.chart.spec.version` in `kubernetes/apps/monitoring/loki/app/helmrelease.yaml`, commit, push; Flux reconciles within the 1h `interval`, or force with `flux reconcile helmrelease loki -n monitoring`.
- Pause reconciliation: `flux suspend kustomization loki -n monitoring` / `flux suspend helmrelease loki -n monitoring`.
- Check ingestion health: Gatus polls `GET /ready` every 1m; the built-in `lokiCanary` synthetic check is disabled (see Known quirks), so Gatus's readiness probe is the only automated health signal today.

## TODOs / unknowns
- Whether the `world:443` egress rule (added for "S3 object store") is still required now that storage is `filesystem`-backed — not verified either way.
- Why `lokiCanary` is disabled while its CNP rules remain — not confirmed whether that's deliberate or leftover.
- No dedicated incident postmortem references Loki directly; the fluent-bit OOM incident (commit `d4b2793`) mentions a "loki CNP gap" fixed in the same commit, but has no standalone `docs/incidents/*.md` entry.
