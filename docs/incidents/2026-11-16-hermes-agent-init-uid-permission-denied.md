# hermes-agent Down for Six Days: Init Containers Locked Out of /opt/data

- **Date:** 2026-11-16 to 2026-11-22
- **Component:** `hermes-agent/hermes-agent` (Deployment init containers, `/opt/data` PVC)
- **Severity:** SEV2. The personal agent was fully down: no Telegram/WhatsApp, no cron jobs, no Paperless webhook processing.
- **Duration of impact:** about 6 days (2026-11-16 15:56 to 2026-11-22 12:44)
- **Data loss:** None. Paperless documents added in that window were never run through the extraction webhook (see Follow-ups).

## TL;DR
The pod's containers restarted during the Talos v1.14.6 node reboots on 2026-11-16. After that, `bootstrap-config` failed on every retry with `mkdir: cannot create directory '/opt/data': Permission denied`. The pod sat in `Init:CrashLoopBackOff` (7,300+ back-offs) until 2026-11-22.

The init containers ran as uid 1000, but `/opt/data` is always mode `0700` owned by uid 10000 once the app has run:
- The image's boot hook (`docker/stage2-hook.sh`) chowns `$HERMES_HOME` to its runtime user `hermes` (uid 10000).
- Hermes itself chmods `$HERMES_HOME` to `0700` whenever it writes a secret file there (`hermes_constants.py`, `secure_parent_dir`).

The setup had only worked while the directory happened to be group-accessible. The fix runs every volume-writing init container as uid 10000, the owner the image enforces anyway.

## Timeline
- **2026-11-16 15:56** Node reboot (Talos v1.14.5 → v1.14.6). The pod's main container exits 137, and the init containers restart.
- **2026-11-16 to 2026-11-22** `bootstrap-config` exits 1 on every back-off.
  - The cron jobs `daily-digest`, `vertrags-abo-monitoring`, `portfolio-price-check` and `homelab-waechter` do not run.
  - `homelab-waechter` runs inside hermes itself, so it could not report its own outage.
- **2026-11-22 ~12:30** Found while inspecting the cluster for an unrelated change.
- **2026-11-22 12:44** The agent came back up, but by accident:
  - A diagnostic pod mounting the same PVC set `fsGroupChangePolicy: OnRootMismatch`.
  - The kubelet's recursive group/permission pass from that pod made `/opt/data` reachable for uid 1000 again.
  - The next back-off of `bootstrap-config` then succeeded.
- **2026-11-22 13:00** Hotfix `408d544` pushed: init containers run as uid 10000 and `fix-data-ownership` is removed. The rollout was verified with a fresh pod.

## Root cause
Two layers fight over `/opt/data`:

| Actor | What it does |
| --- | --- |
| Image boot hook (root, main container) | `chown hermes:hermes $HERMES_HOME` when the owner isn't uid 10000 |
| hermes runtime (uid 10000) | `chmod 0700` on the parent of every secret file it writes (`.env`, `auth.json`) |
| `fix-data-ownership` init container (uid 1000) | Tried `chown 1000:1000 /opt/data`. This fails as a non-root user, and the boot hook would revert it anyway. |
| `bootstrap-config` / `tools-install` (uid 1000) | Need to create entries directly under `/opt/data` |

With owner 10000 and mode `0700`, uid 1000 cannot even traverse `/opt/data`.

The comments in `deployment.yaml` explained earlier failures as an NFS restriction ("not even root can chmod the mount root"). That explanation was wrong: the application itself keeps resetting the mode.

## Resolution
- `bootstrap-config` and `tools-install` run as `runAsUser: 10000` and `runAsGroup: 1000`. Everything below `/opt/data` is either owned by 10000 or group-writable for gid 1000, so this works in steady state, after a Velero/Kopia restore (which keeps uid 10000) and on a fresh volume.
- `fix-data-ownership` was removed.

## Detection gap
- Gatus checks `hermes-agent:8644` (TCP) and `:9119/api/status` and alerts via Pushover, so an alert should have fired. It is unclear whether it reached anyone; Pushover quota exhaustion has happened before (`2026-10-11-wazuh-pushover-quota-exhaustion.md`).
- The agent's own `homelab-waechter` cannot watch itself.

## Follow-ups
- [ ] Re-run the Paperless extraction webhook for documents added 2026-11-16 to 2026-11-22.
- [ ] Check why the Gatus/Pushover alert for "Hermes Agent (Webhook)" went unnoticed.
- [x] Every pod start spent 5 to 10 minutes on the kubelet's recursive fsGroup pass (`OnRootMismatch` always saw the private root as a mismatch), and the pass left `/opt/data` at `2775`. hermes v0.23 then refused its own control socket (`unsafe_control_permissions`), so every Kanban worker failed with "Gateway starting: deadline". `fsGroup` was dropped on 2026-11-22: the ServiceAccount token is `0644` without it, and `bootstrap-config` runs `chmod go-w /opt/data`.
