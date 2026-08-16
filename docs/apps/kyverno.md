# Kyverno

> **Namespace**  `kyverno` (workload namespace; the Flux Kustomization objects live in `security`, see `kubernetes/apps/security/kyverno/ks.yaml`)
> **Source**     Helm chart `kyverno/kyverno` v3.8.1 from `https://kyverno.github.io/kyverno/` (`kubernetes/apps/security/kyverno/app/helmrepository.yaml`, `kubernetes/apps/security/kyverno/app/helmrelease.yaml`)
> **Hostname**   none — no HTTPRoute; Kyverno is only reachable as a `ValidatingWebhookConfiguration`/`MutatingWebhookConfiguration` target called by kube-apiserver, plus a Prometheus scrape target

## What it does here
Kyverno is this cluster's policy engine for the CIS Kubernetes Benchmark v2.0.1 section 5 (RBAC, Pod Security, secrets hygiene, image provenance, network policy) rollout that started 2026-07-04. It runs as an admission webhook plus three background controllers, evaluating every Pod/Role/ClusterRole/ClusterRoleBinding/etc. against a set of `ClusterPolicy` objects under `kubernetes/apps/security/kyverno/policies/`. Every policy currently runs in `validationFailureAction: Audit` — it reports violations via `PolicyReport`/`ClusterPolicyReport` but blocks nothing — with one exception: `add-default-seccomp` actually mutates pods to inject a `RuntimeDefault` seccomp profile where missing.

## Architecture at a glance
- **Depends on:** kube-apiserver (calls its admission webhooks on port 9443), CoreDNS (`kube-system/kube-dns`) for its own DNS egress. No database, cache, or object storage — fully stateless, all state (PolicyReports) lives in the Kubernetes API.
- **Depended on by:** in principle every pod-creating workload in the cluster, since the admission webhooks match `Pod` cluster-wide — but `features.forceFailurePolicyIgnore.enabled: true` makes every webhook fail open, so nothing actually breaks if Kyverno is down. Referenced by name in `docs/apps/fluent-bit.md`, `docs/apps/nextcloud-mcp.md`, and `docs/apps/flux-operator.md`, each pointing at a specific policy exemption or allow-list entry described below.

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/security/kyverno/ks.yaml` | Two Flux Kustomizations: `kyverno` (chart, targets `./app`) and `kyverno-policies` (`dependsOn: kyverno`, targets `./policies`, `wait: false` because "ClusterPolicy CRD health status not consistently recognized by Flux") |
| `kubernetes/apps/security/kyverno/app/helmrelease.yaml` | Chart version, per-controller resources/replicas, control-plane node-affinity exclusion, `forceFailurePolicyIgnore` |
| `kubernetes/apps/security/kyverno/app/helmrepository.yaml` | Upstream Helm repo |
| `kubernetes/apps/security/kyverno/app/namespace.yaml` | `kyverno` namespace, prune disabled |
| `kubernetes/apps/security/kyverno/app/ciliumnetworkpolicy.yaml` | Two CiliumNetworkPolicies: `kyverno-admission-controller` and `kyverno-controllers` |
| `kubernetes/apps/security/kyverno/policies/kustomization.yaml` | Lists all 11 `ClusterPolicy` files + 1 `ClusterRole` deployed by the `kyverno-policies` Kustomization |
| `kubernetes/apps/security/kyverno/policies/clusterrole-cnp-reader.yaml` | Grants Kyverno's controllers `get`/`list` on `ciliumnetworkpolicies`, aggregated via `rbac.kyverno.io/aggregate-to-*` labels |

## Secrets
None. `kubernetes/apps/security/kyverno/app/` has no `externalsecret*.yaml` — Kyverno needs no credentials of its own; its webhook TLS certs are self-managed by the chart.

## Routing & access
No HTTPRoute — internal-only. Two CiliumNetworkPolicies:
- **`kyverno-admission-controller`**: ingress from the reserved `kube-apiserver` Cilium entity on `9443` (the actual webhook calls) and from `monitoring/prometheus` on `8000` (metrics scrape); egress to `kube-system/kube-dns` and back to the `kube-apiserver` entity. An inline comment notes this uses the `kube-apiserver` entity specifically because it "correctly covers Talos' KubePrism-proxied API access (127.0.0.1:7445) too, not just direct hostNetwork traffic."
- **`kyverno-controllers`**: same DNS/apiserver egress; ingress from Prometheus on `8000`, plus `kube-apiserver` on `9443` because the cleanup-controller also runs its own validating webhook.

No OIDC/SSO — Kyverno has no UI.

## Storage
None. Stateless; findings live as `PolicyReport`/`ClusterPolicyReport` custom resources in the API server, not on disk.

## Policy set (`kubernetes/apps/security/kyverno/policies/`)
12 `ClusterPolicy` objects across 11 files, all mapped to CIS Kubernetes Benchmark v2.0.1 section 5 controls, **all `validationFailureAction: Audit`** except the one mutate rule:

| ClusterPolicy | File | CIS control(s) | Mode | Checks |
| --- | --- | --- | --- | --- |
| `pod-security-baseline` | `clusterpolicy-pod-security.yaml` | 5.2.2–5.2.5 | Audit | Native Kyverno `podSecurity` validation, PSS `baseline` profile |
| `pod-security-restricted` | `clusterpolicy-pod-security.yaml` | 5.2.6–5.2.12 | Audit | Native `podSecurity`, PSS `restricted` profile |
| `add-default-seccomp` | `clusterpolicy-seccomp-default.yaml` | 5.6.2 / 4.2.14 | **Mutate** | Patches in `seccompProfile: RuntimeDefault` only where the pod doesn't already set one |
| `restrict-cluster-admin-binding` | `clusterpolicy-rbac-cluster-admin.yaml` | 5.1.1 | Audit | Denies any `ClusterRoleBinding` to `cluster-admin` whose subjects aren't `kustomize-controller`/`helm-controller`/`flux-operator` |
| `restrict-rbac-least-privilege` | `clusterpolicy-rbac-least-privilege.yaml` | 5.1.2–5.1.4, 5.1.8–5.1.13 | Audit | No wildcard resources/verbs/apiGroups; no `bind`/`impersonate`/`escalate`; no sensitive resource+verb combos |
| `restrict-default-serviceaccount` | `clusterpolicy-default-serviceaccount.yaml` | 5.1.5 / 5.1.6 | Audit | Flags a Pod using the `default` ServiceAccount without explicitly setting `automountServiceAccountToken: false` |
| `restrict-token-automount` | `clusterpolicy-token-automount.yaml` | 5.1.6 | Audit | Broader superset — flags *any* Pod (any ServiceAccount) that leaves the token automounted |
| `require-network-policy` | `clusterpolicy-network-policy-required.yaml` | 5.3.2 | Audit | Counts `CiliumNetworkPolicy` objects per namespace on Pod creation; denies if zero |
| `prefer-secrets-as-files` | `clusterpolicy-secrets-as-files.yaml` | 5.4.1 | Audit | Denies Pods consuming Secrets via `env.valueFrom.secretKeyRef`/`envFrom.secretRef` instead of a mounted volume |
| `restrict-image-registries` | `clusterpolicy-image-provenance.yaml` | 5.5.1 | Audit | Heuristic allow-list (Docker Hub, `ghcr.io`, `quay.io`, `registry.k8s.io`, `gcr.io`, `lscr.io`) as a substitute for `ImagePolicyWebhook` |
| `restrict-default-namespace` | `clusterpolicy-default-namespace.yaml` | 5.6.3 / 5.6.4 | Audit | Denies workloads in the `default` namespace |
| `require-security-context` | `clusterpolicy-security-context-required.yaml` | 5.6.2 | Audit | Literal check for the presence of a `securityContext` block |

**Common namespace exemption set:** `pod-security-baseline`, `pod-security-restricted`, `add-default-seccomp`, `restrict-default-serviceaccount`, and `restrict-token-automount` all exclude the same five namespaces — `kube-system`, `falco`, `kubescape`, `monitoring`, `logging`. Per the comment in `clusterpolicy-pod-security.yaml`, this was "verified via live pod scan to be exactly the namespaces that legitimately run privileged/hostNetwork/hostPID/hostPath workloads."

## Known quirks
- **Fails open cluster-wide by design.** `features.forceFailurePolicyIgnore.enabled: true` means an unreachable webhook never blocks resource creation.
- **Kept off control-plane nodes entirely.** All four controllers use `nodeAffinity` excluding `node-role.kubernetes.io/control-plane`. The inline comment states this is because CP nodes have "basically no headroom (confirmed live: the Talos OOM controller was killing cgroups on a CP node during rollout)" — fixed in commit `99800b4`, same pattern as Falco.
- **`require-network-policy` needed extra RBAC to avoid permission-denied errors.** Its `context.apiCall` to `ciliumnetworkpolicies` failed until `clusterrole-cnp-reader.yaml` was added, aggregated onto all three controller ClusterRoles — fixed in commit `ed552dc`.
- **`restrict-default-serviceaccount`/`restrict-token-automount` needed an autogen opt-out.** Both carry `pod-policies.kyverno.io/autogen-controllers: none` because without it, Kyverno's autogen rewrites the rule for Deployments/Jobs/CronJobs too, and the JMESPath deny-condition then errors on the absent nested field — also fixed in `ed552dc`.
- **The webhook CiliumNetworkPolicy relies on the `kube-apiserver` reserved entity**, not `host`/`remote-node`, specifically because it covers Talos' KubePrism-proxied API access too.
- **`kyverno-policies` Kustomization runs with `wait: false`**, unlike almost every other Kustomization in this repo, because "ClusterPolicy CRD health status not consistently recognized by Flux."
- **`prefer-secrets-as-files` (CIS 5.4.1) fires on nextcloud-mcp on every rollout** — its container consumes a Secret via `envFrom.secretRef` rather than a mounted volume. Non-blocking (Audit).
- **`restrict-cluster-admin-binding`'s allow-list is Flux's three controller ServiceAccounts** — the Talos break-glass admin cert (`system:masters`) is intentionally *not* covered, since it's a client cert, not a `ClusterRoleBinding` object Kyverno can see.
- **Whether/when `pod-security-baseline` moves from Audit to Enforce is an open, deliberately deferred decision** — *sourced from operator memory, not a file*: as of a 2026-07-11 review it was reportedly down to 9 legitimate infra-only failures and effectively enforce-ready, but the operator chose to keep it at Audit. This isn't written into the repo anywhere Kyverno-side — treat it as recollection, not a citable fact.

## Common operations
- Upgrade chart version: edit `kubernetes/apps/security/kyverno/app/helmrelease.yaml`, commit, push, Flux reconciles within `interval: 1h` (or force with `flux reconcile helmrelease kyverno -n kyverno`).
- Add/change a policy: edit or add a file under `kubernetes/apps/security/kyverno/policies/`, add it to `policies/kustomization.yaml`, commit, push (or `flux reconcile kustomization kyverno-policies -n security`).
- Check current audit results: `kubectl get clusterpolicyreport,policyreport -A`.
- Pause reconciliation: `flux suspend kustomization kyverno -n security` (chart) / `flux suspend kustomization kyverno-policies -n security` (policies) / `flux suspend helmrelease kyverno -n kyverno`.

## TODOs / unknowns
- No `PolicyException` resources exist yet — flipping any policy to `Enforce` will need a formal exception mechanism for legitimate exclusion cases currently handled only by namespace-level `exclude` blocks.
- Full image signing (`verifyImages` + cosign) is explicitly out of scope of `restrict-image-registries`, noted as "a bigger follow-on item requiring a signing/attestation pipeline."
- Whether any policy will move to `Enforce`, and on what timeline, is unresolved — verify current state with `kubectl get clusterpolicyreport -A` rather than assuming Audit-only is still accurate by the time this doc is read.

---
_Cite every non-obvious claim with a repo-root-relative file path (e.g. `kubernetes/apps/security/authentik/app/helmrelease.yaml`), not a bare filename — this doc lives under `docs/apps/`, so relative paths must resolve from there._
