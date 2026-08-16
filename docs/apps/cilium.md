# Cilium

> **Namespace**  kube-system
> **Source**     `cilium` OCIRepository, chart `cilium` v1.20.0, `oci://quay.io/cilium/charts/cilium` (`kubernetes/apps/kube-system/cilium/app/ocirepository.yaml`), referenced by the HelmRelease via `chartRef` (`kubernetes/apps/kube-system/cilium/app/helmrelease.yaml`)
> **Hostname**   none — this is the cluster's CNI, not an exposed service

## What it does here
The cluster's only CNI, replacing kube-proxy entirely (`kubeProxyReplacement: true`, and Talos's own `proxy.disabled: true` in `talos/patches/controller/cluster.yaml`) and running with `cniConfig: name: none` in `talos/talconfig.yaml` so Cilium — not a Talos built-in — owns the dataplane. Runs in native routing mode with per-node pod CIDRs (`routingMode: native`, `autoDirectNodeRoutes: true`, `ipv4NativeRoutingCIDR: "10.42.0.0/16"`), reaches the apiserver at runtime via Talos KubePrism (`k8sServiceHost: 127.0.0.1`, `k8sServicePort: 7445`) rather than a direct control-plane IP, and additionally hands out LoadBalancer IPs for internal Services via its own IPAM/L2-announcement (`kubernetes/apps/kube-system/cilium/app/networks.yaml`) — all from `kubernetes/apps/kube-system/cilium/app/helmrelease.yaml`.

## Architecture at a glance
- **Depends on:** nothing in-cluster — it bootstraps before any other workload. At runtime it depends on Talos KubePrism (`127.0.0.1:7445`) to reach `kube-apiserver`, per `k8sServiceHost`/`k8sServicePort` in `kubernetes/apps/kube-system/cilium/app/helmrelease.yaml`.
- **Depended on by:** every pod and Service in the cluster — it is both the CNI and the kube-proxy replacement, and every `CiliumNetworkPolicy` in the repo is enforced by its eBPF datapath. Concretely also: `envoy-gateway` and `k8s-gateway` request their LoadBalancer IPs from the `CiliumLoadBalancerIPPool` defined here (`kubernetes/apps/kube-system/cilium/app/networks.yaml`), e.g. via `lbipam.cilium.io/ips` annotations in `kubernetes/apps/network/envoy-gateway/app/envoy.yaml`.

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/kube-system/cilium/app/helmrelease.yaml` | All chart values: kube-proxy replacement, routing mode, resources, Hubble, security context capabilities |
| `kubernetes/apps/kube-system/cilium/app/ocirepository.yaml` | Chart source/version pin (`1.20.0`) |
| `kubernetes/apps/kube-system/cilium/app/networks.yaml` | `CiliumLoadBalancerIPPool` + `CiliumL2AnnouncementPolicy` for internal LB IPs |
| `kubernetes/apps/kube-system/cilium/app/ciliumnetworkpolicy.yaml` | Self-protection policy for `hubble-relay` only (agent itself is deliberately unrestricted, see below) |
| `kubernetes/apps/kube-system/cilium/app/kustomization.yaml` | Wires the above into the Flux Kustomization |
| `kubernetes/apps/kube-system/cilium/ks.yaml` | Flux `Kustomization`, targets `kube-system`, `prune: true` |

## Secrets
None. No `ExternalSecret` exists under `kubernetes/apps/kube-system/cilium/app/` (confirmed by directory listing).

## Routing & access
- Not exposed via Gateway/HTTPRoute — this app *is* the network layer everything else routes through.
- **Hubble**: enabled with relay (`hubble.relay.enabled: true`) but UI disabled (`hubble.ui.enabled: false`); flow metrics (`dns`, `drop`, `tcp`, `flow`, `port-distribution`, `icmp`, `httpV2`) are scraped by Prometheus (`hubble.metrics.serviceMonitor.enabled: true`) — `kubernetes/apps/kube-system/cilium/app/helmrelease.yaml`.
- **`hubble-relay` CiliumNetworkPolicy** (`kubernetes/apps/kube-system/cilium/app/ciliumnetworkpolicy.yaml`): ingress allowed from `cluster` entities on TCP/4245 (CLI/UI queries), from `host` entity on TCP/4245 (kubelet probes, since those originate from the host network and aren't covered by the `cluster` entity), and from `monitoring` namespace's Prometheus on TCP/9966; egress restricted to `kube-dns` (UDP/TCP 53) and `cluster` entities on TCP/4244 (collecting flows from every node's `hubble-peer`). The comment at the top of the file is explicit that **cilium-agent itself is not policy-restricted** — restricting the eBPF dataplane that enforces policies risks deadlocking the cluster's own connectivity.
- **LoadBalancer IPs**: `CiliumLoadBalancerIPPool` (`kubernetes/apps/kube-system/cilium/app/networks.yaml`) allocates from a private RFC1918 range (`allowFirstLastIPs: "No"`), announced via a `CiliumL2AnnouncementPolicy` selecting all Linux nodes. `l2announcements.enabled: true` and `loadBalancer.mode: "snat"` / `algorithm: maglev` in `helmrelease.yaml` back this.

## Storage
None — no PVCs.

## Known quirks
- **cilium-agent ran with zero resource accounting until 2026-08-16.** `kubernetes/apps/kube-system/cilium/app/helmrelease.yaml` documents that no `resources` block existed for the agent DaemonSet before commit `7334b47` (`fix(cilium): add missing resource requests for the agent DaemonSet`), despite live usage of ~90m CPU / ~215Mi memory per node — a contributor to the control-plane memory-overcommit investigated the same day (see auto-memory `project_talos_1138_network_flapping`, not independently re-verified in this pass). Only a `requests` block was added (`cpu: 100m`, `memory: 256Mi`); a `limits` entry was deliberately *not* added — the same file's comment reasons that throttling/OOM-killing the cluster's own CNI under load is a worse failure mode than the accounting gap it fixes.
- **`bpf.hostLegacyRouting: true`** is a workaround for a specific Talos incompatibility, linked in-file to `siderolabs/talos#10002`.
- **`socketLB.hostNamespaceOnly: false`** (commit `63be357`, `fix(cilium): enable in-pod socket-LB (disable hostNamespaceOnly)`): the chart default (`true`) bypasses in-pod socket-LB and breaks a pod calling a LoadBalancer/ClusterIP that hairpins back to the same node — the file cites `dashboards -> envoy LB -> authentik` as the observed case. The default exists for Multus/KubeVirt/Istio-sidecar setups, none of which are deployed in this repo (confirmed — no Multus manifests exist anywhere under `kubernetes/`), which is why it's safe to disable cluster-wide here.
- **`loadBalancer.mode` was changed from Cilium's DSR default to `snat`** for Proxmox compatibility (commit `e014ce7`, `fix(cilium): change LB mode from DSR to SNAT for Proxmox compatibility`) — the commit message is the only record of the underlying Proxmox-specific reason; not re-derived from a live repro in this pass.
- **`cni.exclusive: false`** is commented in-file as "required for pairing with Multus CNI", but no Multus deployment exists anywhere in this repo (grep-confirmed). Likely inherited from an upstream cluster template rather than describing something actually running here — flagged in TODOs below rather than assumed either way.
- **Cilium's own Envoy and Gateway API integration are both off** (`envoy.enabled: false`, `gatewayAPI.enabled: false`) — the cluster's Gateway API implementation lives in the separate `envoy-gateway` app, not in this chart.
- **A previously-suspected single-apiserver-backend fragility does not currently reproduce.** Auto-memory `project_cilium_apiserver_single_backend` (session memory, not a repo file) recorded that in July 2026 Cilium's eBPF service map for `kubernetes.default` only ever showed one of three control-plane nodes as a backend; re-checked live on 2026-08-16 across 5 of 8 agents via `cilium-dbg service list`, all three backends were present. That same memory also established, from the Cilium 1.20.0 chart's own `daemonset.yaml` template, that `k8s.apiServerURLs` (a previously-proposed fix, never applied here) only wires into the agent's bootstrap flag and would not have addressed the original runtime symptom, which depends on `k8sServiceHost`/`k8sServicePort` (i.e. Talos KubePrism) instead. Nothing in this app's own files currently reflects or requires a fix for this — noted here only because it's the app's most notable recent operational history. If a similar symptom (or a downstream one, e.g. crash-looping pods with apiserver connectivity errors) reappears, re-verify with `cilium-dbg service list` across several nodes before assuming a Cilium regression.

## Common operations
- Upgrade chart version: bump `ref.tag` in `kubernetes/apps/kube-system/cilium/app/ocirepository.yaml`, commit, push; Flux reconciles within the OCIRepository's 15m interval or the HelmRelease's 1h interval, or force with `flux reconcile helmrelease cilium -n kube-system`.
- Any values change under `spec.values` in `helmrelease.yaml` triggers a full agent rollout by design: `rollOutCiliumPods: true` (agent) and `operator.rollOutPods: true` (operator).
- Pause reconciliation: `flux suspend kustomization cilium -n flux-system` / `flux suspend helmrelease cilium -n kube-system`.
- Inspect live agent state (debugging only, not part of normal ops): `kubectl exec -n kube-system <cilium-pod> -c cilium-agent -- cilium-dbg service list` to check apiserver backend count; `hubble observe` (via the relay) for live flow data.

## TODOs / unknowns
- Whether `cni.exclusive: false`'s Multus-pairing comment reflects real future intent or is stale boilerplate from an upstream template — no Multus deployment exists in this repo today, so the setting currently has no effect either way.
- Whether the CP memory-overcommit contribution attributed to cilium-agent's previously-missing resource requests (see Known quirks) has measurably improved post-`7334b47` — not re-measured in this pass, and no cross-link exists yet to a control-plane-memory incident doc if one gets written.
- The Proxmox-specific reason behind the DSR→SNAT loadBalancer.mode change (commit `e014ce7`) is not documented beyond the commit message itself.

---
_Cite every non-obvious claim with a repo-root-relative file path (e.g. `kubernetes/apps/security/authentik/app/helmrelease.yaml`), not a bare filename — this doc lives under `docs/apps/`, so relative paths must resolve from there._
