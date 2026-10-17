# Authentik 2026.8.4 Upgrade Stalled by the GeoIP Sidecar (Third Time)

- **Date:** 2026-10-17
- **Component:** `security/authentik` (HelmRelease, `geoip` sidecar in `authentik-server` and `authentik-worker`)
- **Severity:** SEV2 — `authentik-server` had no Ready pod for ~3h20m and the HelmRelease ended up `Stalled`; logins kept working almost throughout, most likely only because Envoy routes to unhealthy endpoints when none are healthy (not by design)
- **Duration of impact:** 0 Ready `authentik-server` pods 11:27–13:34 and 13:50–15:02 UTC; HelmRelease not Ready from ~10:42 UTC until the fix in this PR is applied
- **Data loss:** None

## TL;DR
Renovate's chart bump 2026.8.3 → 2026.8.4 restarted every Authentik pod. The new pods' `geoip` sidecars got `429 Daily GeoIP database download limit reached` from MaxMind, crash-looped, and kept their pods NotReady although `server`/`worker` were healthy. Helm's upgrade and its rollback both timed out, the rollbacks themselves recreated pods into the same 429, and after the last failed rollback the release went `Stalled`. 2026.8.4 itself was fine. The quota is used up every day by the `authentik-server` HPA, which has flapped 1↔2 replicas ~20–25 times a day since 2026-10-09 (each new pod downloads both editions). Fixed by wrapping the sidecar's entrypoint so a failed download is retried while the container keeps running; a pod can no longer be held NotReady by `geoip`.

## Impact
- **`authentik-server`:** 0 Ready pods 11:27:25–13:33:55 and 13:49:55–15:01:55 UTC (two 30 s blips in between), from `kube_pod_status_ready`. `authentik-worker` had 0 available replicas 14:18–15:00.
- **User-visible:** small. Gatus `Authentik SSO` (`https://id.${SECRET_DOMAIN}`, 1 min interval) failed once inside these windows (11:32). Most likely explanation, not verified for the NotReady case: Envoy Gateway hands Envoy every endpoint with an explicit `health_status` (seen live: `HEALTHY` for the Ready pod), and with all hosts unhealthy Envoy's default 50 % panic threshold routes to all of them. The `server` containers in those pods were healthy, so requests succeeded. Anything that reaches the `authentik-server` ClusterIP directly, not through Envoy, would have had no backend.
- **HelmRelease:** `UpgradeFailed`, then `RollbackFailed`, then `Stalled` (`RetriesExceeded`, 18 failures) at 13:58:36. Flux stops retrying a stalled release, so it stayed red.
- **Ongoing until the fix:** every HPA scale-up of `authentik-server` created a pod that never turned Ready (18:04, 18:28, 18:35 seen in events).

## Symptoms
```text
Helm upgrade failed for release security/authentik with chart authentik@2026.8.4: timeout waiting for:
  [Deployment/security/authentik-worker status: 'InProgress', Deployment/security/authentik-server status: 'InProgress']
Helm rollback to previous release security/authentik.v101 with chart authentik@2026.8.3 failed: ... (same)
```
```text
$ kubectl logs -n security authentik-server-78d9f498c5-js5fr -c geoip -p
2026-10-17T14:55:58Z # STATE: Running geoipupdate
2026-10-17T14:55:58Z Error retrieving updates: ... received HTTP status code: 429:
  {"code":"LIMIT_EXCEEDED","error":"Daily GeoIP database download limit reached"}
```
Key signals: a pod at 1/2 with the `geoip` container restarting and the main container healthy; `helm history` alternating upgrade/rollback; the Flux log line `the .spec.rollback.recreate field is deprecated and has no effect` (so rollbacks do not delete pods; the pods come from the template change itself).

## Root cause
**Trigger:** `ecfd4ff` (Renovate, 10:36 UTC) bumped the chart to 2026.8.4, a pod-template change for both Deployments.

**Underlying causes:**
1. **The sidecar can fail the pod.** The chart runs `ghcr.io/maxmind/geoipupdate` as a plain container with the image's entrypoint. `/usr/bin/entry.sh` runs under `set -e`, so any failed download (429, 401, network) exits the container. A Pod is Ready only when every container is, so a crash-looping `geoip` holds the whole pod NotReady. The chart offers no command or probe override for the container. Known since 2026-08-16, worked around then by disabling `geoip` and later re-enabled without changing this.
2. **The quota is used up daily by the HPA alone.** The database lives in a per-pod `emptyDir`, so every new pod downloads both editions (City + ASN). Since 2026-10-09 the `authentik-server` HPA (70 % CPU target, 100 m + 10 m request, median usage ~20 m) has scaled 1→2→1 all day: pods created per day 20–28 (10-10 to 10-17, `kube_pod_created`), 48 desired-replica changes in the 24 h before this incident. That is ~40–50 download attempts a day before any upgrade. What triggers the spikes was not investigated.
3. **Remediation multiplies the damage.** The cluster-wide HelmRelease patch (`kubernetes/flux/cluster/ks.yaml`, `RemediateOnFailure`, retries 2) rolls back and retries. Every attempt changes the template again, every new pod downloads again, and none of them can become Ready while the quota is exhausted.

**Not a cause — 2026.8.4 itself:** the chart diff 2026.8.3 → 2026.8.4 is the image tags only. All four 2026.8.4 server pods and six worker pods had their main container Ready 30–180 s after creation with 0 restarts (`kube_pod_container_status_ready`); only `geoip` failed.

**Not as documented — the quota window:** the earlier incident and the HelmRelease comment say the quota resets at 00:00 UTC. Today it answered 429 at 14:55:58 and served a download at 15:01:03 UTC, which fits a rolling 24 h window better.

## Timeline
All UTC.
- **10:36:23** — `ecfd4ff` (chart 2026.8.4) on `main`.
- **10:37:33** — Flux upgrades; new ReplicaSets `authentik-server-6fb7cb4cb`, `authentik-worker-54b789c95d`. Main containers Ready within 30–180 s; most `geoip` sidecars crash-loop (3–6 restarts each; the logs of these pods are gone, the 429 is confirmed from 13:53 on).
- **~10:42** — one 2026.8.4 server pod (`ksrgv`, created 10:41:02) is evicted from `k8s-wrk-1` for `DiskPressure` (the node reports no pressure at 18:45).
- **11:20–13:29** — helm-controller alternates upgrade/rollback (rollbacks logged at 11:20, 11:31, 11:39, 11:50, 12:08, 12:29, 12:49, 13:09, 13:29; upgrade at 11:25).
- **11:27:25** — last Ready `authentik-server` pod gone. Gatus fails once at 11:32.
- **13:29:41** — rollback to 2026.8.3 recorded as deployed (rev 101). **13:33:55** — one server pod Ready.
- **13:48:32** — upgrade to 2026.8.4 again (rev 102); **13:49:55** server back to 0 Ready.
- **13:53:34** — upgrade timed out; rollback (rev 103) starts. **13:58:36** — rollback timed out: `RetriesExceeded`, `Stalled`.
- **14:18:53–15:00:53** — 0 available `authentik-worker` replicas.
- **15:01:03** — `geoip` in `js5fr` downloads successfully; **15:01:55** server Ready again (still 2026.8.3).
- **18:04, 18:28, 18:35** — HPA scale-ups create server pods that stay NotReady (429) until the HPA scales back.
- **~18:40** — noticed in passing by another session.

## Diagnosis process

### What did NOT work
- **Treating the rollback as the cause of the pod churn.** Flux logs `the .spec.rollback.recreate field is deprecated and has no effect`; rollbacks did not delete pods. The churn came from template changes and the HPA.
- **`kube_endpoint_address` for endpoint history.** Returned nothing here; `kube_pod_status_ready` per pod gave the windows.
- **Assuming the quota resets at midnight UTC.** See Root cause.

### What DID work
- Prometheus per container: `max_over_time(kube_pod_container_status_ready{pod=~"authentik-(server-6fb7cb4cb|worker-54b789c95d).*"}[9h])` and the matching restart counter separated "authentik is broken" from "only `geoip` is broken".
- `kube_pod_created` counted per day showed the HPA-driven pod creation rate, which explains the daily quota exhaustion.
- Reading `/usr/bin/entry.sh` from the running image (`set -e`), then testing the wrapper locally against `ghcr.io/maxmind/geoipupdate:v7.1.1`: with invalid credentials (401, same exit path as 429) the stock entrypoint exits 1; the wrapper logs, keeps running and retries; `docker stop` returns in ≤ 1 s in both the retry sleep and entry.sh's 48 h sleep.

## Fix applied

### Live remediation
None by hand. Between failures the cluster was stable on 2026.8.3 (one Ready server pod, two Ready workers), and the stalled HelmRelease no longer created pods. A new HelmRelease generation resets Flux's failure counters, so merging the fix retries the 2026.8.4 upgrade with it.

### Preventive change committed
- `kubernetes/apps/security/authentik/app/helmrelease.yaml`:
  - `postRenderers`: a JSON patch on `authentik-(server|worker)` sets the `geoip` container's `command` to a `/bin/sh` loop around the stock `/usr/bin/entry.sh`. When entry.sh exits, the loop logs `# STATE: geoipupdate exited (<rc>), retrying in 1 hour`, sleeps an hour and runs it again; a `TERM` trap keeps shutdown immediate. A `test` op asserts that container index 1 is `geoip`, so a reordered chart fails the render instead of patching `server`/`worker`. A JSON patch rather than a strategic merge because kustomize's strategic merge moved `geoip` to the front of the list (verified), which would make it the default container for `kubectl logs/exec`.
  - `timeout: 10m` (Helm default 5 m): two worker replicas roll one after the other, and a worker took up to 3 min to turn Ready on a node that still had to pull the image.
  - Verified locally: `flux envsubst --strict` keeps the `$$` escapes as shell variables; `helm template` (2026.8.4) + `kustomize build` with the patch differs from the unpatched render only in `geoip.command` of the two Deployments; `kubeconform` passes.
- `docs/apps/authentik.md`: quirks updated.

**Trade-off:** authentik opens the GeoIP database only if it exists when the process starts (`GeoIPContextProcessor.city()` returns early while no reader is open, before `check_expired()` could pick up a new file). A pod that starts while MaxMind refuses runs without geo data until its next restart. GeoIP only enriches events here; there is no GeoIP policy (checked: `GeoIPPolicy.objects.all()` is empty).

## Runbook — if this fires again
1. `kubectl get pods -n security -l app.kubernetes.io/instance=authentik`. With the fix, `geoip` should never restart. `kubectl logs -n security <pod> -c geoip --tail=3` shows `retrying in 1 hour` while MaxMind refuses; that is cosmetic now.
2. If a pod is NotReady anyway, look at the `server`/`worker` container, not `geoip`.
3. HelmRelease `Stalled` with `RetriesExceeded`: a bare `flux reconcile` does not retry. Fix the cause in Git (a new generation resets the counters), or `flux suspend helmrelease authentik -n security && flux resume helmrelease authentik -n security`.
4. Quota check without spending a download: the last `geoip` log lines of the existing pods; a 429 means the 24 h window is full.

## References
- `docs/incidents/2026-08-16-authentik-geoip-sidecar-sso-outage.md`, `docs/incidents/2026-10-11-wazuh-pushover-quota-exhaustion.md` (same failure, earlier).
- Chart source: `templates/server/deployment.yaml` and `templates/worker/deployment.yaml` in `authentik` 2026.8.4 (`geoip` container: env, volumeMounts, resources, securityContext only).
- authentik 2026.8.4 release notes: https://github.com/goauthentik/authentik/releases/tag/version%2F2026.8.4

## Action items
- [x] Sidecar can no longer fail the pod; Helm timeout raised (this PR)
- [x] Postmortem written (this file)
- [ ] After merge: HelmRelease Ready on 2026.8.4, `geoip` restarts stay 0, both Deployments fully available
- [ ] Stop the `authentik-server` HPA flapping (since 2026-10-09, ~22 new pods a day): find what spikes CPU, then e.g. a longer `behavior.scaleDown` stabilization window or a request that matches the spikes. This is what burns the MaxMind quota.
- [ ] Re-apply the worker CPU request of ~200m (deferred 2026-10-11 only because of this sidecar; now unblocked)
- [ ] 2026.8.4 changes client-IP extraction to the **leftmost** `X-Forwarded-For` entry when the peer is a trusted proxy (goauthentik/authentik#26353). Behind Cloudflare, which appends to a client-supplied `X-Forwarded-For`, that entry may be client-controlled; check what authentik now logs as client IP and whether the reputation policy can be gamed
- [ ] Upstream: authentik never loads a GeoIP database that appears after process start

---
_For related context, see `docs/apps/authentik.md`._
