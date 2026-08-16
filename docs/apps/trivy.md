# Trivy Operator

> **Namespace**  Kustomization metadata namespace `security` / HelmRelease `targetNamespace: trivy-system` (`kubernetes/apps/security/trivy/ks.yaml`)
> **Source**     `aqua` HelmRepository (`https://aquasecurity.github.io/helm-charts/`), chart `trivy-operator` v0.32.1 (`kubernetes/apps/security/trivy/app/helmrepository.yaml`, `kubernetes/apps/security/trivy/app/helmrelease.yaml`)
> **Hostname**   none — no ingress, cluster-internal security tooling only

## What it does here
Scans workloads for vulnerabilities (CVEs), misconfigurations, and exposed secrets, producing `VulnerabilityReport`/`ConfigAuditReport`/`ExposedSecretReport` CRDs per scanned resource. This is the only vulnerability scanner in the cluster — Kubescape's `vulnerabilityScan` is explicitly set to `disable` in favor of it (`kubernetes/apps/security/kubescape/app/helmrelease.yaml`, `# handled by Trivy Operator`). It's the tool that found the gotenberg CVEs (48 CRITICAL/664 HIGH) which triggered the `8.34.0`→`8.36.0` image bump earlier this session (commit `02628d6`).

## Architecture at a glance
- **Depends on:** `kube-prometheus-stack` (Kustomization `dependsOn`) — the HelmRelease's `wait: true` blocks on Prometheus CRDs existing for the `serviceMonitor`; kube-apiserver, to list/watch workloads and write report CRDs; its own built-in `trivy-server` (`operator.builtInTrivyServer: true`) for a locally cached vulnerability DB — deployed as a separate pod despite being "built-in", per the dedicated CNP for it; egress to the public internet (port 443) to pull vulnerability DBs from GitHub/AquaSec and pull image manifests from registries.
- **Depended on by:** `opensearch-cluster`'s `crd-to-opensearch` CronJob, which reads `VulnerabilityReport`/`ConfigAuditReport` CRDs cluster-wide and indexes them into OpenSearch's `trivy-*` daily indices, which in turn back a dedicated OpenSearch Dashboards "Trivy Vulnerability Dashboard"; Prometheus scrapes it via the chart's own `serviceMonitor`; Kubescape's design explicitly defers vulnerability scanning to this app.

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/security/trivy/ks.yaml` | Flux Kustomization — `dependsOn: kube-prometheus-stack`, `targetNamespace: trivy-system`, `interval: 1h` |
| `kubernetes/apps/security/trivy/app/helmrelease.yaml` | Chart version, operator/trivy/node-collector values, resource limits |
| `kubernetes/apps/security/trivy/app/helmrepository.yaml` | Chart source (`aqua` repo) |
| `kubernetes/apps/security/trivy/app/ciliumnetworkpolicy.yaml` | Three CNPs: `trivy-operator`, `trivy-server`, `trivy-scan-jobs` |
| `kubernetes/apps/security/trivy/app/namespace.yaml` | Dedicated `trivy-system` namespace, prune disabled |
| `kubernetes/apps/security/trivy/app/kustomization.yaml` | Kustomize resource list for the four files above |

## Secrets
None — no ExternalSecret in this app's directory. Scans are unauthenticated/offline against public vulnerability DBs and whatever image registries the cluster already has pull access to.

## Routing & access
None — no HTTPRoute; all access is `kubectl` (`kubectl get vulnerabilityreports -A`, `configauditreports`) or via Prometheus/OpenSearch Dashboards.

Three CiliumNetworkPolicies, one per component:
- **`trivy-operator`** — egress only: DNS to `kube-dns`, `kube-apiserver` (to create scan jobs / write report CRDs), `trivy-server:4954` (cached vuln DB), and `world:443` (pull vuln DBs from GitHub/AquaSec, scan registries).
- **`trivy-server`** — ingress from `trivy-operator` and anything `app.kubernetes.io/managed-by: trivy-operator` (the ephemeral scan jobs) on `4954`, from `monitoring` namespace's `prometheus` on `9198` (metrics), and from `host` entity on `4954` (kubelet probes — added in commit `ba3bce2`); egress for DNS and `world:443` for its own initial DB download from GitHub releases.
- **`trivy-scan-jobs`** — matches `app.kubernetes.io/managed-by: trivy-operator` (the ephemeral per-workload scan pods); egress for DNS, `kube-apiserver`, `trivy-server:4954`, and `world:443` to pull the image layers/manifests of whatever it's scanning. This policy was added after the fact in commit `209cce0` — scan jobs were originally uncovered by any CNP and presumably failing/timing out silently until this was noticed.

## Storage
None — no PVC. Findings live entirely in Kubernetes CRDs (`VulnerabilityReport`, `ConfigAuditReport`, `ExposedSecretReport`), which the OpenSearch `crd-to-opensearch` CronJob exports hourly for longer-term retention/dashboards. Not in Velero's backup scope directly — the CRDs are ephemeral scan output, not source state.

## Known quirks
- **Memory-tuned twice against real OOM events, not preemptively.** A `512Mi` limit was OOM-killed when Velero's restore-test spun up ~15 pods at once across newly created `*-restore-test` namespaces, and the operator queued a scan job per pod simultaneously (commit `c453390`). Fixed two ways at once: `excludeNamespaces: "kube-system,flux-system,*-restore-test"` (glob, not exact names, so it survives whatever restore-test namespace names Velero generates) and the memory limit raised to `1Gi` as headroom against any other legitimate burst.
- **`nodeCollector` (node-level CIS scanning) is deliberately disabled** — `infraAssessmentScannerEnabled: false`. It tried to write to `/etc/systemd` on the host, which fails on Talos's immutable filesystem (commit `9dba0db`). The first fix attempt used the wrong key (`nodeCollectorEnabled`) and had to be corrected to `infraAssessmentScannerEnabled` in a follow-up commit (`e288b4f`) — CIS node benchmarks are handled by Kubescape instead, consistent with the broader Talos-incompatibility pattern for security tooling in this cluster.
- **`concurrentScanJobsLimit: 3`** caps how many scan jobs run at once — this, together with the namespace exclusion above, is the second line of defense against the same burst-scanning OOM pattern.
- **No dedicated Grafana dashboard found for this app** — only the OpenSearch Dashboards one, despite `serviceMonitor.enabled: true` shipping metrics to Prometheus.

## Common operations
- Upgrade chart version: edit `kubernetes/apps/security/trivy/app/helmrelease.yaml` `spec.chart.spec.version`, commit, push; Flux reconciles within the `1h` interval or force with `flux reconcile helmrelease trivy-operator -n trivy-system`.
- Pause reconciliation: `flux suspend kustomization trivy-operator -n security` / `flux suspend helmrelease trivy-operator -n trivy-system`.
- Check current findings: `kubectl get vulnerabilityreports -A` / `kubectl get configauditreports -A`.
- If scan jobs start failing/timing out after any CNP change, re-check the `trivy-scan-jobs` policy first — it was the last of the three CNPs added and is easy to overlook since it doesn't share a name with the operator or server.

## TODOs / unknowns
- Whether the missing Grafana dashboard (see Known quirks) is an intentional gap (OpenSearch Dashboards considered sufficient) or simply not yet built — not stated anywhere in the repo, worth asking the operator.
- Manual/on-demand scan trigger outside the operator's normal watch-and-scan loop not exercised or documented from this repo.
- Whether `severity: "MEDIUM,HIGH,CRITICAL"` should include `LOW` has not been revisited since initial rollout (commit `7b2739b`).

---
_Cite every non-obvious claim with a repo-root-relative file path (e.g. `kubernetes/apps/security/authentik/app/helmrelease.yaml`), not a bare filename — this doc lives under `docs/apps/`, so relative paths must resolve from there._
