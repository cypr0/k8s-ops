# Alloy

> **Namespace**  monitoring
> **Source**     `grafana/alloy` Helm chart v1.8.2 — `kubernetes/apps/monitoring/alloy/app/helmrepository.yaml` + `kubernetes/apps/monitoring/alloy/app/helmrelease.yaml`
> **Hostname**   none — internal-only DaemonSet, no HTTPRoute in this app's directory

## What it does here
Cluster-wide log collector: a DaemonSet on every worker node (control-plane nodes excluded via `nodeAffinity`, `kubernetes/apps/monitoring/alloy/app/helmrelease.yaml:102-108`) that tails Kubernetes pod container logs and the Kubernetes events stream, then forwards both to Loki only (`kubernetes/apps/monitoring/alloy/app/helmrelease.yaml:33-95`). It does **not** currently ship anything to OpenSearch — an OpenSearch dual-write path was built and then fully reverted (see Known quirks). Pods can opt out of collection with the `log.io/skip: "true"` annotation, dropped in the relabel rule at `kubernetes/apps/monitoring/alloy/app/helmrelease.yaml:68-72`.

## Architecture at a glance
- **Depends on:** Loki, explicitly via `dependsOn` in `kubernetes/apps/monitoring/alloy/ks.yaml:12-14` (Flux won't apply Alloy until Loki's Kustomization is ready); the in-cluster API server for pod/event discovery (`discovery.kubernetes "pods"`, `loki.source.kubernetes_events`, `kubernetes/apps/monitoring/alloy/app/helmrelease.yaml:41-43,92-95`); `kube-dns` for DNS resolution (egress rule, `kubernetes/apps/monitoring/alloy/app/ciliumnetworkpolicy.yaml:30-40`).
- **Depended on by:** Grafana's Loki-backed log views/alerts for pod logs and Kubernetes events — if Alloy is down, no new pod-log or event data reaches Loki. This is independent of the SIEM/OpenSearch pipeline: per-app container logs also land in OpenSearch, but that path is fed by Fluent Bit reading log files directly (`kubernetes/apps/logging/fluent-bit/app/helmrelease.yaml:319-338`), not by Alloy — Fluent Bit's own comment there notes "App logs already go to Loki via Alloy; this adds them to OpenSearch" as a parallel, not downstream, pipeline.

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/monitoring/alloy/ks.yaml` | Flux Kustomization; `dependsOn: loki`, 1h reconcile interval |
| `kubernetes/apps/monitoring/alloy/app/helmrelease.yaml` | Chart version 1.8.2, embedded River config (loki.write + Kubernetes pod/event sources), DaemonSet scheduling, resources, ServiceMonitor |
| `kubernetes/apps/monitoring/alloy/app/helmrepository.yaml` | Points at `https://grafana.github.io/helm-charts` |
| `kubernetes/apps/monitoring/alloy/app/ciliumnetworkpolicy.yaml` | Ingress from Prometheus + kubelet probes; egress to DNS, apiserver, Loki, OpenSearch (unused, see Known quirks), and `world:443` |
| `kubernetes/apps/monitoring/alloy/app/externalsecret-opensearch.yaml` | Present on disk but **not applied** — not listed in `kustomization.yaml` (see Known quirks) |
| `kubernetes/apps/monitoring/alloy/app/kustomization.yaml` | Only wires in `helmrepository.yaml`, `helmrelease.yaml`, `ciliumnetworkpolicy.yaml` |

## Secrets
| ExternalSecret | 1Password source | Consumed by |
| --- | --- | --- |
| `alloy-opensearch-credentials` (`kubernetes/apps/monitoring/alloy/app/externalsecret-opensearch.yaml`) | item `opensearch`, field `OPENSEARCH_ADMIN_PASSWORD` → templated key `OPENSEARCH_PASSWORD`; `OPENSEARCH_USER` is a literal `"admin"` baked into the template, not vault-sourced | **Nothing.** The file exists but `kubernetes/apps/monitoring/alloy/app/kustomization.yaml` does not reference it, so Flux never applies it, and the current `helmrelease.yaml` has no `envFrom` to consume these keys anyway. Dead/orphaned manifest — see Known quirks. |

## Routing & access
- No HTTPRoute — Alloy is never reached from outside the cluster; it only pushes outbound to Loki.
- CiliumNetworkPolicy ingress (`kubernetes/apps/monitoring/alloy/app/ciliumnetworkpolicy.yaml:12-28`): Prometheus scrapes metrics on port `12345`; kubelet readiness/liveness probes reach the same port via the `host` entity.
- Egress: DNS to `kube-dns` (53/UDP+TCP); Kubernetes API via `kube-apiserver` entity (pod/event discovery); Loki push on `3100`; an allow rule to OpenSearch on `9200` labeled "dual-write via OTLP/HTTP" that nothing in the current config actually uses (stale, see Known quirks); `world:443` for Grafana's own update-check phone-home.
- No OIDC/SSO — Alloy has no UI.

## Storage
None. Stateless DaemonSet — no PVC, no `values.alloy` persistence block in `kubernetes/apps/monitoring/alloy/app/helmrelease.yaml`. Not part of any Velero/Kopia backup schedule (nothing to back up).

## Known quirks
- **The OpenSearch dual-write path was built, then fully reverted — it is not running today.** Commit `6faeb07` ("dual-write all logs to Loki + OpenSearch (SIEM)") added an `otelcol.*` pipeline plus `externalsecret-opensearch.yaml`; commit `bba3db0` found `otelcol.exporter.elasticsearch` doesn't exist in Alloy 1.8.x and switched to `otelcol.exporter.otlphttp`; commit `af189ac` ("revert(monitoring): remove OpenSearch pipeline from Alloy - stabilize first") removed the entire `otelcol.*` block and the `envFrom` wiring because `otelcol.auth.basic` credential injection "not working reliably." The config currently in `kubernetes/apps/monitoring/alloy/app/helmrelease.yaml` is Loki-only — there is no `otelcol` component in it at all. **This corrects an assumption in this operator's own memory notes** (`project_opensearch_lessons.md`, "Alloy → OpenSearch dual-write") that describes the intended `otelcol.exporter.elasticsearch` design as if it were live; per the commit history above and the current file, it never stabilized and was pulled, not shipped.
- **Two leftover artifacts from that reverted feature are still sitting in the repo:** `externalsecret-opensearch.yaml` was dropped from `kustomization.yaml` in commit `767087d` but the file itself was never deleted, so it's dead weight; the CiliumNetworkPolicy's OpenSearch egress rule and its "dual-write via OTLP/HTTP" comment (`kubernetes/apps/monitoring/alloy/app/ciliumnetworkpolicy.yaml:53-61`, added later in `14d0b60`) allow traffic the current config never generates. Harmless (an unused allow-rule doesn't grant anything on its own), but misleading if read as current-state documentation.
- **Actual OpenSearch ingestion for logs now lives entirely in Fluent Bit** (`kubernetes/apps/logging/fluent-bit/app/`), which took over both syslog collection (Proxmox/OPNsense, moved off Alloy in `767087d`) and a separate app-container-log-to-OpenSearch path (`kubernetes/apps/logging/fluent-bit/app/helmrelease.yaml:319-338`) that reads log files independently rather than consuming Alloy's Loki output.
- **Memory/CPU limits were sized for the reverted feature and never scaled back down.** Commit `6faeb07` raised memory from 128Mi/256Mi to 192Mi/384Mi (request/limit) explicitly for "dual-write overhead"; that headroom is still the current value (`kubernetes/apps/monitoring/alloy/app/helmrelease.yaml:111-116`) even though the dual-write path it was sized for no longer runs.

## Common operations
- Upgrade chart version: edit `kubernetes/apps/monitoring/alloy/app/helmrelease.yaml`, commit, push, Flux reconciles within the 1h `interval` (or force with `flux reconcile helmrelease alloy -n monitoring`).
- Edit the collection pipeline: it's inline River config under `spec.values.alloy.configMap.content` in `kubernetes/apps/monitoring/alloy/app/helmrelease.yaml` — no separate ConfigMap file to touch.
- Pause reconciliation: `flux suspend kustomization alloy -n monitoring` / `flux suspend helmrelease alloy -n monitoring`.
- Skip logs for a specific pod: add the annotation `log.io/skip: "true"` to that pod (matched by the `drop` rule at `kubernetes/apps/monitoring/alloy/app/helmrelease.yaml:68-72`).

## TODOs / unknowns
- Whether `externalsecret-opensearch.yaml` and the OpenSearch egress rule in `ciliumnetworkpolicy.yaml` should be deleted (dead config from a reverted feature) or the OpenSearch dual-write path re-attempted properly — not decided here, flagged for the operator; not touched on this branch per the campaign's "no manifest fixes in a docs PR" rule.
- Not verified live (would require `kubectl`/`flux` access at documentation time): whether the DaemonSet is currently healthy on all workers, or actual memory usage against the 384Mi limit.
