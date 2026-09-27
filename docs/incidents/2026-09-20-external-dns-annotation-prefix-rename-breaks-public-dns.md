# external-dns v0.22.0 Annotation-Prefix Rename Silently Drops Public DNS Records

- **Date:** 2026-09-20
- **Component:** `network/external-dns` (annotation consumption), `mail/mailu` (`dnsendpoint.yaml`), `network/envoy-gateway` (`envoy.yaml`)
- **Severity:** SEV1 — real-world impact (inbound mail delivery failure across multiple domains, missing public DNS for 7 hostnames), invisible to every in-cluster health check for an unknown but non-trivial duration
- **Duration of impact:** Unknown start (silent regression tied to an `external-dns` chart bump, exact date of that bump not pinned down in this pass) — discovered and fixed 2026-09-20, both fixes ~3 minutes apart (21:22–21:26 CEST)
- **Data loss:** None (mail was rejected/timed out at the sender, not lost after acceptance)

## TL;DR
`external-dns` v0.22.0 renamed its `DefaultAnnotationPrefix` from `external-dns.alpha.kubernetes.io/` to `external-dns.kubernetes.io/`. This cluster's manifests still used only the old prefix on two resources: Mailu's `mail.${SECRET_DOMAIN}` DNSEndpoint (which needs `cloudflare-proxied: "false"` — mail ports can't go through Cloudflare's HTTP proxy) and the main Envoy Gateway (which needs `target`, `hostname`, and a `controller: none` override on the redirect route). Because an unrecognised `providerSpecific` annotation key is **ignored, not rejected**, both resources silently reverted to `external-dns`'s chart-wide defaults: the mail record got proxied (breaking inbound SMTP entirely) and 7 other hostnames (`cloud`, `id`, `collabora`, `media`, `echo`, `grafana`, `flux-webhook`) lost their public DNS record altogether, because `external-dns` tried to publish a proxied CNAME to a private RFC1918 IP and Cloudflare rejected every such CREATE. Nothing alerted, because every Gatus check runs inside the cluster and CoreDNS/k8s-gateway kept answering those names correctly from the LAN throughout.

## Impact
- **Inbound mail to `mail.${SECRET_DOMAIN}` was unreachable from the internet.** Senders connected on port 25, got no SMTP greeting, and timed out — no bounce, nothing in this cluster's own logs, because the connection never reached Mailu at all (Cloudflare's proxy accepted the TCP handshake and then said nothing).
- **Second-order SPF failure:** two of the cluster's other secondary domains carry SPF records referencing `mail.${SECRET_DOMAIN}` via an `a:` mechanism, which resolved to Cloudflare's proxy IPs instead of the real mail server. Combined with DMARC `p=reject`/`aspf=s` already set on both domains, outbound mail from them was failing SPF at every DMARC-enforcing receiver.
- **7 hostnames had no public A/CNAME record at all**: `cloud`, `id`, `collabora`, `media`, `echo`, `grafana`, `flux-webhook`. Anyone trying to reach these from outside the LAN (e.g. mobile clients off-network) would have gotten NXDOMAIN.
- **Nothing internal was affected.** k8s-gateway/CoreDNS answer all of these names correctly for in-cluster and LAN clients regardless of what's published externally, so every Gatus check (which runs from inside the cluster) passed the whole time. This is precisely why it went unnoticed.

## Symptoms
- Direct connection test to the mail server's public edge IPs: TCP connects, no SMTP banner, then timeout — the signature of Cloudflare's HTTP-only proxy silently swallowing a non-HTTP protocol.
- `external-dns` controller logs (nobody was watching them) showed repeated `CREATE` failures:
  ```
  Target 192.168.10.103 is not allowed for a proxied record
  ```
  — once per unresolved hostname, every reconcile interval, indefinitely.
- No CoreDNS, Gatus, or Flux health-check signal — this incident had **zero automated detection**, purely manual discovery while fixing the mail issue and then generalizing the check to other hostnames.

## Root cause
**Trigger:** `external-dns` was upgraded to v0.22.0 (exact commit/date not pinned in this pass — the regression predates its discovery by an unknown margin).

**Underlying cause:** v0.22.0 changed `DefaultAnnotationPrefix` from `external-dns.alpha.kubernetes.io/` to `external-dns.kubernetes.io/` (confirmed against `source/annotations/annotations.go` in both tags). Two resources in this repo carried `providerSpecific` overrides under only the old prefix:
- `kubernetes/apps/mail/mailu/app/dnsendpoint.yaml` — `cloudflare-proxied: "false"` (mail ports need the DNS-only, unproxied record; Cloudflare's proxy is HTTP/HTTPS-only and silently drops non-HTTP TCP traffic rather than erroring).
- `kubernetes/apps/network/envoy-gateway/app/envoy.yaml` — `target: external.${SECRET_DOMAIN}` (CNAME to the Cloudflare Tunnel hostname instead of the Gateway's own address), a `hostname` override on the infrastructure block, and `controller: none` on the `https-redirect` HTTPRoute (keeps that route out of DNS entirely).

An unrecognised `providerSpecific` key is **ignored by `external-dns`, not rejected** — no error, no event, no admission failure. Without the annotation, `external-dns` fell back to its chart-wide `--cloudflare-proxied` default (`true`) and the Gateway's real (private) address. Cloudflare then refused every CREATE for a proxied record pointing at an RFC1918 IP, silently dropping DNS for every hostname routed through that Gateway.

## Timeline
- **Unknown date** — `external-dns` bumped to v0.22.0; both resources' old-prefix annotations stop being recognised. No detection at this point.
- **2026-09-20, ~21:22 CEST** — Mailu MX failure investigated directly (SMTP connect-then-timeout against the mail server's edge IPs); root cause (annotation-prefix rename, `cloudflare-proxied` no longer honored) identified and fixed in `0b4fbbd`.
- **2026-09-20, ~21:26 CEST** — Same root cause generalized to check every other externally-routed hostname; found 7 more with no public record at all; fixed in `c552cf3` by declaring both prefixes on the Gateway's annotations.
- **Both fixes committed and pushed same session** — no separate live remediation needed beyond the GitOps fix; `external-dns`'s own reconcile loop re-created the missing records once the annotations were recognised again.

## Diagnosis process

### What did NOT work
- Nothing was tried and discarded — this was root-caused directly from the `external-dns` controller's own log line (`Target ... is not allowed for a proxied record`) and cross-referenced against the chart's changelog/source for the exact prefix rename. The actual difficulty was **detection latency**, not diagnosis: nothing surfaced this until someone was already looking at the mail record for an unrelated complaint.

### What DID work
1. Read the `external-dns` controller logs directly — the `CREATE` failure message named the exact IP and the exact Cloudflare API rejection reason.
2. Diffed the `DefaultAnnotationPrefix` constant between the previously-pinned and currently-running chart tag in `siderolabs/external-dns` (source/annotations/annotations.go) — confirmed the rename.
3. Declared **both** the old and new annotation prefixes on every affected resource. Annotations are a plain map, so carrying both costs nothing and survives the rename in either direction (relevant if a future version reverts or renames again).

## Fix applied

### Live remediation
None needed beyond the GitOps commits below — `external-dns` re-created the missing records on its own next reconcile once the annotations were recognised again.

### Preventive change committed
- `kubernetes/apps/mail/mailu/app/dnsendpoint.yaml` (commit `0b4fbbd`, `fix(mailu): un-proxy mail.${SECRET_DOMAIN} — an external-dns rename broke the MX`) — declared `cloudflare-proxied: "false"` under both the `alpha` and non-`alpha` prefix.
- `kubernetes/apps/network/envoy-gateway/app/envoy.yaml` (commit `c552cf3`, `fix(network): restore public DNS — the same external-dns rename took it out`) — declared `target`, `hostname`, and `controller: none` under both prefixes.

## Runbook — if this fires again
1. **Confirm the signature:** a hostname that should be publicly resolvable returns NXDOMAIN externally but resolves fine from inside the LAN/cluster (k8s-gateway/CoreDNS still answer it) — or a non-HTTP service (mail, anything not behind Envoy) is reachable on TCP but never gets a protocol response.
2. **Check the `external-dns` controller's own logs** for `CREATE`/`UPDATE` failures — an unrecognised-annotation regression shows up there as a rejected API call, not as a Kubernetes-level error anywhere else.
3. **Diff `DefaultAnnotationPrefix`** between the currently-pinned and any newer `external-dns` chart/image tag if a version bump is suspected as the trigger.
4. **Fix by declaring both prefixes**, not by picking one — this survives future renames in either direction with zero ongoing cost.
5. **There is still no automated detection for this class of failure.** Consider a Gatus check that resolves each externally-routed hostname against a public resolver (e.g. `1.1.1.1`) from outside the cluster's own DNS view — every existing check runs in-cluster and would stay green through a repeat of this exact incident.

## References
- Fix commits: `0b4fbbd` (`fix(mailu): un-proxy mail.${SECRET_DOMAIN}`), `c552cf3` (`fix(network): restore public DNS`)
- Related: `docs/apps/cloudflare-dns.md`, `docs/apps/cloudflare-tunnel.md`, `docs/apps/mailu.md`, `docs/apps/envoy-gateway.md`

## Action items
- [x] GitOps preventive change committed (`0b4fbbd`, `c552cf3`)
- [x] Postmortem written (this file)
- [ ] Add a Gatus check that resolves externally-routed hostnames against a public DNS resolver from outside the cluster's own view, so a repeat of this exact failure mode (correct in-cluster resolution, broken public resolution) would actually alert
- [ ] Audit every other `providerSpecific` annotation in the repo for the same old-prefix-only pattern — only Mailu and the main Envoy Gateway were checked/fixed in this pass; other DNSEndpoint/Gateway/HTTPRoute resources with `external-dns.alpha.kubernetes.io/...` annotations were not exhaustively re-audited
- [ ] Pin down the exact `external-dns` version-bump commit/date that introduced the regression, to bound how long the outage actually lasted (currently unknown — could be days to weeks)

---
_For related context: `docs/apps/cloudflare-dns.md`, `docs/apps/mailu.md`, `docs/apps/envoy-gateway.md`._
