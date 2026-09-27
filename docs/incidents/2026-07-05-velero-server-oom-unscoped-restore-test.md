# Velero Server OOM-Killed by the First Live Restore-Test (Unscoped Namespace Restore)

- **Date:** 2026-07-05
- **Component:** `velero/velero` (server Deployment, backup Schedules, restore-test script)
- **Severity:** SEV2 — Velero server itself went down (OOM-killed) during a restore test; no application data was lost, and the failure was caught by the very restore-test mechanism meant to catch problems, but a live restore of the affected resource types could have caused real cluster-wide damage
- **Duration of impact:** Scoped to the single restore-test run; Velero server recovered after the fix (memory limit raised, resources excluded) — same-day
- **Data loss:** None

## TL;DR
The first live restore-test performed an unscoped namespace restore that pulled in every cluster-wide CRD backed up alongside the target namespace's app data — Flux `HelmRelease`/`Kustomization`, Trivy, Kubescape, `ServiceMonitor`, `HTTPRoute`, `CiliumNetworkPolicy`, Jobs/CronJobs — and OOM-killed the 512Mi-limited Velero server under the resulting load. Beyond the direct memory cost, restoring these specific resource types is actively dangerous on this cluster: a restored `HelmRelease`/`Kustomization` is picked up cluster-wide by helm-controller/kustomize-controller regardless of namespace, and a restored `HTTPRoute` can conflict with the one already live. Fixed by adding `excludedResources` to all three backup Schedules and to the restore-test script itself (defense-in-depth for pre-existing backups), plus raising the server's memory limit 512Mi→1Gi.

## Impact
- Velero server OOM-killed mid-restore-test — the server itself went down, not application data.
- **No actual data loss or corruption** — the failure was caught by the restore-test mechanism working as intended (surfacing a real problem in a test namespace, not production).
- **Latent risk beyond the OOM itself, not realized in this incident:** had the restore proceeded further, restoring cluster-wide Flux objects (`HelmRelease`/`Kustomization`) or an `HTTPRoute` into a live cluster could have caused genuine cluster-wide disruption — helm-controller/kustomize-controller act on these objects regardless of which namespace they land in, and a restored `HTTPRoute` can directly conflict with the production one for the same hostname.
- Also fixed in the same commit: Pushover notifications from the restore-test script were failing with "Network unreachable" — `urllib` was preferring `api.pushover.net`'s AAAA record on a cluster with no IPv6 routing.

## Symptoms
- Velero server process OOM-killed during the first live restore-test run.
- The restore pulled in far more resource types than the target namespace's actual application data — visible from the restore's resource list including Flux, Trivy, Kubescape, monitoring, and networking CRDs that have nothing to do with the app being tested.
- Separately, the restore-test's own failure-notification path (Pushover) was itself failing with a `Network unreachable` error trying to reach `api.pushover.net`'s IPv6 address on a cluster with no IPv6 routing at all.

## Root cause
**Trigger:** The first live restore-test performed a namespace-scoped restore without any `excludedResources`, so it restored *everything* Velero had backed up for that namespace — which, by default, includes every cluster-scoped-adjacent CRD instance that happens to reference or live alongside the namespace's objects (Flux `HelmRelease`/`Kustomization`, `ServiceMonitor`, `HTTPRoute`, `CiliumNetworkPolicy`, Jobs/CronJobs, Trivy/Kubescape report CRDs).

**Underlying cause:** Nothing in the original backup Schedules or the restore-test script excluded these resource types, because the schedules were written assuming "back up the namespace's app data" without accounting for how many *other* CRD instances Velero's default namespace-scoped backup also captures as a side effect (anything with an owner reference or matching label in that namespace). The 512Mi server memory limit was sized for restoring genuine app data (Deployments, Services, ConfigMaps, Secrets, PVCs), not for processing this much unplanned-for volume in one restore operation.

**Compounding risk (not the direct OOM cause, but the reason the exclusion matters beyond memory):** restoring a `HelmRelease`/`Kustomization` object is not namespace-scoped in effect — helm-controller/kustomize-controller will act on it cluster-wide the moment it exists, regardless of which namespace it was restored into. A restored `HTTPRoute` can likewise conflict with the live one for the same hostname. These are not just "extra data restored by mistake" — they are objects capable of causing damage on their own if a restore is ever allowed to complete against them.

## Timeline
- **Restore-test mechanism stood up** (prior to this incident, exact commit not re-derived in this pass) — first live run performed without `excludedResources`.
- **2026-07-05, ~21:10 CEST** — first live restore-test run OOM-kills the Velero server; root cause (unscoped restore pulling in cluster-wide CRDs) identified directly from the restore's resource list. Fixed in `7134f21` (`fix(velero): scope restores to app data, fix Pushover IPv6, raise memory`).
- Same commit also fixes the unrelated-but-co-discovered Pushover IPv6 notification failure and adds `restore.status.failureReason` logging before cleanup deletes the `Restore` object, so a failed run stays diagnosable from job logs alone going forward.

## Diagnosis process

### What did NOT work
- Nothing was tried and discarded — the OOM event and the oversized resource list were directly visible from the restore's own output; the fix was identified in one pass rather than through elimination of alternative theories.

### What DID work
1. Reviewed the actual resource list the restore attempted to process — immediately visible that it included far more than app data (Flux, Trivy, Kubescape, monitoring/networking CRDs).
2. Added `excludedResources` to all three backup Schedules (`kubernetes/apps/velero/schedules/schedule-{daily,weekly,monthly}.yaml`) scoping backups going forward to real app data only (Deployments, Services, ConfigMaps, Secrets, PVCs, ...).
3. Added the same `excludedResources` to the restore-test script itself, as defense-in-depth against *pre-existing* backups that were taken before the schedule fix and would still contain the excluded types.
4. Raised the Velero server memory limit 512Mi→1Gi as additional headroom.
5. Forced IPv4-only DNS resolution in the restore-test script for the separate Pushover notification bug.
6. Verified via a subsequent restore-test run completing without OOM (not independently re-confirmed with fresh command output in this postmortem pass; based on no further related fix commits since).

## Fix applied

### Live remediation
None beyond letting the OOM-killed pod restart under the standard Deployment self-healing — no manual intervention recorded.

### Preventive change committed
`kubernetes/apps/velero/` (commit `7134f21`, 2026-07-05):
- `excludedResources` added to all 3 Schedules (`schedules/schedule-{daily,weekly,monthly}.yaml`) — scopes future backups to real app data only.
- Same `excludedResources` added to the restore-test script — defense-in-depth for restoring from backups taken before this fix.
- IPv4-only DNS forced in the restore-test script (unrelated Pushover/IPv6 fix, same commit).
- `restore.status.failureReason` now logged before cleanup deletes the `Restore` object, so a future failed run is diagnosable from job logs alone.
- Velero server memory limit raised 512Mi → 1Gi.

## Runbook — if this fires again
1. **Confirm the signature:** Velero server OOM-kills during a restore, and/or the restore's resource list includes CRD types with no direct relationship to the target namespace's application (Flux objects, scanner report CRDs, networking/monitoring CRDs).
2. **Check `excludedResources` on the relevant Schedule and the restore-test script** — if a *new* resource type has since started appearing unexpectedly in a restore, the exclusion list may need extending again (e.g. a newly adopted CRD that gets labeled/owned within the target namespace).
3. **Never let a restore of `HelmRelease`/`Kustomization`/`HTTPRoute` complete against a live cluster** — these are the specific types called out as actively dangerous, not just wasteful, because the corresponding controllers act on them cluster-wide regardless of restore namespace.
4. **Check the Velero server's current memory limit against actual restore-test resource usage** if OOM recurs — 1Gi may itself become insufficient as the number of backed-up namespaces/apps grows (see `docs/apps/velero.md` for later capacity notes, e.g. Trivy's own OOM from restore-test-induced burst scanning).

## References
- Fix commit: `7134f21` (`fix(velero): scope restores to app data, fix Pushover IPv6, raise memory`)
- Related: `docs/apps/velero.md` (existing "Known quirks" already reference this commit for the `excludedResources` and memory-limit history)

## Action items
- [x] Preventive change committed (`7134f21`)
- [x] Postmortem written (this file)
- [ ] Re-verify with a fresh restore-test run that the current `excludedResources` list is still sufficient given every app added to Velero's backup scope since 2026-07-05 (e.g. Immich, added later per `109e37b`) — not re-verified in this postmortem pass
- [ ] Confirm the Velero server's current 1Gi memory limit is still adequate given cluster growth since this fix, cross-referencing the later Trivy OOM (`docs/apps/trivy.md`) which was itself caused by restore-test-induced burst scanning load

---
_For related context: `docs/apps/velero.md`._
