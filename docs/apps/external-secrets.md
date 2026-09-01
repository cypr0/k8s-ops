# external-secrets

> **Namespace**  security
> **Source**     OCI chart `oci://ghcr.io/external-secrets/charts/external-secrets`, tag `2.10.0` — `kubernetes/apps/security/external-secrets/app/ocirepository.yaml`
> **Hostname**   none — internal controller/operator only, not exposed via any Gateway/HTTPRoute

## What it does here
The External Secrets Operator (ESO) is the mechanism by which every `ExternalSecret` resource in this cluster resolves to a real Kubernetes `Secret`. Concretely, it watches for `ExternalSecret`/`ClusterSecretStore` objects and, via the single `ClusterSecretStore` named `onepassword` (`kubernetes/apps/security/external-secrets/stores/clustersecretstore.yaml`), calls the in-cluster 1Password Connect API to pull item fields and materialize them as `Secret` objects. As of this doc, 49 `ExternalSecret` files across 10 namespaces (`automation`, `database`, `hermes-agent`, `logging`, `monitoring`, `nextcloud`, `open-webui`, `paperless`, `security`, `velero`) reference `secretStoreRef: {kind: ClusterSecretStore, name: onepassword}` — if this controller or its store is down, no new/rotated secret reaches any of those apps (existing synced `Secret` objects keep working until their `refreshInterval` fires).

## Architecture at a glance
- **Depends on:** `onepassword-connect` HelmRelease/Service in the same namespace (`http://onepassword-connect.security.svc.cluster.local`, port 80); Flux-wise, the `external-secrets` Kustomization explicitly `dependsOn` the `onepassword-connect` Kustomization, and the `external-secrets-stores` Kustomization `dependsOn` `external-secrets`.
- **Bootstraps itself via one SOPS secret, not 1Password:** the `ClusterSecretStore`'s `auth.secretRef` points at a plain Kubernetes `Secret` named `onepassword-secret` (key `token`), which is SOPS-encrypted (not ESO-managed) at `kubernetes/apps/security/onepassword-connect/app/secret.sops.yaml` — necessarily so, since this is the credential that lets ESO talk to 1Password Connect in the first place. The same `Secret`'s `onepassword-credentials.json` key is separately consumed by the `onepassword-connect` pods themselves as `OP_SESSION` — one Secret, two independent consumers.
- **Depended on by:** effectively every app in the cluster that needs a secret from 1Password — 49 `ExternalSecret` manifests across the 10 namespaces listed above. This is foundational infra: nearly every already-documented app's "Secrets" section ultimately traces back to this controller and the `onepassword` store.

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/security/external-secrets/ks.yaml` | Two Flux Kustomizations: `external-secrets` (the controller, path `app/`) and `external-secrets-stores` (the `ClusterSecretStore`, path `stores/`), wired with explicit `dependsOn` |
| `kubernetes/apps/security/external-secrets/app/ocirepository.yaml` | Chart source — OCI ref, currently pinned to tag `2.10.0` |
| `kubernetes/apps/security/external-secrets/app/helmrelease.yaml` | HelmRelease; `chartRef` points at the OCIRepository above, values sourced from a generated ConfigMap |
| `kubernetes/apps/security/external-secrets/app/helm/values.yaml` | All Helm values (resources, security contexts, autoscaling, monitoring) — folded into a ConfigMap by `kustomization.yaml`'s `configMapGenerator` |
| `kubernetes/apps/security/external-secrets/app/ciliumnetworkpolicy.yaml` | Three separate `CiliumNetworkPolicy` objects — main controller, cert-controller, webhook |
| `kubernetes/apps/security/external-secrets/stores/clustersecretstore.yaml` | The single `ClusterSecretStore` (`onepassword`) every app's `ExternalSecret` references |

## Secrets
This app has no `ExternalSecret` of its own to pull application secrets — it *is* the operator. Its only secret dependency is structural: the `ClusterSecretStore`'s `auth.secretRef` resolves `token` from the `onepassword-secret` `Secret` in namespace `security`, which is created directly from SOPS, not synced by ESO itself. No value from that secret is restated here.

## Routing & access
No HTTPRoute — this app is never exposed outside the cluster. Three `CiliumNetworkPolicy` objects, one per workload:
- **`external-secrets` (main controller):** egress only — kube-dns, `kube-apiserver` entity, and `onepassword-connect` on **port 80**. That port was originally `8080` and caused `ExternalSecret` sync to fail with an i/o timeout until fixed in commit `965c950` (`fix(paperless,external-secrets): correct Flux dependency namespaces and onepassword CNP port`) — 1Password Connect's actual service port is 80, not 8080.
- **`external-secrets-cert-controller`:** egress only — kube-dns and `kube-apiserver`; this component rotates the admission webhook's TLS certificate.
- **`external-secrets-webhook`:** the only one of the three with an explicit **ingress** allow-list — `kube-apiserver` (validating webhook calls, port 10250), `host` entity (kubelet readiness/liveness probes, port 10250), and `monitoring/prometheus` (metrics scrape, port 8080).
- Note: since the main controller and cert-controller policies specify egress rules only (no `ingress` key), and no namespace-wide default-deny `CiliumNetworkPolicy` exists for `security`, their ingress is not restricted by policy — only the webhook pod has ingress explicitly locked down.
- No OIDC/Authentik integration — this is a controller, not a user-facing app.

## Storage
Stateless — no PVC in this app's directory. All persisted state is either in 1Password itself or in the `Secret` objects ESO writes into consuming namespaces; nothing here needs Velero/Kopia backup coverage.

## Known quirks
- **CNP port for 1Password Connect is 80, not 8080** — fixed in commit `965c950`; if this regresses (e.g. a values change assumes the sync port), `ExternalSecret` resources across the cluster will start failing sync with connect timeouts.
- **The `external-secrets-stores` Flux health check had to be rewritten** — it originally checked a `HelmRelease` that doesn't exist under that Kustomization's path (only a `ClusterSecretStore` lives there), which Flux flagged as invalid. Commit `913c2b5` replaced it with a `healthCheckExprs` check on the `ClusterSecretStore`'s `Ready` condition.
- **Chart upgrades happen via the `OCIRepository` tag, not the `HelmRelease`** — `helmrelease.yaml` uses `chartRef: {kind: OCIRepository, name: external-secrets}`, so bumping the version means editing `ocirepository.yaml`'s `spec.ref.tag`, not `helmrelease.yaml`.
- **`installCRDs: true`** — this HelmRelease owns the `ExternalSecret`/`ClusterSecretStore`/etc. CRDs cluster-wide; a rollback of this release can downgrade CRDs consumed by every other app's `ExternalSecret`.
- No `docs/incidents/` entry references this app by name despite the CNP port bug above having caused a real sync outage — it predates this documentation pass and was fixed same-day via commit, not written up as a postmortem.

## Common operations
- Upgrade chart version: edit `spec.ref.tag` in `ocirepository.yaml` (not `helmrelease.yaml`), commit, push; Flux reconciles within the OCIRepository's 5m `interval`, or force with `flux reconcile helmrelease external-secrets -n security`.
- Rotate the 1Password Connect token: update `kubernetes/apps/security/onepassword-connect/app/secret.sops.yaml` with `sops`, commit; then either wait or `kubectl rollout restart` the `onepassword-connect` deployment and re-annotate downstream `ExternalSecret`s if an immediate resync is needed.
- Force-resync a specific app's secret: `kubectl annotate externalsecret <name> -n <ns> force-sync=$(date +%s)`.
- Check store health directly: `kubectl get clustersecretstore onepassword -o yaml`.
- Pause reconciliation: `flux suspend kustomization external-secrets -n security` / `flux suspend kustomization external-secrets-stores -n security` / `flux suspend helmrelease external-secrets -n security`.

## TODOs / unknowns
- `refreshInterval`/poll behavior for the `ClusterSecretStore` and individual `ExternalSecret`s is left at chart/CRD defaults for v2.10.0 — not overridden in `values.yaml`.
- Only one 1Password vault is wired up (`vaults: {Kubernetes: 1}`) — unclear whether additional vaults are planned or intentionally out of scope.

---
_Cite every non-obvious claim with a repo-root-relative file path (e.g. `kubernetes/apps/security/authentik/app/helmrelease.yaml`), not a bare filename — this doc lives under `docs/apps/`, so relative paths must resolve from there._
