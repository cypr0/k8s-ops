# Cloudflare DNS (external-dns)

> **Namespace**  network
> **Source**     `oci://ghcr.io/home-operations/charts-mirror/external-dns`, chart `external-dns` tag `1.21.1` (`kubernetes/apps/network/cloudflare-dns/app/ocirepository.yaml`, `helmrelease.yaml`) — upstream project is `kubernetes-sigs/external-dns`, configured here with `provider: cloudflare`
> **Hostname**   none of its own — not exposed via HTTPRoute. It *creates* DNS records for other apps' public hostnames.

## What it does here
Runs upstream `external-dns` against the Cloudflare API to keep public DNS records in sync with two sources: HTTPRoutes parented to the `envoy-external` Gateway, and explicit `DNSEndpoint` CRDs (`extraArgs: --gateway-name=envoy-external`, `sources: [crd, gateway-httproute]`). It manages two Cloudflare zones in the same account — `${SECRET_DOMAIN}` and the separate apex `${SECRET_SECOND_DOMAIN}` (added for the portfolio site) — and is deliberately scoped to public-facing routes only; internal-only apps behind the `envoy-internal` Gateway are resolved instead by `k8s-gateway`'s split-horizon DNS, not by this controller.

## Architecture at a glance
- **Depends on:**
  - A SOPS/age-encrypted `Secret` `cloudflare-dns-secret` (`kubernetes/apps/network/cloudflare-dns/app/secret.sops.yaml`) supplying `CF_API_TOKEN` — **not** the repo's usual `ExternalSecret` + 1Password pattern (only 5 of ~50 apps in the repo use a raw SOPS `Secret` instead of `ExternalSecret`; this is one of them, alongside `onepassword-connect`, `cert-manager`, `cloudflare-tunnel`, `flux-instance`).
  - CoreDNS (`kube-dns` in `kube-system`) for its own DNS resolution, and `kube-apiserver` to watch `HTTPRoute`/`DNSEndpoint`/`Service` — both explicit egress rules.
  - The public Cloudflare API (`toEntities: [world]`, port 443 only) — this is the one app in `network` whose CNP intentionally allows egress to the whole internet, scoped to 443.
- **Depended on by:**
  - HTTPRoutes parented to Gateway `envoy-external`: `nextcloud`, `whiteboard`, `collabora`, `flux-instance`, `authentik`, `philipp-rosch-site`, and `echo`'s inline HTTPRoute.
  - `DNSEndpoint` CRDs: `cloudflare-tunnel`'s (the tunnel's CNAME record) and `philipp-rosch-site`'s (same tunnel, `${SECRET_SECOND_DOMAIN}` zone). If `cloudflare-dns`'s `domainFilters` doesn't include a CRD's zone, `external-dns` silently ignores that `DNSEndpoint` — no error, just no record.
  - **Not** depended on by anything behind `envoy-internal` (`open-webui`, `gatus`, `grafana`, `opensearch-cluster`, `paperless-ngx`, and `nextcloud`'s internal listener) — those resolve via `k8s-gateway` instead, a separate internal-only DNS path this app has no role in.

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/network/cloudflare-dns/ks.yaml` | Flux Kustomization (`targetNamespace: network`, `interval: 1h`, substitutes `${SECRET_DOMAIN}` via `postBuild`) |
| `kubernetes/apps/network/cloudflare-dns/app/helmrelease.yaml` | Chart values — provider, domain filters, sources, TXT registry, security context |
| `kubernetes/apps/network/cloudflare-dns/app/ocirepository.yaml` | OCI chart source pin |
| `kubernetes/apps/network/cloudflare-dns/app/secret.sops.yaml` | SOPS/age-encrypted `Secret` holding the Cloudflare API token |
| `kubernetes/apps/network/cloudflare-dns/app/ciliumnetworkpolicy.yaml` | Egress-only policy: CoreDNS, kube-apiserver, Cloudflare API (443) |

## Secrets
| ExternalSecret / Secret | Source | Consumed by |
| --- | --- | --- |
| `cloudflare-dns-secret` — a directly SOPS/age-encrypted `Secret` manifest, **not** an `ExternalSecret` sourced from 1Password like most apps in this repo | Key `api-token`, decrypted in-cluster by the SOPS Flux component | Env var `CF_API_TOKEN` on the `external-dns` container |

Rotation goes through `sops` re-encryption of this file directly, not a 1Password-item update + `ExternalSecret` refresh — see Common operations. `podAnnotations: secret.reloader.stakater.com/reload: cloudflare-dns-secret` means Stakater Reloader restarts the pod automatically once a re-encrypted secret is applied.

## Routing & access
- No HTTPRoute of its own — not exposed to any Gateway.
- The CNP defines **egress only** (no `ingress:` key at all) — allows CoreDNS (53/UDP+TCP), the `kube-apiserver` entity, and `world` on 443. No cluster-wide default-deny policy exists, so Prometheus's scrape reaches it unimpeded by absence of a competing rule, not by an explicit allow rule.
- `--cloudflare-proxied` means every record this controller creates is proxied through Cloudflare's edge (orange-cloud), not DNS-only — required for the Cloudflare Tunnel CNAMEs to actually route traffic; manually flipping a created record to "DNS only" would break tunnel routing for that hostname.
- `policy: sync` (vs `upsert-only`) means deleting an HTTPRoute or `DNSEndpoint` also deletes the corresponding Cloudflare record. `triggerLoopOnEvent: true` makes this near-immediate on k8s events rather than waiting for the full reconcile interval.
- `txtOwnerId: default` + `txtPrefix: k8s.` is the ownership-registry pattern — TXT records prefixed `k8s.` mark which DNS records this `external-dns` instance owns, so `policy: sync` doesn't delete records it didn't create.

## Storage
No PVCs — fully stateless. Source of truth is the Kubernetes API and the Cloudflare zone itself; nothing here needs Velero coverage.

## Known quirks
- **Two zones, one shared token.** `domainFilters` covers both `${SECRET_DOMAIN}` and `${SECRET_SECOND_DOMAIN}`, added in commit `90f7366` for the portfolio site. The single `CF_API_TOKEN` must have `Zone:DNS:Edit` scope on *both* zones in the Cloudflare dashboard — this can't be verified from the repo (a Cloudflare-side scope, not tracked by git), so if a new zone is ever added to `domainFilters` without widening the token's scope, records for that zone will silently fail to create/update.
- **Scoped by `--gateway-name=envoy-external`, not by namespace.** Any HTTPRoute anywhere in the cluster parented to the `envoy-external` Gateway gets a public DNS record automatically; anything on `envoy-internal` is invisible to this controller by design. Easy to mis-diagnose as "DNS not syncing" when the real issue is the HTTPRoute is parented to the wrong Gateway.
- **Not the repo's usual secret pattern.** Unlike most other apps' `ExternalSecret` + 1Password, this app's Cloudflare token is a directly SOPS-encrypted `Secret` — rotation is a `sops`/git operation, not a 1Password-item change.
- **`resources.limits` has no `cpu` key**, only `memory: 128Mi` — per commit `f4172f8`'s message, this is intentional repo-wide policy ("No CPU limits set intentionally — avoid throttling on nodes with free capacity"), not an oversight specific to this app.

## Common operations
- Upgrade chart: bump `spec.ref.tag` in `ocirepository.yaml`, commit, push — Flux reconciles within `interval: 15m`/`1h`, or force with `flux reconcile helmrelease cloudflare-dns -n network`.
- Add a new zone/domain: append it to `domainFilters` in `helmrelease.yaml`, **and** manually widen `CF_API_TOKEN`'s zone scope for that zone in the Cloudflare dashboard first.
- Rotate the API token: re-encrypt `secret.sops.yaml` with `sops` after generating a new Cloudflare token, commit, push. The `secret.reloader.stakater.com/reload` annotation restarts the pod automatically once Flux applies the new value.
- Pause reconciliation: `flux suspend kustomization cloudflare-dns -n flux-system` / `flux suspend helmrelease cloudflare-dns -n network`.
- Check what a new public HTTPRoute needs to get a DNS record: parent it to the `envoy-external` Gateway — no per-app cloudflare-dns config required.

## TODOs / unknowns
- Whether the `CF_API_TOKEN` currently has `Zone:DNS:Edit` scope on the `${SECRET_SECOND_DOMAIN}` zone cannot be verified from the repo — it's a Cloudflare dashboard-side setting.
- No entry in `docs/incidents/` references `cloudflare-dns` by name — no known past outage tied to this app as of this writing.
- Whether the CNP's lack of an explicit ingress allow-list is intentional or an oversight relative to sibling apps like `reloader` that do allowlist Prometheus ingress explicitly — not documented anywhere in the repo.

---
_Cite every non-obvious claim with a repo-root-relative file path (e.g. `kubernetes/apps/security/authentik/app/helmrelease.yaml`), not a bare filename — this doc lives under `docs/apps/`, so relative paths must resolve from there._
