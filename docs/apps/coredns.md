# CoreDNS

> **Namespace**  kube-system
> **Source**     `coredns` OCIRepository, chart `coredns` (`kubernetes/apps/kube-system/coredns/app/helmrelease.yaml`)
> **Hostname**   none — cluster-internal DNS only, `kube-dns` Service at `10.43.0.10`

## What it does here
Cluster DNS, replacing Talos's built-in CoreDNS (`cluster.coreDNS.disabled: true` in `talos/patches/controller/cluster.yaml`) so it can be Flux/Helm-managed instead of static-pod-managed. Beyond stock service discovery, this deployment carries two cluster-specific behaviors: split-horizon resolution for two hostnames, and cluster-wide AAAA suppression (this cluster has no working IPv6 egress anywhere).

## Architecture at a glance
- **Depends on:** nothing — first-tier cluster infrastructure, everything else depends on it.
- **Depended on by:** literally every pod doing any DNS lookup, in-cluster or external. A regression here is maximally cluster-wide by nature (see Known quirks and `docs/incidents/2026-08-16-coredns-aaaa-nxdomain-breaks-internal-dns.md`).

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/kube-system/coredns/app/helmrelease.yaml` | Full Corefile plugin chain, resources, autoscaling, node affinity |

## Secrets
None.

## Routing & access
N/A — not exposed via Gateway/HTTPRoute; consumed cluster-internally via the `kube-dns` Service ClusterIP (`10.43.0.10`), pinned explicitly (`service.clusterIP`) so it matches every pod's injected `resolv.conf`.

## Storage
None.

## Known quirks
- **Split-horizon DNS for two hostnames** (`hosts` plugin, `helmrelease.yaml`): `id.${SECRET_DOMAIN}` (Authentik) and `cloud.${SECRET_DOMAIN}` (Nextcloud) resolve in-cluster to the `envoy-internal` gateway ClusterIP instead of the public Cloudflare edge. Both exist to avoid a Cloudflare "CDN loop" rejection when an in-cluster service calls back into one of these hostnames server-to-server (OpenSearch validating Authentik JWKS; Collabora's WOPI `CheckFileInfo` callback into Nextcloud, which needs to arrive from a pod-CIDR IP for `richdocuments`' `wopi_allowlist` to accept it — see `docs/apps/nextcloud.md`). Uses `hosts` rather than `rewrite name` deliberately: `rewrite` only affects A queries, and several in-cluster clients (Node.js) prefer AAAA and would still hairpin through Cloudflare and time out.
- **Cluster-wide AAAA suppression, and a sharp edge in how it's implemented.** A `template ANY AAAA .` rule returns an empty/no-record answer for every AAAA query cluster-wide, because this cluster has zero working IPv6 egress and several subsystems (Kopia/Velero, Flux artifact fetches, Alertmanager→Pushover) would otherwise pick the AAAA address of a dual-stack external host and hang. **The rule must use `rcode NOERROR` (NODATA), never `rcode NXDOMAIN`.** The `kubernetes` plugin does not fully own AAAA queries for existing `cluster.local` names (no `fallthrough` configured for that zone, no IPv6 endpoint to answer with), so the template rule still sees AAAA queries for real internal service names. `NXDOMAIN` there falsely asserts the whole name doesn't exist, and glibc-based resolvers (confirmed live: Nextcloud's PHP container) abort instead of falling back to the working A record — this exact regression happened 2026-08-16, see the incident postmortem linked below. If this plugin block is ever touched again, treat `rcode NOERROR` as load-bearing, not a stylistic choice.
- **Plugin order matters.** `kubernetes` (with `fallthrough in-addr.arpa ip6.arpa`) must come before the AAAA `template` rule, so internal `cluster.local`/reverse-lookup resolution isn't itself broken by the suppression rule. Current order: `errors, health, ready, hosts, kubernetes, autopath, template (AAAA), forward, cache, loop, reload, loadbalance, prometheus, log`.

## Common operations
- Edit the Corefile: all of it lives inline in `helmrelease.yaml` under `servers[0].plugins` — no separate ConfigMap to hand-edit.
- Test a change before trusting it live: `kubectl run --rm -it --image=busybox:1.38.0 -- nslookup <name>` from any namespace, checking specifically that AAAA queries return NODATA (not NXDOMAIN) for names you expect to still resolve via A.
- Force reconcile: `flux reconcile helmrelease coredns -n kube-system`.

## TODOs / unknowns
- No current test/CI step verifies "AAAA query to an existing cluster.local name returns NOERROR, not NXDOMAIN" automatically — the 2026-08-16 incident was caught by user report, not monitoring. A synthetic check here would close a real gap (tracked as an action item in the incident postmortem).

---
_See also: `docs/incidents/2026-08-16-coredns-aaaa-nxdomain-breaks-internal-dns.md`._
