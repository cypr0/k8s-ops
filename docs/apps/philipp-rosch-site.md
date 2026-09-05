# philipp-rosch-site

> **Namespace**  portfolio  
> **Source**     Plain manifests (Deployment + Service)  
> **Hostname**   philipp-rosch.de, www.philipp-rosch.de

## What it does here
Serves a static personal portfolio site for the domain `philipp-rosch.de`. The site is fully self-contained in the container image (no runtime API calls or external dependencies beyond DNS), deployed as a plain nginx-based Deployment with two replicas. Public traffic is routed through the cluster's shared Cloudflare Tunnel and Envoy Gateway.

## Architecture at a glance
- **Depends on:** Cloudflare Tunnel (`network/cloudflare-tunnel`) for public ingress, Envoy Gateway (`network/envoy-external`) for internal routing, `cloudflare-dns` for DNS record management.
- **Depended on by:** None — this is a leaf service with no reverse dependencies in the cluster.

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/portfolio/philipp-rosch-site/ks.yaml` | Flux Kustomization that reconciles the app directory |
| `kubernetes/apps/portfolio/philipp-rosch-site/app/deployment.yaml` | 2-replica Deployment, nginx on port 8080, hardened securityContext |
| `kubernetes/apps/portfolio/philipp-rosch-site/app/service.yaml` | ClusterIP Service exposing port 80 → container port 8080 |
| `kubernetes/apps/portfolio/philipp-rosch-site/app/httproute.yaml` | Gateway API route for both apex and www hostnames |
| `kubernetes/apps/portfolio/philipp-rosch-site/app/dnsendpoint.yaml` | external-dns CRD pointing both hostnames at the shared Cloudflare Tunnel CNAME |
| `kubernetes/apps/portfolio/philipp-rosch-site/app/ciliumnetworkpolicy.yaml` | Network policy allowing ingress from Envoy and kubelet, egress only to kube-dns |

## Secrets
None. This app has no ExternalSecrets or runtime credentials — the static site bundle is baked into the container image at build time.

## Routing & access
- **Public ingress:** Traffic arrives via Cloudflare Tunnel (tunnel ID `f768b542-3a28-4fa6-b132-daa8e38b2658.cfargotunnel.com`, shared with other apps in this cluster). Cloudflare terminates client TLS at the edge and re-encrypts to the origin using a fixed SNI of `external.${SECRET_DOMAIN}`, which hits the cluster-wide wildcard certificate on Envoy Gateway's `https` listener. Envoy routes to the correct backend by HTTP Host header (`philipp-rosch.de` or `www.philipp-rosch.de`), independent of the TLS SNI used (`kubernetes/apps/portfolio/philipp-rosch-site/app/httproute.yaml`).
- **DNS:** Both `philipp-rosch.de` and `www.philipp-rosch.de` are managed as CNAME records pointing to the tunnel endpoint, created by `external-dns` from the DNSEndpoint resource (`kubernetes/apps/portfolio/philipp-rosch-site/app/dnsendpoint.yaml`). The `philipp-rosch.de` zone must exist in the same Cloudflare account, and `cloudflare-dns`'s `domainFilters` must include it (see `kubernetes/apps/network/cloudflare-dns/app/helmrelease.yaml`).
- **Network policy:** CiliumNetworkPolicy allows ingress only from Envoy Gateway (`network` namespace, `app.kubernetes.io/name: envoy`) and kubelet health probes (host entity), and egress only to kube-dns. No other runtime network access is permitted (`kubernetes/apps/portfolio/philipp-rosch-site/app/ciliumnetworkpolicy.yaml`).
- **SSO:** Not applicable — this is a public static site with no authentication.

## Storage
None. The container runs with `readOnlyRootFilesystem: true`; ephemeral `emptyDir` volumes are mounted at `/var/cache/nginx` and `/tmp` for nginx's runtime scratch space (`kubernetes/apps/portfolio/philipp-rosch-site/app/deployment.yaml`).

## Known quirks
- **Separate apex domain, shared tunnel:** Unlike most apps in this cluster that use subdomains of `${SECRET_DOMAIN}`, this app serves a completely separate apex domain (`philipp-rosch.de`). It reuses the same Cloudflare Tunnel and Envoy Gateway listener as other apps, relying on HTTP Host-based routing rather than per-domain TLS certificates. The inline comment in `httproute.yaml` documents the TLS/SNI behavior in detail.
- **Manual Cloudflare API token scope:** The Cloudflare API token used by `cert-manager` and `cloudflare-dns` (stored in `CF_API_TOKEN` secret) must have `Zone:DNS:Edit` permissions on *both* `${SECRET_DOMAIN}` and `philipp-rosch.de`. If the token was originally scoped to only one zone, DNS record creation for this app will silently fail until the token's scope is widened in the Cloudflare dashboard (`kubernetes/apps/portfolio/philipp-rosch-site/app/dnsendpoint.yaml` comment).
- **Image tag `latest`:** The Deployment pulls `ghcr.io/cypr0/philipp-rosch-site:latest` with no explicit digest or semantic version tag. This means Flux will not detect upstream image changes automatically — the operator must manually update the Deployment or configure an ImageUpdateAutomation if automatic updates are desired (`kubernetes/apps/portfolio/philipp-rosch-site/app/deployment.yaml`).

## Common operations
- **Update the site content:** Push a new container image to `ghcr.io/cypr0/philipp-rosch-site:latest`, then force a rollout: `kubectl rollout restart deployment philipp-rosch-site -n portfolio`. (Or switch to a versioned tag and update `deployment.yaml` to trigger a GitOps-driven rollout.)
- **Pause reconciliation:** `flux suspend kustomization philipp-rosch-site -n flux-system`.
- **Resume reconciliation:** `flux resume kustomization philipp-rosch-site -n flux-system`.
- **Force Flux to reconcile immediately:** `flux reconcile kustomization philipp-rosch-site -n flux-system`.

## TODOs / unknowns
- **Backup coverage:** No PVCs exist for this app (it's stateless), so Velero/Kopia backup schedules are not applicable. However, it's unclear whether the container image itself is backed up or versioned anywhere outside the GitHub Container Registry. If the registry or the source repo were lost, recovery would depend on external backups of the image or source code — this is not documented in the repo.
- **Image update automation:** The use of `:latest` suggests manual updates are intended, but there is no corresponding ImageRepository or ImagePolicy resource in the repo. Clarify whether automatic image updates are planned, or document the manual update procedure more explicitly if `:latest` is intentional.

---

**Secret/IP scan:** Clean. No ExternalSecrets, no credentials, no real public IPs beyond the Cloudflare Tunnel ID (which is already public by design and appears in multiple other app manifests). The `${SECRET_DOMAIN}` substitution is preserved as a variable reference, not resolved.

---
_All file paths are relative to the repository root. This document lives at `docs/apps/philipp-rosch-site.md`._
