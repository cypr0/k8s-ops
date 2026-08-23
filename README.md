# k8s-ops

A single Kubernetes cluster, homelab-run, deployed with [Talos Linux](https://github.com/siderolabs/talos) and managed entirely through GitOps with [Flux](https://github.com/fluxcd/flux2). Everything the cluster runs — infrastructure, security tooling, and applications — is declared in this repo and reconciled automatically; nothing is applied by hand.

Originally scaffolded from [onedr0p/cluster-template](https://github.com/onedr0p/cluster-template) (credit where due — see [Acknowledgements](#-acknowledgements)), it has since grown into its own thing: 8 nodes, ~55 applications, and an in-repo documentation system (see [Documentation](#-documentation) below) covering every app plus every notable incident.

## 🧱 Stack

| Layer | Component |
| --- | --- |
| OS | [Talos Linux](https://github.com/siderolabs/talos) — immutable, API-managed, no SSH |
| GitOps | [Flux](https://github.com/fluxcd/flux2) |
| CNI | [Cilium](https://github.com/cilium/cilium) (kube-proxy replacement, native routing) |
| DNS | [CoreDNS](https://github.com/coredns/coredns) (cluster), [k8s_gateway](https://github.com/k8s-gateway/k8s_gateway) (home split-DNS) |
| Ingress | [Envoy Gateway](https://github.com/envoyproxy/gateway) (Gateway API) + [Cloudflare Tunnel](https://github.com/cloudflare/cloudflared) |
| Secrets | [SOPS](https://github.com/getsops/sops) (infra bootstrap secrets) + [External Secrets](https://external-secrets.io/) backed by 1Password Connect (app credentials) |
| Storage | [csi-driver-nfs](https://github.com/kubernetes-csi/csi-driver-nfs) (ZFS-backed NFS) |
| Databases | [CloudNativePG](https://cloudnative-pg.io/) (Postgres), [Dragonfly](https://github.com/dragonflydb/dragonfly) (Redis-compatible cache) |
| Backups | [Velero](https://velero.io/) + Kopia, off-site to S3 |
| Security | [Kyverno](https://kyverno.io/), [Falco](https://falco.org/), [Trivy Operator](https://github.com/aquasecurity/trivy-operator) |
| Observability | [kube-prometheus-stack](https://github.com/prometheus-operator/kube-prometheus-stack), [Grafana](https://grafana.com/), [Loki](https://grafana.com/oss/loki/), [OpenSearch](https://opensearch.org/), [Gatus](https://gatus.io/) |
| Identity | [Authentik](https://goauthentik.io/) — OIDC SSO in front of every user-facing app |
| Mail | [Stalwart](https://stalw.art/) — SMTP/IMAP/JMAP mail + CalDAV/CardDAV, own public LoadBalancer IP (raw TCP mail protocols bypass Envoy/Cloudflare Tunnel) |
| Dependency automation | [Renovate](https://www.mend.io/renovate) |

## 🖥️ Topology

3 control-plane + 5 worker nodes, all Talos, all capable of running workloads (control-plane nodes are tainted against non-critical scheduling — see `talos/patches/controller/cluster.yaml`).

## 📁 Repository layout

```
talos/        Talos machine config (talhelper), per-node patches
kubernetes/   Flux-managed cluster state — apps/, components/, bootstrap Kustomizations
docs/         Per-app documentation + incident postmortems (see below)
bootstrap/    One-time cluster-bootstrap secrets/manifests
```

## 📚 Documentation

This repo maintains its own operational documentation under [`docs/`](docs/), written and updated as an ongoing, human-reviewed campaign (contract at [`docs/prompts/app-docs-campaign.md`](docs/prompts/app-docs-campaign.md)):

- [`docs/apps/`](docs/apps/) — one file per application: what it does *in this cluster*, its dependencies, secrets (structure only, never values), routing, storage, and known operational quirks.
- [`docs/incidents/`](docs/incidents/) — postmortems for notable incidents: root cause, timeline, what was tried, the fix, and a runbook for next time.

Every doc is required to ground its claims in an actual file path in this repo, and every doc (create or update) passes a mandatory secret/sensitive-info gate before being committed — see `docs/prompts/app-docs-campaign.md` §3.

## 🚀 Rebuilding / bootstrapping

Tooling is pinned via [mise](https://mise.jdx.dev/) (`.mise.toml`) — `mise install` gets every CLI (`talosctl`, `talhelper`, `flux2`, `helm`, `sops`, etc.) at the exact versions this repo expects.

```sh
just init          # generate cluster.toml, age key, deploy key from samples
just configure      # render kubernetes/ + talos/ config from cluster.toml
just talos bootstrap # install Talos across all nodes
just kube bootstrap  # install cilium/coredns/flux, sync to repo state
```

Talos node config changes go through `talhelper`:

```sh
just talos generate-config
just talos apply-node <ip>       # apply without reboot where possible
just talos upgrade-node <ip>     # Talos OS version bump (reboot)
just talos upgrade-k8s           # Kubernetes version bump
```

## 🐛 Debugging

```sh
flux get sources git -A && flux get ks -A && flux get hr -A   # reconciliation state
kubectl -n <ns> get pods -o wide                              # is it scheduled/running
kubectl -n <ns> logs <pod> -f                                 # what's it saying
kubectl -n <ns> describe <resource> <name>                    # events + conditions
```

If the app has a doc under `docs/apps/`, check its "Known quirks" section first — several past incidents have exact reproduction/runbook steps already written down.

## 🙏 Acknowledgements

Scaffolded from [onedr0p/cluster-template](https://github.com/onedr0p/cluster-template) — the Talos/Flux/SOPS bootstrap tooling (`justfile`, `talhelper` integration, `mise`-pinned CLIs) is still substantially theirs. If you're starting a similar cluster from scratch, that template is the right place to begin, not this repo.
