# Object-Store Write Outage Fails Every Backup Path, Silently

- **Date:** 2026-10-27
- **Component:** `velero` (daily schedule, Kopia maintenance), `database/postgres` (CloudNativePG barman-cloud plugin), object store `de-fra.i3storage.com`
- **Severity:** SEV2 — no data lost and the previous day's backups stayed restorable, but every backup path failed for ~6h and nothing alerted; discovered by hand ~9h after the fact
- **Duration of impact:** writes rejected ~02:00–08:00 UTC; no fresh backup existed 2026-10-26 02:00 → 2026-10-27 10:50 UTC (~32h)
- **Data loss:** None

## TL;DR
The S3-compatible object store returned `500 InternalError` on writes for roughly six hours. Reads kept working, so the `BackupStorageLocation` stayed `Available` and nothing looked broken from the outside. The daily Velero backup failed, the Kopia maintenance jobs failed repeatedly across all seven repositories, and the CloudNativePG base backup failed in the same window. No alert existed for any of it — the only detection was the ISMS assessment, which reads backup phases once a day and writes a report rather than paging. The store recovered on its own; catch-up backups were taken by hand and seven alert rules now cover the gap.

## Impact
- **Velero:** `daily-20261027020047` → `Failed`, 24 errors, 5 warnings. Not retried; Velero schedules have no retry, so the next attempt would have been 2026-10-28 02:00.
- **Kopia maintenance:** failing across all seven repositories (`gitlab`, `hermes-agent`, `immich`, `mail`, `nextcloud`, `open-webui`, `paperless`) for the duration. Maintenance pruning is not a backup, but a repository that cannot be maintained grows unbounded.
- **CloudNativePG:** `postgres-daily-20261027023000` → `failed`. This is the only backup path for the `database` namespace, which is in **no** Velero schedule.
- **What kept working:** WAL archiving continued throughout (`ContinuousArchiving=True`, archiver backlog `ready=0`), so point-in-time recovery was never interrupted. Reads from the object store worked the whole time, which is precisely why nothing noticed.

## Symptoms
```text
# Velero
error putting object backups/daily-20261027020047/velero-backup.json:
  InternalError: An internal error occurred. Try again.
  status code: 500, request id: 2131b7ab-52da-4b44-bbaa-4be2266b3805

# Kopia maintenance
Repo maintenance error: failed to prune repo: ... error to run maintenance under mode auto:
  error updating maintenance schedule: unable to complete PutBlob(kopia.maintenance)
  despite 10 retries: An internal error occurred. Try again.

# CloudNativePG
LastBackupSucceeded = False | rpc error: code = Unknown desc = exit status 1
```

Key signals: **writes fail while reads succeed.** The `BackupStorageLocation` reported `Available` with a current `lastSyncedTime` and `lastValidationTime` throughout, because validation only reads. Any check that asks "is the backup location healthy" would have said yes.

## Root cause
**Trigger:** the storage provider returned HTTP 500 `InternalError` on `PutObject`/`PutBlob`. A 500 is a server-side fault, not a quota or permission problem — those return `507`/`QuotaExceeded` and `403` respectively. Cause on the provider side not determinable from here; it recovered without intervention.

**Underlying cause — the one worth fixing:** there was no alerting on backup outcomes at all. `kubectl get prometheusrules -A` returned **zero** rules matching Velero, backup or CNPG, while both components had been exporting perfectly good metrics the whole time. Backup failure was therefore observable but unobserved.

Two metric traps found while building the alerts, both of which would have produced an alert that lies:

1. **`velero_backup_last_status{schedule="daily"}` read `1` (success)** while that very backup sat in phase `Failed`. The obvious metric for "did the last backup work" is the wrong one. `velero_backup_failure_total` and `velero_backup_last_successful_timestamp` both reported the failure correctly.
2. **`cnpg_collector_last_available_backup_timestamp` is stuck at `0`.** With the barman-cloud **plugin** (`spec.plugins`, `isWALArchiver: true`) rather than the legacy in-tree configuration, neither that metric nor `cluster.status.lastSuccessfulBackup` is populated — measured with 30 `Backup` objects in phase `completed`. An alert comparing "last available" against "last failed" would fire permanently.

That second one also cost a wrong conclusion during diagnosis: `lastSuccessfulBackup: null` was briefly read as "the base backup has never worked", which would have been a far more serious finding. Counting the `Backup` objects disproved it.

## Timeline
All times UTC.

- **02:00:47** — daily Velero backup starts.
- **02:09:01** — backup fails: `error putting object .../velero-backup.json`, 500.
- **02:32:48** — CloudNativePG base backup fails in the same window.
- **~02:00–08:00** — Kopia maintenance jobs fail across all repositories; WAL archiving unaffected.
- **~08:00 onwards** — writes start succeeding again; maintenance jobs complete normally from here.
- **10:30** — failure noticed by hand, while verifying unrelated work. Nine hours after the fact.
- **10:35** — minimal test backup (`echo` namespace) completes in 11s with 0 errors → writes confirmed working.
- **10:37–10:50** — catch-up backups taken: Velero 2994/2994 items `Completed`, CloudNativePG `completed`.
- **~11:00** — seven alert rules added.

Detection lagged the incident by ~9h, and only because someone happened to look. Without that, the first signal would have been the ISMS assessment report at 04:00 the following day.

## Diagnosis process

### What did NOT work
- **Checking the `BackupStorageLocation`.** `Available`, freshly synced and validated. Validation reads, and reads were never broken, so this said "healthy" during a total write outage.
- **Reading `velero_backup_last_status`.** Reported success for the schedule whose latest backup had failed.
- **Reading `cluster.status.lastSuccessfulBackup` / `cnpg_collector_last_available_backup_timestamp`.** Both empty with the plugin-based setup, which looks identical to "no backup has ever succeeded".
- **Looking for the server log in the wrong pod.** `-l app.kubernetes.io/name=velero` selects a `node-agent` pod; the controller log is in the `velero-*` Deployment pod.

### What DID work
- **Comparing write and read behaviour.** Every failing operation was a `PutObject`/`PutBlob`; everything that only read succeeded. That immediately ruled out credentials and network reachability.
- **Counting `Backup` objects instead of trusting status fields.** 30 `completed` versus 2 `failed` disproved the "base backups never worked" reading.
- **Issuing one small write.** A single-namespace backup completed in 11s, which settled "is it still broken" in under a minute and was far cheaper than the 2h full run.
- **Separating the two failures.** Velero had worked until that morning; CNPG's status fields merely looked like a long-standing failure. Treating them as one problem would have sent the investigation after a non-existent bug.

## Fix applied
Seven alert rules in `kubernetes/apps/monitoring/kube-prometheus-stack/app/prometheusrule.yaml`, groups `backup-velero` and `backup-cnpg`:

| Alert | Expression basis | Severity |
| --- | --- | --- |
| `VeleroBackupFailed` | `increase(velero_backup_failure_total[2h]) > 0` | critical |
| `VeleroBackupStaleDaily` | last success older than 36h | critical |
| `VeleroBackupStaleWeekly` | last success older than 9d | warning |
| `VeleroBackupStaleMonthly` | last success older than 35d | warning |
| `CNPGBaseBackupFailed` | `changes(cnpg_collector_last_failed_backup_timestamp[1h]) > 0` | critical |
| `CNPGWALArchiveStalled` | no segment archived for 30m | critical |
| `CNPGWALArchiveFailing` | `increase(cnpg_pg_stat_archiver_failed_count[1h]) > 0` | warning |

Design choices worth keeping in mind when editing these:

- **Staleness, not last status.** Age of the last *successful* backup is both trustworthy and the property that actually matters. `velero_backup_last_status` is not used at all, for the reason above.
- **`changes()` rather than an age test for the CNPG failure.** An age comparison would stay lit for a day after one failure and would not clear when the next backup succeeded — which is how an alert teaches its reader to ignore it. `changes()` fires on a new failure timestamp and clears an hour later.
- **Per-schedule thresholds.** A single threshold cannot serve a daily and a monthly schedule; the monthly series legitimately sits at 26 days.
- **A manual catch-up does not clear the staleness alert.** The metric is keyed by the schedule label, and an on-demand backup carries a different one. Intentional: the question is whether the *schedule* is producing backups.

Every expression was validated against live Prometheus, and each underlying series was confirmed to return data — a rule that is silent because its metric does not exist is worse than no rule.

## Open items
- **`database` is in no Velero schedule.** Legitimate, because CloudNativePG backs it up independently, but it means a CNPG backup failure has no second line of defence. Worth deciding whether that is acceptable.
- **The ISMS evidence records `lastSuccessfulBackup`,** which is always empty with the plugin. The check itself reads the cluster *conditions* and is therefore correct, but the recorded field is misleading to a human reading the snapshot.
- **No retry for a failed Velero schedule.** A transient provider fault at 02:00 costs a full day. The alerts now make that visible; automatic retry is a separate decision.
- **Provider reliability is unmeasured.** Six earlier backup failures exist (2026-08-01, 08-02, 08-09, 08-16, 09-20 and today). Whether those share this cause was not investigated.
