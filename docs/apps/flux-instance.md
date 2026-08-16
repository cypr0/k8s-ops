# Flux Instance

> **Namespace**  flux-system
> **Source**     `flux-instance` OCIRepository, chart `flux-instance` v0.57.0, `oci://ghcr.io/controlplaneio-fluxcd/charts/flux-instance` (`kubernetes/apps/flux-system/flux-instance/app/ocirepository.yaml`)
> **Hostname**   `flux-webhook.${SECRET_DOMAIN}` — GitHub webhook receiver only; no UI/API surface of its own

## What it does here
Declares and configures the actual Flux GitOps reconciliation engine for this cluster via the `FluxInstance` custom resource (templated by this HelmRelease's `values.instance.*`), which `flux-operator` consumes to install/upgrade the four core controllers (`source-controller`, `kustomize-controller`, `helm-controller`, `notification-controller` — `kubernetes/apps/flux-system/flux-instance/app/helmrelease.yaml`). It also points those controllers at this repo's own git source (`instance.sync.url: https://github.com/cypr0/k8s-ops.git`, `ref: refs/heads/main`, `path: kubernetes/flux/cluster`) — i.e. this is the resource that makes the cluster reconcile from `k8s-ops` in the first place. Everything else in the cluster is downstream of this app being healthy.

## Architecture at a glance
- **Depends on:** the `flux-operator` Kustomization (`dependsOn: flux-operator`, `kubernetes/apps/flux-system/flux-instance/ks.yaml`), which owns the `FluxInstance` CRD (`kubernetes/apps/flux-system/flux-operator/app/ciliumnetworkpolicy.yaml:2`: "Flux Operator: manages the FluxInstance CRD, installs and upgrades Flux controllers").
- **Depended on by:** effectively every other `Kustomization`/`HelmRelease` in the repo, since they all reference `sourceRef: {kind: GitRepository, name: flux-system, namespace: flux-system}` — the `GitRepository` this instance's `sync` block creates. One explicit case: the `flux-operator-mcp` Kustomization lists `dependsOn: flux-instance` (`kubernetes/apps/flux-system/flux-operator-mcp/ks.yaml`).

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/flux-system/flux-instance/ks.yaml` | Kustomization wiring; `dependsOn: flux-operator`, `wait: false` |
| `kubernetes/apps/flux-system/flux-instance/app/ocirepository.yaml` | Pins the `flux-instance` chart to tag `0.57.0` |
| `kubernetes/apps/flux-system/flux-instance/app/helmrelease.yaml` | `FluxInstance` spec: which controllers run, the git sync target, and a long list of `kustomize.patches` tuning the controller Deployments |
| `kubernetes/apps/flux-system/flux-instance/app/secret.sops.yaml` | SOPS-encrypted GitHub webhook validation token |
| `kubernetes/apps/flux-system/flux-instance/app/httproute.yaml` | Exposes the GitHub webhook receiver externally |
| `kubernetes/apps/flux-system/flux-instance/app/receiver.yaml` | Wires the webhook to an immediate re-sync of `GitRepository`/`Kustomization` `flux-system` |
| `kubernetes/apps/flux-system/flux-instance/app/ciliumnetworkpolicy.yaml` | Network policy for all four controller pods (`app.kubernetes.io/part-of: flux`) |

## Secrets
One secret, and it's the odd one out versus most apps in this repo: `kubernetes/apps/flux-system/flux-instance/app/secret.sops.yaml` is a **SOPS-encrypted Kubernetes Secret** (`github-webhook-token-secret`, key `token`), not an `ExternalSecret`/1Password-sourced one. It's consumed by `receiver.yaml`'s `secretRef` to validate incoming GitHub webhook signatures. Decryption at the controller level is enabled by a dedicated kustomize patch in `helmrelease.yaml` ("Controller-level SOPS decryption": adds `--sops-age-secret=sops-age` to `kustomize-controller`'s args) — separate from the `cluster-apps` Kustomization's own `decryption.provider: sops` (`kubernetes/flux/cluster/ks.yaml`).

Notably, the git sync itself (`instance.sync` in `helmrelease.yaml`) has no `secretRef` — `source-controller` clones `https://github.com/cypr0/k8s-ops.git` anonymously over plain HTTPS, since it's a public repository. There is no deploy key or PAT to document here.

## Routing & access
- `httproute.yaml`: `flux-webhook.${SECRET_DOMAIN}` via the `envoy-external` Gateway (namespace `network`), `PathPrefix /hook/` → Service `webhook-receiver` (flux-system, port 80). That Service is created by the `flux-instance` chart itself (part of `notification-controller`), not by a manifest in this app directory.
- `receiver.yaml`: a `Receiver` (type `github`, events `ping`/`push`) that, once the webhook token validates, triggers reconciliation of `GitRepository` and `Kustomization` `flux-system` — i.e. a `git push` to `main` re-syncs immediately instead of waiting for the 1h `interval`.
- `ciliumnetworkpolicy.yaml` covers all four controllers via the shared `app.kubernetes.io/part-of: flux` label. Its header comments flag a specific gotcha, born out in commit history (`ba3bce2`, `eed7677`, `9ab0a76`): Cilium enforces egress policy **post-DNAT**, so intra-flux rules must allow the container ports (`9090` for source-controller, `9292` for notification-controller), not the Service ports (`80`). Ingress also allows Prometheus scraping (`8080`) and kubelet probes (`fromEntities: host`); egress allows `kube-dns`, `kube-apiserver`, intra-flux traffic, and `world:443` for pulling OCI charts/images, Git, and Helm repos.
- `instance.cluster.networkPolicy: false` in `helmrelease.yaml` disables the chart's own default NetworkPolicy generation, since this repo manages Cilium policy for flux itself via `ciliumnetworkpolicy.yaml`.

## Storage
None. Flux's state is the git repo plus in-cluster CRs — no PVC for this app. One related detail: a kustomize patch ("Enable in-memory kustomize builds") replaces `kustomize-controller`'s `temp` volume with an `emptyDir: {medium: Memory}` rather than disk-backed scratch space (`helmrelease.yaml`).

## Known quirks
- **Container-port vs. Service-port CNP gotcha** — see Routing & access above; this cost two follow-up fix commits (`eed7677`, `9ab0a76`) before `ba3bce2` widened kubelet-probe ingress cluster-wide. Anyone touching `ciliumnetworkpolicy.yaml` for this app should re-read its inline `NOTE:` comment first.
- **The `helmrelease.yaml` `kustomize.patches` block is the actual control surface for controller tuning** — concurrency (`--concurrent=10`/`20`), memory limits (`1Gi` for all three non-notification controllers), Helm chart-cache sizing/TTL, an OOM-watch feature gate on `helm-controller` (`--oom-watch-memory-threshold=95`), `DisableChartDigestTracking`, and `CancelHealthCheckOnNewRevision`. There's no separate config file — changes to any of these go directly into this one YAML block, matched by regex against `(kustomize-controller|helm-controller|source-controller)` Deployment names.
- **This app's `ks.yaml` uses `wait: false`**, unlike `flux-operator`'s (`wait: true`) — a deliberate ordering choice given the chicken-and-egg nature of Flux managing its own controllers, but not otherwise explained in-repo.

## Common operations
- Upgrade the Flux controller stack: bump `spec.ref.tag` in `app/ocirepository.yaml` (currently `0.57.0`), commit. `OCIRepository` re-fetches within its 15m `interval`; `HelmRelease` reconciles within its 1h `interval`, or force with `flux reconcile helmrelease flux-instance -n flux-system`.
- Change which controllers run, or the sync target: edit `instance.components` / `instance.sync` in `helmrelease.yaml`.
- Force an immediate re-sync from git without waiting on the webhook or interval: `flux reconcile source git flux-system -n flux-system && flux reconcile kustomization flux-system -n flux-system`.
- Rotate the GitHub webhook validation token: re-encrypt a new value into `app/secret.sops.yaml` with `sops`, commit.
- Pause reconciliation: `flux suspend kustomization flux-instance -n flux-system` / `flux suspend helmrelease flux-instance -n flux-system` — note this has a materially larger blast radius than pausing a regular app, since it can stop the mechanism that would otherwise auto-heal or roll back other suspensions.

## TODOs / unknowns
- Whether flux controller metrics (port `8080`, allowed inbound by `ciliumnetworkpolicy.yaml` from `monitoring`/`prometheus`) are actually being scraped — no `ServiceMonitor`/`PodMonitor` for flux was found under `kubernetes/apps/monitoring/`. Not verified either way from the repo.
- Why `ks.yaml` sets `wait: false` specifically for this Kustomization (noted above) isn't explained by any commit message or inline comment found — worth asking the operator directly if it ever needs changing.
- No incident postmortem currently references `flux-instance` itself; the CNP-related fixes above were resolved as regular commits, not documented incidents.

---
_Cite every non-obvious claim with a repo-root-relative file path (e.g. `kubernetes/apps/security/authentik/app/helmrelease.yaml`), not a bare filename — this doc lives under `docs/apps/`, so relative paths must resolve from there._
