# plugin-barman-cloud

> **Namespace**  database
> **Source**     plain manifests — upstream release manifest pinned by tag (not a HelmRelease): `https://github.com/cloudnative-pg/plugin-barman-cloud/releases/download/v0.12.0/manifest.yaml`, referenced directly as a `kustomization.yaml` resource (`kubernetes/apps/database/plugin-barman-cloud/app/kustomization.yaml`), renovate-tracked via `datasource=github-releases`
> **Hostname**   none — no HTTPRoute, no user-facing surface; a cluster-internal CNPG sidecar plugin only

## What it does here
The CNPG (CloudNativePG) plugin that does WAL archiving and backup/restore of the cluster's Postgres data to S3-compatible object storage. It backs the `postgres` `Cluster` (`kubernetes/apps/database/cloudnative-pg/cluster/cluster.yaml:124-128`, `plugins: [{name: barman-cloud.cloudnative-pg.io, isWALArchiver: true, parameters: {barmanObjectName: postgres-backup}}]`) and the nightly `ScheduledBackup` (`kubernetes/apps/database/cloudnative-pg/cluster/scheduledbackup.yaml`, `method: plugin`). Destination is Intercolo, an S3-compatible provider, not AWS — several quirks below exist specifically because Intercolo isn't AWS S3.

## Architecture at a glance
- **Depends on:** `ExternalSecret` `intercolo-credentials` → 1Password item `intercolo` (see Secrets); the `ObjectStore` custom resource `postgres-backup` (`kubernetes/apps/database/plugin-barman-cloud/objectstore/objectstore.yaml`) it manages; `external-secrets-stores` (namespace `security`) per `kubernetes/apps/database/plugin-barman-cloud/ks.yaml`.
- **Depended on by:** the `postgres` `Cluster` (WAL archiver) and the `postgres-daily` `ScheduledBackup`, both of which reference the `ObjectStore` by name (`barmanObjectName: postgres-backup`) — `kubernetes/apps/database/cloudnative-pg/ks.yaml:39-41` makes this explicit: the `cloudnative-pg-cluster` Kustomization `dependsOn` `plugin-barman-cloud-objectstore` because "the Cluster references the ObjectStore by name."

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/database/plugin-barman-cloud/ks.yaml` | Two Flux Kustomizations: `plugin-barman-cloud` (plugin + secret) and `plugin-barman-cloud-objectstore` (the `ObjectStore` CR), the latter `dependsOn` the former |
| `kubernetes/apps/database/plugin-barman-cloud/app/kustomization.yaml` | Pulls the upstream plugin manifest by pinned release tag, plus local CNP + ExternalSecret |
| `kubernetes/apps/database/plugin-barman-cloud/app/externalsecret.yaml` | S3 credentials for the `ObjectStore` (see Secrets) |
| `kubernetes/apps/database/plugin-barman-cloud/app/ciliumnetworkpolicy.yaml` | Egress-only policy for the plugin pod (`app: barman-cloud`) |
| `kubernetes/apps/database/plugin-barman-cloud/objectstore/objectstore.yaml` | The `ObjectStore` CR: destination bucket, compression, retention, sidecar env overrides |

## Secrets
| Key (in `intercolo-credentials`) | 1Password source | Consumed by |
| --- | --- | --- |
| `S3_API_KEY` | item `intercolo`, property `S3_API_KEY` | `ObjectStore.spec.configuration.s3Credentials.accessKeyId` |
| `S3_API_SECRET` | item `intercolo`, property `S3_API_SECRET` | `ObjectStore.spec.configuration.s3Credentials.secretAccessKey` |
| `S3_ENDPOINT` | item `intercolo`, property `S3_ENDPOINT` | Flux `postBuild.substituteFrom` into `ObjectStore.spec.configuration.endpointURL` (`${S3_ENDPOINT}`) — resolved at kustomize-build time, not read directly by the plugin pod |
| `S3_REGION` | item `intercolo`, property `S3_REGION` | pulled but currently unused — `s3Credentials.region` is commented out in `objectstore.yaml:18-22` ("Intercolo's S3 item may not expose S3_REGION; not all S3-compatible providers require it") |

`kubernetes/apps/database/plugin-barman-cloud/app/externalsecret.yaml`'s own comment explains why these are explicit per-key `remoteRef`s rather than a whole-item `dataFrom.extract`: the `intercolo` 1Password item is a Login item with extra fields (email, one-time password, ...) whose names aren't valid envsubst identifiers, which previously broke `postBuild.substituteFrom` — fixed in commit `aeb5ab7`. Velero (`kubernetes/apps/velero/app/externalsecret.yaml`) reuses the same `intercolo` 1Password item for its own, separately-scoped `ExternalSecret`.

## Routing & access
No HTTPRoute, no ingress rule at all — `kubernetes/apps/database/plugin-barman-cloud/app/ciliumnetworkpolicy.yaml` only defines egress for `endpointSelector: app: barman-cloud`:
- kube-dns (53/UDP+TCP)
- `kube-apiserver` entity
- `world` on 443/TCP — labeled in-file as "S3-compatible backup storage (Backblaze B2, Cloudflare R2, AWS S3)"

The `postgres` Cluster's own CNP (`kubernetes/apps/database/cloudnative-pg/cluster/ciliumnetworkpolicy.yaml:53-57`) separately allows `cluster` + `world` egress from the Postgres pods themselves, commented "Allow replication + barman S3 backup + DNS" — i.e. the WAL-archive path egresses from the Postgres pod, not (only) from the plugin pod.

## Storage
The plugin itself owns no PVC — it's a sidecar/controller pattern operating against the `postgres` Cluster's existing storage and the S3 `ObjectStore`. The `ObjectStore` (`objectstore.yaml`) is the backup target, not a target *of* backup:
- `destinationPath: s3://k8s-postgres-backup/`
- `wal.compression` / `data.compression`: `gzip`
- `retentionPolicy: 30d` — flat retention, no GFS tiering (unlike Velero's volume-backup schedules in `kubernetes/apps/velero/`)

Postgres is deliberately excluded from Velero's volume-backup scope since it has this separate, Postgres-native backup path (per `project_backup_infrastructure` memory; not independently re-derived from a Velero manifest for this doc — see TODOs).

## Known quirks
- **Intercolo rejects botocore's default trailing-checksum header on WAL archive uploads.** `objectstore.yaml`'s `instanceSidecarConfiguration.env` sets `AWS_REQUEST_CHECKSUM_CALCULATION` / `AWS_RESPONSE_CHECKSUM_VALIDATION` to `when_required`. Without this, every `barman-cloud-wal-archive` call failed with a `BadHeader "x-amz-trailer"` (exit status 4) — commit `caabcaf` notes one instance (`postgres-3`) crash-looped because the resulting stalled WAL backlog blocked it from ever starting.
- **Two-stage Flux Kustomization is required, not stylistic.** `plugin-barman-cloud-objectstore` `dependsOn` `plugin-barman-cloud` because the `ObjectStore`'s `endpointURL` needs `postBuild.substituteFrom` against the `intercolo-credentials` Secret, and a single Kustomization can't create a Secret and substitute from it in the same reconcile (`kubernetes/apps/database/plugin-barman-cloud/ks.yaml`, comment on the second Kustomization; commit `19de454`).
- **Falco flags routine WAL archiving as suspicious — both are known false positives, already excluded.** `kubernetes/apps/security/falco/app/helmrelease-falco.yaml:99-105` excludes the `cnpg_wal_archive` process (was firing CRITICAL ~1300x/week on one Falco pod alone) and `:115-121` excludes the CNPG operator's `sh -c /controller/manager wal-archive ...` invocation on the Postgres pod (confirmed live 2026-08-16, 30 CRITICAL hits in 6h). If either exclusion is ever removed, expect an alert storm, not an actual incident.
- **`S3_REGION` is pulled but not wired up.** The commented-out `s3Credentials.region` block in `objectstore.yaml` exists specifically for future use if uploads start failing without an explicit region — not currently needed against Intercolo.

## Common operations
- Bump the plugin version: edit the pinned release-tag URL in `kubernetes/apps/database/plugin-barman-cloud/app/kustomization.yaml` (renovate manages this via `datasource=github-releases depName=cloudnative-pg/plugin-barman-cloud`), commit, push.
- Rotate S3 credentials: update the `intercolo` 1Password item, then `kubectl annotate externalsecret intercolo-credentials -n database force-sync=$(date +%s)` (this also affects Velero, which reads the same item).
- Force a reconcile after changing either stage: `flux reconcile kustomization plugin-barman-cloud -n flux-system --with-source`, then `flux reconcile kustomization plugin-barman-cloud-objectstore -n flux-system` (respect the dependency order — the objectstore stage will fail to substitute if the secret stage hasn't landed yet).
- Trigger an on-demand backup: create a `Backup` CR with `method: plugin` and `pluginConfiguration.name: barman-cloud.cloudnative-pg.io` / `parameters.barmanObjectName: postgres-backup`, mirroring `kubernetes/apps/database/cloudnative-pg/cluster/scheduledbackup.yaml`'s shape.

## TODOs / unknowns
- Whether the 30d flat `retentionPolicy` on `postgres-backup` has ever been exercised by an actual restore is not verified from this repo — no restore-test automation for Postgres analogous to Velero's daily restore-test CronJob (`kubernetes/apps/velero/restore-test/`) was found under `kubernetes/apps/database/`.
- Cross-reference to `docs/apps/cloudnative-pg.md` for the `Cluster`/`ScheduledBackup` side of this pipeline.

---
_Secret/IP scan: clean — no resolved secret values, no real public IPs/hostnames (bucket name and vendor name "Intercolo" are already used in plaintext elsewhere in this repo's comments; endpoint URL is cited only as the `${S3_ENDPOINT}` envsubst variable, never resolved)._
