# k8s-gateway

> **Namespace**  `network`
> **Source**     OCI Helm chart `oci://ghcr.io/k8s-gateway/charts/k8s-gateway`, tag `3.7.2` (`kubernetes/apps/network/k8s-gateway/app/ocirepository.yaml`)
> **Hostname**   None of its own — it's a DNS server, not an HTTP-routed app. Exposed as a `LoadBalancer` Service on port 53.

## What it does here
This is the [k8s_gateway](https://github.com/k8s-gateway/k8s_gateway) project — a CoreDNS plugin packaged as its own Deployment, distinct from Kubernetes' own Gateway API despite the similar name (the root `README.md` lists them as two separate stack entries: "Envoy Gateway (Gateway API)" vs "k8s_gateway (home split-DNS)"). It's configured with `domain: "${SECRET_DOMAIN}"` and `watchedResources: ["HTTPRoute", "Service"]` — it watches every `HTTPRoute`/`Service` in the cluster and answers DNS queries for the cluster's own domain based on what those resources expose, making it authoritative for that zone rather than just a forwarder. Its LoadBalancer IP is where the home router (OPNsense) is configured to split-DNS-forward the domain's queries — so LAN/VPN clients resolve the cluster's own hostnames straight to internal addresses instead of the public Cloudflare edge.

## Architecture at a glance
- **Depends on:** CoreDNS (`kube-system`) for its own upstream resolution; Cilium's LoadBalancer IP pool for its LB IP; `kube-apiserver`, to watch `Service`/`HTTPRoute` objects cluster-wide. No `ExternalSecret`/1Password dependency — the app directory has no secrets file at all.
- **Depended on by:** every app whose `HTTPRoute` needs to resolve to an internal address for LAN/VPN clients rather than the public Cloudflare tunnel path — concretely the apps attached to the `envoy-internal` Gateway: `nextcloud`, `open-webui`, `security/authentik`, `paperless/paperless-ngx`, `monitoring/grafana`, `monitoring/gatus`, `logging/opensearch-cluster`. `cert-manager` deliberately does **not** rely on it for its ACME self-check — see Known quirks.

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/network/k8s-gateway/app/helmrelease.yaml` | Chart values: `domain`, `watchedResources`, LB service, TTL, resources, hardened security contexts |
| `kubernetes/apps/network/k8s-gateway/app/ocirepository.yaml` | Pins chart to `3.7.2`, 1h poll interval |
| `kubernetes/apps/network/k8s-gateway/app/ciliumnetworkpolicy.yaml` | DNS ingress/egress rules, with inline commentary on the SNAT/DNAT port semantics (see Routing & access) |
| `kubernetes/apps/network/k8s-gateway/app/kustomization.yaml` | Bundles the three manifests above |
| `kubernetes/apps/network/k8s-gateway/ks.yaml` | Flux Kustomization; `targetNamespace: network`, substitutes `${SECRET_DOMAIN}` from the `cluster-secrets` Secret |

## Secrets
None. No `ExternalSecret` or SOPS secret file exists anywhere under this app's directory — the only sensitive value involved is `${SECRET_DOMAIN}`, substituted at Kustomization build time from the SOPS-encrypted `cluster-secrets` Secret, never resolved in this app's own manifests.

## Routing & access
- No `HTTPRoute` — this app doesn't sit behind Envoy Gateway; it *is* the DNS resolver other apps' `HTTPRoute` hostnames get resolved through for LAN/VPN clients.
- `Service` type `LoadBalancer` on port 53, `externalTrafficPolicy: Cluster`.
- **CiliumNetworkPolicy is genuinely non-obvious here** — Cilium's kube-proxy-replacement enforces policy **post-DNAT**, so every rule targets container port **1053**, not the Service's port 53:
  - Ingress from `world` (the LAN/OPNsense path via the LB IP) on 1053 — the file's header comment explains why: the L2-owner node SNATs the client IP to its own node IP before DNAT'ing to the pod, so the packet the pod actually sees has `src=node-IP` (`remote-node` entity), not the original client.
  - A second ingress rule explicitly allows `remote-node` on 1053 for that same reason — added by commit `c388129` ("allow remote-node on port 1053 for k8s-gateway with SNAT LB mode") after the first cut of the policy didn't account for the SNAT hop.
  - Ingress from `kube-dns` (east-west) and from `monitoring`'s Prometheus on 9153 (pod-direct metrics scrape, no DNAT).
  - Egress to `kube-dns:53` (self-resolution) and to the `kube-apiserver` entity (watching `Service`/`HTTPRoute` objects for records to serve).
  - This ruleset took four follow-up commits to get right, all converging on the same root cause: post-DNAT policy evaluation plus SNAT'd LoadBalancer traffic needing `remote-node`, not just `world`, on the container port.

## Storage
None. No PVCs anywhere under this app's directory. The `network` namespace is not in Velero's `includedNamespaces` list — expected, since all state here is either git-defined or reissuable from watched cluster objects.

## Known quirks
- **`ttl: 1`** — a 1-second DNS TTL, unusually low by any normal standard. Makes sense given the whole point of this app is to reflect live `HTTPRoute`/`Service` changes almost immediately; the tradeoff is that resolvers essentially never cache answers from it.
- **The CoreDNS↔k8s-gateway "zone delegation" described in both apps' CiliumNetworkPolicy comments doesn't correspond to a visible Corefile stanza.** This app's own CNP header says "CoreDNS in kube-system forwards selected zones here," and `kube-system/coredns`'s CNP has a reciprocal egress rule commented "k8s-gateway zone delegation." But `coredns`'s actual Corefile has exactly one zone (`.`), forwarding to `/etc/resolv.conf` — no distinct zone/`forward` stanza targeting k8s-gateway — and `docs/apps/coredns.md` doesn't mention k8s-gateway at all. The only confirmed path from a LAN client to this app is OPNsense's own split-DNS config pointing at the LB IP; the CNP egress rule from CoreDNS is provisioned but its trigger condition inside the Corefile isn't visible in-repo. Flagged as a TODO rather than asserted as fact.
- **Corroborates, doesn't just infer, `docs/apps/cert-manager.md`'s reasoning for pinning ACME self-checks to public DoH.** That doc guesses cert-manager avoids local DNS because "k8s-gateway serves `${SECRET_DOMAIN}` itself as an authoritative internal split-horizon zone." This app's `domain: "${SECRET_DOMAIN}"` setting confirms the authoritative-zone half of that claim directly — k8s_gateway, once configured with a `domain`, claims authority over the whole zone rather than only answering for names it has records for. Whether an in-cluster pod's default resolution path would actually reach this app for such a query isn't traced end-to-end (see the delegation-mechanism quirk above).

## Common operations
- Upgrade chart version: bump `tag` in `ocirepository.yaml`, commit, push (Flux `OCIRepository` polls every 1h, or force with `flux reconcile ocirepository k8s-gateway -n network` then `flux reconcile helmrelease k8s-gateway -n network`).
- Change the served domain or LB IP: edit `domain` or the `lbipam.cilium.io/ips` annotation in `helmrelease.yaml`, commit, push.
- Pause reconciliation: `flux suspend kustomization k8s-gateway -n flux-system` / `flux suspend helmrelease k8s-gateway -n network`.
- Test resolution directly against the LB IP: `dig @<lb-ip> <name>.${SECRET_DOMAIN}` from any LAN host.

## TODOs / unknowns
- The exact mechanism (if any) by which CoreDNS's Corefile actually delegates `${SECRET_DOMAIN}` queries to this app is unverified — the CNP comments on both sides describe it, but no corresponding Corefile `forward`/zone stanza was found, and `docs/apps/coredns.md` doesn't reference it either.
- Whether cert-manager's DNS-01 self-check pod could actually reach this app via default in-cluster resolution (as opposed to only LAN clients via OPNsense) is inferred from the `domain` setting, not traced through an actual resolution path.
- Replica count and whether a `ServiceMonitor`/`PodMonitor` exists for the Prometheus scrape allowed by the CNP aren't set in this app's `helmrelease.yaml` — likely chart defaults, not confirmed from the chart's own `values.yaml`.

---
_Cite every non-obvious claim with a repo-root-relative file path (e.g. `kubernetes/apps/security/authentik/app/helmrelease.yaml`), not a bare filename — this doc lives under `docs/apps/`, so relative paths must resolve from there._
