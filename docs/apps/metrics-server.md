# Metrics Server

> **Namespace**  kube-system
> **Source**     `metrics-server` chart via OCIRepository `oci://ghcr.io/home-operations/charts-mirror/metrics-server`, tag `3.13.1` (`kubernetes/apps/kube-system/metrics-server/app/ocirepository.yaml`)
> **Hostname**   none — cluster-internal only, no HTTPRoute

## What it does here
Aggregates live CPU/memory usage from every node's kubelet and exposes it via the `metrics.k8s.io` API. Nothing in this cluster stores or graphs this data long-term (that's Prometheus/Grafana's job) — it exists purely to feed two consumers: `kubectl top nodes/pods` for ad-hoc debugging, and the Kubernetes HPA controller, which polls `metrics.k8s.io` to compute CPU-utilization percentages. Every resource-based HPA in the cluster is a hard dependency on this Deployment being up (`kubernetes/apps/kube-system/metrics-server/app/ciliumnetworkpolicy.yaml:2` — comment in the policy itself states this role).

## Architecture at a glance
- **Depends on:** the kubelet API on every node, port `10250` (`kubernetes/apps/kube-system/metrics-server/app/ciliumnetworkpolicy.yaml:43-48`, egress `toEntities: cluster`); `kube-dns` for name resolution (same file, lines 30-39); the `kube-apiserver` for `metrics.k8s.io` APIService registration (egress `toEntities: kube-apiserver`, line 40-41).
- **Depended on by:** every CPU-based HPA in the cluster — `coredns` (`kubernetes/apps/kube-system/coredns/app/helmrelease.yaml:22-30`), `grafana` (`kubernetes/apps/monitoring/grafana/app/helmrelease.yaml:35-39`), `authentik` server + worker (`kubernetes/apps/security/authentik/app/helmrelease.yaml:92-99` and `:111-118`), and `falcosidekick` (`kubernetes/apps/security/falco/app/helmrelease-sidekick.yaml:94-98`). If this Deployment is down, `kubectl top` fails and all of the above HPAs stop scaling (they hold at last-known replica count rather than failing pods — the API being unavailable doesn't evict anything, it just freezes scaling decisions).

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/kube-system/metrics-server/ks.yaml` | Flux Kustomization, 1h reconcile interval |
| `kubernetes/apps/kube-system/metrics-server/app/ocirepository.yaml` | Chart source: OCI mirror, pinned tag `3.13.1` |
| `kubernetes/apps/kube-system/metrics-server/app/helmrelease.yaml` | kubelet args, resources, security contexts |
| `kubernetes/apps/kube-system/metrics-server/app/ciliumnetworkpolicy.yaml` | Ingress from apiserver + Prometheus, egress to kubelets/DNS/apiserver |

## Secrets
None — no `ExternalSecret` in `kubernetes/apps/kube-system/metrics-server/app/`.

## Routing & access
Not exposed via Gateway/HTTPRoute — cluster-internal only. The `CiliumNetworkPolicy` (`kubernetes/apps/kube-system/metrics-server/app/ciliumnetworkpolicy.yaml`) is the entire access story:
- **Ingress:** `kube-apiserver` aggregation layer on port `10250` (the actual `metrics.k8s.io` API calls), and Prometheus (`monitoring` namespace, `app.kubernetes.io/name: prometheus` — the `kube-prometheus-stack` deployment) scraping the same port for its own `serviceMonitor.enabled: true` metrics (`helmrelease.yaml:20-21`).
- **Egress:** kube-dns (port 53), the apiserver entity, and every node in the cluster (`toEntities: cluster`) on port `10250` — this last rule is metrics-server reaching out to each node's kubelet `/metrics/resource` endpoint, which is the actual scrape path (not the ingress rule — that one's the API-read path from the aggregation layer).

## Storage
None — stateless, in-memory metrics cache only (last scrape interval per node, nothing persisted). No PVC, not part of any Velero/Kopia backup schedule, and doesn't need to be.

## Known quirks
- **`--kubelet-insecure-tls` is set** (`helmrelease.yaml:13`) — metrics-server does not validate kubelet serving certificates against a trusted CA before scraping. This is a deliberate repo choice, not a chart default (chart v3.13.1's `defaultArgs` do not include it — verified via `helm pull oci://ghcr.io/home-operations/charts-mirror/metrics-server --version 3.13.1`).
- **`--metric-resolution=10s`** (`helmrelease.yaml:16`) overrides the chart's own default of `15s`, for a slightly snappier feed into the HPA controller's polling loop.
- **Duplicated args, harmless.** `helmrelease.yaml`'s `args:` list restates `--kubelet-preferred-address-types=...` and `--kubelet-use-node-status-port`, both of which are already in the chart's `defaultArgs`. The chart's Deployment template renders `defaultArgs` and `args` as two separate, concatenated ranges (confirmed in the chart's `templates/deployment.yaml`), so both end up on the container's command line twice. Cosmetic only — not a bug, just redundant.
- **`containerSecurityContext` key is very likely a no-op.** `helmrelease.yaml:34-38` sets a top-level `containerSecurityContext` value, but chart v3.13.1's Deployment template only reads `.Values.podSecurityContext` (pod-level) and `.Values.securityContext` (container-level) — there is no `containerSecurityContext` key anywhere in the chart's templates. Practically harmless *today* only because the chart's own default `securityContext` already sets the same hardening (`allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true`, `runAsNonRoot: true`, `runAsUser: 1000`, `capabilities.drop: [ALL]`) — so the container ends up hardened by coincidence, not by this override. If the chart's default ever changes, this block would silently stop taking effect. Not fixed here per the campaign's no-inline-fixes rule — flagged for a follow-up (rename to `securityContext` in `helmrelease.yaml`).

## Common operations
- Upgrade chart version: bump `tag` in `kubernetes/apps/kube-system/metrics-server/app/ocirepository.yaml`, commit, push — the `OCIRepository` polls every `15m`, the `HelmRelease` reconciles hourly, or force both with `flux reconcile helmrelease metrics-server -n kube-system`.
- Check it's actually serving data: `kubectl top nodes` / `kubectl top pods -A` — if these return "metrics not available," check this Deployment's pod status before assuming the HPA layer is broken elsewhere.
- Pause reconciliation: `flux suspend kustomization metrics-server -n kube-system` / `flux suspend helmrelease metrics-server -n kube-system`.

## TODOs / unknowns
- `containerSecurityContext` vs. the chart's actual `securityContext` key (see Known quirks) — worth fixing so the override is real rather than accidental, but out of scope for this doc per the no-inline-fix rule.
- No incident in `docs/incidents/` currently references this app.

---
_Cross-referenced consumers: `docs/apps/authentik.md` (HPA-enabled), `docs/apps/coredns.md` (HPA-enabled)._
