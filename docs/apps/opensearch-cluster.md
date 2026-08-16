# OpenSearch Cluster

> **Namespace**  logging
> **Source**     `OpenSearchCluster` custom resource (`opensearch.org/v1`), reconciled by the opensearch-k8s-operator installed by the sibling app `kubernetes/apps/logging/opensearch-operator/` — not a HelmRelease itself. Defined in `kubernetes/apps/logging/opensearch-cluster/app/cluster.yaml`.
> **Hostname**   `logs.${SECRET_DOMAIN}` (Dashboards) — internal-only via the `envoy-internal` gateway, requires VPN, not exposed through the Cloudflare tunnel

## What it does here
The actual 3-node OpenSearch data cluster + Dashboards backing this cluster's centralized logging and lightweight SIEM. It ingests syslog (OPNsense, Proxmox, fail2ban via Fluent Bit), Falco runtime alerts, Trivy/Kubescape scan findings, Nextcloud/Paperless stats-exporter output, and Velero backup/restore-test results, then serves them through pre-built OpenSearch Dashboards visualizations and a handful of Security Analytics alerting monitors (firewall block spikes, SSH brute force, Falco criticals, Trivy CRITICAL CVEs, Kubescape critical CIS violations) that page out via Pushover. This doc covers only this app's own CR/config; operator installation, CRDs, and operator-level behavior are documented separately under `opensearch-operator`.

## Architecture at a glance
- **Depends on:** `opensearch-operator` Kustomization (CRDs + controller) and `external-secrets-stores`/`onepassword` ClusterSecretStore — both declared as `dependsOn` in `kubernetes/apps/logging/opensearch-cluster/ks.yaml`; Authentik OIDC provider for Dashboards + Security Plugin login (blueprint `kubernetes/apps/security/authentik/app/blueprints/02-opensearch-oidc.yaml`); `zfs-nfs` StorageClass for master node PVCs.
- **Depended on by:** `fluent-bit` (ships logs in), `falco`'s falcosidekick, the Nextcloud/Paperless stats-exporter CronJobs, the Velero restore-test CronJob, and Gatus health checks. The `crd-to-opensearch` CronJob defined *inside this app* also reads Trivy/Kubescape/Velero CRDs cluster-wide via its own ClusterRole.

## Repo layout
Two Flux Kustomizations, both in `kubernetes/apps/logging/opensearch-cluster/ks.yaml`: `opensearch-cluster` (the CR + secrets + routing, path `app/`) and `opensearch-config` (post-install setup Jobs/CronJobs, path `config/`, `dependsOn: opensearch-cluster`).

| File | Purpose |
| --- | --- |
| `kubernetes/apps/logging/opensearch-cluster/app/cluster.yaml` | The `OpenSearchCluster` CR: version, node pool sizing/affinity, TLS, Dashboards OIDC config |
| `kubernetes/apps/logging/opensearch-cluster/app/externalsecrets.yaml` | Admin/Dashboards internal-user credentials + Dashboards OIDC/cookie secrets |
| `kubernetes/apps/logging/opensearch-cluster/app/externalsecret-securityconfig.yaml` | Security Plugin `config.yml` (OIDC auth domain) + `roles_mapping.yml` |
| `kubernetes/apps/logging/opensearch-cluster/app/httproute.yaml` | Dashboards route, internal-only |
| `kubernetes/apps/logging/opensearch-cluster/app/ciliumnetworkpolicy.yaml` | CNPs for masters + Dashboards pods |
| `kubernetes/apps/logging/opensearch-cluster/config/externalsecret.yaml` | Credentials for the setup/dashboard/pipeline Jobs |
| `kubernetes/apps/logging/opensearch-cluster/config/job.yaml` + `configmap-setup.yaml` | One-shot SIEM setup: Pushover notification channel, index templates, alerting monitors |
| `kubernetes/apps/logging/opensearch-cluster/config/job-*-dashboard.yaml` + matching `configmap-*-dashboard.yaml` | One-shot Jobs creating index-patterns/visualizations/dashboards: Trivy, Proxmox, OPNsense, security (Falco+Kubescape), Talos, apps (Nextcloud/Paperless generic), apps-stats, Velero |
| `kubernetes/apps/logging/opensearch-cluster/config/job-proxmox-pipeline.yaml`, `job-opnsense.yaml` + matching configmaps | One-shot ingest-pipeline setup for Proxmox/OPNsense syslog enrichment |
| `kubernetes/apps/logging/opensearch-cluster/config/cronjob-exporter.yaml` + `configmap-exporter.yaml` + `crd-exporter-rbac.yaml` | Hourly export of Trivy/Kubescape/Velero CRs into `trivy-*`/`kubescape-*` daily indices |
| `kubernetes/apps/logging/opensearch-cluster/config/cronjob-refresh-fields.yaml` + `configmap-refresh-fields.yaml` | Hourly refresh of every Dashboards index-pattern's field list from the live mapping |
| `kubernetes/apps/logging/opensearch-cluster/config/ciliumnetworkpolicy.yaml` | CNP for the one-shot/CronJob pods (`opensearch-config-jobs`) |

## Secrets
All pulled from the 1Password `onepassword` `ClusterSecretStore`. Field names only — never resolved values.

| ExternalSecret | 1Password source | Consumed by |
| --- | --- | --- |
| `opensearch-admin-credentials` | item `opensearch`, field `OPENSEARCH_ADMIN_PASSWORD` (username templated as `admin`) | `cluster.yaml` `spec.security.config.adminCredentialsSecret` — operator hashes the password itself |
| `opensearch-dashboards-credentials` | item `opensearch`, field `OPENSEARCH_DASHBOARDS_PASSWORD` (username templated as `kibanaserver`) | `cluster.yaml` `spec.dashboards.opensearchCredentialsSecret` |
| `opensearch-dashboards-oidc` | item `opensearch`, fields `OPENSEARCH_COOKIE_SECRET`, `OPENSEARCH_OIDC_CLIENT_ID`, `OPENSEARCH_OIDC_CLIENT_SECRET` | Dashboards pod env vars, referenced from `cluster.yaml`'s `additionalConfig` via `$${...}` placeholders |
| `opensearch-security-config` | item `opensearch` (templates the full `config.yml`/`roles_mapping.yml`, no separate field names) | `cluster.yaml` `spec.security.config.securityConfigSecret` — the Security Plugin's own OIDC auth-domain + roles mapping, distinct from the Dashboards-side OIDC config above |
| `opensearch-config-credentials` | item `opensearch` field `OPENSEARCH_ADMIN_PASSWORD` → key `OPENSEARCH_PASSWORD`; item `pushover` fields `ALERTMANAGER_PUSHOVER_API_TOKEN` → `PUSHOVER_TOKEN`, `PUSHOVER_USER_KEY` → `PUSHOVER_USER` | All one-shot setup/dashboard/pipeline Jobs and both CronJobs in `config/` |

## Routing & access
- **Dashboards**: routes `logs.${SECRET_DOMAIN}` through the `envoy-internal` Gateway (namespace `network`) only — deliberately VPN/Split-DNS-only, not tunneled to the public internet.
- **SSO — two separate OIDC integration points, both against the same Authentik provider**:
  1. Dashboards' own `opensearch_security.auth.type: openid` config, redirecting to `https://logs.${SECRET_DOMAIN}`.
  2. The Security Plugin's `openid_auth_domain`, which independently validates the OIDC token against OpenSearch's own REST API — this is why the masters' CNP needs its own egress path to Authentik, separate from Dashboards'.
- **roles_mapping.yml** maps Authentik's `groups` claim (`backend_roles`) to internal Security Plugin roles: `all_access` ← `admin`/`OpenSearch Admins`/`authentik Admins`, `readall_and_monitor` ← `OpenSearch Users`, `kibana_user` ← `OpenSearch Admins`/`OpenSearch Users`/`authentik Admins`. Note `admin` is deliberately included in `all_access` — see Known quirks.
- **CiliumNetworkPolicy**:
  - `opensearch-masters`: REST (9200) allowed from any pod in `logging`, `monitoring`, `security`, `nextcloud`, `paperless` namespaces, plus specifically `app: velero-restore-test` in `velero`; transport (9300) only between OpenSearch peers; kubelet probes via `fromEntities: host`. Egress includes a JWKS-validation path to `network/envoy` on port `10443` (post-DNAT Envoy data-plane port) so the Security Plugin's OIDC token validation stays in-cluster.
  - `opensearch-dashboards`: ingress from `network/envoy` (5601), Gatus, and any pod in `logging` (the setup Jobs); egress requires an L7 `matchPattern: "*"` DNS rule plus the same `envoy:10443` OIDC path.
  - `opensearch-config-jobs`: egress for the one-shot Jobs/CronJobs — DNS (L7), `kube-apiserver` (the CRD exporter), OpenSearch REST, Dashboards, `api.pushover.net:443`, and `dl-cdn.alpinelinux.org:443` (the latter added after the setup Job's `apk add curl` step hit a cold egress gap on a rerun).

## Storage
Master node pool: 3 replicas, `20Gi` each on `zfs-nfs`, `ReadWriteOnce`. Dashboards is stateless (no PVC).

**Not covered by Velero.** The `logging` namespace does not appear in `includedNamespaces` of any of the three schedule files (only `nextcloud`, `paperless`, `open-webui`, `hermes-agent` are backed up there). The cluster's own CR/config is git-restorable via Flux, but index data on the master PVCs has no backup path if the underlying NFS storage is lost.

## Known quirks
- **3 replicas is load-bearing, not just for HA.** Commit history (`84307f9`, `d8f86a2`, `8ea45bf`) records that the Bootstrap→Masters transition fails on restart with fewer nodes — `initial_cluster_manager_nodes` only contains the bootstrap node's ID, so masters can't self-elect afterward.
- **Master sizing is a scheduling compromise, not a performance target.** An earlier 8Gi/4g-heap config left masters-1/-2 stuck `Pending` (no quorum, masters-0 crash-looped "cluster-manager not discovered") on this cluster's worker nodes; current values are 3Gi request/limit with a 1.5g heap specifically so all three replicas can schedule.
- **`admin` must stay in `all_access`'s `backend_roles`.** `roles_mapping.yml` explicitly lists `"admin"` alongside the OIDC groups (commit `276d56c` added this after a prior admin lockout with an OIDC-groups-only mapping, per memory — not independently re-verified this session beyond the current file content).
- **`http.generate: true` (Security Plugin HTTP TLS) must stay enabled.** Per memory, disabling it prevents the operator's `securityconfig-update` Job from running at all. Not independently re-tested this session.
- **Dashboards' OIDC secrets need `$${VAR}` (double-dollar) escaping in `additionalConfig`** — a single `$` would get resolved away by Flux's `postBuild.substituteFrom` before it ever reaches the operator.
- **Dashboards index-patterns are created with empty field lists.** Every dashboard-setup script creates its index-pattern saved objects with `"fields": "[]"` and relies on a separate refresh step; the refresh CronJob's header comment documents the resulting failure mode directly: every field-specific visualization broke with "Could not locate that index-pattern-field" while plain counts kept working. Fixed generically by the hourly `refresh-index-pattern-fields` CronJob, which also keeps fields current across daily index rollover.
- **Empty Velero timestamps silently failed bulk-indexing.** Commit `2beef9c` fixed backups that never finished being exported with `startTimestamp`/`completionTimestamp`/`expiration` as `""`, which OpenSearch's strict `date` mapping rejected per-item with no visible error — the Velero dashboard was frozen on stale data, masking real backup failures. Now nulled with `.get(...) or None` and bulk-index errors are surfaced to stderr.
- **One-shot setup Jobs must not carry `ttlSecondsAfterFinished`.** Commit `f76935b` removed it from all of them: it collided with the Kustomization's 1h reconcile interval, causing completed Jobs to be garbage-collected and silently rerun roughly hourly — which tripped Gatus health checks and fired spurious Pushover down/resolved alerts. The recurring CronJobs do keep a TTL, which is fine since they're expected to run repeatedly.
- **One-shot Jobs are re-run via a version annotation bump, not automatically** — each carries its own `<name>-version` annotation; bump it and commit to force a rerun, since a Job's pod template is otherwise immutable and Flux can't reapply it in place.
- **The trading-bot dashboard is gone.** Commit `5e39883` removed the "Hermes Trading Bot" section from the appstats dashboard config (and its exporter CronJob/CNP egress) when the trading bot was deprovisioned; the appstats dashboard now only covers Nextcloud/Paperless stats.

## Common operations
- **Bump OpenSearch/Dashboards version:** edit `spec.general.version` / `spec.dashboards.version` in `cluster.yaml` (Renovate-tracked), commit, push; the `opensearch-cluster` Kustomization reconciles within 1h, or force with `flux reconcile kustomization opensearch-cluster -n logging --with-source`.
- **Rotate a secret:** update the relevant field in the `opensearch` (or `pushover`) 1Password item, then `kubectl annotate externalsecret <name> -n logging force-sync=$(date +%s)` for the specific ExternalSecret, or wait for its refresh interval.
- **Re-run a one-shot setup/dashboard/pipeline Job:** bump that Job's `<name>-version` annotation and commit.
- **Pause reconciliation:** `flux suspend kustomization opensearch-cluster -n logging` and/or `flux suspend kustomization opensearch-config -n logging`.

## TODOs / unknowns
- Whether the NFS "PVC name reused as directory → stale cluster state on delete/recreate" pitfall (from memory) still applies to the current `zfs-nfs` StorageClass config — not independently re-verified against a live PVC delete/recreate this session.
- No explicit repo comment states *why* `logging` is excluded from all three Velero schedules (vs. an intentional "log/scan data is re-derivable" decision) — confirmed as fact from the schedule files, but the reasoning is inferred, not documented.
- The `opensearch-security-config` ExternalSecret's exact 1Password field-to-template mapping wasn't fully broken out beyond `dataFrom: extract: key: opensearch`.

---
_Cite every non-obvious claim with a repo-root-relative file path (e.g. `kubernetes/apps/security/authentik/app/helmrelease.yaml`), not a bare filename — this doc lives under `docs/apps/`, so relative paths must resolve from there._
