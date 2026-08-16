# CloudNativePG

> **Namespace**  database
> **Source**     `cloudnative-pg` chart v0.29.0 via OCIRepository `oci://ghcr.io/cloudnative-pg/charts/cloudnative-pg` (`kubernetes/apps/database/cloudnative-pg/app/ocirepository.yaml`, `helmrelease.yaml`)
> **Hostname**   none — internal only, reached via `postgres-rw`/`postgres-ro`/`postgres-r.database.svc.cluster.local` (CNPG's default per-role Service naming, not overridden here)

## What it does here
Runs the CNPG operator plus a single shared 3-instance PostgreSQL 18 cluster (`postgres`) that backs nearly every stateful app in the cluster — one Postgres, one role + one `Database` CR per consuming app, rather than a dedicated cluster per app. It's the one thing that, if down, takes six other apps down with it.

## Architecture at a glance
- **Depends on:** `external-secrets-stores` (security ns, for the `onepassword` `ClusterSecretStore`); `plugin-barman-cloud-objectstore` (database ns) for the S3 `ObjectStore` the `Cluster` references by name; storage class `zfs-nfs`.
- **Depended on by:** authentik, nextcloud, paperless-ngx, open-webui, firecrawl, and hermes-agent's portfolio-tracking job — see [Secrets](#secrets) for the full per-app breakdown. Also polled (not a functional dependency) by Gatus's health check and incidentally referenced in a CoreDNS comment unrelated to actual usage — see [Known quirks](#known-quirks).

## Repo layout
This app is three Flux Kustomization stages (`kubernetes/apps/database/cloudnative-pg/ks.yaml`), each depending on the previous:

| Stage / File | Purpose |
| --- | --- |
| `kubernetes/apps/database/cloudnative-pg/app/ocirepository.yaml` + `helmrelease.yaml` | The operator itself (chart, CRDs, RBAC, monitoring) |
| `kubernetes/apps/database/cloudnative-pg/cluster/cluster.yaml` | The shared `Cluster` CR: instances, storage, tuning, managed roles, barman plugin config |
| `kubernetes/apps/database/cloudnative-pg/cluster/externalsecret.yaml` | Superuser credentials |
| `kubernetes/apps/database/cloudnative-pg/cluster/ciliumnetworkpolicy.yaml` | Ingress/egress rules for the `postgres` cluster's pods |
| `kubernetes/apps/database/cloudnative-pg/cluster/scheduledbackup.yaml` | Daily barman-cloud backup schedule |
| `kubernetes/apps/database/cloudnative-pg/databases/database-*.yaml` | One `Database` CR per consuming app (declarative `CREATE DATABASE`) |
| `kubernetes/apps/database/cloudnative-pg/databases/externalsecret-*.yaml` | One managed-role password ExternalSecret per consuming app |
| `kubernetes/apps/database/plugin-barman-cloud/` | Separate app: the barman-cloud CNPG plugin + S3 `ObjectStore` + Intercolo S3 credentials (`plugin-barman-cloud-objectstore` Kustomization is a dependency of the `cluster` stage above) |

The `cloudnative-pg-cluster` Kustomization is deliberately `wait: false` (`ks.yaml`, with an inline comment): Flux doesn't recognize the CNPG `Cluster` CRD's health status, so `wait: true` would just time out even when the cluster is healthy.

## Secrets
| ExternalSecret | 1Password source | Consumed by |
| --- | --- | --- |
| `cloudnative-pg-superuser` (`cluster/externalsecret.yaml`) | item `cloudnative-pg`, field `POSTGRES_SUPER_PASS` | `Cluster.spec.superuserSecret` — `enableSuperuserAccess: true` |
| `authentik-db-role` (`databases/externalsecret-authentik.yaml`) | item `authentik`, field `AUTHENTIK_POSTGRESQL_PASSWORD` | managed role `authentikusr` → database `authentik` |
| `paperless-db-role` (`databases/externalsecret-paperless.yaml`) | item `paperless`, field `PAPERLESS_DBPASS` | managed role `paperlessusr` → database `paperlessdb` |
| `open-webui-db-role` (`databases/externalsecret-open-webui.yaml`) | item `openwebui`, field `OPENWEBUI_DB_PASS` | managed role `openwebuiusr` → databases `openwebuidbapp` and `openwebuidbrag` |
| `nextcloud-db-role` (`databases/externalsecret-nextcloud.yaml`) | item `nextcloud`, field `NEXTCLOUD_POSTGRESQL_PASSWORD` | managed role `nextcloudusr` → database `nextcloud` |
| `firecrawl-db-role` (`databases/externalsecret-firecrawl.yaml`) | item `firecrawl`, field `FIRECRAWL_POSTGRESQL_PASSWORD` | managed role `firecrawlusr` → database `firecrawl` |
| `portfolio-db-role` (`databases/externalsecret-portfolio.yaml`) | item `portfolio`, field `PORTFOLIO_POSTGRESQL_PASSWORD` | managed role `portfoliousr` → database `portfolio` |

Every managed-role ExternalSecret's `target.template.data` sets both `username` and `password` — required by CNPG 0.29.0, not optional (see [Known quirks](#known-quirks)). Each consuming app then holds its **own** copy of the same password in its own namespace's ExternalSecret (K8s Secrets don't cross namespaces) — e.g. `kubernetes/apps/security/authentik/app/externalsecret.yaml` pulls the same `AUTHENTIK_POSTGRESQL_PASSWORD` field independently rather than referencing this namespace's secret.

The barman-cloud S3 credentials (`intercolo-credentials` ExternalSecret, item `intercolo`, fields `S3_API_KEY`/`S3_API_SECRET`/`S3_ENDPOINT`/`S3_REGION`) live under `kubernetes/apps/database/plugin-barman-cloud/app/externalsecret.yaml`, not this directory — the `ObjectStore` CR (`kubernetes/apps/database/plugin-barman-cloud/objectstore/objectstore.yaml`) consumes `S3_ENDPOINT` via Flux `postBuild.substituteFrom` (`${S3_ENDPOINT}` in `endpointURL`), never a literal value in the manifest.

## Routing & access
No HTTPRoute — this app is never exposed outside the cluster. Access is Service DNS only: `postgres-rw`/`postgres-ro`/`postgres-r.database.svc.cluster.local` (CNPG's standard read-write/read-only/read-any Service split, not something this repo configures explicitly).

`kubernetes/apps/database/cloudnative-pg/cluster/ciliumnetworkpolicy.yaml` governs the `postgres` cluster pods:
- **Ingress:** cluster-internal replication between instances; the operator's health-check/management traffic on `:8000` (Patroni-style REST API); the **host** entity on `:8000` specifically for kubelet readiness/liveness probes (a plain pod-to-pod allow isn't enough since kubelet runs in host network — inline comment explains this, and commit `bdef8fb` is the fix that added it); `:5432` from **any pod in the cluster** (`fromEntities: [cluster]` — not scoped to a namespace allow-list, unlike most other apps' CiliumNetworkPolicies in this repo); and `:9187` from Prometheus (`monitoring` ns) for the CNPG metrics exporter, added in `d85ff72` after PodMonitor targets came back `up=0`.
- **Egress:** `toEntities: [cluster, world]` with no port restriction — needed for barman-cloud's WAL/base-backup uploads to Intercolo S3 and DNS resolution, but broader than a scoped egress rule (comment says "replication + barman S3 backup + DNS" but the rule itself doesn't limit ports or destinations to just those).

No OIDC/SSO — this is a database, not a UI.

## Storage
Each of the 3 instances gets its own `storage` (20Gi) and `walStorage` (5Gi) PVC on `storageClass: zfs-nfs` (`cluster/cluster.yaml`). `topologySpreadConstraints` forces `DoNotSchedule` across distinct nodes (`kubernetes.io/hostname`), so all 3 replicas land on different worker nodes.

Two independent, non-overlapping backup paths:
- **Postgres data itself:** barman-cloud plugin, `ScheduledBackup` `postgres-daily` (`cluster/scheduledbackup.yaml`) at 02:30 nightly to `s3://k8s-postgres-backup/` (Intercolo S3, `plugin-barman-cloud/objectstore/objectstore.yaml`), flat 30-day `retentionPolicy` (no GFS tiering here, unlike Velero's volume backups).
- **PVCs themselves:** **not** covered by Velero — `kubernetes/apps/velero/schedules/schedule-daily.yaml`'s `includedNamespaces` lists `nextcloud`, `paperless`, `open-webui`, `hermes-agent` only; `database` is deliberately absent since Postgres data is already covered by barman-cloud and a filesystem-level snapshot of a running cluster's PVCs wouldn't be consistent anyway.

## Known quirks
- **Managed-role secrets need both `username` and `password` keys, and the `Secret.type` is immutable.** CNPG 0.29.0 silently fails to reconcile a managed role if its `passwordSecret` is missing `username` (`Cluster.status.managedRolesStatus.cannotReconcile`), and once a role's Secret is created, its `.type` can't be flipped between `Opaque` and `kubernetes.io/basic-auth` — ESO errors on the update. Every `databases/externalsecret-*.yaml` sets `username` explicitly for this reason; several carry an inline `# CNPG managed roles require both username and password keys` comment as a result. Recorded from an earlier debugging session, not re-verified live for this doc.
- **The "portfolio" database has nothing to do with `kubernetes/apps/portfolio/`.** The `portfoliousr`/`portfolio` role+database pair (added in `5cbb487`) is consumed entirely by `hermes-agent`'s portfolio-tracking feature (`kubernetes/apps/hermes-agent/hermes-agent/app/job-portfolio-schema.yaml` applies the schema; `deployment.yaml` connects at runtime) — the actual `philipp-rosch-site` portfolio website under `kubernetes/apps/portfolio/` doesn't touch Postgres at all. Same name, unrelated apps.
- **`tradingusr`/`tradingreadonly` and the `trading` database were fully dropped, not just unmanaged**, on 2026-08-12 when hermes-agent's trading-bot feature was deprovisioned (`5e39883`; comment in `cluster/cluster.yaml` above `managed.roles`) — nothing left to clean up if this resurfaces.
- **`firecrawl` used to be a dedicated CNPG cluster; it was migrated onto this shared one** (`7bc5304`) — if debugging firecrawl's Postgres connectivity, don't look for a separate cluster.
- **`wait: false` on the `cloudnative-pg-cluster` Kustomization is intentional**, not a missed setting — see [Repo layout](#repo-layout).

## Common operations
- Upgrade chart or Postgres image: chart version in `app/ocirepository.yaml` (`spec.ref.tag`), Postgres image in `cluster/cluster.yaml` (`spec.imageName`, pinned by digest) — both carry `# renovate:` markers and are usually bumped automatically. Commit, push, Flux reconciles within `interval` (1h) or force with `flux reconcile helmrelease cloudnative-pg -n database`.
- Add a new consuming app: add a `Database` CR + a `passwordSecret`-backed role in `cluster.yaml`'s `managed.roles`, plus a matching `externalsecret-<app>.yaml`/`database-<app>.yaml` pair under `databases/`, and reference both in `databases/kustomization.yaml`. Remember the `username`+`password` requirement above.
- Rotate a role password: update the 1Password item's field, then `kubectl annotate externalsecret <name>-db-role -n database force-sync=$(date +%s)` (or wait for refresh) — the operator picks up the new password on its own reconcile loop, no restart needed unless status is stuck on stale `cannotReconcile` (see Known quirks, then `kubectl rollout restart deploy/cloudnative-pg -n database`).
- Pause reconciliation: `flux suspend kustomization cloudnative-pg -n database` (operator), `cloudnative-pg-cluster` (Cluster CR), or `cloudnative-pg-databases` (per-app roles/DBs) independently.
- Check cluster health: `kubectl get cluster postgres -n database` / `kubectl cnpg status postgres -n database` (if the `kubectl-cnpg` plugin is installed).

## TODOs / unknowns
- Whether the barman-cloud daily backup or the automated Velero restore-test pattern (`kubernetes/apps/velero/`) has ever been used to actually restore this Postgres cluster is not verified from this repo — only that the schedule exists and runs.
- CNPG's own Grafana dashboard is enabled (`monitoring.grafanaDashboard.create: true`, `app/helmrelease.yaml`) but this doc doesn't verify it's actually surfaced/used in `kubernetes/apps/monitoring/grafana/` dashboards — not checked for this pass.
- Gatus's TCP health check (`kubernetes/apps/monitoring/gatus/app/configmap.yaml`, group "Database") and a CoreDNS comment referencing `postgres-rw.database.svc.cluster.local` as an example hostname in an unrelated AAAA/NXDOMAIN fix are the only other repo hits for this Service name — confirmed neither is a functional dependency, just noted here so a future grep doesn't have to redo that check.

---
_Cite every non-obvious claim with a repo-root-relative file path (e.g. `kubernetes/apps/security/authentik/app/helmrelease.yaml`), not a bare filename — this doc lives under `docs/apps/`, so relative paths must resolve from there._
