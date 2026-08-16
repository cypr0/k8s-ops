# Spegel

> **Namespace**  kube-system
> **Source**     `spegel` OCIRepository, chart `spegel` v0.7.4 (`kubernetes/apps/kube-system/spegel/app/ocirepository.yaml`, `kubernetes/apps/kube-system/spegel/app/helmrelease.yaml`)
> **Hostname**   none — cluster-internal only, no HTTPRoute

## What it does here
A stateless peer-to-peer OCI image mirror: one DaemonSet pod per node (confirmed live — `kubectl get daemonset -n kube-system spegel` shows 8/8, one per node) that hooks into each node's containerd via a mirror-config directory and a local registry endpoint, so a node pulling an image layer another node already has gets it from that peer instead of hitting the upstream registry again. It exists to cut redundant pulls against upstream registry rate limits/bandwidth during rolling deploys, node replacements, or the 5-worker fleet all pulling the same image ("P2P image distribution — nodes share layers to avoid repeated registry pulls").

## Architecture at a glance
- **Depends on:** CoreDNS — bootstrap ordering installs `kube-system/spegel` only after `kube-system/coredns` (`bootstrap/helmfile/apps.yaml:29-34`), and its CiliumNetworkPolicy egress explicitly allows DNS to `k8s-app: kube-dns` for resolving upstream registries on cache miss. No ExternalSecret, no database, no cache backend — genuinely stateless besides a per-node hostPath cache.
- **Depended on by:** everything that pulls container images, transitively — `cert-manager` is bootstrapped right after it (`needs: ['kube-system/spegel']`, `bootstrap/helmfile/apps.yaml:36-41`), i.e. it's meant to be up before the rest of the bootstrap chain starts pulling images. Nothing breaks outright if it's down (containerd falls back to the upstream registry per mirror semantics), it just loses the peer-cache benefit and puts more load on upstream registries.

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/kube-system/spegel/app/ocirepository.yaml` | Chart source: `oci://ghcr.io/spegel-org/helm-charts/spegel`, tag `0.7.4` |
| `kubernetes/apps/kube-system/spegel/app/helmrelease.yaml` | containerd integration paths, registry hostPort, resources, security contexts |
| `kubernetes/apps/kube-system/spegel/app/ciliumnetworkpolicy.yaml` | P2P/host/metrics traffic rules |
| `kubernetes/apps/kube-system/spegel/app/kustomization.yaml` | Wires the three resources above |
| `kubernetes/apps/kube-system/spegel/ks.yaml` | Flux Kustomization, 1h reconcile interval, `targetNamespace: kube-system` |

## Secrets
None — no `externalsecret*.yaml` in `kubernetes/apps/kube-system/spegel/app/`.

## Routing & access
- Not exposed via Gateway/HTTPRoute — internal-only, node-local by design.
- `CiliumNetworkPolicy`:
  - Ingress: peer spegel pods on ports 5000 (registry) and 5001 (P2P router); `fromEntities: host` on port 5000 only (this is how the node's own containerd, via the `hostPort` binding, reaches the local registry); `monitoring` namespace's Prometheus on port 9090 (metrics).
  - Egress: DNS to `kube-dns` (53/UDP+TCP); peer spegel pods on 5000/5001; `toEntities: world` on 443 (upstream registry pulls when no peer has the layer).
- Chart's Pod Security Standards needs (hostPath mounts, hostPort) are why `kube-system` is excluded wholesale from the Kyverno baseline PSS audit policy, spegel named explicitly as one of the reasons.

## Storage
- No PVC. Chart mounts host paths directly on every node (confirmed live via `kubectl get daemonset -n kube-system spegel -o jsonpath='{.spec.template.spec.volumes}'`): `/var/lib/spegel` (P2P identity persistence across pod restarts — chart default, not overridden in the repo), `/run/containerd/containerd.sock`, `/var/lib/containerd/io.containerd.content.v1.content` (containerd's own content store, read for serving cached layers to peers), and `/etc/cri/conf.d/hosts` (mirror config the chart writes for containerd to pick up — see Known quirks).
- Not in Velero/Kopia scope, and shouldn't need to be: it's a rebuildable local cache, not source-of-truth data.

## Known quirks
- **`containerSecurityContext` value is silently dropped — the key is wrong.** `helmrelease.yaml` sets `containerSecurityContext: {allowPrivilegeEscalation: false, capabilities: {drop: ["ALL"]}}`, but chart 0.7.4's `templates/daemonset.yaml` reads `.Values.securityContext` for the container, not `.Values.containerSecurityContext` (verified against the pulled chart template, and against the live DaemonSet: `kubectl get daemonset -n kube-system spegel -o jsonpath='{.spec.template.spec.containers[0].securityContext}'` returns only `{"readOnlyRootFilesystem":true}` — the chart's own default, with none of the values from `helmrelease.yaml` applied). `podSecurityContext` (pod-level `seccompProfile`) uses the correct key and *is* applied. Net effect: not a live security gap on its own, but the intended `allowPrivilegeEscalation: false` / capability drop is currently a no-op. Not fixed here per campaign rule — surfaced for a follow-up: rename to `securityContext` in `helmrelease.yaml`.
- **`containerdRegistryConfigPath` is overridden for Talos.** Chart default is `/etc/containerd/certs.d`; the repo sets `/etc/cri/conf.d/hosts`, matching Talos's CRI plugin config layout (`talos/patches/global/machine-files.yaml` writes into `/etc/cri/conf.d/`) rather than upstream containerd's default cert.d path. Without this override the mirror config Spegel writes would land somewhere Talos's containerd never reads, and the whole mechanism would silently no-op.
- **`hostPort` overridden from the chart default (30020) to 29999**. Reason for that specific value isn't recorded anywhere in the repo — see TODOs.
- DaemonSet, not a Deployment (chart-inherent, confirmed live) — not a candidate for HPA/VPA scaling knobs; one pod per node is the whole point of the P2P design.

## Common operations
- Upgrade chart version: bump `spec.ref.tag` in `kubernetes/apps/kube-system/spegel/app/ocirepository.yaml`, commit, push; Flux re-pulls within the 15m `OCIRepository` interval and the 1h `HelmRelease` interval, or force with `flux reconcile helmrelease spegel -n kube-system`.
- Pause reconciliation: `flux suspend kustomization spegel -n kube-system` / `flux suspend helmrelease spegel -n kube-system`.
- Check per-node health: `kubectl get pods -n kube-system -l app.kubernetes.io/name=spegel -o wide` — one Running pod per node is the expected steady state.

## TODOs / unknowns
- Why `hostPort: 29999` specifically (vs. chart default 30020) isn't documented anywhere in the repo — no commit message or comment explains it. Possible port-conflict avoidance, but unverified; don't guess further without asking the operator.
- The `containerSecurityContext` → `securityContext` key-name bug above is unfixed as of this doc; tracked here as a pointer for whoever picks it up next.
