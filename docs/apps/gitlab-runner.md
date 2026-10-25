# GitLab Runner

> **Namespace**  `gitlab-runner`
> **Source**     HelmRepository `https://charts.gitlab.io`, chart `gitlab-runner` v0.94.1 (`kubernetes/apps/gitlab-runner/gitlab-runner/app/helmrepository.yaml`, `helmrelease.yaml`), images `docker.io/gitlab/gitlab-runner:alpine-v19.5.1` and helper `docker.io/gitlab/gitlab-runner-helper:x86_64-v19.5.1`
> **Hostname**   none (outbound only)

## What it does here
Instance runner for [GitLab](gitlab.md) with the Kubernetes executor: the manager pod polls GitLab and starts one pod per CI job in the same namespace. Meant for IaC lint/validate and compliance-as-code jobs, not for building images (no privileged mode, no Docker-in-Docker).

## Architecture at a glance
- **Depends on:** GitLab (Flux `dependsOn: gitlab`), ExternalSecret → 1Password item `gitlab`, field `GITLAB_RUNNER_TOKEN`.
- **Depended on by:** every pipeline on the instance.

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/gitlab-runner/gitlab-runner/app/helmrelease.yaml` | Chart values, `config.toml` template for the executor, least-privilege RBAC rules |
| `kubernetes/apps/gitlab-runner/gitlab-runner/app/externalsecret.yaml` | Runner authentication token as `runner-token`, empty `runner-registration-token` |
| `kubernetes/apps/gitlab-runner/gitlab-runner/app/ciliumnetworkpolicy.yaml` | One policy for the manager, one for the job pods |

## Secrets
| ExternalSecret | 1Password item → field | Consumer |
| --- | --- | --- |
| `gitlab-runner` | `gitlab` → `GITLAB_RUNNER_TOKEN` | Secret `gitlab-runner-secret`, read by the chart (`runners.secret`) |

## Routing & access
- The runner talks to GitLab over the in-cluster Service (`http://gitlab.gitlab.svc.cluster.local`), both for polling (`gitlabUrl`) and for cloning (`clone_url`). The public hostname resolves to envoy-internal, which job pods may not reach.
- **Manager policy:** DNS, GitLab :8181 (post-DNAT container port), kube-apiserver. No ingress.
- **Job pod policy** (label `app.kubernetes.io/name: gitlab-runner-job`, set in `config.toml`): DNS, GitLab :8181, and TCP 80/443 to the internet except RFC 1918. No ingress, no route to the LAN or to cluster services. Widen it per use case, for example a compliance job that has to query the Kubernetes or Proxmox API.
- Job containers run with `privileged = false`, `allow_privilege_escalation = false`, `drop: ALL`, no service-account token.

## Storage
None. No cache or artifact store is configured; artifacts go to GitLab.

## Known quirks
- **`unregisterRunners: false` on purpose.** With an authentication token the chart's default would delete the runner in GitLab on every pod shutdown.
- **Job images and registries:** the chart defaults to `registry.gitlab.com`; both runner and helper image are pinned to Docker Hub to stay inside `restrict-image-registries`. Pipelines using other registries show up as audit findings.
- **Keep the runner on the same minor version as GitLab** (`19.5.x`); bump `helmrelease.yaml` (chart and both image tags) together with the GitLab image.
- Verified on a trial instance with these exact values: runner registers, a job pod starts, clones via the Service and finishes successfully.

## Common operations
- **Create the runner:** in GitLab, Admin → CI/CD → Runners → New instance runner, copy the `glrt-…` token into the 1Password item `gitlab` (`GITLAB_RUNNER_TOKEN`). Until then the ExternalSecret fails and the Flux Kustomization `gitlab-runner` stays not-ready; nothing else is affected.
- **Upgrade chart version:** edit `helmrelease.yaml`, commit, push, Flux reconciles (or `flux reconcile helmrelease gitlab-runner -n gitlab-runner`).
- **Rotate the token:** reset it in GitLab, update the 1Password field, then `kubectl annotate externalsecret gitlab-runner -n gitlab-runner force-sync=$(date +%s)` and restart the runner Deployment.
- **Pause reconciliation:** `flux suspend kustomization gitlab-runner -n gitlab-runner` / `flux suspend helmrelease gitlab-runner -n gitlab-runner`.

## TODOs / unknowns
- `concurrent = 3` and the job pod limits (`memory_limit = 2Gi`) are first guesses for a single user.
- The runner manager has no `kubectl`-visible readiness beyond the process; there is no alert on a dead runner.
