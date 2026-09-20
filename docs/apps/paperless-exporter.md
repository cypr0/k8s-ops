# paperless-exporter

> **Namespace**  paperless  
> **Source**     plain manifests  
> **Hostname**   (internal only, no HTTPRoute)

## What it does here
Exposes Paperless-ngx's REST API statistics (`/api/statistics/`, `/api/status/`) as Prometheus metrics. Paperless itself has no native `/metrics` endpoint, so this scrape-time proxy translates JSON responses into the Prometheus text format. Runs as a single-replica Deployment with a ServiceMonitor that Prometheus scrapes every 5 minutes.

## Architecture at a glance
- **Depends on:** `paperless-ngx` Service (same namespace), ExternalSecret → 1Password item `paperless-secret` (reused from the main Paperless app, not provisioned here)
- **Depended on by:** Prometheus (monitoring namespace) — scrapes the `/metrics` endpoint

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/paperless/paperless-exporter/ks.yaml` | Kustomization that deploys this app; deliberately does NOT depend on the main `paperless` Kustomization so metrics remain available even when the app is unhealthy |
| `kubernetes/apps/paperless/paperless-exporter/app/configmap-exporter.yaml` | The Python exporter script itself, embedded as a ConfigMap |
| `kubernetes/apps/paperless/paperless-exporter/app/deployment.yaml` | Deployment + Service + ServiceMonitor |
| `kubernetes/apps/paperless/paperless-exporter/app/ciliumnetworkpolicy.yaml` | Network policy: allows Prometheus ingress on port 9877, allows egress to Paperless API and DNS |

## Secrets
| ExternalSecret | 1Password item/field | Consumer |
| --- | --- | --- |
| (none — reuses existing secret) | `paperless-secret` item, fields `PAPERLESS_ADMIN_USER` and `PAPERLESS_ADMIN_PASSWORD` | Deployment env vars `PAPERLESS_USER` and `PAPERLESS_PASS`, used for HTTP Basic auth against the Paperless API |

The exporter does not provision its own ExternalSecret; it reads the same `paperless-secret` that the main Paperless app already owns.

## Routing & access
- **Internal only:** no HTTPRoute, no external exposure. Prometheus scrapes the Service on port 9877.
- **SSO:** none — this is a metrics endpoint, not a user-facing UI.
- **CiliumNetworkPolicy:** `kubernetes/apps/paperless/paperless-exporter/app/ciliumnetworkpolicy.yaml` allows ingress from `monitoring/prometheus` and from the kubelet (readiness probe), and allows egress to `kube-system/kube-dns` and `paperless/paperless-ngx` on port 80.

## Storage
None — stateless exporter, no PVCs.

## Known quirks
- **Deliberately decoupled from the main Paperless Kustomization.** The `ks.yaml` has an inline comment explaining that `dependsOn: paperless` is omitted so the exporter (and its metrics) remain available even when the main app is unhealthy — "losing the metrics exactly when the app is unhealthy and they matter most" (`kubernetes/apps/paperless/paperless-exporter/ks.yaml`).
- **Stdlib-only Python, no pip.** The exporter uses plain `alpine:3.24.2` + `apk add python3`, never a `python:*-alpine` image. An inline comment in `kubernetes/apps/paperless/paperless-exporter/app/deployment.yaml` explains this avoids CPython version collisions that broke `proxmox-ansible` (see `kubernetes/apps/automation/proxmox-ansible/app/cronjob.yaml` for the incident that motivated this choice).
- **Scrape-time proxy, not a sidecar.** Every Prometheus scrape triggers two live API calls to Paperless (`/api/statistics/` and `/api/status/`), so the ServiceMonitor interval is set to 5 minutes — "None of these values move on a shorter timescale" (`kubernetes/apps/paperless/paperless-exporter/app/deployment.yaml`).
- **Replaces an earlier OpenSearch-based stats CronJob.** The ConfigMap comment notes this exporter "replaces it with a pull-based endpoint so the data lands in Prometheus instead of a second datastore" — the CronJob was removed on 2026-09-20 (`kubernetes/apps/paperless/paperless-exporter/app/configmap-exporter.yaml`).
- **API failures reported as `paperless_up == 0`.** If the Paperless API is unreachable or returns an error, the exporter emits `paperless_up 0` and an `# ERROR` comment in the metrics output, rather than failing the HTTP request. This ensures a broken API is visible in Prometheus as data, not as a target flapping to "down" with no detail (see the `collect()` function in `kubernetes/apps/paperless/paperless-exporter/app/configmap-exporter.yaml`).

## Common operations
- **Update the exporter script:** edit `kubernetes/apps/paperless/paperless-exporter/app/configmap-exporter.yaml`, commit, push. Flux reconciles within 1 hour (or force with `flux reconcile kustomization paperless-exporter -n paperless`). The Deployment does not auto-restart on ConfigMap changes — manually delete the pod or add a `kubectl rollout restart` after the reconcile.
- **Check current metrics:** `kubectl port-forward -n paperless svc/paperless-exporter 9877:9877`, then `curl http://localhost:9877/metrics`.
- **Rotate Paperless admin credentials:** update the `paperless-secret` 1Password item, then `kubectl annotate externalsecret paperless-secret -n paperless force-sync=$(date +%s)` (or wait for the ExternalSecret refresh interval). Restart the exporter pod afterward.
- **Pause reconciliation:** `flux suspend kustomization paperless-exporter -n paperless`.

## TODOs / unknowns
- The ConfigMap references a `proxmox-ansible` incident that motivated the stdlib-only Python choice, but no corresponding `docs/incidents/*.md` file was found in the provided manifests. The exact failure mode is documented in `kubernetes/apps/automation/proxmox-ansible/app/cronjob.yaml` but not yet in a postmortem.
- The comment "removed on 2026-09-20" for the OpenSearch stats CronJob could not be verified from the provided manifests (no git log was supplied). This is stated as fact in the ConfigMap but should be cross-checked against commit history.

---
**Secret/IP scan:** clean. No resolved secret values, real public IPs, or account-identifying paths restated. The exporter reuses the existing `paperless-secret` ExternalSecret (owned by the main Paperless app); only the 1Password item name and field names are cited here, never the credential values themselves.
