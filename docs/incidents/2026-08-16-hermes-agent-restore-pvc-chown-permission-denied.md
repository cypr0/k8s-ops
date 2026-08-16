# hermes-agent Restore-PVC Ownership Fix Broke Production (chown Denied on Live NFS Mount)

- **Date:** 2026-08-16
- **Component:** `hermes-agent/hermes-agent` (Deployment init containers, `/opt/data` PVC)
- **Severity:** SEV2 — production `hermes-agent` Deployment CrashLoopBackOff for one Flux reconcile cycle; no data loss, quickly caught via Kustomization health check
- **Duration of impact:** a few minutes (one failed reconcile → hotfix → recovery, same session)
- **Data loss:** None

## TL;DR
A Velero/Kopia-restored copy of hermes-agent's `/opt/data` PVC had a top-level directory left with restrictive ownership that blocked the existing (non-root) init containers from writing to it. The fix — a new `fix-data-ownership` init container running `chown 1000:1000 /opt/data` with elevated `CAP_CHOWN` — was verified extensively against the restored test-namespace copy (a freshly csi-driver-nfs-provisioned subdirectory) and worked there. Rolled out live, it broke **production** instead: production's own, much older `/opt/data` mount rejects `chown` outright ("Operation not permitted") even under `CAP_CHOWN`, a previously-documented hard constraint of this specific CSI/NFS mount once it's past its initial provisioning. Because the fix script used `set -eu`, that expected-and-harmless failure on production became a fatal `CrashLoopBackOff` of an otherwise fully healthy Deployment. Fixed by making the chown step best-effort.

## Impact
- Production `hermes-agent` Deployment: `CrashLoopBackOff` on its init sequence for one reconcile cycle — 8 container restarts before the hotfix landed.
- No user-facing app depends on hermes-agent synchronously (it's an integration/automation agent), so this was an internal-only degradation, not a customer-facing outage.
- Zero data loss or corruption — the failure was purely in an init container refusing to proceed, never touching data destructively.

## Symptoms

### Kustomization health check
```
$ kubectl get kustomization hermes-agent -n hermes-agent
Ready: False
message: health check failed... Deployment/hermes-agent/hermes-agent status: 'Failed'
```

### Pod status
```
NAME                            READY   STATUS                  RESTARTS
hermes-agent-579879b64-xxxxx    0/2     Init:CrashLoopBackOff   8
```

### Init container log (`fix-data-ownership`)
```
chown: changing ownership of '/opt/data': Operation not permitted
```
— this exact message, on the exact same command, had succeeded moments earlier against the restored test-namespace PVC. The difference was which PVC/mount it ran against, not the command itself.

## Root cause
**Trigger:** rolling out a new `fix-data-ownership` init container (`kubernetes/apps/hermes-agent/hermes-agent/app/deployment.yaml`) that runs `chown 1000:1000 /opt/data` with `securityContext.capabilities.add: ["CHOWN"]`, to fix a real problem: a Velero/Kopia-restored PVC's top-level directory can retain restrictive ownership that blocks the existing non-root `bootstrap-config`/`tools-install` init containers.

**Underlying cause — two distinct facts that combined into a false generalization:**
1. **Directory-vs-file Unix permission semantics**: whether a process can create/overwrite entries *inside* a directory is governed by that directory's own write+execute bits, independent of the ownership of files already inside it — this is what made the ownership-mismatch theory plausible and (on the restore copy) correct.
2. **This specific CSI/NFS mount has a hard, previously-documented restriction**: `chown`/`chmod` succeeds only on a *freshly* csi-driver-nfs-provisioned subdirectory (i.e., the very first mount, exactly what a Velero restore produces) — not on a mount that's already been live for a long time. Production's `/opt/data` has been correctly owned since its first mount 28+ days ago and has simply never been chowned since; the moment something tries to chown it again, the NFS server/CSI driver rejects it outright, `CAP_CHOWN` notwithstanding.

The fix was verified only against case (1) — the restore scenario it was built for — and the verification never crossed-checked case (2), a constraint already sitting in prior project memory. Combined with `set -eu`, an expected-and-harmless "no-op, already correct" condition on production became a hard failure.

## Timeline
- Init container added, first version (commit `410cbda`) — verified extensively against the restored test-namespace PVC, appeared solid.
- Committed, pushed, reconciled to production.
- Production pod enters `Init:CrashLoopBackOff`; Kustomization reports `Ready: False`.
- Container logs show the `Operation not permitted` chown failure; recognized as matching a previously-documented mount-specific constraint, not a new bug.
- Hotfix (commit `aca1f14`) — made the chown step best-effort — committed, pushed, reconciled within the same session.
- New pod (`hermes-agent-cc8969d45-...`) reaches `2/2 Running`, 0 restarts; Kustomization confirms `Ready: True`, `Healthy: True` ("Health check passed in 2m35s").

## Diagnosis process

### What did NOT work
- Nothing was tried and discarded mid-incident — the fix path was direct once the log message was read: recognized the exact error text and mount-age pattern from prior operational memory before attempting anything else. The lesson here isn't a diagnostic dead end, it's a **verification** gap: the original fix (`410cbda`) was validated thoroughly against the restore scenario but was never cross-checked against this specific, already-known production-mount constraint before rollout.

### What DID work
Making the chown step best-effort instead of fatal:
```sh
# MUST be best-effort, not `set -e`: confirmed live that this chown fails
# outright on the LIVE production volume -- "Operation not permitted" --
# matching a previously documented constraint that not even root can
# chmod/chown this specific CSI/NFS-backed mount root once it's past
# initial provisioning. It only succeeds on a FRESHLY csi-driver-nfs-
# provisioned subdirectory, which is exactly the restore scenario this
# container exists for -- production's own /opt/data has been correctly
# owned since its very first mount and never needed this fix, so a
# failure here is expected and harmless there.
chown 1000:1000 /opt/data || echo "chown skipped (already correct, or this mount denies it)"
exit 0
```
Verified via the pod reaching `2/2 Running` and the Kustomization's own health check passing.

## Fix applied

### Live remediation
None beyond the GitOps fix itself — Flux's own reconcile-and-health-check loop was the fastest path to recovery once the corrected commit was pushed; there was no faster manual stopgap that wouldn't itself have required editing the same init container script.

### Preventive change committed
`kubernetes/apps/hermes-agent/hermes-agent/app/deployment.yaml`:
1. `410cbda` — added `fix-data-ownership` init container (the fix that exposed the gap).
2. `aca1f14` — made its chown step best-effort (`|| echo ...; exit 0` instead of failing under `set -eu`), with an inline comment recording the production-mount constraint so a future pass doesn't reintroduce the same assumption.

## Runbook — if this fires again
1. **Confirm the signature:** an init container fails with `Operation not permitted` on a `chown`/`chmod` against a long-lived (not freshly restored) PVC mount, despite `CAP_CHOWN`/root.
2. **Do not assume the fix is wrong** — check whether the *target* volume is a fresh restore-provisioned subdirectory (fix applies, should succeed) vs. a long-lived production mount (fix is a legitimate no-op there; the underlying data is already correctly owned from its first mount).
3. **Any init container that does a "just in case" ownership fix on a PVC must be best-effort**, never `set -e`/fatal, specifically because this mount type's behavior differs between fresh-provision and long-lived state — a failure here is not necessarily a bug.
4. **Verify recovery** the same way as this incident: `kubectl get pods -n hermes-agent -l app.kubernetes.io/name=hermes-agent` reaching `2/2 Running`, then `kubectl get kustomization hermes-agent -n hermes-agent` showing `Ready: True`.

## References
- Fix commits: `410cbda` (initial, exposed the gap), `aca1f14` (hotfix, best-effort chown)
- Related: this incident is the origin of the general lesson now in the auto-memory system: a fix verified only against a freshly-restored/test copy of a volume cannot be assumed safe against a long-lived production volume without cross-checking documented volume-specific constraints first.

## Action items
- [x] Live fix applied via GitOps reconcile (`aca1f14`)
- [x] Postmortem written (this file)
- [ ] Consider whether other init containers in this repo that touch restored PVCs make the same "verified-on-restore-copy-only" assumption — worth a quick audit pass
