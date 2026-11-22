# collector

> **Namespace**  isms
> **Source**     plain manifests (no HelmRelease)
> **Hostname**   none (internal CronJob only)

## What it does here
Harvests compliance evidence from this cluster into a GitLab repository as a daily commit. Reads cluster state (Kyverno policy reports, Trivy scan results, Velero/CNPG backup schedules, Flux resources, network policies, certificates, Prometheus metrics) and writes a normalized snapshot that the ISMS pipeline then assesses without needing cluster access. This separation keeps the GitLab runner's job pods isolated from the Kubernetes API while still automating evidence collection.

## Architecture at a glance
- **Depends on:** Kubernetes API (cluster-wide read), Prometheus (monitoring namespace), GitLab (gitlab namespace, for pushing commits)
- **Depended on by:** The ISMS compliance-as-code pipeline (consumes the committed snapshots; does not directly depend on this CronJob being live, but stale evidence will cause assessment drift)

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/isms/collector/ks.yaml` | Flux Kustomization that deploys this app |
| `kubernetes/apps/isms/collector/app/cronjob.yaml` | Daily 04:00 CET job that clones the ISMS repo, runs `scripts/harvest.py`, commits, and pushes |
| `kubernetes/apps/isms/collector/app/rbac.yaml` | ServiceAccount + ClusterRole (cluster-wide read for ~15 resource types) + ClusterRoleBinding |
| `kubernetes/apps/isms/collector/app/secret.sops.yaml` | SOPS-encrypted GitLab personal access token for pushing commits |
| `kubernetes/apps/isms/collector/app/ciliumnetworkpolicy.yaml` | Egress-only: kube-apiserver, CoreDNS, Prometheus, GitLab Workhorse (port 8181), api.github.com (for open update PRs) |

## Secrets
| Secret | 1Password item/field | Consumer |
| --- | --- | --- |
| `isms-collector-git` | *(SOPS-encrypted in-repo; not sourced from 1Password ExternalSecret)* | CronJob container env var `GIT_TOKEN`, used in the git clone/push URL |

The token is SOPS-encrypted directly in `kubernetes/apps/isms/collector/app/secret.sops.yaml` rather than pulled from 1Password via ExternalSecret.

## Routing & access
- **No ingress.** This is a CronJob that initiates outbound connections only.
- **Egress policy** (`kubernetes/apps/isms/collector/app/ciliumnetworkpolicy.yaml`):
  - kube-apiserver (reading cluster inventory)
  - CoreDNS (UDP/TCP 53)
  - Prometheus at `kube-prometheus-stack-prometheus.monitoring.svc.cluster.local:9090` (for Fluent Bit delivery metrics behind compliance check DET.3.1, per inline comment)
  - GitLab Workhorse at port 8181 in the `gitlab` namespace (pushing commits; uses the in-cluster Service rather than the public hostname to avoid routing through envoy-internal, which this namespace cannot reach)
  - `api.github.com:443` (for open update pull requests behind compliance checks KONF.8.1/8.2, per inline comment; the platform repository is public so no credential needed)

## Storage
None. The CronJob uses two `emptyDir` volumes (`/work` for the git clone, `/tmp` for `$HOME`) that are ephemeral per job run.

## Known quirks
- **Why a CronJob here rather than a CI pipeline step:** The GitLab runner's job pods are deliberately denied Kubernetes API access (`kubernetes/apps/gitlab-runner/gitlab-runner/app/ciliumnetworkpolicy.yaml`). Handing CI a cluster-wide read token would concentrate privilege in the place that executes arbitrary pipeline code. This job holds the read access, writes the snapshot, and the pipeline assesses the committed snapshot without touching the cluster. This also makes assessments reproducible months later. *(Source: inline comment in `kubernetes/apps/isms/collector/app/cronjob.yaml`)*
- **Partial snapshots are committed:** The harvester script tolerates individual sources failing and records them as unavailable; the assessment then reports those dependent checks as `error`. A partial snapshot is still worth committing. *(Source: inline comment in `kubernetes/apps/isms/collector/app/cronjob.yaml`)*
- **RBAC is enumerated, not wildcarded:** The ClusterRole lists ~15 specific resource types rather than using `*` because the `restrict-rbac-least-privilege` ClusterPolicy rejects wildcards. `secrets` is deliberately absent; reading them would trip the `no-sensitive-resource-access` policy rule. ConfigMaps are restricted by `resourceNames` to the two the harvester actually parses: `loki` (retention config) and `authentik-blueprints` (MFA stage declaration). *(Source: inline comments in `kubernetes/apps/isms/collector/app/rbac.yaml`)*
- **HTTP to GitLab, not SSH:** The push goes to `http://gitlab.gitlab.svc.cluster.local` (the ClusterIP Service) rather than the public hostname `gitlab.cisotop.de`, which resolves to `envoy-internal` and is unreachable from this namespace. Same path the CI runner uses for cloning. *(Source: inline comment in `kubernetes/apps/isms/collector/app/cronjob.yaml`)*

## Common operations
- **Trigger an immediate run:** `kubectl create job --from=cronjob/isms-collector isms-collector-manual -n isms`
- **Check last run status:** `kubectl get jobs -n isms -l app.kubernetes.io/name=isms-collector --sort-by=.status.startTime`
- **View logs from last run:** `kubectl logs -n isms -l app.kubernetes.io/name=isms-collector --tail=100`
- **Rotate the GitLab token:** Update the SOPS-encrypted value in `kubernetes/apps/isms/collector/app/secret.sops.yaml`, commit, push; Flux will reconcile within 1h (or force with `flux reconcile kustomization isms-collector -n isms`).
- **Pause the CronJob:** `kubectl patch cronjob isms-collector -n isms -p '{"spec":{"suspend":true}}'` (resume with `false`).

## TODOs / unknowns
- The harvester script (`scripts/harvest.py`) is referenced but lives in the ISMS repository (`gitlab.cisotop.de/isms/compliance-as-code`), not in this k8s-ops repo, so its exact behavior and error handling are not directly verifiable here.
- The specific compliance checks (DET.3.1, KONF.8.1/8.2) mentioned in comments are defined in the ISMS repository's assessment logic, not in this cluster's manifests.

---

**Secret/IP scan:** Clean. The SOPS-encrypted token in `secret.sops.yaml` is cited but not resolved. The GitLab Service DNS name (`gitlab.gitlab.svc.cluster.local`) and Prometheus Service DNS name are internal cluster addresses. The public hostname `gitlab.cisotop.de` is already public by design (the cluster deliberately exposes it). No real public IPs, WAN addresses, or 1Password vault identifiers appear in this document.
