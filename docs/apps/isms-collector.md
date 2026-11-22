# isms-collector

> **Namespace**  security
> **Source**     Plain manifests (CronJob + RBAC)
> **Hostname**   None (internal job only)

## What it does here
Harvests compliance evidence from the cluster into a Git repository on a daily schedule. Reads cluster state (pods, policies, Kyverno reports, Trivy scans, backup schedules, Flux resources), queries Prometheus for log delivery metrics, normalizes the snapshot into structured files, and commits the result to the ISMS compliance-as-code repository in GitLab. The assessment pipeline then runs against that committed snapshot without needing cluster access itself, which keeps the CI runner's network policy locked down and makes assessments reproducible months later.

## Architecture at a glance
- **Depends on:** Kubernetes API (read-only, cluster-wide), Prometheus (monitoring namespace), GitLab (gitlab namespace, HTTP push over ClusterIP Service)
- **Depended on by:** The ISMS compliance-as-code pipeline (indirectly — the pipeline reads the committed evidence, but does not call this job)

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/security/isms-collector/ks.yaml` | Flux Kustomization; explicitly disables `postBuild` substitution to avoid Flux mistaking the CronJob's shell `${GIT_TOKEN}` for a Flux variable |
| `kubernetes/apps/security/isms-collector/app/cronjob.yaml` | Daily 04:00 Europe/Berlin harvest job; runs `scripts/harvest.py` from the ISMS repo, commits and pushes the snapshot |
| `kubernetes/apps/security/isms-collector/app/rbac.yaml` | ServiceAccount + ClusterRole (enumerated read-only access to ~20 resource types) + ClusterRoleBinding |
| `kubernetes/apps/security/isms-collector/app/secret.sops.yaml` | SOPS-encrypted GitLab project access token for pushing commits |
| `kubernetes/apps/security/isms-collector/app/ciliumnetworkpolicy.yaml` | Egress-only: kube-apiserver, Prometheus (monitoring), GitLab Workhorse (gitlab), api.github.com (for open update PRs), DNS |

## Secrets
| Secret | 1Password item/field | Consumer |
| --- | --- | --- |
| `isms-collector-git` | (SOPS-encrypted in-repo, not 1Password) | CronJob container env `GIT_TOKEN`, used in the Git remote URL for HTTP push to GitLab |

## Routing & access
- **No ingress.** This is a CronJob that runs internally and pushes outbound.
- **Egress** (from `kubernetes/apps/security/isms-collector/app/ciliumnetworkpolicy.yaml`):
  - Kube-apiserver (reading cluster state)
  - `kube-prometheus-stack-prometheus.monitoring:9090` (Fluent Bit delivery metrics for DET.3.1)
  - `gitlab.gitlab:8181` (Workhorse container port, post-DNAT; pushes the snapshot commit)
  - `api.github.com:443` (checks for open update PRs in the public platform repo, backing KONF.8.1/8.2)
  - DNS (kube-system/kube-dns)
- **DNS quirk:** `dnsConfig.options.ndots=1` instead of the Kubernetes default of 5, because Cilium's FQDN-based policy learns the name from the first query and `api.github.com` (two dots) would otherwise be tried as `api.github.com.security.svc.cluster.local` first, causing the toFQDNs rule to never match. Confirmed via `cilium-dbg fqdn cache list` during debugging. Harmless here because the collector only uses fully qualified service names internally.

## Storage
None. The job clones the ISMS repo into an emptyDir (`/work`), writes the snapshot, commits, pushes, and exits. No persistent state.

## Known quirks
1. **Why a CronJob here rather than a CI pipeline step:** The GitLab runner's job pods are deliberately cut off from the Kubernetes API (`kubernetes/apps/gitlab-runner/gitlab-runner/app/ciliumnetworkpolicy.yaml`). Handing CI a cluster-wide read token would concentrate privilege in the place that executes arbitrary pipeline code. This job holds the read access instead, writes a normalized snapshot, and the pipeline assesses the committed snapshot without touching the cluster. That also makes an assessment reproducible months later. (Source: inline comment in `kubernetes/apps/security/isms-collector/app/cronjob.yaml`)

2. **Flux `postBuild` substitution explicitly disabled:** The Kustomization has no `postBuild` block on purpose. Flux runs `envsubst` in strict mode across the whole manifest set and would mistake the CronJob's shell script variable `${GIT_TOKEN}` for a Flux variable, aborting reconciliation. If substitution is ever needed here, shell variables must be written as `$${VAR}` (the same way `proxmox-ansible` and `gatus` do it). (Source: inline comment in `kubernetes/apps/security/isms-collector/ks.yaml`)

3. **Partial snapshots are committed:** The harvester script tolerates individual sources failing (e.g., Prometheus unreachable) and records them as unavailable. The assessment then reports those dependent checks as `error`. A partial snapshot is still worth committing; silence is not. The CronJob logs `WARNING: at least one source failed` but does not exit non-zero. (Source: inline comment in `kubernetes/apps/security/isms-collector/app/cronjob.yaml`)

4. **RBAC is enumerated, not wildcarded:** The ClusterRole lists ~20 resource types explicitly rather than using `*` in `resources` or `verbs`, because the cluster's own `restrict-rbac-least-privilege` ClusterPolicy rejects wildcards. `secrets` is absent on purpose — nothing here needs them, and reading them would trip the policy's `no-sensitive-resource-access` rule. ConfigMaps are restricted by `resourceNames` to the two the harvester actually parses (`loki` and `authentik-blueprints`). (Source: inline comment in `kubernetes/apps/security/isms-collector/app/rbac.yaml`)

5. **HTTP push to GitLab's ClusterIP, not the public hostname:** The CronJob pushes to `http://isms-collector:${GIT_TOKEN}@gitlab.gitlab.svc.cluster.local/...` rather than `https://gitlab.cisotop.de/...`, because the public hostname resolves to `envoy-internal`, which the `security` namespace has no route to. Same path the CI runner uses for cloning. (Source: inline comment in `kubernetes/apps/security/isms-collector/app/cronjob.yaml`)

## Common operations
- **Trigger an immediate run:** `kubectl create job --from=cronjob/isms-collector isms-collector-manual -n security`
- **Check last run status:** `kubectl get jobs -n security -l app.kubernetes.io/name=isms-collector --sort-by=.metadata.creationTimestamp`
- **View logs from last run:** `kubectl logs -n security -l app.kubernetes.io/name=isms-collector --tail=-1`
- **Rotate the Git token:** Update `kubernetes/apps/security/isms-collector/app/secret.sops.yaml` with `sops`, commit, push. The next CronJob run will pick up the new token. (No ExternalSecret here — the token is SOPS-encrypted in-repo.)
- **Pause the schedule:** `kubectl patch cronjob isms-collector -n security -p '{"spec":{"suspend":true}}'`
- **Resume:** `kubectl patch cronjob isms-collector -n security -p '{"spec":{"suspend":false}}'`

## TODOs / unknowns
- The SOPS-encrypted `token` in `secret.sops.yaml` is not sourced from 1Password (unlike most secrets in this cluster). The rotation procedure and where the canonical token is stored (GitLab project settings? 1Password item not yet wired up?) is not documented in the manifests.
- The `scripts/harvest.py` script itself lives in the ISMS repository (`compliance-as-code.git`), not in this repo. What resources it actually queries and how it normalizes them is not visible here — only the RBAC grants and the CiliumNetworkPolicy egress rules hint at the scope.

---
**Secret/IP scan:** Clean. The SOPS-encrypted token value is not resolved. The GitLab Service DNS name (`gitlab.gitlab.svc.cluster.local`) and Prometheus Service DNS name are internal and meaningless outside this cluster. No public IPs, no 1Password vault identifiers.

---
_All file paths are relative to the repository root (e.g., `kubernetes/apps/security/isms-collector/app/cronjob.yaml`)._
