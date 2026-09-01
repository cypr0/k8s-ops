# nextcloud-exporter

> **Namespace**  nextcloud
> **Source**     `bjw-s-labs/app-template` chart v5.1.0 via `OCIRepository` (`kubernetes/apps/nextcloud/nextcloud-exporter/app/ocirepository.yaml`) for the Deployment; the CronJob component is a plain manifest, no chart
> **Hostname**   none — internal-only, no HTTPRoute in this app's directory

## What it does here
Two independent metrics pipelines for Nextcloud's `serverinfo` API, bundled under one Flux Kustomization (`kubernetes/apps/nextcloud/nextcloud-exporter/ks.yaml`):
1. `nextcloud-exporter` — a `Deployment` running `ghcr.io/xperimental/nextcloud-exporter:0.9.1` that scrapes the serverinfo API and re-exposes it as Prometheus metrics on `:9205` (`kubernetes/apps/nextcloud/nextcloud-exporter/app/helmrelease.yaml`).
2. `nextcloud-stats-exporter` — a `CronJob` (every 15 min) running a hand-written Python script that hits the same serverinfo API and bulk-indexes the result into OpenSearch (`kubernetes/apps/nextcloud/nextcloud-exporter/app/configmap-stats-exporter.yaml`), so OpenSearch dashboards can query Nextcloud stats directly instead of only through Prometheus.

## Architecture at a glance
- **Depends on:** Nextcloud's internal nginx Service (`nextcloud.nextcloud.svc.cluster.local:8080`); the `nextcloud-credentials` Secret, owned by the **nextcloud** app's own ExternalSecret (`kubernetes/apps/nextcloud/nextcloud/app/externalsecret.yaml`), not by this app; OpenSearch (`opensearch.logging.svc.cluster.local:9200`) for the stats CronJob write path; kube-dns.
- **Depended on by:** none at runtime. Prometheus (`ServiceMonitor`) and Grafana (a dashboard) consume its metrics, but nothing breaks if this app is down other than a metrics gap.

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/nextcloud/nextcloud-exporter/app/ocirepository.yaml` | Pins `app-template` chart to v5.1.0 |
| `kubernetes/apps/nextcloud/nextcloud-exporter/app/helmrelease.yaml` | `nextcloud-exporter` Deployment: image, env, probes, resources |
| `kubernetes/apps/nextcloud/nextcloud-exporter/app/servicemonitor.yaml` | Prometheus scrape config for the Deployment |
| `kubernetes/apps/nextcloud/nextcloud-exporter/app/ciliumnetworkpolicy.yaml` | Network policy for the Deployment |
| `kubernetes/apps/nextcloud/nextcloud-exporter/app/cronjob-stats-exporter.yaml` | `nextcloud-stats-exporter` CronJob (schedule, container, security context) |
| `kubernetes/apps/nextcloud/nextcloud-exporter/app/configmap-stats-exporter.yaml` | The Python script the CronJob runs |
| `kubernetes/apps/nextcloud/nextcloud-exporter/app/ciliumnetworkpolicy-stats.yaml` | Network policy for the CronJob |
| `kubernetes/apps/nextcloud/nextcloud-exporter/app/externalsecret-opensearch.yaml` | OpenSearch write credentials for the CronJob |
| `kubernetes/apps/nextcloud/nextcloud-exporter/ks.yaml` | Flux Kustomization — no `dependsOn` (see Known quirks) |

## Secrets
| ExternalSecret | 1Password source | Consumed by |
| --- | --- | --- |
| `nextcloud-env` (owned by the `nextcloud` app, not this one) | item `nextcloud`, fields `NEXTCLOUD_ADMIN_USERNAME`/`NEXTCLOUD_ADMIN_PASSWORD`, templated into the `nextcloud-credentials` Secret as `ADMIN_USER`/`ADMIN_PASS` | Both the `nextcloud-exporter` container (`NEXTCLOUD_USERNAME`/`NEXTCLOUD_PASSWORD` env) and the `nextcloud-stats-exporter` CronJob container (`NEXTCLOUD_USER`/`NEXTCLOUD_PASS` env) |
| `opensearch-write-credentials` (`kubernetes/apps/nextcloud/nextcloud-exporter/app/externalsecret-opensearch.yaml`) | item `opensearch`, field `OPENSEARCH_ADMIN_PASSWORD` → templated as `OPENSEARCH_PASSWORD` | `nextcloud-stats-exporter` CronJob container, as `OPENSEARCH_PASSWORD` env |

The OpenSearch ExternalSecret is duplicated in this namespace rather than referenced cross-namespace, because ExternalSecrets/Secrets are namespace-scoped — see the file's header comment.

## Routing & access
- No HTTPRoute; both components are internal-only (ClusterIP Service for the Deployment, no Service for the CronJob).
- `ciliumnetworkpolicy.yaml`: ingress allowed only from `prometheus` pods in the `monitoring` namespace on `:9205`; egress to kube-dns and to the `nextcloud` Service on port 80 (nginx). A comment there notes Cilium evaluates egress against the container port post-DNAT.
- `ciliumnetworkpolicy-stats.yaml`: no ingress rule at all (nothing scrapes a CronJob); egress to kube-dns, the `nextcloud` Service (:80), and OpenSearch (`logging` namespace, :9200).
- No SSO/OIDC — both components authenticate to Nextcloud with the shared admin credentials above, not via Authentik.
- Grafana ships a dashboard built on this exporter's metrics.

## Storage
No PVCs — both components are stateless. Not part of any Velero/Kopia backup schedule; there's nothing here that needs restoring.

## Known quirks
- The Kustomization deliberately has **no `dependsOn`** on the `nextcloud` Kustomization (`kubernetes/apps/nextcloud/nextcloud-exporter/ks.yaml`, comment + commit `f896f82`). It originally pointed at `flux-system/nextcloud`, which was wrong (the Kustomization lives in the `nextcloud` namespace) and would have blocked this app indefinitely whenever the `nextcloud` HelmRelease degraded — it was dropped since the exporter only needs the chart and the already-existing `nextcloud-credentials` Secret.
- `configmap-stats-exporter.yaml` coalesces null numeric fields (e.g. `cpuload`) to `0.0` before writing to OpenSearch — a comment there explains OpenSearch's dynamic mapping never creates a field for a null first value, which permanently breaks any dashboard referencing that field until a real-typed value is written once (fixed in commit `69faedc`).
- The `ServiceMonitor` scrapes every 60s with a 30s timeout — longer than the stack's usual default — because the serverinfo API can take a couple of seconds to respond and a tight timeout was marking the target down.
- The two components have different container security contexts: the Deployment runs `readOnlyRootFilesystem: true` while the CronJob runs `readOnlyRootFilesystem: false` — likely because the stock `python:3.14-alpine` image needs a writable root filesystem, but this isn't stated anywhere in the repo.

## Common operations
- Upgrade the exporter image or app-template chart version: edit `helmrelease.yaml` (image tag) or `ocirepository.yaml` (chart tag), commit, push; Flux reconciles within `interval: 1h` or force with `flux reconcile helmrelease nextcloud-exporter -n nextcloud`.
- Edit the stats-export script: change `configmap-stats-exporter.yaml`; the CronJob picks it up on its next scheduled run (no rolling restart mechanism configured).
- Rotate a secret: update the relevant 1Password item, then `kubectl annotate externalsecret <name> -n nextcloud force-sync=$(date +%s)`, or wait for the refresh interval.
- Pause reconciliation: `flux suspend kustomization nextcloud-exporter -n flux-system`.
- Check the stats CronJob's recent runs: `kubectl get jobs -n nextcloud -l app.kubernetes.io/name=nextcloud-stats-exporter` (history capped at 3 successful / 3 failed).

## TODOs / unknowns
- No `PrometheusRule` alerts on `nextcloud-stats-exporter` CronJob failures — a silent failure (e.g. bad OpenSearch creds) would only surface as a gap in the OpenSearch dashboards, not a page. Not verified whether this is intentional.
- No incident postmortem in `docs/incidents/` currently references this app.
- Why the CronJob needs `readOnlyRootFilesystem: false` isn't documented in-repo — worth confirming next time the image is touched.
