# kube-prometheus-stack

> **Namespace**  monitoring
> **Source**     `prometheus-community` HelmRepository, chart `kube-prometheus-stack` v86.1.0 (`kubernetes/apps/monitoring/kube-prometheus-stack/app/helmrepository.yaml`, `helmrelease.yaml`)
> **Hostname**   none — no HTTPRoute, cluster-internal only

## What it does here
Core metrics and alerting backbone for the cluster: Prometheus scrapes every ServiceMonitor/PodMonitor cluster-wide (selectors are explicitly set to ignore label matching — `serviceMonitorSelectorNilUsesHelmValues: false` etc. in `helmrelease.yaml`), and Alertmanager routes firing alerts to Pushover for on-call-less, single-operator paging. The release also owns the ServiceMonitor/PodMonitor/PrometheusRule/Probe CRDs (`crds.enabled: true`) that every other app in the cluster relies on to be scraped, and ships `kube-state-metrics` + a `node-exporter` DaemonSet. Grafana is deliberately excluded here (`grafana.enabled: false`, `helmrelease.yaml`) and lives in its own app, `kubernetes/apps/monitoring/grafana/`.

## Architecture at a glance
- **Depends on:** `external-secrets-stores` Kustomization in `security` namespace (`kubernetes/apps/monitoring/kube-prometheus-stack/ks.yaml`, `dependsOn`) — must be healthy before the `alertmanager-pushover` ExternalSecret can sync; storage class `zfs-nfs` for both Prometheus and Alertmanager PVCs.
- **Depended on by:** every app that defines a ServiceMonitor/PodMonitor (via the CRDs this release installs); `grafana` queries this release's Prometheus (`http://kube-prometheus-stack-prometheus.monitoring.svc.cluster.local:9090`) and Alertmanager (`http://kube-prometheus-stack-alertmanager.monitoring.svc.cluster.local:9093`) as datasources (`kubernetes/apps/monitoring/grafana/app/helmrelease.yaml`); `gatus` health-checks both endpoints (`kubernetes/apps/monitoring/gatus/app/configmap.yaml`). At the Flux level, `loki`, `grafana`, `gatus`, `falco`, and `trivy` all list `kube-prometheus-stack` in their Kustomization `dependsOn` (their respective `ks.yaml` files) — this app's health check gates all five.

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/monitoring/kube-prometheus-stack/app/helmrelease.yaml` | Chart version/values: Grafana disabled, Alertmanager config-secret wiring, Prometheus retention/storage, `defaultRules` selection, disabled control-plane component monitors |
| `kubernetes/apps/monitoring/kube-prometheus-stack/app/helmrepository.yaml` | Chart source (`prometheus-community`) |
| `kubernetes/apps/monitoring/kube-prometheus-stack/app/externalsecret.yaml` | Renders the Alertmanager config secret (`alertmanager-pushover`) from 1Password |
| `kubernetes/apps/monitoring/kube-prometheus-stack/app/ciliumnetworkpolicy.yaml` | Five CiliumNetworkPolicies: `prometheus`, `alertmanager`, `kube-state-metrics`, `prometheus-operator`, `prometheus-node-exporter` |
| `kubernetes/apps/monitoring/kube-prometheus-stack/ks.yaml` | Flux Kustomization; `dependsOn: external-secrets-stores`, gates six downstream Kustomizations |

## Secrets
| ExternalSecret | 1Password source | Consumed by |
| --- | --- | --- |
| `alertmanager-pushover` (`kubernetes/apps/monitoring/kube-prometheus-stack/app/externalsecret.yaml`) | `dataFrom.extract` of the entire `pushover` item (fields referenced in the template: `PUSHOVER_USER_KEY`, `ALERTMANAGER_PUSHOVER_API_TOKEN`) | Rendered into secret `alertmanager-pushover`, referenced by `alertmanager.alertmanagerSpec.configSecret` in `helmrelease.yaml` — this is Alertmanager's entire routing config, not just credentials |

## Routing & access
No HTTPRoute — Prometheus, Alertmanager, and kube-state-metrics are reached only via in-cluster Service DNS (by Grafana, Gatus, and each other). Alertmanager's alert delivery path:

- **Route tree** (`externalsecret.yaml` template, changed 2026-10-11): the root receiver is **`null`**, so anything no route claims is dropped, not pushed (`Watchdog` and `InfoInhibitor` need no explicit route any more). `severity = "critical"` goes to `pushover` (priority 0, `retry: 60s`/`expire: 3600s`, `send_resolved: true`, root `repeat_interval: 12h`). `severity = "warning"` goes to `pushover-warning`: `send_resolved: false` and `repeat_interval: 3d`, because a warning that is still firing days later is a reminder, not news, and the "resolved" message doubled every flap. `info` and label-less alerts reach nobody.
- **Why this matters:** the Pushover plan is 10,000 messages a month. Alertmanager and Wazuh use the same app token; Gatus and Authentik use their own, but the limits API (`GET https://api.pushover.net/1/apps/limits.json?token=<token>`, costs no message) reports one identical counter for all three, so treat it as one shared budget. Falco has no Pushover path of its own, it pages through Wazuh. Alertmanager itself was never the big sender (~140 messages in 14 days); see `docs/incidents/2026-10-11-wazuh-pushover-quota-exhaustion.md` for the Wazuh flood.
- **Alert rule source:** almost all rules — including `CPUThrottlingHigh` and `KubePodCrashLooping` — come from the chart's own `defaultRules` groups enabled in `helmrelease.yaml` (`kubelet: true`, `k8s: true`, `node: true`, etc.). The one custom rule is `ScrapeTargetSustainedDown` in `app/prometheusrule.yaml` (see `docs/incidents/2026-09-20-cilium-cnp-scrape-target-drop.md`). Upstream rules that fire permanently are usually a stale object, not a real alert: the 2026-10-11 audit found `KubeJobFailed` held up by old Failed Jobs and `KubeHpaMaxedOut` by an undersized CPU request.
- **CiliumNetworkPolicy egress** for `alertmanager` allows `toEntities: world` on 443 (Pushover's API) and DNS only — plus a stale rule to `openclaw` namespace port 18789 (see Known quirks).

## Storage
- Prometheus: 20Gi PVC, `storageClassName: zfs-nfs`, `retention: 30d` / `retentionSize: 18GiB` (`helmrelease.yaml`, `prometheus.prometheusSpec.storageSpec`).
- Alertmanager: 1Gi PVC, same storage class (`helmrelease.yaml`, `alertmanager.alertmanagerSpec.storage`).
- **No Velero coverage.** None of the three schedules (`kubernetes/apps/velero/schedules/schedule-{daily,weekly,monthly}.yaml`) list `monitoring` in `includedNamespaces` (only `nextcloud`, `paperless`, `open-webui`, `hermes-agent`) — Prometheus's TSDB and Alertmanager's silences/notification state are unprotected by Velero. Likely acceptable for metrics (re-derived from live scraping), less so for Alertmanager silences, which aren't stored anywhere else either.

## Known quirks
- **Alertmanager's Pushover config silently no-ops on bad duration syntax.** `retry`/`expire` under `pushover_configs` need Go duration strings (`60s`, not `60`) — prometheus-operator rejected the whole rendered secret and fell back to an empty config (`receiver: "null"`) with no error surfaced anywhere obvious, so Pushover alerting looked wired up but never actually fired. Fixed in commit `b075e25`. Worth re-checking the rendered secret after any future edit to the ExternalSecret template, not just trusting that Flux applied it.
- **A dead OpenClaw egress rule is still present in the CiliumNetworkPolicy.** `ciliumnetworkpolicy.yaml`'s `alertmanager` policy still has an egress rule to `openclaw` namespace, port 18789, labeled "openclaw-hooks receiver: POST /hooks/alertmanager" — but that receiver (and its route) was removed from the Alertmanager config in commit `ed2e25c` ("remove dead openclaw alertmanager webhook receiver") when OpenClaw was deprovisioned in favor of Hermes Agent. The CNP rule is now orphaned/harmless but should be cleaned up; not fixed here per the one-file-per-commit rule.
- **This app's ExternalSecret failure blocks five other Kustomizations, not just its own.** The same `ed2e25c` commit message documents the actual incident: a missing 1Password item made `alertmanager-pushover` fail to sync continuously, which blocked this Kustomization's `wait: true` health check for hours and cascaded to every Kustomization with `dependsOn: kube-prometheus-stack` — confirmed present in `loki`, `grafana`, `gatus`, and (in `security`) `falco`, `trivy`'s respective `ks.yaml` files.
- **Grafana is intentionally not part of this release.** `grafana.enabled: false` in `helmrelease.yaml` — don't look here for dashboard/datasource config; that's `kubernetes/apps/monitoring/grafana/`.

## Common operations
- Upgrade chart version: edit `spec.chart.spec.version` in `helmrelease.yaml`, commit, push. Flux reconciles within `interval: 1h`, or force with `flux reconcile helmrelease kube-prometheus-stack -n monitoring`.
- Change Pushover routing/thresholds: edit the `alertmanager.yaml` template in `externalsecret.yaml`, then `kubectl annotate externalsecret alertmanager-pushover -n monitoring force-sync=$(date +%s)` — verify the rendered secret actually contains the `pushover` receiver afterward given the past silent-rejection failure mode above.
- Pause reconciliation: `flux suspend kustomization kube-prometheus-stack -n monitoring` / `flux suspend helmrelease kube-prometheus-stack -n monitoring` (remember this also stalls the six dependent Kustomizations' ability to re-reconcile if they were mid-retry).

## TODOs / unknowns
- Stale `openclaw` egress rule in `ciliumnetworkpolicy.yaml`'s `alertmanager` policy (port 18789) should be removed in a follow-up commit — noted above, not fixed here.
- Alertmanager replica count is not explicitly set in `helmrelease.yaml` (chart default applies); the CNP's HA-gossip rule (port 9094 TCP/UDP, "if multiple replicas") suggests this was anticipated but actual replica count wasn't verified against live cluster state for this doc.
- No Velero coverage for the `monitoring` namespace (see Storage) — worth an explicit decision on whether Alertmanager silences deserve backup, rather than leaving it as an implicit gap.
