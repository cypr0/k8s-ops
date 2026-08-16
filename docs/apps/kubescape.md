# Kubescape

> **Namespace**  security (Kustomization) / kubescape (HelmRelease `targetNamespace`)
> **Source**     `kubescape` HelmRepository, chart `kubescape-operator` v1.40.2 (`kubernetes/apps/security/kubescape/app/helmrepository.yaml`, `helmrelease.yaml`)
> **Hostname**   none — no ingress, cluster-internal security tooling only

## What it does here
Runtime Kubernetes configuration/compliance scanner (CIS benchmark) and node-level runtime scanner, running in offline mode (no Kubescape cloud account). Vulnerability scanning is deliberately disabled here — that's Trivy Operator's job in this cluster, not Kubescape's — so Kubescape's actual scope is configuration scanning (`configurationScan`), continuous scanning, and node scanning only.

## Architecture at a glance
- **Depends on:** nothing external — self-contained scanner, no database/cache dependency.
- **Depended on by:** Prometheus (via `serviceMonitor`), Alertmanager (fires on this namespace's own pod health — see Known quirks).

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/security/kubescape/app/helmrelease.yaml` | Chart version, scan config, per-component resources, HelmRelease `timeout` |
| `kubernetes/apps/security/kubescape/app/helmrepository.yaml` | Chart source |
| `kubernetes/apps/security/kubescape/app/ciliumnetworkpolicy.yaml` | Network policy |
| `kubernetes/apps/security/kubescape/app/namespace.yaml` | Dedicated `kubescape` namespace (HelmRelease deploys here, not into `security`) |

## Secrets
None — no ExternalSecret in this app's directory. Runs fully offline with no cloud account credentials.

## Routing & access
None — no HTTPRoute. All access is `kubectl`-based (`kubectl get vulnerabilityreports`/`configurationscansummaries` CRDs) or via Prometheus/Grafana.

## Storage
None — findings live in Kubernetes CRDs, not a PVC.

## Known quirks
- **`nodeAgent` requires generous CPU headroom, not just enough to avoid OOM.** It does continuous eBPF-based node scanning — confirmed live 2026-08-16: at the prior `10m` CPU request, `node-agent` on `k8s-wrk-0` was `CrashLoopBackOff` for 12+ hours (119 restarts), with Alertmanager actively firing `KubePodCrashLooping` + `CPUThrottlingHigh`. Raised to `150m` request / `750m` limit (`helmrelease.yaml`, `nodeAgent.resources`) — the limit matters here as much as the request, so a scanning burst can't starve other pods on the same worker.
- **The HelmRelease needs an explicit, longer `spec.timeout` (15m).** `node-agent`'s `/readyz` takes several minutes per pod to respond after a (re)start (eBPF collector init, worse under CPU contention), and the DaemonSet rolls one of 5 worker nodes at a time — a full rollout structurally exceeds Helm's ~5-minute default wait. Without this, *any* future change to `nodeAgent` (not just a resources bump) gets falsely marked failed and silently rolled back to the previous release before the new values ever actually take effect — confirmed live 2026-08-16 when the CPU-request fix above sat "committed" but never actually landed for a period, because the HelmRelease kept auto-rolling-back on the timeout. See `docs/incidents/2026-08-16-kubescape-helm-timeout-rollback-loop.md`.
- **CP nodes are explicitly excluded** (`nodeAgent.affinity.nodeAntiAffinity` on `node-role.kubernetes.io/control-plane`) — consistent with this cluster's broader control-plane-memory-pressure remediation (Falco and Trivy follow the same pattern).

## Common operations
- Upgrade chart: edit `helmrelease.yaml` `spec.chart.spec.version`, commit, push. **Do not remove `spec.timeout: 15m`** without re-verifying node-agent's actual rollout time first — see Known quirks.
- If a HelmRelease upgrade to this app looks "stuck"/reverted: `helm history kubescape -n kubescape` — repeated `Rollback to N` entries mean the timeout issue above has recurred; `flux reconcile helmrelease kubescape -n kubescape` after confirming the timeout is still set will retry cleanly.
- Manually trigger a scan outside the `scanSchedule` (`0 2 * * *`): see chart docs for the operator's manual-trigger CRD — not yet exercised/documented in this repo (TODO below).

## TODOs / unknowns
- Manual/on-demand scan trigger procedure not yet documented from this repo directly — chart supports it, but no local example to cite.
- Whether `configuration.frameworks: [cis-v1.23-t1.0]` should be expanded to additional frameworks hasn't been evaluated since the original CIS benchmark rollout (see `project_cis_benchmark_review` memory) — worth revisiting alongside a future CIS review pass.
