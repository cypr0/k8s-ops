# nextcloud-exporter

> **Namespace**  nextcloud
> **Source**     `bjw-s-labs/app-template` chart v5.1.0 via `OCIRepository` (`kubernetes/apps/nextcloud/nextcloud-exporter/app/ocirepository.yaml`)
> **Hostname**   none — internal-only, no HTTPRoute in this app's directory

## What it does here
A `Deployment` running `ghcr.io/xperimental/nextcloud-exporter:0.9.1` that scrapes Nextcloud's `serverinfo` API and re-exposes it as Prometheus metrics on `:9205` (`kubernetes/apps/nextcloud/nextcloud-exporter/app/helmrelease.yaml`), under one Flux Kustomization (`kubernetes/apps/nextcloud/nextcloud-exporter/ks.yaml`).

A second component, `nextcloud-stats-exporter`, used to sit alongside it: a CronJob that polled the same API every 15 minutes and bulk-indexed the result into OpenSearch for dashboards there. It was removed on 2026-09-20 with OpenSearch itself. Nothing was lost in terms of data — the Prometheus exporter above reads the same `serverinfo` endpoint; what went away is a second copy of it in a second store.

## Architecture at a glance
- **Depends on:** Nextcloud's internal nginx Service (`nextcloud.nextcloud.svc.cluster.local:8080`); the `nextcloud-credentials` Secret, owned by the **nextcloud** app's own ExternalSecret (`kubernetes/apps/nextcloud/nextcloud/app/externalsecret.yaml`), not by this app; kube-dns.
- **Depended on by:** none at runtime. Prometheus (`ServiceMonitor`) and Grafana (a dashboard) consume its metrics, but nothing breaks if this app is down other than a metrics gap.

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/nextcloud/nextcloud-exporter/app/ocirepository.yaml` | Pins `app-template` chart to v5.1.0 |
| `kubernetes/apps/nextcloud/nextcloud-exporter/app/helmrelease.yaml` | `nextcloud-exporter` Deployment: image, env, probes, resources |
| `kubernetes/apps/nextcloud/nextcloud-exporter/app/servicemonitor.yaml` | Prometheus scrape config for the Deployment |
| `kubernetes/apps/nextcloud/nextcloud-exporter/app/ciliumnetworkpolicy.yaml` | Network policy for the Deployment |
| `kubernetes/apps/nextcloud/nextcloud-exporter/ks.yaml` | Flux Kustomization — no `dependsOn` (see Known quirks) |

## Secrets
| ExternalSecret | 1Password source | Consumed by |
| --- | --- | --- |
| `nextcloud-env` (owned by the `nextcloud` app, not this one) | item `nextcloud`, fields `NEXTCLOUD_ADMIN_USERNAME`/`NEXTCLOUD_ADMIN_PASSWORD`, templated into the `nextcloud-credentials` Secret as `ADMIN_USER`/`ADMIN_PASS` | the `nextcloud-exporter` container (`NEXTCLOUD_USERNAME`/`NEXTCLOUD_PASSWORD` env) |

This app owns no ExternalSecret of its own.

## Routing & access
- No HTTPRoute; internal-only (ClusterIP Service).
- `ciliumnetworkpolicy.yaml`: ingress allowed only from `prometheus` pods in the `monitoring` namespace on `:9205`; egress to kube-dns and to the `nextcloud` Service on port 80 (nginx). A comment there notes Cilium evaluates egress against the container port post-DNAT.
- No SSO/OIDC — it authenticates to Nextcloud with the shared admin credentials above, not via Authentik.
- Grafana ships a dashboard built on this exporter's metrics.

## Storage
No PVCs — stateless. Not part of any Velero/Kopia backup schedule; there's nothing here that needs restoring.

## Known quirks
- The Kustomization deliberately has **no `dependsOn`** on the `nextcloud` Kustomization (`kubernetes/apps/nextcloud/nextcloud-exporter/ks.yaml`, comment + commit `f896f82`). It originally pointed at `flux-system/nextcloud`, which was wrong (the Kustomization lives in the `nextcloud` namespace) and would have blocked this app indefinitely whenever the `nextcloud` HelmRelease degraded — it was dropped since the exporter only needs the chart and the already-existing `nextcloud-credentials` Secret.
- The `ServiceMonitor` scrapes every 60s with a 30s timeout — longer than the stack's usual default — because the serverinfo API can take a couple of seconds to respond and a tight timeout was marking the target down.

## Common operations
- Upgrade the exporter image or app-template chart version: edit `helmrelease.yaml` (image tag) or `ocirepository.yaml` (chart tag), commit, push; Flux reconciles within `interval: 1h` or force with `flux reconcile helmrelease nextcloud-exporter -n nextcloud`.
- Rotate the Nextcloud admin credential: update the `nextcloud` 1Password item, then force-sync the `nextcloud-env` ExternalSecret (owned by the `nextcloud` app) and restart this Deployment.
- Pause reconciliation: `flux suspend kustomization nextcloud-exporter -n nextcloud`.

## TODOs / unknowns
- No incident postmortem in `docs/incidents/` currently references this app.
- No `PrometheusRule` alerts on this exporter going down; a failure shows only as a gap in the Grafana dashboard.
