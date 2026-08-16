# Authentik GeoIP Sidecar Crash-Loop Causing Full SSO Outage

- **Date:** 2026-08-16
- **Component:** `security/authentik` (server + worker Deployments, `geoip` sidecar)
- **Severity:** SEV1 — `authentik-server` Service had 0 endpoints; every OIDC-integrated app (Grafana, Paperless, Open WebUI, Nextcloud, OpenSearch Dashboards, Proxmox, the Kubernetes API server itself) lost login capability simultaneously
- **Duration of impact:** ~13 min (10:52–11:05 UTC)
- **Data loss:** None

## TL;DR
The `geoip` sidecar (`geoipupdate`, MaxMind GeoLite2) crash-looped on MaxMind's `429 LIMIT_EXCEEDED: Daily GeoIP database download limit reached` — almost certainly exhausted by several unrelated Helm upgrades restarting authentik-server/worker in quick succession earlier the same session, each restart retriggering a fresh download attempt against the account-wide daily quota. The sidecar has no readiness-probe override, so Kubernetes' default all-containers-AND'd Pod `Ready` condition took the *entire* pod NotReady despite the `server`/`worker` container being healthy — emptying the Service's endpoints. Fixed by disabling `geoip` until the quota resets; recovery was complicated by the HelmRelease's own upgrade/rollback remediation cycling on the same crash before the fix could land cleanly.

## Impact
- `authentik-server` Service: 0 endpoints for the full duration — a genuine, complete SSO outage, not degraded service.
- Every app depending on Authentik OIDC lost new-login capability; already-authenticated sessions with a still-valid token were likely unaffected until token refresh (not verified).
- `authentik-worker` (background task processing — blueprint reconciliation, scheduled jobs) was equally NotReady for the same reason, though it has no Service exposure so this manifested only as the same crash-loop symptom, not a second distinct outage.

## Symptoms

### Pod status
```
NAME                                READY   STATUS             RESTARTS
authentik-server-85f94684f4-rqkrx   0/2     CrashLoopBackOff   12 (99s ago)
authentik-worker-7fd7598ff4-7pvnr   0/2     CrashLoopBackOff   12 (99s ago)
```

### Container-level breakdown (the key signal — main container was fine)
```
server  ready=true  restarts=0  state=running
geoip   ready=false restarts=12 state=waiting (CrashLoopBackOff)
```

### geoip container log
```
# STATE: Running geoipupdate
Error retrieving updates: running the job processor: running job:
unexpected HTTP status code: received HTTP status code: 429:
{"code":"LIMIT_EXCEEDED","error":"Daily GeoIP database download limit reached"}
```

### Service endpoints (the actual outage signal)
```
$ kubectl get endpoints -n security authentik-server
NAME               ENDPOINTS   AGE
authentik-server               75d
```
Empty `ENDPOINTS` column — confirms this is a real Service-level outage, not just noisy restart counts.

## Root cause
**Trigger:** MaxMind's account-wide daily GeoLite2 download quota was exhausted, most likely by this same session's own repeated authentik-server/worker restarts (multiple unrelated Helm upgrades reconciled within the same afternoon) — each pod restart re-triggers `geoipupdate`'s download attempt regardless of the configured `updateInterval: 48h`, since that interval only governs the *scheduled* re-check, not startup behavior.

**Underlying cause:** `kubernetes/apps/security/authentik/app/helmrelease.yaml`'s `geoip` sidecar has no readiness-probe override, so kubelet treats it as a normal container for the Pod's aggregate `Ready` condition (logical AND across all containers). A sidecar that is not core to serving traffic can still take the whole Pod out of a Service's Endpoints by crash-looping — the chart doesn't isolate it.

## Timeline
- **~10:15–10:52 UTC** — several unrelated Helm upgrades to the `authentik` HelmRelease reconciled in sequence as part of the same session's broader cluster-health-check remediation (each one restarts server/worker).
- **~10:52 UTC** — `geoip` begins crash-looping on the 429 response; `authentik-server` Service endpoints go to 0.
- **11:12 UTC** — user reports the outage alongside two unrelated issues.
- **~11:13 UTC** — root cause identified via container-level pod status + geoip log.
- **~11:14 UTC** — fix committed (`aff638d`, `geoip.enabled: false`) and pushed.
- **~11:15–11:31 UTC** — recovery complicated: the HelmRelease's own `upgrade.remediation.strategy: rollback` kept auto-rolling-back to an earlier (still geoip-enabled) release because the crash-looping sidecar also made the upgrade's own `--wait` time out, so the fix never got a clean window to land via the normal Flux path.
- **~11:15 UTC** — `flux suspend helmrelease authentik -n security`, then `kubectl patch deployment authentik-server|authentik-worker --type=json` to directly remove the `geoip` container (index 1) from both live Deployments.
- **~11:16 UTC** — `authentik-server` Service confirmed with real endpoints again (`10.42.7.215:9000,9443`) — SSO restored.
- **~11:31 UTC** — `flux resume helmrelease authentik -n security`; since live state now matched the committed `geoip.enabled: false` value, the next Helm upgrade (`.v36`) succeeded cleanly on the first try.

## Diagnosis process

### What did NOT work
- **Just committing `geoip.enabled: false` and reconciling** — the fix was correct but the HelmRelease's own remediation loop (auto-rollback on upgrade timeout, `retries: 3`) kept reverting to an earlier release before the corrected value ever got a clean upgrade window, because the *same* crash-looping container that needed the fix was also what made each upgrade attempt time out waiting for Deployment availability. Several `flux reconcile helmrelease` calls in a row just re-triggered the same failing cycle.

### What DID work
1. `flux suspend helmrelease authentik -n security` — stops Flux/Helm from fighting the manual fix.
2. Direct patch to remove the crashing sidecar from both live Deployments:
   ```bash
   kubectl patch deployment authentik-server -n security --type=json \
     -p='[{"op":"remove","path":"/spec/template/spec/containers/1"}]'
   kubectl patch deployment authentik-worker -n security --type=json \
     -p='[{"op":"remove","path":"/spec/template/spec/containers/1"}]'
   ```
   (Container index confirmed first via `kubectl get deployment ... -o jsonpath='{range .spec.template.spec.containers[*]}{.name}{" "}{end}'` — `geoip` was index 1 in both.)
3. Verified recovery: `authentik-server` pod `1/1 Running`, Service endpoints populated.
4. `flux resume helmrelease authentik -n security` — with live state now matching the already-committed `geoip.enabled: false`, the next Helm upgrade succeeded immediately (no crash-looping container left to cause a timeout).

## Fix applied

### Live remediation
See "What DID work" above — suspend HelmRelease, `kubectl patch` to strip the sidecar, verify, resume HelmRelease.

### Preventive change committed
`kubernetes/apps/security/authentik/app/helmrelease.yaml` (commit `aff638d`): `geoip.enabled: false`, with an inline comment explaining the crash-loop and Pod-readiness interaction, plus a note to re-enable once MaxMind's daily quota resets (UTC midnight on the free tier) and to confirm no crash-loop before trusting it again.

## Runbook — if this fires again
1. **Confirm the signature:** `kubectl get pods -n security -l app.kubernetes.io/instance=authentik` shows `CrashLoopBackOff` — then check *per-container* status, not just the pod summary:
   ```bash
   kubectl get pod -n security <pod> -o jsonpath='{range .status.containerStatuses[*]}{.name}{" ready="}{.ready}{" restarts="}{.restartCount}{"\n"}{end}'
   ```
   If `server`/`worker` show `ready=true` but `geoip` shows `ready=false` with climbing restarts, this is the same incident.
2. **Confirm 0 Service endpoints:** `kubectl get endpoints -n security authentik-server` — empty `ENDPOINTS` confirms full outage, not just noisy restarts.
3. **If the HelmRelease's own reconcile is fighting a fix you've already committed** (repeated `UpgradeFailed`/`RollbackSucceeded` conditions, `helm history authentik -n security` oscillating between old revisions): suspend the HelmRelease, `kubectl patch` the live Deployments directly to match the already-correct git state, verify recovery, then resume — don't just keep calling `flux reconcile` and hoping.
4. **Longer-term:** re-enable `geoip.enabled: true` only after confirming MaxMind's quota has reset, and avoid multiple authentik-server/worker redeploys in a short window afterward (each restart re-consumes quota).

## References
- Fix commit: `aff638d` — `fix(authentik): temporarily disable geoip sidecar (SSO outage)`
- Auto-memory: `project_authentik_geoip_outage` (persistent cross-session note, includes the same recovery technique)

## Action items
- [x] Live remediation applied (HelmRelease suspend + kubectl patch)
- [x] GitOps preventive change committed (`aff638d`)
- [x] HelmRelease resumed and confirmed converging cleanly (`.v36`, `UpgradeSucceeded`)
- [x] Postmortem written (this file)
- [ ] Re-enable `geoip.enabled: true` once MaxMind's daily quota resets — confirm no crash-loop before considering this closed
- [ ] Consider whether the chart supports a readiness-probe override or `restartPolicy: Always` sidecar semantics for `geoip` so a future quota exhaustion degrades gracefully (missing geo-enrichment) instead of taking the whole pod down

---
_For related context: `docs/apps/authentik.md`._
