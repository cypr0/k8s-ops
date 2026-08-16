# OpenSearch Operator

> **Namespace**  `logging`
> **Source**     Helm chart `opensearch-operator` v3.0.2 from `https://opensearch-project.github.io/opensearch-k8s-operator/` (`kubernetes/apps/logging/opensearch-operator/app/helmrepository.yaml`)
> **Hostname**   None — no HTTPRoute/Ingress; cluster-internal only

## What it does here
Runs the `opensearch-k8s-operator` controller that watches `OpenSearchCluster` (and related) custom resources in this namespace and reconciles them into StatefulSets, Services, and TLS material. This app is **only the controller Deployment** — it owns no data and holds no PVCs. The actual OpenSearch data cluster it manages is the sibling app `opensearch-cluster` (`kubernetes/apps/logging/opensearch-cluster/`), documented separately; this doc intentionally stops at the operator boundary.

## Architecture at a glance
- **Depends on:** kube-apiserver only (to watch/patch CRDs and StatefulSets) — no database, cache, or ExternalSecret of its own. See egress rules in `kubernetes/apps/logging/opensearch-operator/app/ciliumnetworkpolicy.yaml`.
- **Depended on by:** `opensearch-cluster`'s Flux Kustomization explicitly waits on this one — `kubernetes/apps/logging/opensearch-cluster/ks.yaml` sets `spec.dependsOn: [{name: opensearch-operator, namespace: logging}]`, so the `OpenSearchCluster` CR is never applied before the operator (and its CRDs) exist.

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/logging/opensearch-operator/ks.yaml` | Flux Kustomization: path, namespace, `wait: true`, 10m timeout |
| `kubernetes/apps/logging/opensearch-operator/app/helmrepository.yaml` | HelmRepository source (upstream operator's own Helm repo, not the OpenSearch project's main chart repo) |
| `kubernetes/apps/logging/opensearch-operator/app/helmrelease.yaml` | Chart version pin (3.0.2), resource limits, security context, `install/upgrade.crds: CreateReplace` |
| `kubernetes/apps/logging/opensearch-operator/app/ciliumnetworkpolicy.yaml` | Egress-only allow-list (DNS, apiserver, OpenSearch REST, Dashboards); no ingress permitted to the operator pod |
| `kubernetes/apps/logging/opensearch-operator/app/kustomization.yaml` | Wires the three files above into the app's Kustomize build |

No `externalsecret*.yaml`, `httproute.yaml`, or PVC manifest exists under this app's directory — confirmed by directory listing.

## Secrets
None. This app has no `ExternalSecret` resource of its own — the admin/Dashboards/OIDC secrets consumed by the OpenSearch cluster CR live under `opensearch-cluster`'s app directory instead (out of scope here; see that app's doc).

## Routing & access
- Not exposed via any Gateway/HTTPRoute — cluster-internal controller only.
- `kubernetes/apps/logging/opensearch-operator/app/ciliumnetworkpolicy.yaml` sets `ingress: []`, i.e. nothing is allowed to initiate a connection to the operator pod at all.
- Egress is a narrow allow-list: kube-dns (UDP/TCP 53), the `kube-apiserver` entity (to watch/patch CRDs and StatefulSets), the OpenSearch cluster's REST API on 9200 (selected via `opensearch.org/opensearch-cluster: opensearch` label), and OpenSearch Dashboards on 5601 (via `opensearch.cluster.dashboards: opensearch` label) — the last two so the operator can health-check the resources it manages.
- No SSO/OIDC — this is a controller, not a user-facing app.

## Storage
None. `replicaCount: 1` (`kubernetes/apps/logging/opensearch-operator/app/helmrelease.yaml`), no volumes, no StorageClass, no Velero coverage needed for this app itself — it's stateless and fully reconstructible from Git + the CRDs it recreates on install (`crds: CreateReplace`).

## Known quirks
- `maxHistory: 2` and `upgrade.remediation.strategy: rollback` on the HelmRelease (`kubernetes/apps/logging/opensearch-operator/app/helmrelease.yaml`) — a failed upgrade auto-rolls-back rather than sitting in a failed state, at the cost of only 2 kept Helm release revisions.
- `install.crds` / `upgrade.crds: CreateReplace` means Flux replaces the operator's CRDs wholesale on every install/upgrade rather than leaving out-of-band CRD edits alone — expected behavior for this chart, worth knowing if a CRD is ever hand-patched for a quick test.
- The commit that introduced this app, `8012673` (`feat(logging): replace OpenSearch Helm charts with Kubernetes Operator`), replaced separate `opensearch`/`opensearch-dashboards` Helm charts with this operator + the `OpenSearchCluster` CR pattern — most of the interesting operational lessons from that migration (single-node bootstrap failures, NFS stale state, security-plugin config, TLS requirements) are properties of the `OpenSearchCluster` CR the operator manages, not of the operator Deployment itself, so they belong in the `opensearch-cluster` doc rather than here.
- A subsequent commit, `2a99388` (`feat(network): add CiliumNetworkPolicies for logging namespace`), added the CiliumNetworkPolicy described above — before that, the operator pod had no network policy at all.

## Common operations
- Upgrade chart version: edit `kubernetes/apps/logging/opensearch-operator/app/helmrelease.yaml`, commit, push, Flux reconciles within the 1h `interval` (or force with `flux reconcile helmrelease opensearch-operator -n logging`).
- Pause reconciliation: `flux suspend kustomization opensearch-operator -n logging` / `flux suspend helmrelease opensearch-operator -n logging`.
- Check operator health directly: `kubectl -n logging get pods -l app.kubernetes.io/name=opensearch-operator` and `kubectl -n logging logs -l app.kubernetes.io/name=opensearch-operator`.

## TODOs / unknowns
- No PodMonitor/ServiceMonitor for the operator was found under `kubernetes/apps/monitoring/` — unclear whether operator-level metrics (reconcile errors, CRD watch health) are scraped at all, or only the OpenSearch cluster's own metrics are. Not verified from the repo.
- Whether the operator exposes a validating/mutating webhook for the `OpenSearchCluster` CRD (common for this operator upstream) isn't visible from this app's manifests — the `ingress: []` rule in the CiliumNetworkPolicy would block such a webhook if the API server needed to call back into the pod; not confirmed either way from the repo alone.

---
_Cite every non-obvious claim with a repo-root-relative file path (e.g. `kubernetes/apps/security/authentik/app/helmrelease.yaml`), not a bare filename — this doc lives under `docs/apps/`, so relative paths must resolve from there._
