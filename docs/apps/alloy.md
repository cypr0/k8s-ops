# Alloy

> **Namespace**  monitoring
> **Source**     `grafana/alloy` Helm chart v1.8.2 — `kubernetes/apps/monitoring/alloy/app/helmrepository.yaml` + `kubernetes/apps/monitoring/alloy/app/helmrelease.yaml`
> **Hostname**   none — internal-only DaemonSet, no HTTPRoute in this app's directory

## What it does here
Cluster-wide log collector: a DaemonSet on every worker node (control-plane nodes excluded via `nodeAffinity`, `kubernetes/apps/monitoring/alloy/app/helmrelease.yaml:102-108`) that tails Kubernetes pod container logs and the Kubernetes events stream, then forwards both to Loki only (`kubernetes/apps/monitoring/alloy/app/helmrelease.yaml:33-95`). Loki is now its only destination, and since the 2026-09-20 slim-down it is the cluster's **sole** collector of pod logs — the Fluent Bit tail that used to duplicate four namespaces into OpenSearch is gone. Pods can opt out of collection with the `log.io/skip: "true"` annotation, dropped in the relabel rule at `kubernetes/apps/monitoring/alloy/app/helmrelease.yaml:68-72`.

## Architecture at a glance
- **Depends on:** Loki, explicitly via `dependsOn` in `kubernetes/apps/monitoring/alloy/ks.yaml:12-14` (Flux won't apply Alloy until Loki's Kustomization is ready); the in-cluster API server for pod/event discovery (`discovery.kubernetes "pods"`, `loki.source.kubernetes_events`, `kubernetes/apps/monitoring/alloy/app/helmrelease.yaml:41-43,92-95`); `kube-dns` for DNS resolution (egress rule, `kubernetes/apps/monitoring/alloy/app/ciliumnetworkpolicy.yaml:30-40`).
- **Depended on by:** Grafana's Loki-backed log views/alerts for pod logs and Kubernetes events. Since 2026-09-20 there is no second path: if Alloy is down, **no** pod-log or event data reaches Loki at all. Fluent Bit no longer tails container logs — it handles only the Talos node-log stream (see `docs/apps/fluent-bit.md`).

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/monitoring/alloy/ks.yaml` | Flux Kustomization; `dependsOn: loki`, 1h reconcile interval |
| `kubernetes/apps/monitoring/alloy/app/helmrelease.yaml` | Chart version 1.8.2, embedded River config (loki.write + Kubernetes pod/event sources), DaemonSet scheduling, resources, ServiceMonitor |
| `kubernetes/apps/monitoring/alloy/app/helmrepository.yaml` | Points at `https://grafana.github.io/helm-charts` |
| `kubernetes/apps/monitoring/alloy/app/ciliumnetworkpolicy.yaml` | Ingress from Prometheus + kubelet probes; egress to DNS, apiserver, Loki, and `world:443` |
| `kubernetes/apps/monitoring/alloy/app/kustomization.yaml` | Only wires in `helmrepository.yaml`, `helmrelease.yaml`, `ciliumnetworkpolicy.yaml` |

## Secrets
None. Alloy pushes to Loki unauthenticated over the cluster network. The orphaned
`alloy-opensearch-credentials` ExternalSecret — never applied, because it was
absent from `kustomization.yaml` — was deleted on 2026-09-20.

## Routing & access
- No HTTPRoute — Alloy is never reached from outside the cluster; it only pushes outbound to Loki.
- CiliumNetworkPolicy ingress (`kubernetes/apps/monitoring/alloy/app/ciliumnetworkpolicy.yaml:12-28`): Prometheus scrapes metrics on port `12345`; kubelet readiness/liveness probes reach the same port via the `host` entity.
- Egress: DNS to `kube-dns` (53/UDP+TCP); Kubernetes API via `kube-apiserver` entity (pod/event discovery); Loki push on `3100`; `world:443` for Grafana's own update-check phone-home.
- No OIDC/SSO — Alloy has no UI.

## Storage
None. Stateless DaemonSet — no PVC, no `values.alloy` persistence block in `kubernetes/apps/monitoring/alloy/app/helmrelease.yaml`. Not part of any Velero/Kopia backup schedule (nothing to back up).

## Known quirks
- **The OpenSearch dual-write path was built, then fully reverted — it is not running today.** Commit `6faeb07` ("dual-write all logs to Loki + OpenSearch (SIEM)") added an `otelcol.*` pipeline plus `externalsecret-opensearch.yaml`; commit `bba3db0` found `otelcol.exporter.elasticsearch` doesn't exist in Alloy 1.8.x and switched to `otelcol.exporter.otlphttp`; commit `af189ac` ("revert(monitoring): remove OpenSearch pipeline from Alloy - stabilize first") removed the entire `otelcol.*` block and the `envFrom` wiring because `otelcol.auth.basic` credential injection "not working reliably." The config currently in `kubernetes/apps/monitoring/alloy/app/helmrelease.yaml` is Loki-only — there is no `otelcol` component in it at all. **This corrected an assumption in this operator's own memory notes** (`project_opensearch_lessons.md`, "Alloy → OpenSearch dual-write") that described the intended `otelcol.exporter.elasticsearch` design as if it were live; per the commit history above, it never stabilized and was pulled, not shipped. Moot since 2026-09-20 — OpenSearch is gone from the cluster entirely — but kept as the record of why Alloy is Loki-only.
  The two leftovers it named — the unapplied `externalsecret-opensearch.yaml` and the stale OpenSearch egress rule — were both deleted on 2026-09-20 when OpenSearch itself was removed.
- **Memory/CPU limits were sized for the reverted feature and never scaled back down.** Commit `6faeb07` raised memory from 128Mi/256Mi to 192Mi/384Mi (request/limit) explicitly for "dual-write overhead"; that headroom is still the current value (`kubernetes/apps/monitoring/alloy/app/helmrelease.yaml:111-116`) even though the dual-write path it was sized for never ran. Arguably fine to keep now that Alloy carries every pod log alone.

## Common operations
- Upgrade chart version: edit `kubernetes/apps/monitoring/alloy/app/helmrelease.yaml`, commit, push, Flux reconciles within the 1h `interval` (or force with `flux reconcile helmrelease alloy -n monitoring`).
- Edit the collection pipeline: it's inline River config under `spec.values.alloy.configMap.content` in `kubernetes/apps/monitoring/alloy/app/helmrelease.yaml` — no separate ConfigMap file to touch.
- Pause reconciliation: `flux suspend kustomization alloy -n monitoring` / `flux suspend helmrelease alloy -n monitoring`.
- Skip logs for a specific pod: add the annotation `log.io/skip: "true"` to that pod (matched by the `drop` rule at `kubernetes/apps/monitoring/alloy/app/helmrelease.yaml:68-72`).

## TODOs / unknowns
- Alloy is now a single point of failure for all pod logs. There is no alert on Alloy itself falling behind or dying, and with Fluent Bit no longer duplicating any of it, an outage is silent until someone notices an empty Grafana panel.
- Not verified live (would require `kubectl`/`flux` access at documentation time): whether the DaemonSet is currently healthy on all workers, or actual memory usage against the 384Mi limit.
