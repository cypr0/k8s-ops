# philipp-rosch-site

> **Namespace**  portfolio
> **Source**     Plain manifests, no HelmRelease/OCIRepository — `kubernetes/apps/portfolio/philipp-rosch-site/app/kustomization.yaml` lists a bare `Deployment`/`Service`/`CiliumNetworkPolicy`/`HTTPRoute`/`DNSEndpoint`
> **Hostname**   `${SECRET_SECOND_DOMAIN}`, `www.${SECRET_SECOND_DOMAIN}` — currently **not live** (see Known quirks)

## What it does here
The cluster operator's personal portfolio/blog site: a static, pre-built bundle (commit message on `90f7366` describes it as an Astro site) served by an unprivileged nginx container, on its own apex domain `${SECRET_SECOND_DOMAIN}` — a separate Cloudflare zone from the cluster's main `${SECRET_DOMAIN}`. It rides the same public-ingress path as every other exposed app (Cloudflare Tunnel → `envoy-external`), but is otherwise the simplest app in the cluster: no database, no session state, no OIDC, no runtime egress besides DNS ("Static site, fully self-contained in the served bundle"). As of this doc it is fully defined in-repo but **deliberately not deployed** — see Known quirks.

## Architecture at a glance
- **Depends on:**
  - `kube-dns` (namespace `kube-system`) — the only egress the `CiliumNetworkPolicy` allows, on `:53` TCP/UDP.
  - `envoy` in namespace `network` (the `envoy-external` Gateway) for ingress — the CNP's only allowed ingress source besides the host entity for kubelet probes.
  - Cloudflare Tunnel's existing ingress mapping and Cloudflare account, and `cloudflare-dns`'s `domainFilters` — both were extended for this app's zone in the same commit that added it (`90f7366`).
- **Depended on by:** nothing else in the cluster — this is a leaf app with no consumers.

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/portfolio/philipp-rosch-site/app/deployment.yaml` | 2-replica Deployment, hardened `securityContext` (non-root uid/gid `101`, read-only root filesystem, all capabilities dropped), `emptyDir` volumes for `/var/cache/nginx` and `/tmp` |
| `kubernetes/apps/portfolio/philipp-rosch-site/app/service.yaml` | ClusterIP Service, port `80` → container port `http` (`8080`) |
| `kubernetes/apps/portfolio/philipp-rosch-site/app/httproute.yaml` | `HTTPRoute` for both hostnames, parented to `envoy-external` in namespace `network` |
| `kubernetes/apps/portfolio/philipp-rosch-site/app/dnsendpoint.yaml` | `DNSEndpoint` CRD: CNAME records for both hostnames, same Cloudflare Tunnel target as `kubernetes/apps/network/cloudflare-tunnel/app/dnsendpoint.yaml` |
| `kubernetes/apps/portfolio/philipp-rosch-site/app/ciliumnetworkpolicy.yaml` | Ingress from `envoy` (network ns) + host (kubelet probes) only; egress to `kube-dns` only |
| `kubernetes/apps/portfolio/philipp-rosch-site/ks.yaml` | Flux `Kustomization`, `targetNamespace: portfolio`, `interval: 1h` |
| `kubernetes/apps/portfolio/kustomization.yaml` | Aggregating Kustomization for the `portfolio` namespace — currently has this app's `ks.yaml` **commented out** |

## Secrets
None. No `ExternalSecret` (or SOPS `Secret`) file exists anywhere under this app's directory — this is a static site with no backend, no database, and nothing to authenticate.

## Routing & access
- `HTTPRoute` hostnames: `${SECRET_SECOND_DOMAIN}` and `www.${SECRET_SECOND_DOMAIN}`, parented to `envoy-external`.
- Public traffic never reaches Envoy Gateway directly — it's proxied through Cloudflare Tunnel, which terminates client TLS at Cloudflare's edge and re-encrypts to the origin using a fixed `originServerName` (`external.${SECRET_DOMAIN}`) regardless of which public hostname was requested; Envoy still dispatches to this app's backend by the real HTTP `Host` header, so no new listener or per-domain Certificate is needed for the separate zone.
- DNS for both hostnames is a CNAME to the same Cloudflare Tunnel target used by the main domain; it only takes effect if `cloudflare-dns`'s `domainFilters` includes the `${SECRET_SECOND_DOMAIN}` zone (it does) and the Cloudflare API token behind it has `Zone:DNS:Edit` on that zone — the inline comment on the `DNSEndpoint` flags this as a manual, non-git-trackable prerequisite.
- `CiliumNetworkPolicy` ingress: only `envoy` pods in namespace `network` on container port `8080`, plus the `host` entity (kubelet readiness/liveness probes). Egress: `kube-dns` only, ports `53`/UDP+TCP. No SSO/OIDC — this app has no auth of its own.

## Storage
No PVCs. The Deployment mounts two `emptyDir` volumes (`nginx-cache`, `tmp`) for nginx's cache and scratch directories only — both ephemeral, required solely because `readOnlyRootFilesystem: true` is set. Nothing here is in Velero's backup scope; there is nothing stateful to back up.

## Known quirks
- **Not currently deployed.** `kubernetes/apps/portfolio/kustomization.yaml` has this app's `ks.yaml` line commented out. The inline comment gives two reasons: the site "is not ready to be published," and separately, the image it references (`ghcr.io/cypr0/philipp-rosch-site:latest`) doesn't exist in the registry yet, so pods would fail regardless. Flux's `prune: true` on this Kustomization means removing the line durably deletes every resource this app created (Deployment/Service/HTTPRoute/DNSEndpoint) rather than leaving orphans.
- **A live `kubectl`-level suspend does not survive a parent reconcile — this is why it was disabled at the aggregating-`kustomization.yaml` level instead.** Per the commit that disabled this app (`1a481a6`): "A live kubectl suspend on the Kustomization doesn't survive a parent reconcile (the field isn't git-tracked, so it gets reset back to unsuspended) — that's why it kept coming back as ImagePullBackOff pods despite being manually taken offline earlier." The durable way to take any Flux-managed app offline in this repo is to remove its `ks.yaml` line from the aggregating `kustomization.yaml`, not `flux suspend`.
- **Two other apps' configs already assume this site is live, and were not rolled back when it was disabled:** `cloudflare-tunnel`'s ingress mapping for `${SECRET_SECOND_DOMAIN}`/`www.${SECRET_SECOND_DOMAIN}` and `cloudflare-dns`'s `domainFilters` for the same zone both still reference this app's hostnames. This is harmless while dark — this app's own `DNSEndpoint`/`HTTPRoute` aren't in the live cluster since its `ks.yaml` isn't applied — but re-enabling is a one-line uncomment, not a multi-app change.
- **The CNPG "portfolio" database is a different, unrelated thing with the same name.** `docs/apps/cloudnative-pg.md` documents a `portfoliousr`/`portfolio` managed role+database pair, but that database is consumed entirely by `hermes-agent`'s stock/portfolio-tracking cron feature — not by this website. This static site does not touch Postgres, or any database, at all.
- **No image tag pinning.** `deployment.yaml` references `ghcr.io/cypr0/philipp-rosch-site:latest` — unlike most other apps in this repo (which pin by digest with a `# renovate:` marker), this one floats on `:latest`, consistent with the image not existing yet / the app being pre-publish.

## Common operations
- **Re-enable the site:** first make sure `ghcr.io/cypr0/philipp-rosch-site:latest` actually exists and is pullable, then uncomment `- ./philipp-rosch-site/ks.yaml` in `kubernetes/apps/portfolio/kustomization.yaml`, commit, push.
- **Take it offline again (if ever redeployed):** edit `kubernetes/apps/portfolio/kustomization.yaml` to comment out its `ks.yaml` line and let `prune: true` remove the resources — do **not** rely on `flux suspend kustomization philipp-rosch-site -n flux-system`, per Known quirks above.
- Upgrade/pin the image: edit the `image:` line in `deployment.yaml`, commit, push; Flux reconciles within the `1h` interval once the app is actually deployed.

## TODOs / unknowns
- No build/CI pipeline for `ghcr.io/cypr0/philipp-rosch-site` exists in this repo — where/how that image gets built and pushed is outside `k8s-ops` and not recorded here.
- No target date or checklist for "ready to publish" is recorded anywhere in-repo — operator knowledge only.
- Whether the Cloudflare API token currently has `Zone:DNS:Edit` scope on the `${SECRET_SECOND_DOMAIN}` zone cannot be verified from the repo.

---
_Cite every non-obvious claim with a repo-root-relative file path (e.g. `kubernetes/apps/security/authentik/app/helmrelease.yaml`), not a bare filename — this doc lives under `docs/apps/`, so relative paths must resolve from there._
