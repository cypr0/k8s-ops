# csi-driver-nfs

> **Namespace**  kube-system
> **Source**     Helm chart `csi-driver-nfs` v4.13.4 from `https://raw.githubusercontent.com/kubernetes-csi/csi-driver-nfs/master/charts` (`kubernetes/apps/kube-system/csi-driver-nfs/app/helmrepository.yaml`)
> **Hostname**   none (cluster-internal CSI driver, no HTTP endpoint)

## What it does here
The dynamic-provisioning CSI driver behind the cluster's default StorageClass, `zfs-nfs` — it provisions ReadWriteMany/ReadWriteOnce PersistentVolumes as subdirectories on a single external NFS export (`/rpool/k8s-rwx`, a ZFS dataset) and mounts them into pods on every node via its node DaemonSet. Effectively the shared-storage backbone for every app in the cluster that needs a PVC without a dedicated CNPG/object-store backend (`kubernetes/apps/kube-system/csi-driver-nfs/app/helmrelease.yaml:17-32`).

## Architecture at a glance
- **Depends on:** the external NFS server referenced by the `SECRET_NFS_SERVER` postBuild variable, sourced from the SOPS-encrypted `cluster-secrets` Secret (`kubernetes/apps/kube-system/csi-driver-nfs/ks.yaml:9-12`, key defined in `kubernetes/components/sops/cluster-secrets.sops.yaml`) — never resolved here, cite the key name only. No in-cluster dependency (no CNPG/Dragonfly/OIDC).
- **Depended on by:** every PVC with `storageClassName: zfs-nfs` — confirmed via repo-wide grep: `kubernetes/apps/nextcloud/nextcloud/app/pvc.yaml`, `kubernetes/apps/hermes-agent/hermes-agent/app/pvc.yaml`, `kubernetes/apps/paperless/paperless-ngx/app/pvc.yaml` (the `paperless-data-pvc`, not the consume PVC — see Storage below), `kubernetes/apps/open-webui/open-webui/app/pvc.yaml`, `kubernetes/apps/open-webui/open-terminal/app/pvc.yaml`, plus the monitoring stack's own volume claim templates in `kubernetes/apps/monitoring/kube-prometheus-stack/app/helmrelease.yaml:57-60,92-95` (Prometheus/Alertmanager storage) and `kubernetes/apps/monitoring/grafana/app/helmrelease.yaml:84-87`. If this driver or the NFS server is down, none of these PVCs can be freshly bound/expanded, and any pod scheduled to a node without the export already mounted will fail to start.

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/kube-system/csi-driver-nfs/app/helmrelease.yaml` | Chart version (4.13.4), `kubeletDir`, and the `zfs-nfs` StorageClass definition (server/share/mount options) |
| `kubernetes/apps/kube-system/csi-driver-nfs/app/helmrepository.yaml` | Upstream chart source (raw GitHub charts URL, not a Helm repo index) |
| `kubernetes/apps/kube-system/csi-driver-nfs/app/ciliumnetworkpolicy.yaml` | Egress policy for the controller Deployment and node DaemonSet separately |
| `kubernetes/apps/kube-system/csi-driver-nfs/ks.yaml` | Flux Kustomization — `postBuild.substituteFrom: cluster-secrets` for `${SECRET_NFS_SERVER}` |

## Secrets
No dedicated ExternalSecret for this app. The one credential-adjacent value it needs — the NFS server address — comes from the cluster-wide `cluster-secrets` Secret (SOPS-encrypted at `kubernetes/components/sops/cluster-secrets.sops.yaml`, key `SECRET_NFS_SERVER`) via the Kustomization's `postBuild.substituteFrom` (`kubernetes/apps/kube-system/csi-driver-nfs/ks.yaml:9-12`), substituted directly into the StorageClass's `parameters.server` field (`kubernetes/apps/kube-system/csi-driver-nfs/app/helmrelease.yaml:30`). Not resolved here — it's SOPS-encrypted in the repo, so treat it the same as any other secret.

## Routing & access
Not exposed via Gateway/HTTPRoute — this is a node-local/cluster-internal CSI driver, not a routable service. Two `CiliumNetworkPolicy` resources scope its network access (`kubernetes/apps/kube-system/csi-driver-nfs/app/ciliumnetworkpolicy.yaml`):
- `csi-driver-nfs-controller` (the provisioning Deployment): DNS to `kube-dns`, kube-apiserver, and `world` on NFS-family ports 2049 (nfs), 111 (portmapper/rpcbind), 20048 (mountd) — the controller needs rpcbind/mountd to negotiate exports, the node agent doesn't.
- `csi-driver-nfs-node` (the per-node DaemonSet that performs mounts): DNS, kube-apiserver, and `world` on port 2049 only.
- Commit `2aa6942` (`git log --oneline -- kubernetes/apps/kube-system/csi-driver-nfs/`) added the 111/20048 ports to the controller policy after presumably hitting a blocked mount negotiation — worth knowing if a future NFS-protocol change (e.g. switching off v4.2, which doesn't need portmapper/mountd) makes those ports removable.

## Storage
This is the storage layer, not a storage consumer — no PVCs of its own. It backs the `zfs-nfs` StorageClass (`storageclass.kubernetes.io/is-default-class: "true"`, so it's the cluster's implicit default), with `reclaimPolicy: Delete`, `volumeBindingMode: Immediate`, `allowVolumeExpansion: true`, mount options `nfsvers=4.2,hard,intr`, and `parameters.mountPermissions: "0755"` (`kubernetes/apps/kube-system/csi-driver-nfs/app/helmrelease.yaml:17-32`) — this last parameter is what chmods a **freshly provisioned** subdirectory to 0755 on first creation; it has no effect on a subdirectory's permissions after that.

Not everything with an NFS-shaped `storageClassName` is provisioned by this driver: `paperless-consume-nfs` (`kubernetes/apps/paperless/paperless-ngx/app/pvc.yaml`) is a **statically defined** `PersistentVolume` using the plain in-tree `nfs:` volume source (server/path fields), not this CSI driver — no StorageClass object for `paperless-consume-nfs` exists anywhere in the repo (confirmed via repo-wide search for `StorageClass` resources and for the name). Don't assume every NFS-backed PVC in this cluster goes through `csi-driver-nfs`.

Backup coverage: no `VolumeSnapshotClass` is defined anywhere in the repo (confirmed via grep), so `zfs-nfs` volumes are not snapshot-backed — they're covered by Velero's Kopia-based filesystem backup instead (`defaultVolumesToFsBackup: true`, `kubernetes/apps/velero/app/helmrelease.yaml:87`), which walks the mounted filesystem rather than taking a storage-layer snapshot. Namespaces using `zfs-nfs` PVCs (`nextcloud`, `paperless`, `open-webui`, `hermes-agent`) are in the daily/weekly/monthly Velero schedules under `kubernetes/apps/velero/schedules/`.

## Known quirks
- **`chown`/`chmod` only works on a freshly provisioned subdirectory, not on a long-lived mount.** Documented and hit in production via `docs/incidents/2026-08-16-hermes-agent-restore-pvc-chown-permission-denied.md`: an init container's `chown 1000:1000` (with `CAP_CHOWN`) succeeded against a Velero-restored, freshly-`csi-driver-nfs`-provisioned copy of a PVC but was rejected outright ("Operation not permitted") against the same app's long-lived production mount. This tracks with the driver's own `mountPermissions: "0755"` StorageClass parameter (`kubernetes/apps/kube-system/csi-driver-nfs/app/helmrelease.yaml:32`), which only applies permissions at the moment of subdirectory creation — the incident's root cause is a constraint of the underlying NFS export/CSI mount behavior after that point, not something visible in this app's own manifests alone. Any init container doing a "just in case" chown against a PVC on this StorageClass must treat the chown as best-effort, not fatal — see the incident's runbook for the exact pattern.
- **CiliumNetworkPolicy for the controller needed extra ports (111, 20048) beyond the obvious NFS port (2049)** to work at all — added in commit `2aa6942` after presumably being missed on first rollout (commit `5a43134` added the initial cluster-wide CNP pass). A useful reminder that NFSv4 mount negotiation isn't single-port even though data transfer is.

## Common operations
- Upgrade chart version: edit `kubernetes/apps/kube-system/csi-driver-nfs/app/helmrelease.yaml`, commit, push, Flux reconciles within the 1h `interval` (or force with `flux reconcile helmrelease csi-driver-nfs -n kube-system`).
- Change StorageClass parameters (mount options, reclaim policy, etc.): edit the `storageClass` block in the same `helmrelease.yaml` — note `volumeBindingMode: Immediate` and `reclaimPolicy: Delete` are live-cluster defaults already in place, changing them only affects newly created PVs.
- Pause reconciliation: `flux suspend kustomization csi-driver-nfs -n flux-system` / `flux suspend helmrelease csi-driver-nfs -n kube-system`.

## TODOs / unknowns
- No ExternalSecret in this app's directory — confirmed by directory listing (`ls kubernetes/apps/kube-system/csi-driver-nfs/app/`), so nothing to add to the Secrets section beyond the `cluster-secrets` postBuild substitution already covered.
- The exact underlying reason the NFS server/export rejects `chown` on an already-mounted (non-fresh) subdirectory — whether it's a `no_root_squash`/`root_squash` export option, an NFSv4.2 ACL interaction, or something in the driver itself — is not established anywhere in this repo; the incident doc treats it as an observed, previously-documented constraint rather than a diagnosed root cause at the NFS-export level. Would need direct testing against the export config (outside this repo) to confirm.

---
_Cite every non-obvious claim with a repo-root-relative file path (e.g. `kubernetes/apps/security/authentik/app/helmrelease.yaml`), not a bare filename — this doc lives under `docs/apps/`, so relative paths must resolve from there._
