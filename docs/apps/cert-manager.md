# cert-manager

> **Namespace**  `cert-manager`
> **Source**     OCI Helm chart `oci://quay.io/jetstack/charts/cert-manager`, tag `v1.21.1` (`kubernetes/apps/cert-manager/cert-manager/app/ocirepository.yaml`)
> **Hostname**   None of its own — it's a cluster-internal controller. It issues the single wildcard TLS certificate consumed by both Envoy Gateway listeners (`kubernetes/apps/network/envoy-gateway/app/envoy.yaml`).

## What it does here
Issues and renews the cluster's TLS certificates via ACME DNS-01 against Let's Encrypt, using Cloudflare as the DNS-01 solver (`kubernetes/apps/cert-manager/cert-manager/app/clusterissuer.yaml`). In practice there is exactly one `Certificate` resource in the whole repo — a wildcard cert for `${SECRET_DOMAIN}` (`kubernetes/apps/network/envoy-gateway/app/certificate.yaml`) — which both the external and internal Envoy Gateway listeners reference as their TLS secret (`kubernetes/apps/network/envoy-gateway/app/envoy.yaml:81,111`). Everything that terminates TLS through either gateway depends transitively on this one cert.

## Architecture at a glance
- **Depends on:** SOPS-encrypted Secret `cert-manager-secret` for the Cloudflare API token (`kubernetes/apps/cert-manager/cert-manager/app/secret.sops.yaml`) — note this is a plain SOPS/age-encrypted `Secret`, not an ExternalSecrets/1Password pull like most other apps in this repo. `cluster-secrets` Secret for `${SECRET_DOMAIN}` substitution (`kubernetes/apps/cert-manager/cert-manager/ks.yaml` `postBuild.substituteFrom`).
- **Depended on by:** `network/envoy-gateway` — both the `envoy-external` and `envoy-internal` Gateways reference the `${SECRET_DOMAIN/./-}-production-tls` Secret that cert-manager's `Certificate` produces (`kubernetes/apps/network/envoy-gateway/app/envoy.yaml:78-81,108-111`). Since every `HTTPRoute` in the cluster attaches to one of these two Gateways, cert-manager being down doesn't break existing TLS immediately (certs just stop renewing), but a failed renewal eventually breaks HTTPS cluster-wide.
- No `HelmRepository`/`ExternalSecret` — chart comes via Flux `OCIRepository` (`kubernetes/apps/cert-manager/cert-manager/app/ocirepository.yaml`), CRDs are installed by the chart itself (`crds.enabled: true` in `helmrelease.yaml`).

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/cert-manager/cert-manager/app/helmrelease.yaml` | Chart values: single replica, resource limits, hardened security contexts for controller/webhook/cainjector, Prometheus ServiceMonitor enabled |
| `kubernetes/apps/cert-manager/cert-manager/app/ocirepository.yaml` | Pins chart to `v1.21.1` from `oci://quay.io/jetstack/charts/cert-manager`, 15m poll interval |
| `kubernetes/apps/cert-manager/cert-manager/app/clusterissuer.yaml` | `ClusterIssuer/letsencrypt-production` — ACME v2 production directory, DNS-01 via Cloudflare, `profile: shortlived` |
| `kubernetes/apps/cert-manager/cert-manager/app/secret.sops.yaml` | SOPS/age-encrypted Cloudflare API token, key `api-token` |
| `kubernetes/apps/cert-manager/cert-manager/app/ciliumnetworkpolicy.yaml` | Three CiliumNetworkPolicies: controller, cainjector, webhook |
| `kubernetes/apps/cert-manager/cert-manager/ks.yaml` | Flux Kustomization; health-checks both the HelmRelease and the ClusterIssuer's `Ready` condition |

## Secrets
| Secret | Source | Consumed by |
| --- | --- | --- |
| `cert-manager-secret` (key `api-token`) | SOPS/age-encrypted in git, not ExternalSecrets/1Password (`kubernetes/apps/cert-manager/cert-manager/app/secret.sops.yaml`) | `ClusterIssuer/letsencrypt-production`'s Cloudflare DNS-01 solver, via `apiTokenSecretRef` (`kubernetes/apps/cert-manager/cert-manager/app/clusterissuer.yaml:15-17`) |

## Routing & access
- No `HTTPRoute` — cert-manager exposes nothing itself; the webhook is only called in-cluster by `kube-apiserver` for `Certificate`/`Issuer` CRD admission.
- **CiliumNetworkPolicy** (`kubernetes/apps/cert-manager/cert-manager/app/ciliumnetworkpolicy.yaml`), one per component:
  - `cert-manager` (controller): egress to `kube-dns`, `kube-apiserver`, and `world` on 80/443 TCP — the last is required for the ACME HTTP directory/DNS-01 exchange with Let's Encrypt and Cloudflare.
  - `cert-manager-cainjector`: egress to `kube-dns` and `kube-apiserver` only.
  - `cert-manager-webhook`: ingress from `kube-apiserver` (10250, admission calls), from `host` entity (10250, kubelet probes — added in `ba3bce2` after a cluster-wide CNP bug where probes were misclassified and caused restart loops), and from `monitoring` namespace's Prometheus (9402, metrics scrape).
- DNS-01 self-check is forced through public DNS: `dns01RecursiveNameservers: https://1.1.1.1:443/dns-query,https://1.0.0.1:443/dns-query` with `dns01RecursiveNameserversOnly: true` (`kubernetes/apps/cert-manager/cert-manager/app/helmrelease.yaml:15-16`). Likely reason (inference, not commented in-repo): `k8s-gateway` serves `${SECRET_DOMAIN}` itself as an authoritative internal split-horizon zone (`kubernetes/apps/network/k8s-gateway/app/helmrelease.yaml:13`), so cert-manager's ACME challenge-propagation check is pinned to Cloudflare's public DoH resolvers to avoid getting answered by the cluster's own internal DNS instead of the real, publicly-propagated TXT record.

## Storage
None. No PVCs anywhere in `kubernetes/apps/cert-manager/`. The `cert-manager` namespace is not in any Velero schedule's `includedNamespaces` (`kubernetes/apps/velero/schedules/schedule-daily.yaml:13-17`) — expected, since all state here is either git-defined or reissuable from Let's Encrypt.

## Known quirks
- **The one `Certificate` in the repo is deliberately short-lived on purpose.** `network/envoy-gateway`'s cert requests `duration: 160h` (~6.7 days) against the `shortlived` ACME profile specifically as a fail-fast canary for the DNS-01 pipeline — full commentary in `kubernetes/apps/network/envoy-gateway/app/certificate.yaml:10-16`. Any monitoring of this cert's expiry must be calibrated to that short window, not a normal 90-day assumption — see the cross-referenced Gatus fix in `kubernetes/apps/monitoring/gatus/app/configmap.yaml:36-42`.
- **No explicit Flux `dependsOn` from `network/envoy-gateway` onto `cert-manager`** (`kubernetes/apps/network/envoy-gateway/ks.yaml` has no `dependsOn` field). Ordering currently relies on cert-manager's Kustomization reconciling first in practice; a from-scratch bootstrap race here hasn't been ruled out.
- **CiliumNetworkPolicy for the webhook needed a follow-up fix** (`ba3bce2`, 2026-06-28) to allow kubelet probes from the `host` entity — the same cluster-wide bug that caused a 19h Unhealthy-restart loop on postgres-1 also affected `cert-manager-webhook`'s probe port (10250).

## Common operations
- Upgrade chart version: bump `tag` in `ocirepository.yaml`, commit, push (Flux `OCIRepository` polls every 15m, or force with `flux reconcile ocirepository cert-manager -n cert-manager` then `flux reconcile helmrelease cert-manager -n cert-manager`).
- Rotate the Cloudflare API token: re-encrypt `secret.sops.yaml` with the new token via `sops`, commit, push — no ExternalSecret refresh path here since it's a static SOPS secret, not pulled live from 1Password.
- Force a certificate renewal: `kubectl delete certificaterequest -n network -l cert-manager.io/certificate-name=<name>` or annotate the `Certificate` to trigger reissuance.
- Pause reconciliation: `flux suspend kustomization cert-manager -n flux-system` / `flux suspend helmrelease cert-manager -n cert-manager`.

## TODOs / unknowns
- The rationale for `dns01RecursiveNameserversOnly` is inferred from the presence of `k8s-gateway` serving the same domain internally — not confirmed by an in-repo comment or the operator. Flag if wrong.
- Whether the missing `dependsOn` between `envoy-gateway` and `cert-manager` has ever caused a real bootstrap-ordering failure is unverified — no incident doc references it as of this writing.
