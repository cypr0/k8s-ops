# Reloader

> **Namespace**  kube-system
> **Source**     `oci://ghcr.io/stakater/charts/reloader`, chart `reloader` v2.2.16 / app `v1.4.21` (`kubernetes/apps/kube-system/reloader/app/ocirepository.yaml`, `helmrelease.yaml`)
> **Hostname**   none — cluster-internal controller, no HTTPRoute

## What it does here
Watches ConfigMaps/Secrets cluster-wide and triggers rolling restarts of any Deployment/DaemonSet/StatefulSet that opts in via `secret.reloader.stakater.com/reload` or `reloader.stakater.com/auto` annotations. Its main job in this cluster is closing the loop after an ExternalSecret refresh from 1Password: without it, apps like authentik would keep running on stale OIDC client secrets after a rotation until someone manually restarted the pod.

## Architecture at a glance
- **Depends on:** nothing — stateless, no ExternalSecret of its own, no persistent storage. Talks only to the Kubernetes API server via a cluster-wide `ClusterRole`/`ClusterRoleBinding` (`watchGlobally: true`, no `namespaceSelector`/`ignoreNamespaces` set).
- **Depended on by (11 apps across 6 namespaces, grepped for the two annotation families):**
  - `secret.reloader.stakater.com/reload` (restart on specific named Secrets): `security/authentik` (`kubernetes/apps/security/authentik/app/helmrelease.yaml` — 6 secrets incl. `authentik-secret` + 5 OIDC client secrets), `monitoring/grafana`, `network/cloudflare-dns`, `paperless/paperless-ngx`, `open-webui/open-webui`, `open-webui/open-terminal`.
  - `reloader.stakater.com/auto: "true"` (restart on *any* referenced ConfigMap/Secret change): `paperless/paperless-ngx`, `open-webui/open-webui`, `open-webui/open-terminal` (both annotation styles), `nextcloud/whiteboard`, `nextcloud/collabora`, `security/onepassword-connect`, `network/cloudflare-tunnel`, `monitoring/gatus`.
  - If `reloader`'s pod is down, none of these apps fail immediately — they just keep running on stale config/secret values until someone restarts them by hand.

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/kube-system/reloader/app/helmrelease.yaml` | Chart values — security context, resources, podMonitor (see Known quirks: some of these values are set at the wrong key path for this chart version) |
| `kubernetes/apps/kube-system/reloader/app/ocirepository.yaml` | OCI chart source pin (`ghcr.io/stakater/charts/reloader`, tag) |
| `kubernetes/apps/kube-system/reloader/app/ciliumnetworkpolicy.yaml` | Ingress from Prometheus + kubelet probes; egress to CoreDNS + kube-apiserver |
| `kubernetes/apps/kube-system/reloader/ks.yaml` | Flux Kustomization (`targetNamespace: kube-system`, `interval: 1h`) |

## Secrets
None. `reloader` has no `ExternalSecret` of its own — it only *reads* other apps' Secrets/ConfigMaps cluster-wide via RBAC to detect changes and trigger a restart of the owning workload; it never stores or re-exposes their content.

## Routing & access
- No HTTPRoute — not exposed, cluster-internal only.
- `kubernetes/apps/kube-system/reloader/app/ciliumnetworkpolicy.yaml`: ingress allowed from `monitoring/prometheus` and from `host` entities (kubelet readiness/liveness probes) on port 9090; egress restricted to CoreDNS (port 53) and `kube-apiserver` — nothing else. The chart's own built-in NetworkPolicy is left disabled (`netpol.enabled: false`) in favor of this repo-managed CiliumNetworkPolicy.
- Metrics: `podMonitor.enabled: true` (`helmrelease.yaml`) scrapes the same port (9090) the CNP allows in from Prometheus.

## Storage
No PVCs — fully stateless.

## Known quirks
- **Chart version bumped 2.2.14 → 2.2.16 same-day by Renovate** (`ocirepository.yaml`, commit `a2305bb` on `main`), bringing app version v1.4.19 → v1.4.21.
- **Live pod is `BestEffort` QoS despite `helmrelease.yaml` defining `resources.requests`/`limits`** (`kubernetes/apps/kube-system/reloader/app/helmrelease.yaml`, `reloader.resources`) — re-verified fresh against the live pod: `status.qosClass` is `BestEffort`, and the rendered container's `resources:` field is empty. Root cause: `helm get values reloader -n kube-system -a` shows chart v2.2.16 actually consumes `reloader.deployment.resources` (nested under `deployment:`), while this repo's `helmrelease.yaml` sets the sibling top-level key `reloader.resources` — a dead/unused key in this chart version. The same misplacement affects `reloader.containerSecurityContext` and part of `reloader.securityContext` (`runAsGroup: 65534` never lands in the rendered pod spec); only `reloader.readOnlyRootFileSystem` happens to be wired at the top level and does take effect. Fix would be moving `resources`/`securityContext`/`containerSecurityContext` under `reloader.deployment.*` — not applied here per the no-fixes-on-this-branch rule; surfaced for a follow-up.
- One practical consequence of the missing `resources` block: the container's `GOMAXPROCS`/`GOMEMLIMIT` env vars are wired via `resourceFieldRef` against `limits.cpu`/`limits.memory` — with no container limit set, the downward API falls back to the node's allocatable capacity for that divisor, so these aren't actually constraining the process to the values intended in `helmrelease.yaml`.
- Being `BestEffort` used to matter more acutely: per operational memory (not a repo file), `reloader` was among the pods repeatedly cgroup-OOM-killed on `k8s-cp-0`/`k8s-cp-1` during a 2026-08-13 memory-pressure incident precisely because BestEffort pods are first in line for kernel OOM kill. That specific risk is now largely closed off — control-plane nodes carry a `NoSchedule` taint as of the incident's remediation (`talos/patches/controller/cluster.yaml`, `allowSchedulingOnControlPlanes: false`), so ordinary workloads — including `reloader` — can no longer land there at all. The QoS/resources gap itself is still open, independent of that mitigation.

## Common operations
- Upgrade chart: edit `ocirepository.yaml` `spec.ref.tag` (Renovate does this automatically), commit, push — Flux reconciles the `OCIRepository` within `interval: 15m` and the `HelmRelease` within `interval: 1h`, or force with `flux reconcile helmrelease reloader -n kube-system`.
- Check who's currently wired to reload on this: `grep -rn "reloader.stakater.com" kubernetes/apps/` — any hit is a live dependent, not just the ones listed above if new apps have been added since this doc was written.
- Pause reconciliation: `flux suspend kustomization reloader -n flux-system` / `flux suspend helmrelease reloader -n kube-system`.
- Verify QoS after any resources/securityContext change: `kubectl get pod -n kube-system -l app.kubernetes.io/name=reloader -o jsonpath='{.status.qosClass}'`.

## TODOs / unknowns
- The `reloader.deployment.resources` vs `reloader.resources` key-path mismatch (Known quirks) should be fixed in `helmrelease.yaml` in a follow-up, dedicated commit — not bundled with this docs change.
- Have not verified whether any of the 8 `reloader.stakater.com/auto: "true"` apps have ever actually triggered an unwanted restart from an unrelated ConfigMap change (auto mode reloads on *any* referenced ConfigMap/Secret, not just ones an operator expects) — no incident found in `docs/incidents/` or repo history referencing `reloader` by name.

---
_Cite every non-obvious claim with a repo-root-relative file path (e.g. `kubernetes/apps/security/authentik/app/helmrelease.yaml`), not a bare filename — this doc lives under `docs/apps/`, so relative paths must resolve from there._
