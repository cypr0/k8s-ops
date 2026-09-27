# Mailu Postfix Chart Upgrade Deadlocks: RollingUpdate + Shared RWX PVC Lock File

- **Date:** 2026-09-27
- **Component:** `mail/mailu` (`mailu-postfix` Deployment, `mailu-storage` PVC)
- **Severity:** SEV2 — cluster reached an inconsistent, retry-looping state for ~3.5 hours; the live, healthy Postfix instance kept processing mail throughout, so there was no actual mail-delivery outage, but the HelmRelease was stuck `False`/`Reconciling` and a second, permanently crash-looping Postfix pod existed the whole time
- **Duration of impact:** ~3.5 hours (15:26–19:07 CEST) of a stuck Helm upgrade/rollback retry loop; live mail service itself never went down
- **Data loss:** None

## TL;DR
Bumping the `mailu` Helm chart 2.7.3→2.8.0 (Renovate PR, merged same day) upgraded Postfix's image. `mailu-postfix` is a single-replica `Deployment` using the chart's default `RollingUpdate` strategy, but its `mailu-storage` PVC is `ReadWriteMany` (`zfs-nfs`) and mounted by both the old and new pod during a rolling update. Postfix keeps a process lock in that shared volume; the moment a second Postfix process starts against the same mail queue directory, it fails immediately with `postfix/postlog: fatal: the Postfix mail system is already running` and crash-loops forever. Because the new pod can never become healthy, the rollout can never complete — and neither can Helm's own automatic rollback attempt when it tried to revert, because the *rollback* hits the exact same RollingUpdate-vs-shared-volume conflict in the opposite direction. The result was a live deadlock: Flux endlessly retrying an upgrade-then-rollback cycle, each attempt spawning a second Postfix pod that immediately crash-looped against the first. Fixed by temporarily patching the Deployment's strategy to `Recreate` (kills the old pod before starting the new one, never two Postfix processes at once), letting the pending change converge, then resuming normal Flux management.

## Impact
- **No user-facing mail outage.** Whichever Postfix pod was "old" in any given cycle kept accepting/relaying mail the entire time — confirmed live via its logs showing successful `qmgr`/`lmtp` deliveries throughout the incident window.
- **The `mailu` HelmRelease was `Ready: False` for ~3.5 hours**, cycling between `UpgradeFailed` and `RollbackFailed` conditions — any other change to the `mailu` chart during this window would have been blocked or made the situation worse.
- **A second, permanently `CrashLoopBackOff` Postfix pod existed continuously**, consuming a scheduling slot and generating constant restart/event noise, which is what surfaced this to begin with ("we have two instances, should only have one").
- Every other Mailu component (`front`, `admin`, `dovecot`, `rspamd`, `webmail`, `webdav`, `tika`, `oletools`) upgraded to 2.8.0 successfully and without incident — this was specifically a Postfix-and-shared-volume problem, not a general chart-upgrade problem.

## Symptoms
```
$ kubectl get pods -n mail
mailu-postfix-64bb599896-fnrps    0/1     CrashLoopBackOff   2 (8s ago)   48s
mailu-postfix-846957cfd5-x9pb2    1/1     Running            0            80m
```
Two ReplicaSets under one Deployment, one perpetually crashing, one stable — the crashing one's identity (which pod-template-hash) changed across restarts of the underlying HelmRelease reconcile loop.

Crash-looping pod's logs:
```
postfix/postlog: fatal: the Postfix mail system is already running
```

Stable pod's logs, at the same time, showing the *other* instance's repeated failed start attempts against the shared queue:
```
Sep 27 18:42:03 mail postfix/postfix-script[285]: fatal: the Postfix mail system is already running
Sep 27 18:42:09 mail postfix/postfix-script[286]: fatal: the Postfix mail system is already running
Sep 27 18:42:26 mail postfix/postfix-script[286]: fatal: the Postfix mail system is already running
```
— confirms both pods were fighting over the exact same lock, not two independent problems.

HelmRelease condition, showing the retry loop:
```
message: "Helm rollback to previous release mail/mailu.v16 with chart mailu@2.7.3
  failed: release mailu failed: failed early due to stalled resources:
  [Deployment/mail/mailu-postfix status: 'Failed']"
reason: RollbackFailed
```

## Root cause
**Trigger:** Renovate PR bumping the `mailu` Helm chart 2.7.3→2.8.0 (`ghcr.io/mailu/*` images 2024.06.55→2024.06.58), merged and reconciled same day.

**Underlying cause:** `kubernetes/apps/mail/mailu/app/helmrelease.yaml`'s `mailu-postfix` Deployment has no override for `spec.strategy` — it inherits the chart's default `RollingUpdate` (`maxSurge: 25%`, `maxUnavailable: 25%`). For a single-replica Deployment, `maxSurge: 25%` still rounds up to allowing 1 extra pod, so Kubernetes starts the new pod *before* terminating the old one. `mailu-storage` (the PVC backing Postfix's `/queue` and lock state) is provisioned `ReadWriteMany` on `zfs-nfs`, specifically so it *can* be mounted by more than one pod — but Postfix itself is not multi-instance-safe against a shared queue directory: it takes an exclusive lock and any second `postfix start` against the same data immediately fails with `the Postfix mail system is already running`. The two pods are not actually clustering or sharing load — the "old" one just keeps running while the "new" one dies forever, and the Deployment controller can never consider the rollout complete, because the new ReplicaSet can never reach `Ready`.

**Why the automatic rollback made it worse, not better:** once the initial upgrade attempt timed out (on an unrelated `mailu-front` DaemonSet rollout delay) and Flux's `helm-controller` tried to auto-remediate by rolling back to the previous release, the rollback *also* creates a new pod (running the *old* image) alongside the then-current (new-image) pod — same `RollingUpdate` mechanism, same shared-PVC lock conflict, just in the opposite direction. This is why the incident was a genuine deadlock rather than a one-off failure: every automatic remediation attempt, upgrade or rollback, hit the identical structural problem.

**Secondary complication during manual remediation:** a `kubectl patch` (non-server-side-apply) issued to fix the strategy created a competing field manager (`kubectl-patch`) on `spec.strategy.type`, which then caused subsequent `helm rollback`/Flux reconcile attempts to fail with Server-Side-Apply field-manager conflicts (`conflict occurred while applying object ... conflicts with "helm-controller"`) — resolved by re-setting the conflicting field to the value Helm itself wanted, so the next apply no longer disagreed.

## Timeline
- **~15:26 CEST** — `mailu` HelmRelease upgrade to chart 2.8.0 begins; times out waiting for `DaemonSet/mail/mailu-front` (`InProgress`), triggers `UpgradeFailed`.
- **~15:36 CEST onward** — Flux's `helm-controller` auto-remediates by rolling back to the previous release (v16, chart 2.7.3). The rollback itself stalls on `Deployment/mail/mailu-postfix status: 'Failed'` — the new (rollback-target) Postfix pod can't come up because the still-live (upgrade-target) Postfix pod holds the shared-volume lock.
- **15:26–18:43 CEST (~3h)** — Flux repeatedly retries the rollback, each attempt failing identically; `helm history mailu` shows 4+ consecutive `failed` revisions across this window.
- **~18:43 CEST** — Issue reported ("two Postfix instances, should only have one"); investigated directly via `kubectl get pods`/`describe`/logs, root cause (shared RWX lock + RollingUpdate) identified within minutes.
- **~18:46 CEST** — `flux suspend helmrelease mailu -n mail` to stop the retry loop; deleted the crash-looping ReplicaSet.
- **~18:47 CEST** — Patched `mailu-postfix`'s strategy to `Recreate` — the then-live old pod was killed immediately, new pod came up clean (`starting the Postfix mail system`, no lock conflict).
- **~18:49–19:02 CEST** — Attempted `helm rollback mailu 16` directly to clear Helm's own `failed` release-history state; hit Server-Side-Apply field-manager conflicts (`kubectl-patch` vs `helm-controller`) from the manual strategy patch; resolved by re-aligning the field's value under the conflicting manager, then resuming the HelmRelease so Flux's own `helm-controller` (which already owns the other conflicting fields) drove reconciliation instead of the `helm` CLI.
- **~19:02 CEST** — First `flux resume` attempt: Flux retried the *upgrade* path again (not rollback — live resources were already closer to 2.8.0 than realized), hit the identical shared-lock conflict a second time (new crash-looping pod appeared). Suspended again, deleted the new conflicting ReplicaSet, re-applied the `Recreate` strategy patch.
- **~19:06 CEST** — Second `flux resume`: reconciliation completed cleanly (`HelmRelease mailu reconciliation completed`, `applied revision 2.8.0`) — by this point every other component was already on 2.8.0 and only Postfix's single pod needed to converge, which it did without a second competing pod.
- **~19:07 CEST** — Verified: single Postfix pod (`1/1 Running`), `helm history` shows revision 36 `deployed` (chart 2.8.0, app 2024.06.58), HelmRelease `Ready: True`, all other Mailu components confirmed on 2024.06.58, full Gatus health sweep green.

## Diagnosis process

### What did NOT work
- **A plain `kubectl patch` (client-side/Update, not Server-Side-Apply) to fix the Deployment's strategy field** — resolved the immediate lock conflict, but left a competing `kubectl-patch` field manager on `spec.strategy.type` that then broke the next `helm rollback`/Flux reconcile with a Server-Side-Apply conflict against `helm-controller`. Lesson: any manual `kubectl patch`/`edit` against a Flux-managed resource should either use `--field-manager` matching the controller, or be treated as strictly temporary and immediately superseded by letting the controller re-apply.
- **`helm rollback mailu 16 --force`** — `--force` (deprecated alias for `--force-replace`) is incompatible with Server-Side-Apply releases (`invalid operation: cannot use server-side apply and force replace together`); not usable here at all.
- **Resuming the HelmRelease immediately after the first `Recreate` patch, without also clearing the *other* pending field-manager conflicts** — Flux's first reconcile attempt after resume still hit the leftover `kubectl-patch` conflict and failed differently before the real fix could land.

### What DID work
1. `flux suspend helmrelease mailu -n mail` — stops the automated retry loop so manual remediation isn't fighting the controller.
2. Delete the crash-looping ReplicaSet (`kubectl delete replicaset <bad-hash> -n mail`) — removes the immediate noise/conflict; the Deployment controller recreates a pod from its current template, but only one at a time once...
3. ...the Deployment's `spec.strategy` is patched to `Recreate` (`kubectl patch deployment mailu-postfix -n mail --type=json -p='[{"op":"replace","path":"/spec/strategy","value":{"type":"Recreate"}}]'`) — this guarantees the old pod is terminated *before* a new one starts, which is the only way to avoid two Postfix processes ever touching the shared queue simultaneously.
4. Re-align any field-manager conflicts created by the manual patch (re-set the contested field to the value the owning controller already wants) before resuming.
5. `flux resume helmrelease mailu -n mail` — let Flux's own `helm-controller` finish reconciliation once nothing was structurally blocking it. Needed twice in this incident: the first resume attempted an upgrade path that still hit a shared-lock race (a different pod-template-hash than before), requiring one more round of suspend → delete ReplicaSet → re-patch `Recreate` → resume before it converged cleanly.
6. Verified via `helm history mailu -n mail` showing a `deployed` (not `failed`) revision, `kubectl get helmrelease mailu -n mail` showing `READY: True`, single Postfix pod, and a full Gatus health sweep.

## Fix applied

### Live remediation
See "What DID work" above — suspend HelmRelease → delete conflicting ReplicaSet → patch strategy to `Recreate` → resolve field-manager conflicts → resume HelmRelease (repeated once). No GitOps commit was needed for the *live* fix itself, since the `Recreate` strategy patch was intentionally temporary (see Action items — whether to make it permanent in git is an open question, not yet decided).

### Preventive change committed
None yet — this incident's remediation was entirely live/manual. See Action items for the open question of whether `strategy: Recreate` should be committed permanently for `mailu-postfix` in `kubernetes/apps/mail/mailu/app/helmrelease.yaml`.

## Runbook — if this fires again
1. **Confirm the signature:** `kubectl get pods -n mail -l app.kubernetes.io/component=postfix` shows two pods, one healthy and one `CrashLoopBackOff` with `postfix/postlog: fatal: the Postfix mail system is already running` in its logs.
2. **Do not panic about mail delivery** — check the *healthy* pod's logs for ongoing `qmgr`/`lmtp` activity first; the stable instance almost always keeps working throughout.
3. **Suspend the HelmRelease immediately** (`flux suspend helmrelease mailu -n mail`) to stop Flux from retrying the same conflict in a loop.
4. **Delete the crash-looping ReplicaSet** (not just the pod — the ReplicaSet will just recreate it): `kubectl delete replicaset <bad-pod-template-hash> -n mail`.
5. **Patch the Deployment's strategy to `Recreate`** to let the pending change (upgrade or rollback) converge without a second Postfix process ever starting: `kubectl patch deployment mailu-postfix -n mail --type=json -p='[{"op":"replace","path":"/spec/strategy","value":{"type":"Recreate"}}]'`.
6. **If a subsequent `flux resume`/`helm rollback` fails with a Server-Side-Apply field-manager conflict** mentioning a manager you don't recognize (e.g. `kubectl-patch`), check `kubectl get deployment mailu-postfix -n mail --show-managed-fields -o json` for which fields that manager claims, and re-set those fields to the value the *owning* controller (usually `helm-controller`) already wants, using the same manager name if possible, before retrying.
7. **Resume the HelmRelease** (`flux resume helmrelease mailu -n mail`) and watch it through to `Ready: True` — may need one more suspend/fix/resume cycle if the first resume attempts the opposite direction (upgrade vs rollback) than expected and hits the same lock race with a new pod-template-hash.
8. **Verify:** single Postfix pod `1/1 Running`, `helm history mailu -n mail` tail shows `deployed` not `failed`, `kubectl get helmrelease mailu -n mail` shows `READY: True`, and a Gatus sweep is clean.

## References
- Related: `docs/apps/mailu.md` (no existing "Known quirks" entry for this — worth adding once the permanent-fix decision below is made)
- Chart: `mailu` Helm chart, `kubernetes/apps/mail/mailu/app/helmrelease.yaml`

## Action items
- [x] Live remediation applied — single healthy Postfix pod on chart 2.8.0, HelmRelease `Ready: True`
- [x] Postmortem written (this file)
- [ ] **Decide whether to commit `strategy: Recreate` permanently** for `mailu-postfix` in `kubernetes/apps/mail/mailu/app/helmrelease.yaml` (via the chart's `deployment.strategy` value override, if supported, or a Kustomize patch) so this can never recur on a future chart bump — as of this postmortem the fix is live-only and will NOT survive the next Helm reconcile if the chart's own template reasserts `RollingUpdate`. **This is the most important open item** — without it, the exact same deadlock can and will recur on any future Postfix image bump.
- [ ] Add a "Known quirks" entry to `docs/apps/mailu.md` documenting the shared-RWX-PVC-plus-RollingUpdate hazard, once the permanent-fix approach is decided
- [ ] Consider whether `mailu-storage` needs to remain `ReadWriteMany` at all for Postfix specifically, or whether Postfix's queue could be split onto its own `ReadWriteOnce` volume — not investigated in this pass; may not be feasible if other Mailu components share the same PVC for unrelated reasons (not verified)

---
_For related context: `docs/apps/mailu.md`._
