# Kyverno Rollout Schedules Onto a Control-Plane Node, Talos OOM-Kills Cgroups Mid-Rollout

- **Date:** 2026-07-04
- **Component:** `security/kyverno` (all four controllers), scheduling onto `k8s-cp-0`
- **Severity:** SEV2 — briefly threatened `kube-apiserver` availability on one control-plane node during rollout; no confirmed apiserver outage, but the causal link is strong enough to treat as a near-miss on a cluster-wide dependency
- **Duration of impact:** Transient, scoped to the rollout window itself (exact duration not recorded) — fixed same-day
- **Data loss:** None

## TL;DR
Kyverno's Helm chart has no node anti-affinity by default, so its rollout landed two of its four controller pods onto `k8s-cp-0` — a 4GB control-plane node already running `kube-apiserver`, `etcd`, `kube-controller-manager`, `kube-scheduler`, Grafana, Gatus, and the Cilium agent. Talos's own OOM controller was observed actively killing cgroups on that node immediately after Kyverno's pods landed there, and cp-0's apiserver briefly stopped accepting connections during the same window. Fixed by giving Kyverno the same node anti-affinity already used by Falco (exclude `node-role.kubernetes.io/control-plane`) and dropping the admission controller back to a single replica, since the cluster has no memory headroom to spare for extra redundancy.

## Impact
- `k8s-cp-0`'s `kube-apiserver` briefly stopped accepting connections during Kyverno's rollout — not independently confirmed via apiserver logs in this pass, but directly correlated in time with Talos's OOM controller killing cgroups on the same node right after Kyverno's pods scheduled there.
- No confirmed data loss or lasting service degradation — this reads as a rollout-time capacity problem, not a sustained outage. Scoped as SEV2 rather than SEV1 specifically because the apiserver symptom was transient and not independently verified against apiserver logs, not because a control-plane-node OOM event is inherently low-severity.
- Established a repo-wide pattern (already used by Falco, later reused for Kyverno) that admission/security-tooling controllers must be explicitly excluded from control-plane nodes on this cluster, since CP nodes run at capacity with no memory to spare for anything beyond the core control-plane components.

## Symptoms
- Talos's OOM controller observed live, actively killing cgroups on `k8s-cp-0` immediately following Kyverno's controller pods landing on that node.
- `k8s-cp-0` already carried, at 4GB total RAM: `kube-apiserver`, `etcd`, `kube-controller-manager`, `kube-scheduler`, Grafana, Gatus, and the Cilium agent — essentially no free headroom before Kyverno's pods were added.
- cp-0's apiserver briefly stopped accepting connections in the same window (temporal correlation, not independently re-derived from apiserver logs in this incident).

## Root cause
**Trigger:** Kyverno's Helm chart ships with no control-plane node anti-affinity by default, and this repo's initial rollout (`b30693d`, adding Kyverno for Pod Security Standards enforcement in audit mode) didn't add one either — the default Kubernetes scheduler was free to place any of Kyverno's four controllers (admission, background, cleanup, reports) on any node, including control-plane nodes.

**Underlying cause:** This cluster's control-plane nodes run at essentially zero memory headroom by design (4GB nodes carrying the full control-plane stack plus a handful of small always-on apps). Any workload landing there without an explicit exclusion is a live risk to `kube-apiserver`/`etcd` availability — a cluster-wide dependency far more consequential than the workload that caused the pressure. Falco had already been given this same anti-affinity treatment for the identical reason; Kyverno's rollout simply hadn't caught up to that established pattern yet.

## Timeline
- **Kyverno's initial rollout** (`b30693d`, prior commit — exact date not re-derived in this pass) — no node anti-affinity present; a later admission-controller replica count of 2 exists at rollout time.
- **2026-07-04, ~11:22 CEST** — live-observed the Talos OOM controller killing cgroups on `k8s-cp-0` right after Kyverno's pods landed there; correlated with a brief apiserver connectivity gap on the same node during the same rollout. Root cause (no anti-affinity, CP node at capacity) identified and fixed in `99800b4` (`fix(kyverno): keep controllers off control-plane nodes, they're at capacity`).
- Fix: added the same `nodeAffinity` exclusion pattern already in use for Falco (`helmrelease-falco.yaml`) to all four Kyverno controllers, and reduced `admissionController` replicas from 2 to 1 to minimize footprint given the cluster's lack of memory headroom for redundancy.

## Diagnosis process

### What did NOT work
- Nothing was tried and discarded — the OOM-kill event was directly observed live during the rollout, and the fix pattern (control-plane node exclusion) already existed in the repo for an identical prior reason (Falco), so this was a direct pattern-match rather than an open-ended investigation.

### What DID work
Added `nodeAffinity` with a `requiredDuringSchedulingIgnoredDuringExecution` rule excluding `node-role.kubernetes.io/control-plane`, plus empty `tolerations: []`, to all four Kyverno controllers — the exact pattern already in place for Falco. Verified via subsequent rollouts not reproducing the OOM/apiserver symptom (not independently re-verified with a fresh reproduction in this postmortem pass; based on the absence of any further related fix commits since).

## Fix applied

### Live remediation
None recorded beyond letting the rollout complete and applying the preventive fix — no evidence of a manual intervention (e.g., cordoning the node, killing pods by hand) beyond the GitOps change itself.

### Preventive change committed
`kubernetes/apps/security/kyverno/app/helmrelease-kyverno.yaml` (commit `99800b4`, 2026-07-04): added control-plane `nodeAffinity` exclusion to all four controllers (matching Falco's existing pattern) and reduced `admissionController` replicas 2→1.

## Runbook — if this fires again
1. **Confirm the signature:** a new/updated workload rolls out, and shortly after, Talos's OOM controller is observed killing cgroups on a control-plane node (`talosctl dmesg -n <cp-node>` or similar), possibly coinciding with a brief `kube-apiserver` connectivity gap on that node.
2. **Check whether the new/changed workload has a control-plane node exclusion** — grep the app's `helmrelease*.yaml` for `nodeAffinity`/`node-role.kubernetes.io/control-plane`. If absent, that's very likely the cause on this cluster given how little headroom the CP nodes carry.
3. **Apply the same pattern used for Falco and Kyverno**: `nodeAffinity` excluding `node-role.kubernetes.io/control-plane`, empty `tolerations: []`.
4. **Consider replica count, not just placement** — this cluster's stated policy (per this fix) is to keep security/admission-tooling footprints minimal rather than add redundancy the memory budget can't support.
5. **This remains a live, structural risk, not a one-time bug** — any *future* app added without this same exclusion pattern can reproduce this exact incident. There is no cluster-wide guardrail (e.g. a Kyverno policy of its own, ironically) currently preventing a new HelmRelease from scheduling onto control-plane nodes by omission.

## References
- Fix commit: `99800b4` (`fix(kyverno): keep controllers off control-plane nodes, they're at capacity`)
- Related pattern: Falco's own control-plane exclusion, `kubernetes/apps/security/falco/app/helmrelease-falco.yaml`
- Related doc: `docs/apps/kyverno.md` (Known quirks section already references this fix)

## Action items
- [x] Preventive change committed (`99800b4`)
- [x] Postmortem written (this file)
- [ ] Consider a cluster-wide guardrail (e.g. a Kyverno `ValidatingPolicy`/admission rule, or a documented rollout checklist item) that catches a *new* HelmRelease scheduling onto control-plane nodes by omission, rather than relying on each app individually remembering to copy the Falco/Kyverno anti-affinity pattern
- [ ] Independently verify the apiserver-connectivity-gap claim against `kube-apiserver` logs from the incident window, if still available in Loki/retained logs — currently based only on temporal correlation, not a confirmed apiserver-side error

---
_For related context: `docs/apps/kyverno.md`, `docs/apps/falco.md`._
