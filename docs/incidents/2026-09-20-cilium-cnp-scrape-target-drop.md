# Missing CiliumNetworkPolicy Egress Ports Silently Drop 28% of Prometheus Scrape Targets

- **Date:** 2026-09-20
- **Component:** `monitoring/kube-prometheus-stack` (Prometheus egress `CiliumNetworkPolicy`), plus ingress-side CNP gaps on `grafana`, `kube-prometheus-stack` (Alertmanager config-reloader), `security/falco` (falcosidekick), `monitoring/loki` (loki-canary), `echo/echo`
- **Severity:** SEV1 — cluster-wide observability blind spot (avg(up) sat at ~73% for the entire Prometheus retention window with no alert), some targets (loki-canary, the operator itself) had *never once* successfully been scraped
- **Duration of impact:** Unknown exact start, at minimum the full Prometheus data-retention window (30d) up to discovery — fixed same-day across two commits, 15:57–16:11 CEST on 2026-09-20
- **Data loss:** None (metrics for the affected targets simply didn't exist — nothing to lose, but no historical data for them either)

## TL;DR
Fourteen of thirty-seven Prometheus scrape jobs had dead targets, every one failing with `context deadline exceeded` — the signature of a Cilium policy drop, not a slow endpoint. The `CiliumNetworkPolicy` governing Prometheus's own egress had an explicit port allow-list that was missing 9 ports across 9 different components (cert-manager, cilium-agent, hubble, coredns, cloudflare-dns, falcosidekick, loki-canary, envoy-proxy, echo). A further 3 targets had the egress port allowed but no matching *ingress* rule on the receiving pod. What hid this for so long: `cilium-agent`/`hubble` showed 3-of-7 nodes up, which reads like a per-node hardware/kubelet fault rather than a blanket policy gap — the 3 "working" nodes were the control-plane nodes, incidentally reachable only because the unrelated `toEntities: kube-apiserver` egress rule carries no port restriction and Cilium maps that entity to the CP node IPs. The policy was accidentally correct for 3 nodes for entirely unrelated reasons, masking the real gap.

## Impact
- **`avg(up)` sat at ~73% for as long as Prometheus retains data (30 days)** — nobody caught this because nothing alerts on "some fraction of scrape targets have always been down"; there is no meta-monitoring for Prometheus's own scrape health beyond what a human would notice by manually reviewing the Grafana dashboard (which happened only incidentally, while rebuilding it for an unrelated reason).
- **14 of 37 scrape jobs affected**, spanning cert-manager (3 components), cilium-agent, hubble, coredns, cloudflare-dns, falcosidekick, loki-canary, envoy-proxy, echo, Grafana's own metrics, the Alertmanager config-reloader sidecar, and — the most pointed casualty — **the Prometheus Operator itself**, meaning the one component whose job is to keep Prometheus's own config correct was invisible to Prometheus.
- **loki-canary (4 pods) had never once been successfully scraped** — this is specifically the mechanism meant to prove the log pipeline works end-to-end; it was blind for its entire lifetime up to this fix.
- No user-facing outage — this is a pure observability gap, not a service disruption. Severity is SEV1 on the "can't see cluster-wide state" reasoning from the severity scale, not on end-user impact.

## Symptoms
- Grafana's `up` panel (while being rebuilt for an unrelated reason) showed a persistent ~73% average, flat across the full 30-day retention window — not a recent regression, a standing condition.
- Every affected target's Prometheus target-status page showed the same error: `context deadline exceeded` — reads like a slow/hung endpoint but is the actual signature of a silent Cilium `CiliumNetworkPolicy` drop (connection attempt just hangs until the client's own timeout, no RST, no ICMP).
- `cilium-agent`/`hubble` targets specifically showed **3 of 7 nodes up** — looked like a per-node fault (bad kubelet, node cordoned, etc.) rather than what it actually was: an egress-port gap that happened to not apply to the 3 control-plane nodes, for reasons unrelated to those nodes' health.

## Root cause
**Trigger:** Not a single discrete change — this is a slow-accumulating policy-drift class of failure. Ports were presumably never added for these 9 components' `ServiceMonitor`/scrape-config additions in the first place (no single "before/after" commit that introduced a regression; the gap likely existed since each component's monitoring was first wired up).

**Underlying cause (two distinct, compounding gaps):**
1. **Egress side (9 of 14 targets):** `kubernetes/apps/monitoring/kube-prometheus-stack/app/ciliumnetworkpolicy.yaml`'s `toEntities: cluster` egress rule carries an explicit port allow-list rather than allowing all cluster-internal ports — an intentional, more-restrictive-than-default posture that requires manual maintenance on every new scrape target. It was missing: `9402` (cert-manager/cainjector/webhook), `9962` (cilium-agent), `9965` (hubble), `9153` (coredns), `7979` (cloudflare-dns), `2801` (falcosidekick), `3500` (loki-canary), `19001` (envoy-proxy), `80` (echo).
2. **Ingress side (3 of 14 targets):** even with egress allowed, the *receiving* pod's own `CiliumNetworkPolicy` had no matching ingress rule for Prometheus — Grafana's own metrics port, the Alertmanager config-reloader sidecar (port 8080), and the Prometheus Operator itself.
3. **Follow-up (`ef91190`) surfaced two further, unrelated-but-adjacent bugs found while closing out the remaining 3 targets:** falcosidekick's rule named port `2802` (that's `falcosidekick-ui`; metrics are on `2801`) — a one-digit typo that meant this scrape job had never once succeeded regardless of the CNP gap above; and loki-canary's pods carry `app.kubernetes.io/name: loki` so the `loki` CNP governs them too, but it only allowed `3100`, not the `3500` the canaries actually serve on.

**What masked the fault for so long:** `cilium-agent`/`hubble` showing 3-of-7 nodes up looked exactly like a hardware/kubelet-specific problem on 4 nodes, not a blanket policy gap on all 7 — because the 3 control-plane nodes happened to be reachable anyway via the unrelated, unscoped `toEntities: kube-apiserver` egress rule (Cilium maps that entity to the control-plane node IPs, and that rule carries no port restriction at all). The policy was accidentally right for 3 nodes for a reason that had nothing to do with those nodes actually being correctly configured.

## Timeline
- **Unknown start** — scrape targets for the affected 14 jobs never successfully resolve; `avg(up)` sits at ~73% from as far back as retention goes.
- **2026-09-20, ~15:57 CEST** — discovered incidentally while taking stock of Prometheus's own health ahead of a Grafana dashboard rebuild; root-caused the 9 missing egress ports and fixed in `350f0e8` (`fix(monitoring): unblock the 28% of scrape targets Cilium was dropping`).
- **2026-09-20, ~16:11 CEST** — follow-up pass (`ef91190`, `fix(monitoring): unblock the last three scrape targets, tidy Authentik`) found and fixed the remaining ingress-side gaps (Grafana, Alertmanager config-reloader, Prometheus Operator) plus the falcosidekick port typo and loki-canary port gap.
- Same commit also cleaned up an unrelated but adjacent finding: orphaned Authentik OpenSearch application/OAuth2-provider/scope-mapping objects left behind when that blueprint was deleted without first being removed via `ak shell` (blueprint deletion doesn't delete what it already created).

## Diagnosis process

### What did NOT work
- Nothing was tried and discarded — `context deadline exceeded` was recognized immediately as a Cilium-drop signature rather than a genuinely slow endpoint, and the per-node `3/7` pattern for cilium-agent/hubble was specifically flagged as suspicious (a real per-node fault would not cleanly split along the control-plane/worker line) rather than accepted at face value.

### What DID work
1. Enumerated all 37 Prometheus scrape jobs and cross-referenced target status against each component's actual serving port (not assumed from memory) — found the port mismatches directly this way (e.g., falcosidekick's CNP rule vs. its real metrics port).
2. Recognized that the 3 "healthy" cilium-agent/hubble nodes were exactly the control-plane set, and traced that to the unrelated, unscoped `kube-apiserver` entity rule rather than assuming those 3 nodes were simply fine.
3. Added the missing egress ports to the Prometheus CNP in one pass (`350f0e8`), then re-checked target status and found 3 remaining failures were ingress-side, not egress — fixed those individually in the follow-up commit (`ef91190`).

## Fix applied

### Live remediation
None needed — `CiliumNetworkPolicy` changes take effect immediately on apply via Flux; no restart or manual step required beyond the next scrape interval.

### Preventive change committed
- `kubernetes/apps/monitoring/kube-prometheus-stack/app/ciliumnetworkpolicy.yaml` (commit `350f0e8`) — added the 9 missing egress ports, with an inline comment now stating explicitly that this allow-list must be extended by hand for every new `ServiceMonitor` and that nothing fails loudly when it isn't. Also flagged (not yet fixed) that port `8001`, labelled "envoy proxy stats," looks wrong — Envoy's actual stats port is `19001`.
- `kubernetes/apps/monitoring/grafana/app/ciliumnetworkpolicy.yaml` (commit `350f0e8`) — added ingress rule for Prometheus scraping.
- `kubernetes/apps/echo/echo/app/ciliumnetworkpolicy.yaml`, `kubernetes/apps/monitoring/loki/app/ciliumnetworkpolicy.yaml`, `kubernetes/apps/security/falco/app/ciliumnetworkpolicy.yaml` (commit `ef91190`) — added/corrected the remaining ingress-side rules and the falcosidekick port typo.
- `kubernetes/apps/security/authentik/app/blueprints/09-wazuh-oidc.yaml` (commit `ef91190`, unrelated cleanup found in passing) — moved Wazuh's Authentik application group from "Security" to "Monitoring."

## Runbook — if this fires again
1. **Confirm the signature:** a Prometheus target shows `context deadline exceeded` (not a 4xx/5xx, not connection-refused) — this specifically indicates a silent network-policy drop, not an application-level failure.
2. **Check both directions**, not just the scraper's egress: the target pod's own `CiliumNetworkPolicy` needs an ingress rule allowing Prometheus, independent of whether Prometheus's egress allows the port.
3. **Don't trust an apparently per-node pattern at face value** — if some nodes/replicas are up and others aren't, check whether the "healthy" ones are incidentally covered by an unrelated, broader rule (e.g. `kube-apiserver`/`host` entities) before concluding it's a per-node fault.
4. **Cross-check the CNP's allowed port against the component's actual serving port directly** (chart values, container spec, `kubectl get svc -o yaml`) — don't assume the port in an existing CNP comment is correct; this incident's own falcosidekick fix was a one-digit typo that had been wrong since the rule was first written.
5. **There is still no automated alert for "a scrape target has been down since forever."** Consider a Prometheus recording rule/alert on `up == 0` sustained over a long window (e.g. `min_over_time(up[7d]) == 0`), since a single-point `up == 0` alert wouldn't have caught this (these targets were never up in the first place, so there was no transition to alert on).

## References
- Fix commits: `350f0e8` (`fix(monitoring): unblock the 28% of scrape targets Cilium was dropping`), `ef91190` (`fix(monitoring): unblock the last three scrape targets, tidy Authentik`)
- Related: `docs/apps/kube-prometheus-stack.md`, `docs/apps/cilium.md`, `docs/apps/grafana.md`, `docs/apps/falco.md`, `docs/apps/loki.md`

## Action items
- [x] GitOps preventive change committed (`350f0e8`, `ef91190`)
- [x] Postmortem written (this file)
- [ ] Add a sustained-`up==0` alert rule so a future silent scrape-target drop is caught by monitoring itself rather than incidental discovery during unrelated work
- [ ] Investigate and fix (or remove) the flagged-but-unresolved port `8001` "envoy proxy stats" entry in the Prometheus CNP — comment states it looks wrong (Envoy's real stats port is `19001`), not corrected in this pass
- [ ] Audit whether any *other* CiliumNetworkPolicy with an explicit port allow-list (rather than an entity-only rule) has drifted out of sync with its component's actual ports the same way — this pass only checked Prometheus's own scrape targets, not every CNP in the repo

---
_For related context: `docs/apps/kube-prometheus-stack.md`, `docs/apps/cilium.md`._
