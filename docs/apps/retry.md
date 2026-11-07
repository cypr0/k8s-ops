# retry

> **Namespace**  velero  
> **Source**     plain manifests  
> **Hostname**   (none — internal CronJob only)

## What it does here
Retries failed scheduled backups for both Velero and CloudNativePG when transient object-store write failures cause a backup cycle to be lost. Neither Velero Schedules nor CNPG ScheduledBackups have built-in retry logic, so a failure at 02:00 UTC would otherwise wait 24 hours for the next attempt even if the underlying fault cleared within minutes (`kubernetes/apps/velero/retry/app/cronjob.yaml`).

## Architecture at a glance
- **Depends on:** Velero Schedules (`daily`, `weekly`, `monthly` in namespace `velero`), CNPG ScheduledBackup `postgres-daily` (namespace `database`), Kubernetes API server
- **Depended on by:** (none — this job improves backup reliability but no other app blocks on it)

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/velero/retry/ks.yaml` | Flux Kustomization; explicitly omits `targetNamespace` so RoleBindings can span `velero` and `database` |
| `kubernetes/apps/velero/retry/app/cronjob.yaml` | CronJob that checks for failed backups and creates retry attempts |
| `kubernetes/apps/velero/retry/app/rbac.yaml` | ServiceAccount, ClusterRole (get/list/create on Velero and CNPG backup resources), RoleBindings in `velero` and `database` |
| `kubernetes/apps/velero/retry/app/ciliumnetworkpolicy.yaml` | Restricts egress to DNS + kube-apiserver (no direct object-store access) |
| `kubernetes/apps/velero/retry/app/kustomization.yaml` | Kustomize manifest list; deliberately no `namespace:` field |

## Secrets
(none — this job only reads Kubernetes API resources and creates new Backup objects; it never touches credentials or the object store directly)

## Routing & access
- **Internal only:** CronJob runs in-cluster, talks only to the Kubernetes API server
- **Network policy:** `ciliumnetworkpolicy.yaml` allows DNS and `kube-apiserver` entity; blocks everything else. The policy exists to prevent accidental inheritance of unrestricted egress in a namespace that otherwise policies all pods (`kubernetes/apps/velero/retry/app/ciliumnetworkpolicy.yaml` inline comment).

## Storage
(none — ephemeral `emptyDir` for `/tmp` only)

## Known quirks
- **No `targetNamespace` in the Flux Kustomization** (`kubernetes/apps/velero/retry/ks.yaml`): the job needs `create` on CloudNativePG Backups in `database` and Velero Backups in `velero`. Setting `targetNamespace` would rewrite both RoleBindings to land in a single namespace, silently breaking one of them.
- **No `postBuild` substitution** (`kubernetes/apps/velero/retry/ks.yaml` inline comment): the CronJob script uses shell variables like `${TODAY}` and `${STAMP}`. Flux's `postBuild.substitute` runs in strict mode, so adding it later would require escaping every shell variable as `$${VAR}`.
- **Runs at 05:00, 08:00, 11:00, 14:00 UTC** (`kubernetes/apps/velero/retry/app/cronjob.yaml`): scheduled backups run at 02:00 and 02:30 UTC. The retry windows are spaced to catch transient faults that clear within hours (the 2026-10-27 S3 outage lasted ~6 hours and cleared by 08:00Z; see `docs/incidents/2026-10-27-s3-write-outage-silent-backup-failure.md`).
- **Retry carries the same `velero.io/schedule-name` label** as the original schedule (`kubernetes/apps/velero/retry/app/cronjob.yaml` inline comment): this ensures the `velero_backup_last_successful_timestamp` metric keys correctly and a successful retry clears the `VeleroBackupStale*` alert. A manual backup without that label would succeed but leave the alert firing.
- **UTC timezone, not local** (`kubernetes/apps/velero/retry/app/cronjob.yaml` inline comment): the "did it already run today" logic compares UTC dates. Using `Europe/Berlin` would shift the job across midnight relative to that comparison twice a year at DST transitions.
- **Motivation:** transient S3 write failures occur roughly 1–2 times per month in this cluster (four of six recorded Velero backup failures were S3 writes: 2026-08-01, 08-02, 08-09, 08-16; the fifth is 2026-10-27). Each lost cycle means no fresh backup for 24 hours after a fault that may have cleared in minutes (`kubernetes/apps/velero/retry/app/cronjob.yaml` inline comment).

## Common operations
- **Check recent runs:** `kubectl -n velero get jobs -l app.kubernetes.io/name=backup-retry --sort-by=.metadata.creationTimestamp`
- **Logs from last run:** `kubectl -n velero logs -l app.kubernetes.io/name=backup-retry --tail=100`
- **Manually trigger a retry check now:** `kubectl -n velero create job --from=cronjob/backup-retry manual-retry-$(date +%s)`
- **Suspend the CronJob:** `kubectl -n velero patch cronjob backup-retry -p '{"spec":{"suspend":true}}'` (resume with `false`)
- **See what backups exist today:** `kubectl -n velero get backups.velero.io -l velero.io/schedule-name=daily` and `kubectl -n database get backups.postgresql.cnpg.io`

## TODOs / unknowns
- The script logic assumes phases `Completed` / `InProgress` / `New` / `Failed` for Velero and `completed` / `running` / `started` / `failed` for CNPG. If either project adds new phases, the "is it still running" check may need adjustment.
- No alert exists yet to notify when this job itself fails (it has `failedJobsHistoryLimit: 3` but no PrometheusRule watching it).

---

**Secret/IP scan:** clean — no secrets, credentials, or sensitive IPs in this document. The job itself never handles secrets; it only creates Kubernetes Backup resources via the API.

_All file paths are relative to repo root. This doc lives at `docs/apps/retry.md`._
