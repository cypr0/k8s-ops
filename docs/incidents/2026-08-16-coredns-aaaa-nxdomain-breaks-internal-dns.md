# CoreDNS AAAA Template Rule Breaking Internal Service Resolution

- **Date:** 2026-08-16
- **Component:** `kube-system/coredns`
- **Severity:** SEV1 — cluster-wide risk (any pod whose resolver aborts on NXDOMAIN could lose internal DNS), confirmed impact on Nextcloud
- **Duration of impact:** ~15 min (11:05–11:20 UTC)
- **Data loss:** None

## TL;DR
A same-day CoreDNS change (this session's earlier F1 fix, meant to stop external dual-stack hosts from hanging on AAAA lookups with no IPv6 egress) used `template ANY AAAA . { rcode NXDOMAIN }`. The `kubernetes` plugin doesn't fully own AAAA queries for existing `cluster.local` names — it has no IPv6 endpoint to answer with and no `fallthrough` configured for that zone — so the rule also caught AAAA queries for real, existing internal service names. Nextcloud's PHP container (glibc resolver) treated the NXDOMAIN answer to the AAAA half of a dual A/AAAA lookup as proof the name doesn't exist at all, and never tried the A record — `could not translate host name "postgres-rw.database.svc.cluster.local" to address`. Fixed by returning `NOERROR` (NODATA) instead of `NXDOMAIN`.

## Impact
- Nextcloud lost its database connection cluster-wide; `nextcloud-nginx`'s startup probe failed repeatedly (`GET /status.php` → 500), `nextcloud` container fatal-errored on every request needing DB access.
- Likely also caused a transient open-webui Kopia/Velero backup job error reported the same window (not confirmed with direct log evidence — see TODO below), self-recovered on retry once DNS was fixed.
- Everything resolving `cluster.local` names via the A record was at equal risk during the window — Nextcloud is simply what surfaced first and loudest (nginx probes retry every 10s, generating a clear signal fast).
- External AAAA suppression (the original F1 goal — Kopia/Flux/Alertmanager not hanging on AAAA for dual-stack external hosts) continued working throughout; this incident is scoped entirely to the *internal* side-effect.

## Symptoms

### Nextcloud PHP log
```
NOTICE: PHP message: PHP Fatal error:  Uncaught Doctrine\DBAL\Exception:
Failed to connect to the database: An exception occurred in the driver:
SQLSTATE[08006] [7] could not translate host name
"postgres-rw.database.svc.cluster.local" to address: Name does not resolve
in /var/www/html/lib/private/DB/Connection.php:228
```

### nginx access log
```
192.168.10.24 - - [16/Aug/2026:11:12:18 +0000] "GET /status.php HTTP/1.1" 500 5 "-" "kube-probe/1.36" "-"
```
(repeated every ~10s for the full duration — kubelet's own startup-probe retry cadence)

### Direct reproduction
```
$ kubectl run --rm -it --image=busybox:1.38.0 -- nslookup postgres-rw.database.svc.cluster.local
** server can't find postgres-rw.database.svc.cluster.local: NXDOMAIN
Name:	postgres-rw.database.svc.cluster.local
Address: 10.43.59.63
```
Key signal: the *same* nslookup response contains both an NXDOMAIN result (for the AAAA query) and a correct address (for the A query) — the name genuinely resolves, only the AAAA half is poisoned.

Cross-checked `postgres-rw`'s own Service/Endpoints (`kubectl get svc,endpoints -n database postgres-rw`) to rule out an actual Service/Endpoints problem before looking at DNS — Endpoints were correct (`10.42.0.231:5432`) throughout, confirming this was purely a resolution-layer issue.

## Root cause
**Trigger:** this session's own earlier commit (`d77aa09`, F1 fix) added `template ANY AAAA . { rcode NXDOMAIN }` after the `kubernetes` plugin in `kubernetes/apps/kube-system/coredns/app/helmrelease.yaml`, intended only to suppress AAAA answers for *external* names (this cluster has no working IPv6 egress anywhere).

**Underlying cause:** the `kubernetes` plugin's zone list (`cluster.local in-addr.arpa ip6.arpa`) only has `fallthrough` configured for the two reverse-lookup zones, not for `cluster.local` itself. For an AAAA query against an existing `cluster.local` service (which has no IPv6 endpoint), the plugin does not terminate the chain with its own NODATA answer the way the original fix's design comment assumed — the query still reaches the `template` rule below it. `rcode NXDOMAIN` there asserts non-existence of the whole name, which glibc's resolver (used inside Nextcloud's official PHP-based image) treats as authoritative for both record types, aborting instead of falling back to the working A record.

## Timeline
- **~10:30–11:00 UTC** — F1 CoreDNS fix (commit `d77aa09`) committed, pushed, and reconciled as part of the broader cluster-health-check remediation session.
- **~11:05 UTC** — Nextcloud pod's DB connections start failing; nginx startup probe begins failing on `/status.php`.
- **11:12–11:16 UTC** — user reports "Nextclouds nginx wird nicht healthy" alongside two other unrelated live issues (Authentik GeoIP outage, a transient open-webui Kopia job error).
- **11:16 UTC** — root cause identified via direct `nslookup` reproduction against an existing `cluster.local` name.
- **~11:18 UTC** — fix committed (`80b8f4a`) and pushed.
- **11:19–11:20 UTC** — CoreDNS rolled out, resolution verified clean, Nextcloud pod confirmed `3/3 Running`, `status.php` returning `200`.

## Diagnosis process

### What did NOT work
- Nothing was tried and discarded here — the DB-connection error message named the exact failing hostname, and testing that hostname's resolution directly was the first and only diagnostic step needed. Cross-checking the Service/Endpoints object first (before assuming DNS) confirmed the Service side was fine, narrowing straight to DNS.

### What DID work
Reproducing the failure directly with `nslookup` against the specific hostname from the error message, from a throwaway pod in the same cluster:
```
kubectl run --rm -it --image=busybox:1.38.0 -- nslookup postgres-rw.database.svc.cluster.local
```
Seeing both an NXDOMAIN line and a correct `Address:` line in the same output immediately pointed at an AAAA-specific answer poisoning an otherwise-working name — which matched the shape of the AAAA-suppression rule added earlier the same session.

## Fix applied

### Live remediation
None beyond the GitOps fix itself — there was no faster stop-the-bleeding step available (the DNS answer is generated per-query, not cached state to flush), so the fix below *was* the live remediation, applied as fast as the normal Flux reconcile path allowed.

### Preventive change committed
`kubernetes/apps/kube-system/coredns/app/helmrelease.yaml` (commit `80b8f4a`): changed the `template ANY AAAA .` rule's `configBlock` from `rcode NXDOMAIN` to `rcode NOERROR`. `NOERROR` with no `answer` clause returns NODATA (empty answer section) — the semantically correct response for "name exists, no AAAA record" — which every resolver, glibc included, correctly falls back from to the A record. The external AAAA-suppression goal is unaffected: those external hosts never had an A-record fallback concern CoreDNS needed to preserve.

## Runbook — if this fires again
1. **Confirm the signature:** an app logs a DNS-resolution failure for a name you can independently verify resolves via `kubectl get svc,endpoints -n <ns> <name>`.
2. **Reproduce directly:**
   ```bash
   kubectl run --rm -it --image=busybox:1.38.0 -n <affected-ns> -- nslookup <the-failing-hostname>
   ```
   If the output contains an NXDOMAIN line alongside a correct `Address:` line, this is the same bug class.
3. **Check the current rule:** `kubernetes/apps/kube-system/coredns/app/helmrelease.yaml` → the `template ANY AAAA .` plugin block. Confirm it still says `rcode NOERROR`, not `NXDOMAIN` — if someone reverted it, that's the regression.
4. **If the rule is already correct but the symptom recurs**, the bug is elsewhere (this specific failure mode is now closed) — don't assume this postmortem's fix without re-verifying step 3 first.

## References
- Fix commit: `80b8f4a` — `fix(coredns): return NODATA not NXDOMAIN for AAAA (was breaking internal DNS)`
- Original (regressing) commit: `d77aa09` — F1 CoreDNS AAAA-suppression fix
- Auto-memory: `project_coredns_aaaa_nxdomain_gotcha` (persistent cross-session note on this exact gotcha)

## Action items
- [x] Live/GitOps fix applied (`80b8f4a`)
- [x] Postmortem written (this file)
- [ ] Confirm the open-webui Kopia job error from the same window was actually DNS-caused, not a coincidence (TODO — no direct log evidence gathered, only temporal correlation)
- [ ] Consider a synthetic check/alert for "AAAA query to an existing cluster.local name returns NXDOMAIN" to catch a future regression of this rule automatically

---
_For related context: `docs/apps/nextcloud.md` (not yet written) and the auto-memory entry `project_coredns_aaaa_nxdomain_gotcha`._
