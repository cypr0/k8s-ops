# flux-operator

> **Namespace**  flux-system
> **Source**     `flux-operator` OCIRepository, chart `flux-operator` at `ghcr.io/controlplaneio-fluxcd/charts/flux-operator:0.58.1` (`kubernetes/apps/flux-system/flux-operator/app/ocirepository.yaml`)
> **Hostname**   none — no ingress, not exposed via Gateway/HTTPRoute

## What it does here
The operator that installs and upgrades the actual Flux controllers (source-controller, kustomize-controller, helm-controller, notification-controller) by reconciling a `FluxInstance` custom resource, per the in-repo comment on its own CiliumNetworkPolicy: "manages the FluxInstance CRD, installs and upgrades Flux controllers" (`kubernetes/apps/flux-system/flux-operator/app/ciliumnetworkpolicy.yaml:2`). In other words, this is the bootstrap layer one level below GitOps itself — it is what turns the `flux-instance` HelmRelease's declarative config (`kubernetes/apps/flux-system/flux-instance/app/helmrelease.yaml`) into the running Flux control plane that then reconciles everything else in this repo, including itself.

## Architecture at a glance
- **Depends on:** nothing in-cluster — bootstrapped directly by the `cluster-apps` root Kustomization (`kubernetes/flux/cluster/ks.yaml`), same as every other top-level app.
- **Depended on by:** `flux-instance`, explicitly — its Kustomization declares `dependsOn: [name: flux-operator]` (`kubernetes/apps/flux-system/flux-instance/ks.yaml:8-9`), and `flux-instance` in turn is a `dependsOn` of `flux-operator-mcp` (`kubernetes/apps/flux-system/flux-operator-mcp/ks.yaml:8-9`). So the chain is flux-operator → flux-instance → flux-operator-mcp. Since flux-operator renders the actual Flux controllers, a broken flux-operator stalls reconciliation cluster-wide (though an already-running Flux control plane can keep functioning even if the operator itself is temporarily down — it manages the controllers, it isn't in their runtime path).
- Also referenced by name in `kubernetes/apps/security/kyverno/policies/clusterpolicy-rbac-cluster-admin.yaml:38`, which allowlists the `flux-operator` ServiceAccount (alongside `kustomize-controller` and `helm-controller`) as the only ones permitted to bind `cluster-admin` — a deliberate, reviewed exception to the CIS 5.1.1 "no cluster-admin bindings" check, not an oversight.

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/flux-system/flux-operator/app/helmrelease.yaml` | Chart install via `chartRef` (OCIRepository-sourced, not a HelmRepository); only override is `serviceMonitor.create: true` |
| `kubernetes/apps/flux-system/flux-operator/app/ocirepository.yaml` | Pins chart version `0.58.1` from `oci://ghcr.io/controlplaneio-fluxcd/charts/flux-operator` |
| `kubernetes/apps/flux-system/flux-operator/app/ciliumnetworkpolicy.yaml` | Network policy — see Routing & access |
| `kubernetes/apps/flux-system/flux-operator/app/kustomization.yaml` | Bundles the three files above |
| `kubernetes/apps/flux-system/flux-operator/ks.yaml` | Flux Kustomization — `targetNamespace: flux-system`, `wait: true`, 1h interval |

## Secrets
None. No `externalsecret*.yaml` in the app directory.

## Routing & access
- Not exposed via Gateway/HTTPRoute — cluster-internal only.
- `serviceMonitor.create: true` (`kubernetes/apps/flux-system/flux-operator/app/helmrelease.yaml:13`) is scraped by Prometheus; the CiliumNetworkPolicy allows ingress on port `8080` from pods labeled `app.kubernetes.io/name: prometheus` in the `monitoring` namespace, plus kubelet probe traffic (`fromEntities: host`) on the same port (`kubernetes/apps/flux-system/flux-operator/app/ciliumnetworkpolicy.yaml:12-27`).
- Egress is scoped to: DNS to `kube-dns` in `kube-system`, the `kube-apiserver` entity (it manages Flux controller resources via the K8s API), and `world:443` — the CNP comments this last rule as "Pull operator and flux release OCI images" (`kubernetes/apps/flux-system/flux-operator/app/ciliumnetworkpolicy.yaml:41-47`), i.e. the operator itself fetches OCI artifacts (its own image updates and/or the Flux distribution manifests it installs).

## Storage
None — no PVCs.

## Known quirks
- The HelmRelease uses `chartRef` against an `OCIRepository`, not the more common `HelmRepository` + `chart` pairing — consistent across all three apps in this namespace (`flux-instance`, `flux-operator`, `flux-operator-mcp`), all sourced straight from GHCR OCI artifacts rather than a Helm repo index.
- Chart version bumps in this file have so far all been mechanical, one-line dependency-bot-style commits (`git log --oneline -- kubernetes/apps/flux-system/flux-operator/`: `0.50.0 → 0.52.0 → 0.54.1 → 0.57.0 → 0.58.0 → 0.58.1`) with no accompanying values changes — no bump has needed a values migration yet, but that's a track record, not a guarantee for the next one.

## Common operations
- Upgrade chart version: edit `kubernetes/apps/flux-system/flux-operator/app/ocirepository.yaml` (`spec.ref.tag`), commit, push, Flux reconciles within the 1h interval (or force with `flux reconcile helmrelease flux-operator -n flux-system`).
- Pause reconciliation: `flux suspend kustomization flux-operator -n flux-system` / `flux suspend helmrelease flux-operator -n flux-system`. Note this only pauses the operator's own reconciliation — it does not stop the Flux controllers it already installed from running.
- Because this app bootstraps Flux itself, `flux reconcile` commands issued while diagnosing a flux-operator problem may not work if the controllers it manages are the thing that's broken — check controller pod health directly (`kubectl get pods -n flux-system`) as a first step in that scenario.

## TODOs / unknowns
- No `docs/incidents/` entries currently reference flux-operator specifically — no incident history to draw quirks from beyond what's above.
- Whether the `0.50.0 → 0.57.0` version bumps were opened by a bot (e.g. Renovate) or applied manually could not be confirmed — no renovate/dependabot config was found in the repo; commit messages (`feat(container): update flux-operator group (...)`) suggest automation but the tool itself isn't identified in-repo.

---
_Cite every non-obvious claim with a repo-root-relative file path (e.g. `kubernetes/apps/security/authentik/app/helmrelease.yaml`), not a bare filename — this doc lives under `docs/apps/`, so relative paths must resolve from there._
