# Kubescape node-agent Rollout Falsely Marked Failed by Helm's Default Wait Timeout

- **Date:** 2026-08-16
- **Component:** `security/kubescape` (HelmRelease), `kubescape/node-agent` DaemonSet
- **Severity:** SEV3 — no user-facing impact; a real fix (F6, node-agent CPU requests) silently never actually landed live despite its git commit succeeding, caught only during post-hoc verification
- **Duration of impact:** N/A (bookkeeping-only issue; the DaemonSet itself was never down, just perpetually reverted to pre-fix values)
- **Data loss:** None

## TL;DR
Earlier the same session, `kubescape`'s node-agent DaemonSet got a CPU-request bump (`kubernetes/apps/security/kubescape/app/helmrelease.yaml`, `nodeAgent.resources`) to fix a real CPU-starvation CrashLoopBackOff. The git commit succeeded, but the live Helm release never actually converged: node-agent's `/readyz` takes several minutes per pod to respond after (re)start (eBPF collector init under CPU contention), and with 5 worker nodes rolling one at a time under the DaemonSet's default `RollingUpdate`, a full rollout comfortably exceeds Helm's ~5-minute default wait. Every upgrade attempt was declared failed and auto-rolled-back to an older release **after the rollout had already started succeeding**, so the fix's own values never stuck — confirmed only when a post-hoc verification pass found the live DaemonSet still running the old (pre-fix) resource values despite the "completed" git commit. Fixed by adding an explicit, longer `spec.timeout: 15m` to the HelmRelease.

## Impact
- No availability impact — `node-agent` pods were Running throughout (just slow to reach Ready, and periodically reset to a template that didn't yet have the CPU fix).
- The actual consequence: the CPU-starvation problem F6 was meant to fix was **not actually resolved** for an unknown period after its commit landed, because Helm kept reverting the live release before the new values could take effect. This is a "the fix looked done but wasn't" class of issue — a git commit succeeding is not proof a HelmRelease actually converged.

## Symptoms

### Kustomization / HelmRelease conditions
```
$ kubectl get kustomization kubescape -n security
message: health check failed after 43ms: failed early due to stalled resources:
         [HelmRelease/kubescape/kubescape status: 'Failed']

$ kubectl get helmrelease kubescape -n kubescape
Stalled: "Failed to upgrade after 3 attempt(s)"
Ready:   False — "Helm rollback to previous release kubescape/kubescape.v17 ... succeeded"
Released: False — "Helm upgrade failed ... timeout waiting for: [DaemonSet/kubescape/node-agent status: 'InProgress']"
```

### Live resources mismatch (the actual tell)
```
$ kubectl get daemonset node-agent -n kubescape -o jsonpath='{.spec.template.spec.containers[0].resources}'
{"limits":{"cpu":"500m","memory":"512Mi"},"requests":{"cpu":"10m","memory":"128Mi"}}
```
— neither the pre-fix values nor the F6 commit's intended values (`150m`/`750m`), because `helm history kubescape -n kubescape` showed the release bouncing between two rollback targets (`Rollback to 14`, then `Rollback to 17`) rather than ever landing on a fresh upgrade.

### node-agent pod startup delay (the mechanism)
```
Warning  Unhealthy  55m (x4 over 56m)  kubelet  Startup probe failed:
  Get "http://10.42.0.253:7888/readyz": context deadline exceeded
```
Took ~3 minutes from pod start before `/readyz` began responding successfully — multiplied across 5 sequentially-rolled nodes, comfortably exceeding a single ~5-minute Helm wait window.

## Root cause
**Trigger:** the F6 fix itself (raising `nodeAgent.resources.requests.cpu` from `10m` to `150m`) required a DaemonSet rolling restart to take effect, same as any resource change.

**Underlying cause:** `kubescape`'s HelmRelease had no explicit `spec.timeout`, defaulting to helm-controller's standard wait (~5 minutes). node-agent's own startup profile — eBPF collector initialization that's measurably slower under CPU contention — plus a DaemonSet's inherently sequential (one-node-at-a-time) rollout across 5 workers, structurally cannot finish within that default window. This is not specific to the F6 change — any future node-agent upgrade would hit the same false-negative timeout, since the mismatch is between Helm's wait duration and the DaemonSet's own rollout pace, not between old and new resource values.

## Timeline
- F6 fix committed (`354e665`) earlier in the session, appeared to complete normally via the standard Flux reconcile flow.
- Later, during unrelated verification of other findings, a routine cluster-wide health pass surfaced `kustomization/kubescape` as `Ready: False` with a stalled HelmRelease.
- Investigation found `helm history kubescape -n kubescape` oscillating between rollback targets, and the live DaemonSet's resources not matching the F6 commit — meaning the fix had silently never actually landed live.
- `node-agent` pod's own event log (`Startup probe failed... context deadline exceeded`, repeated over several minutes) identified the mechanism: genuinely slow-but-eventually-successful startup racing against Helm's default timeout.
- Fix committed (`71a5e03`): `spec.timeout: 15m` added to the HelmRelease.
- `flux reconcile helmrelease kubescape -n kubescape` triggered a fresh upgrade attempt; monitored via a background watch until `Ready: True` — `Helm upgrade succeeded for release kubescape/kubescape.v21` — all 5 node-agent pods confirmed `1/1 Running` with the correct (F6) resource values.

## Diagnosis process

### What did NOT work
- **Re-running `flux reconcile helmrelease kubescape`** without changing anything else — reset the attempt counter, but the underlying timeout mismatch was unchanged, so it failed the same way again within about a minute, showing a fresh `Stalled`/`RetriesExceeded` condition.

### What DID work
Adding `spec.timeout: 15m` to give the rollout enough headroom to actually finish before Helm's wait gives up, then reconciling once more:
```bash
flux reconcile helmrelease kubescape -n kubescape
```
Confirmed via `kubectl get helmrelease kubescape -n kubescape -o jsonpath='{.status.conditions}'` reaching `Ready: True` / `UpgradeSucceeded` and all 5 `node-agent` pods `1/1 Running` with the intended resource values.

## Fix applied

### Live remediation
None separate from the GitOps fix — reconciling with the corrected timeout *was* the resolution; there was no user-facing outage to stop the bleeding on.

### Preventive change committed
`kubernetes/apps/security/kubescape/app/helmrelease.yaml` (commit `71a5e03`): added `spec.timeout: 15m` with an inline comment explaining the eBPF-init-under-CPU-contention + 5-node-sequential-rollout math, so a future edit to this file doesn't remove the timeout thinking it's unrelated cruft.

## Runbook — if this fires again
1. **Don't trust a green git commit as proof a HelmRelease converged.** Spot-check: `helm history <release> -n <ns>` for repeated `Rollback to N` entries, and compare live resource/values (`kubectl get <workload> -o jsonpath='{.spec.template.spec.containers[0].resources}'`) against what the current committed HelmRelease actually specifies.
2. **If `kubectl get helmrelease <name> -n <ns>` shows `Stalled: RetriesExceeded` alongside a `timeout waiting for: [DaemonSet/... status: 'InProgress']` message**, suspect the same class of issue: a legitimately-slow-but-successful rollout racing a too-short Helm wait, not a real application failure.
3. **Reconcile once** (`flux reconcile helmrelease <name> -n <ns>`) to reset the retry counter and confirm it fails again the same way before concluding it's this issue and not something new.
4. **Fix is a longer `spec.timeout`**, sized to (per-pod startup time) × (number of DaemonSet nodes rolling sequentially), not a resource/config change to the workload itself.

## References
- Fix commit: `71a5e03` — `fix(proxmox-ansible,kubescape): drop leaked IP comment, fix kubescape rollback loop`
- Related fix (the one this incident silently blocked): `354e665` — F6, kubescape node-agent CPU-request bump

## Action items
- [x] Timeout fix committed and verified converging (`.v21`, `UpgradeSucceeded`)
- [x] Live node-agent resources confirmed matching the F6 commit's intended values
- [x] Postmortem written (this file)
- [ ] Consider auditing other DaemonSet-backed HelmReleases in this repo (`falco`, `cilium`) for the same missing-explicit-timeout risk before their next resource/config change
