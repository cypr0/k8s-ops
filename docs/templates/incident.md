# <Postmortem title — short, descriptive>

> Filename: `docs/incidents/YYYY-MM-DD-<short-slug>.md`. Title above mirrors the slug, written for human readers. Example: "CoreDNS AAAA Template Rule Breaking Internal Service Resolution".

- **Date:** YYYY-MM-DD
- **Component:** <namespace/app/resource>
- **Severity:** <SEV1 | SEV2 | SEV3> — <one-line justification>
- **Duration of impact:** <approximate range, e.g. "~15 min (11:05–11:20 UTC)">
- **Data loss:** <None | Yes — scope>

> Severity is a simple three-level scheme sized for a one-operator homelab cluster (no on-call rotation, no formal paging):
> - **SEV1** — full outage of something depended on cluster-wide (SSO, DNS, the apiserver itself) or real data-loss risk.
> - **SEV2** — a single app degraded/down, or a workaround exists, no data loss.
> - **SEV3** — cosmetic, caught by monitoring before user impact, or a near-miss.

## TL;DR
2–4 sentences: what happened, root cause, the fix. A reader should be able to stop here and understand the incident.

## Impact
Bullets: what broke, user-visible vs internal-only, duration, what kept working despite the incident (often as informative as what failed).

## Symptoms
Observable signals that pointed at the root cause — alert text, log excerpts, command output. Use code blocks for raw output. Call out the "key signals" so a future incident can be pattern-matched against this one quickly.

## Root cause
Paragraph explaining *why*, not just *what*. Distinguish the **trigger** (the specific event that started it) from the **underlying cause** (the design property that made the trigger fatal). Cite file paths (`kubernetes/apps/<ns>/<app>/app/*.yaml`) and upstream issues/docs where relevant.

## Timeline
Timestamped bullets, UTC, from first signal to resolution. Note when the incident was actually detected if that lagged behind when it started.

## Diagnosis process

### What did NOT work
Diagnostic steps or fix attempts that had no effect, with a one-line reason why. As valuable as the section below — saves a future pass from repeating dead ends.

### What DID work
The fix that actually resolved it, with the verification evidence (command output, state change observed).

## Fix applied

### Live remediation
The exact command(s) run against the live cluster to stop the bleeding. Should be reproducible verbatim if this happens again before the GitOps fix lands.

### Preventive change committed
What changed in git to fix the root cause / reduce recurrence risk / improve detection. List by file path, one-line intent each, with the commit SHA once merged.

## Runbook — if this fires again
Numbered, verbatim-runnable steps for a future pass with no memory of this incident: confirm the signature → apply the fix → verify → escalate/rollback if it doesn't stick.

## References
Links to upstream issues, chart source, related incidents, the commit(s) that fixed it.

## Action items
Checkbox list, checked off as completed in later commits:
- [x] Live fix applied
- [x] GitOps preventive change committed (`<commit sha>`)
- [x] Postmortem written (this file)
- [ ] Follow-up items, if any

---
_For related context, check `docs/apps/<app>.md` if a per-app doc exists, and the auto-memory system for cross-session incident notes._
