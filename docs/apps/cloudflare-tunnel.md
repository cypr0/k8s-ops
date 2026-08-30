# Cloudflare Tunnel

> **Namespace**  network
> **Source**     `bjw-s-labs/helm/app-template` (OCI, `oci://ghcr.io/bjw-s-labs/helm/app-template`, tag `5.0.1`) — `kubernetes/apps/network/cloudflare-tunnel/app/ocirepository.yaml`; runs `docker.io/cloudflare/cloudflared:2026.7.3`
> **Hostname**   `*.${SECRET_DOMAIN}`, `external.${SECRET_DOMAIN}`, `${SECRET_SECOND_DOMAIN}`, `www.${SECRET_SECOND_DOMAIN}`

## What it does here
This is the cluster's only public inbound path. `cloudflared` opens an outbound-only connection from inside the cluster to Cloudflare's edge (QUIC, HTTP/2 fallback) — no inbound port is ever opened on the home network for it. Cloudflare's edge terminates public traffic for the cluster's domains and relays it down that same tunnel; the `config.yaml` ConfigMap tells `cloudflared` which hostname maps to which in-cluster service, and everything gets forwarded to a single backend: the `envoy-external` Gateway (`kubernetes/apps/network/envoy-gateway/app/envoy.yaml`), which then re-dispatches by HTTP `Host` header to the right app's HTTPRoute. Every other app that wants to be reachable from the public internet does so by attaching its HTTPRoute to `envoy-external` — this app is the only thing standing between that Gateway and Cloudflare's edge.

## Architecture at a glance
- **Depends on:**
  - `envoy-external` Gateway, namespace `network` — sole ingress backend, reached at `https://envoy-external.{{ .Release.Namespace }}.svc.cluster.local:443`; only the two container ports the Gateway's Service maps to (`10443`/`10080`) are reachable, per `kubernetes/apps/network/cloudflare-tunnel/app/ciliumnetworkpolicy.yaml`.
  - `kube-dns` (namespace `kube-system`) for outbound DNS resolution — allowed explicitly in the CiliumNetworkPolicy.
  - A SOPS-encrypted Kubernetes `Secret` (`secret.sops.yaml`) for the tunnel credential — see Secrets below.
  - `cloudflare-dns` (external-dns, `kubernetes/apps/network/cloudflare-dns/`) — publishes the DNS record that points `external.${SECRET_DOMAIN}` at this tunnel's Cloudflare-assigned target (`kubernetes/apps/network/cloudflare-tunnel/app/dnsendpoint.yaml`); the actual tunnel identifier in that CNAME target is not restated here (see Secrets).
- **Depended on by:** every app meant to be reachable from the public internet — concretely, anything whose HTTPRoute attaches to `envoy-external`, e.g. Authentik (`id.${SECRET_DOMAIN}`), Nextcloud (`cloud.${SECRET_DOMAIN}`), and `philipp-rosch-site` (apex domain `${SECRET_SECOND_DOMAIN}`). If this tunnel is down, the cluster has no public inbound path at all — but in-cluster calls between apps are unaffected, since they resolve those same hostnames internally via CoreDNS split-horizon straight to `envoy-internal` rather than round-tripping out to Cloudflare and back (see `docs/apps/authentik.md` and `docs/apps/nextcloud.md` for the apps that had to explicitly route around that public hairpin).

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/network/cloudflare-tunnel/app/helmrelease.yaml` | Chart values: cloudflared image/args, probes, security context, the `config.yaml` ingress-mapping ConfigMap |
| `kubernetes/apps/network/cloudflare-tunnel/app/ocirepository.yaml` | Chart source: `app-template` 5.0.1 from `ghcr.io/bjw-s-labs/helm/app-template` |
| `kubernetes/apps/network/cloudflare-tunnel/app/secret.sops.yaml` | SOPS(age)-encrypted `Secret` holding `TUNNEL_TOKEN` |
| `kubernetes/apps/network/cloudflare-tunnel/app/dnsendpoint.yaml` | `DNSEndpoint` CRD: CNAME for `external.${SECRET_DOMAIN}` to this tunnel's Cloudflare target |
| `kubernetes/apps/network/cloudflare-tunnel/app/ciliumnetworkpolicy.yaml` | Egress-only policy: DNS, Cloudflare edge, `envoy-external` — nothing else |
| `kubernetes/apps/network/cloudflare-tunnel/app/kustomization.yaml` | Wires the above into the `network` Kustomization |

## Secrets
This app does not use an `ExternalSecret`/1Password — unlike most apps in this repo, its one credential is a SOPS(age)-encrypted Kubernetes `Secret` committed directly to the repo: `kubernetes/apps/network/cloudflare-tunnel/app/secret.sops.yaml`. It provides one key, `TUNNEL_TOKEN`, consumed via `envFrom.secretRef` by the `app` container — this is the credential that authenticates the tunnel to Cloudflare's edge. Per the security gate for this doc, its value is never restated here, and neither is the tunnel's UUID (visible only as the CNAME target in `dnsendpoint.yaml`) — both are treated as account-identifying and out of scope for a doc that ends up in the repo's own history.

## Routing & access
- No HTTPRoute of its own — routing is defined entirely by the `config.yaml` ConfigMap mounted into the pod: every hostname under `*.${SECRET_DOMAIN}`, plus the separate apex domain `${SECRET_SECOND_DOMAIN}`/`www.${SECRET_SECOND_DOMAIN}` (a different zone, same Cloudflare account/tunnel — see the inline comment), is forwarded to `envoy-external`; anything unmatched falls through to `service: http_status:404`. `originServerName` is pinned to `external.${SECRET_DOMAIN}` for all of them — that's just the TLS SNI cloudflared presents to Envoy's existing wildcard cert; Envoy still routes by the real HTTP `Host` header regardless.
- `CiliumNetworkPolicy` is egress-only and denies everything not explicitly listed: DNS to `kube-dns`, Cloudflare's edge on `443/TCP` and `7844` (`UDP`+`TCP`, the QUIC/`argotunnel` control ports), and `envoy-external` on container ports `10443`/`10080` only. The policy's own header comment states the intent plainly: "All traffic is initiated outbound — no inbound connections from world."
- No SSO/OIDC on this app itself — it's a transport layer, not an application.

## Storage
None. `persistence.config-file` in `helmrelease.yaml` mounts the `config.yaml` ConfigMap as a file, not a volume claim — there's no PVC and nothing here is in Velero's backup scope.

## Known quirks
- `TUNNEL_TRANSPORT_PROTOCOL` is set to `quic` with `TUNNEL_POST_QUANTUM: true`; the inline comment on that line flags that post-quantum must be turned off if the transport is ever switched to `http2` — noted here so a future protocol change doesn't leave a stale, incompatible flag behind.
- The CNP's egress rule to `envoy-external` targets container ports `10443`/`10080`, not `443`/`80` — the Service in front of that Gateway remaps them. This was itself a fix: `git log --oneline -- kubernetes/apps/network/cloudflare-tunnel/` shows `43c5fbe fix(cloudflare-tunnel): use container ports 10443/10080 in CNP` and `e44d9e2 fix(network): allow cloudflare-tunnel ↔ envoy-proxy traffic in CNPs` landing before that — the policy didn't allow this path from the start.
- `${SECRET_SECOND_DOMAIN}` (a separate apex domain/portfolio site) rides the same tunnel and `config.yaml` as the primary domain (commit `90f7366`); the matching DNS zone permission was added separately in `cloudflare-dns`'s API token scope — worth knowing if the portfolio site's DNS ever stops resolving while the main domain is fine.

## Common operations
- Upgrade `cloudflared` image: bump the tag in `kubernetes/apps/network/cloudflare-tunnel/app/helmrelease.yaml`, commit, push — Renovate/Flux has been doing this automatically per the `fix(container): update image docker.io/cloudflare/cloudflared` commits in the git log.
- Add a new public hostname: add an `ingress` entry to the `config.yaml` block in `helmrelease.yaml` before the trailing `service: http_status:404` catch-all, and make sure DNS/`cloudflare-dns` covers the zone.
- Rotate the tunnel token: update `secret.sops.yaml` (re-encrypt with `sops`), commit, push; `reloader.stakater.com/auto: "true"` on the controller restarts the pod automatically once the Secret changes.
- Pause reconciliation: `flux suspend helmrelease cloudflare-tunnel -n network`.

## TODOs / unknowns
- Which Cloudflare Zero Trust account/team this tunnel belongs to, and how the `TUNNEL_TOKEN` in `secret.sops.yaml` was originally provisioned (Cloudflare dashboard vs `cloudflared tunnel create`), isn't recorded in-repo — operator knowledge only.
- No `ServiceMonitor` alerting rules were found referencing this app's metrics (`serviceMonitor.app` just registers the scrape target on port `8080`) — unclear if tunnel health/connection-count is actually alerted on anywhere.

---
_Cite every non-obvious claim with a repo-root-relative file path (e.g. `kubernetes/apps/security/authentik/app/helmrelease.yaml`), not a bare filename — this doc lives under `docs/apps/`, so relative paths must resolve from there._
