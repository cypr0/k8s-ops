# Wazuh Rule 100406 Burns 83% of the Monthly Pushover Quota in 11 Days

- **Date:** 2026-10-11 (noticed); started 2026-09-27
- **Component:** `security/wazuh` (rule 100406 and the `custom-pushover` integration), with smaller contributions from `monitoring/kube-prometheus-stack` (Alertmanager routing), `hermes-agent/firecrawl` and `security/authentik`
- **Severity:** SEV3 — near-miss. 9,141 of 10,000 messages were used when it was analysed, at ~40 an hour; the remaining ~860 would have lasted about a day. Nothing was lost yet, but Pushover rejects messages over the limit, so an exhausted quota would silence every sender on that counter (Alertmanager, Gatus, Authentik, Wazuh) until the reset, critical alerts included.
- **Duration of impact:** 2026-09-27 20:38 UTC (audit-log rule shipped) until the fix is live; the Pushover period resets 2026-11-01 05:00 UTC (the limits API reports 06:00 CET)
- **Data loss:** None. Every suppressed or held-back alert is still in the Wazuh dashboard.

## TL;DR
Wazuh rule 100406 ("RBAC or admission-webhook change not by Flux") was written on the assumption that only Flux touches those objects. In reality Kyverno, the CloudNativePG operator and cert-manager's cainjector rewrite their own webhooks and RoleBindings all day, so the rule fired ~800 times a day, each one a Pushover message, with no rate limit and no monitoring of the quota. It made up 92% of the level-10+ alerts in the October period and, with a few smaller noisy rules, used 9,100 of 10,000 messages. Fixed with level-0 child rules pinned to service account and object, a self-throttling notifier, a stricter Alertmanager route, and cleanup of two alerts that had been firing for two weeks over stale objects.

## Impact
- **Pushover quota:** 9,141 / 10,000 used at 2026-10-11 ~13:47 CEST and still 9,141 at 13:54 (from `GET /1/apps/limits.json`, which costs no message), 859 left, reset 2026-11-01 06:00 CET. Earlier reading: 9,098 at 12:46 CEST.
- **Signal drowned in noise:** ~800 identical messages a day made it impossible to notice a real one. Level-12 alerts go out at *high* priority and bypass quiet hours, and 102 of those since 27 Sep were pure noise (rule 100113).
- **Not affected:** Alertmanager, Gatus and Authentik kept delivering (there was still quota). Detection itself was never degraded; this was an alerting-budget failure.

## Symptoms
- Reported by the operator: 9,000 of 10,000 Pushover messages used ten days into the month, and still a lot of noise.
- Wazuh, unique level 10+ alerts since the period started (2026-10-01 05:00 UTC), 9,023 in total. That leaves ~120 of the 9,141 used for the other senders, which fits Alertmanager's ~110 for the same days. This assumes one message per alert, true while the notifier had no throttle:

  | Rule | Alerts | Share | What it was |
  | --- | ---: | ---: | --- |
  | 100406 | 8,311 | 92.1% | operators updating their own webhooks / RoleBindings |
  | 87702 | 405 | 4.5% | stock "multiple firewall blocks from one source" |
  | 5551 | 214 | 2.4% | PAM failed logins on Proxmox, one source address |
  | 100113 | 82 | 0.9% | inbound traffic from a C2-listed host, dropped at the WAN |
  | 100410 / 5712 | 11 | 0.1% | node denied 10x / sshd brute force |

- Flat profile of 835-1,014 alerts every UTC day since 2026-09-28, and in the 100406 data a constant 32-38 alerts every hour, around the clock. Constant machine rhythm, not human activity.
- Consistency check: between 12:46 CEST (9,098) and ~13:47 CEST (9,141) the quota API rose by 43 in about an hour; the count then did not move for the next several minutes. Rule 100406 produces ~34 alerts an hour, plus the other rules, so the order of magnitude matches. (This is one hour, not a rate measurement.)
- The 100406 alerts, by actor and object (raw counts before de-duplication, 27 Sep - 11 Oct, so roughly 4% high): eight Kyverno webhook configurations (six owned by `kyverno-admission-controller`, two by `kyverno-cleanup-controller`), 1,347 `update`s each; `cloudnative-pg` `create rolebindings/postgres` 411; `cert-manager-cainjector` patching `cert-manager-webhook` 134 (68 validating + 66 mutating); `envoy-gateway-...-certgen` 1.

Key signals for pattern-matching next time: a single rule with a flat hourly count, an actor that is a `system:serviceaccount:*` of an operator, and a quota that falls linearly instead of in bursts.

## Root cause
**Trigger:** `fc58dc4` (2026-09-27 22:38 CEST, "Kubernetes API audit log into Wazuh") fed the kube-apiserver audit stream into Wazuh. Rule 100406 in `kubernetes/apps/security/wazuh/app/configmap-manager-ruleset.yaml` raises level 10 for any create/update/patch/delete on RBAC or admission webhooks by anyone other than `system:serviceaccount:flux-system:*`.

**Underlying causes:**
1. **The rule's premise was never checked against live data.** Its comment says Flux applies all of these objects from Git, so anything else is "a human with kubectl or something that should not be doing it". That is false for operators that own their webhook or RBAC objects and re-stamp them (Kyverno about every 15 minutes per configuration, CNPG on each reconcile, cainjector on certificate rotation). A one-hour sample of the audit stream before enabling the page would have shown it.
2. **The notifier had no rate limit and nothing watched the quota.** `configmap-manager.yaml` said "Last week that was a handful of alerts, so no rate limiting yet" — true for the stock rules, but the threshold (level 10) was reused for a brand-new, much chattier source added within the same evening. Alertmanager and Wazuh use the same Pushover token, and Gatus and Authentik use their own, yet the limits API shows one identical counter for all of them (verified 2026-10-11), so one noisy sender hurts every sender.
3. **Several smaller rules sat at a level that pages although nothing needed doing:** 87702 aimed at the WAN address (the firewall already drops it), 100113 (source on a C2 list, traffic dropped, yet level 12 and high priority).

Two further alerts were permanently firing and re-notifying every 12h, small but pure waste:
- `KubeJobFailed` in `hermes-agent`: failures starting 2026-09-27 ~11:20 UTC (`DeadlineExceeded`; the Job names show ten minutes of schedule slots, the real duration was not measured) left six Failed Jobs of `firecrawl-nuq-reaper`/`-cleanup`. `failedJobsHistoryLimit: 3` keeps a Failed Job until three *newer* ones fail, so they stayed for two weeks. The cause of that failure itself was not investigated.
- `KubeHpaMaxedOut` for `authentik-worker`: the worker needs about 100m in total (p99 133m over 14 days) against a 50m request per replica (+10m geoip), so the 70% HPA target sat far below baseline and 2 of 2 replicas ran permanently.
- (`KubeJobFailed` for trivy-system's `node-collector-5c88744994` and, until it resolved, wazuh: orphaned Jobs, 131 days old for the trivy one; `infraAssessmentScannerEnabled` is false, so nothing will clean it up.)

## Timeline
All UTC.
- **2026-09-27 17:07** — `cffc94b` adds the `custom-pushover` integration (level 10+).
- **2026-09-27 19:42** — `b5af4a3` fixes its script permissions. **20:38** — `fc58dc4` ships the audit log and rule 100406; the first alerts appear in the same hour.
- **2026-09-28 onward** — 835-1,014 alerts a day. Nothing alerts on this.
- **2026-10-01 05:00** — Pushover period starts.
- **2026-10-11** — reported: 9,000 of 10,000 used and still noisy (time not recorded).
- **10:46** — quota API: 9,098 used, 902 left. Analysis begins: Alertmanager (140 messages in 14 days), Gatus (17 state changes), Authentik (0 failed-login events) ruled out as the main source; Wazuh identified.
- **10:57** — first fix committed (`05c02d6`: rules 100411-100414, 100113, 100901, a first throttle implementation; the throttle in this PR replaces it).
- **~11:47 and 11:54** — quota API: 9,141 used both times.

## Diagnosis process

### What did NOT work
- **Assuming Alertmanager was the source.** It was the obvious suspect (it owns the Pushover receiver), but `alertmanager_notifications_total{integration="pushover"}` was 140 over 30 days and `alertmanager_notifications_failed_total` was 0.
- **Reading Gatus logs for alert sends.** Gatus does not log notification sends at its default level (the log is only check results); state changes came from `changes(gatus_results_endpoint_success[14d])` instead, 17 in two weeks.
- **A plain `grep -c` / line count over `alerts.json` plus the dated files.** The two overlap, so it overstates by ~4% (12,312 raw vs 11,861 unique level 10+ lines). Count unique `id`.
- **Authentik.** Its notification rule fires on every `login_failed`, which looked dangerous; the database showed none in 31 days.

### What DID work
- Asking the systems that actually sent the messages: the Pushover limits API for the true total, then counting Wazuh alerts by unique ID, rule and day, then breaking the top rule down by user and object from the raw audit line.
- Testing rule changes with `wazuh-logtest` against real events: seven distinct real 100406 events all landed on the new level-0 rules, six tampered variants (human user, same service account on another object, another account with a Kyverno-looking name, a ClusterRoleBinding, a cainjector on another webhook, the CNPG account on a ClusterRole) all still came back level 10.

## Fix applied

### Live remediation
Nothing was changed by hand on the cluster. Everything went through Git so Flux owns it. Temporary test files (`zz_noise_test.xml` in the manager's `etc/rules/`, scripts in `/tmp`) were removed again; `etc/rules/` holds only `local_rules.xml`.

### Preventive change committed
- `configmap-manager-ruleset.yaml`: **100411-100414** (level 0) for Kyverno (service accounts `kyverno-admission-controller` and `kyverno-cleanup-controller` on `kyverno-*`), cainjector on `cert-manager-webhook`, the CNPG operator on roles/rolebindings, Envoy Gateway certgen on `envoy-gateway-*`. **100113** drops to level 7. **100901** (level 0) drops 87702 when aimed at the WAN address `192.168.100.1`.
- `configmap-manager-integrations.yaml` / `configmap-manager.yaml`: `custom-pushover` throttles itself. The same message goes out at most once per 6 h (1 h at level 12+), with a "+N held back" count on the next one. A daily cap of 40 at level 10-11 and 100 at level 12+ (separate counters, UTC day) sends one notice and then drops. State is `/var/ossec/logs/pushover-throttle.json` on the PVC under an exclusive `flock`; if it cannot be used the alert is sent unthrottled, and a failed post is not recorded. **Worst case from Wazuh is therefore 140 messages a day, ~4,300 a month, so no future runaway rule can exhaust the plan on its own.**
- `kube-prometheus-stack/app/externalsecret.yaml`: the Alertmanager root receiver is now `null` (before: `pushover`, so anything without a matching route paged). Critical goes to `pushover`; warnings go to a new `pushover-warning` receiver with no "resolved" message and `repeat_interval: 3d`. Checked with `amtool check-config` and six `amtool config routes test` cases (critical, warning, info, Watchdog, InfoInhibitor, no severity).
- `kube-prometheus-stack/app/prometheusrule.yaml`: `ScrapeTargetSustainedDown` now requires `and up offset 23h == 0`. Over 14 days of real data the old expression fired for 8 series, all one-scrape-old pods during rollouts; the new one fires for none, and for a target that was down a day ago it still does.
- `hermes-agent/firecrawl/app/cronjob-nuq-maintenance.yaml`: `ttlSecondsAfterFinished: 86400` on both CronJobs' Job templates.
- `security/authentik/app/helmrelease.yaml`: worker CPU request 50m to 200m (100m was computed and rejected: one replica would run at 91-121%).
- Docs: `docs/apps/wazuh.md`, `kube-prometheus-stack.md`, `authentik.md`, `firecrawl.md`, and a note in `2026-09-20-cilium-cnp-scrape-target-drop.md` about the refined alert expression.

## Runbook — if this fires again
1. **How much is left, without spending a message** (token is in the `wazuh-pushover` secret key `token`; do not print it):
   ```sh
   TOKEN=$(kubectl get secret -n wazuh wazuh-pushover -o jsonpath='{.data.token}' | base64 -d)
   curl -s "https://api.pushover.net/1/apps/limits.json?token=$TOKEN"
   ```
2. **Who is sending?** Alertmanager: `sum by (integration) (increase(alertmanager_notifications_total[7d]))`. Wazuh: count level 10+ alerts by *unique* `id` and by `rule.id` per day in `/var/ossec/logs/alerts/` inside `wazuh-manager-master-0`. A flat per-hour count means a machine, not an incident. Gatus: `changes(gatus_results_endpoint_success[14d])` by `name`.
3. **Fix the rule, not the throttle.** Add a level-0 child rule with `if_sid` of the noisy rule, pinned to actor *and* object, never a blanket exclusion of the parent. Test with `wazuh-logtest` inside the manager (output is on stderr) using real events from `full_log`, plus tampered variants that must still alert. Put the test rules in `etc/rules/` under a temp name and delete the file before the pod restarts (the final ConfigMap ships the same IDs).
4. **A throttle summary** `Wazuh: Pushover cap reached` means a daily cap was hit; look at `/var/ossec/logs/integrations.log` and the "+N held back" counts for the culprit.

## References
- `fc58dc4` (audit log into Wazuh), `cffc94b`, `b5af4a3`; the fix commits in the PR that carries this file.
- `docs/apps/wazuh.md` (Notifications, Kubernetes API audit), `docs/apps/kube-prometheus-stack.md` (route tree), `docs/incidents/2026-09-20-cilium-cnp-scrape-target-drop.md`.

## Action items
- [x] Rule 100406 exceptions, 100113, 100901 (tested with `wazuh-logtest`)
- [x] Self-throttling notifier (22 local scenarios, same script that ships in the ConfigMap)
- [x] Alertmanager route and `ScrapeTargetSustainedDown` expression (tested against live config and 14 days of data)
- [x] Firecrawl Job TTL, Authentik worker CPU request
- [x] Postmortem written (this file)
- [ ] Live verification after Flux applies it: the hourly 100406 count drops to ~0, the quota stops falling by ~35 an hour, `custom-pushover` runs under Wazuh's integratord without errors
- [ ] Delete the six stale Failed Jobs in `hermes-agent` and the orphaned `node-collector-5c88744994` Job in `trivy-system` (the TTL only covers Jobs created after it was added)
- [ ] Monitor the quota itself: Pushover exposes `remaining` on the limits API, nothing here polls it. A small CronJob exporting it, with an alert at ~70% used, would have caught this on 2026-09-30.
- [ ] Investigate why `firecrawl-nuq-reaper`/`-cleanup` failed on 2026-09-27 (`DeadlineExceeded`, ~10 minutes); only the symptom was handled here
- [ ] Rule 5551 (PAM failures on Proxmox, 214 alerts, all from `151.237.100.194` against `root`) is still level 10 and now at most ~4 messages a day; that volume is a handful of lines, but a root-login probe against the PVE API deserves a look at whether the host should be reachable from the internet at all
- [ ] Before the next audit-log-derived rule is paged, sample an hour of real events first

---
_For related context, see `docs/apps/wazuh.md` and `docs/apps/kube-prometheus-stack.md`._
