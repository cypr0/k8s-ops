# isms-exporter

> **Namespace**  security
> **Source**     plain manifests (no Helm chart)
> **Hostname**   none (internal-only, scraped by Prometheus)

## What it does here
Fetches `assessment/metrics.prom` from the internal GitLab repository `isms/compliance-as-code` and serves it to Prometheus. The exporter is a pass-through by design: it parses nothing, decides nothing, and derives nothing—it fetches a file and adds a handful of metrics about its own fetching. Any ISMS semantics implemented here would be a second opinion on what "satisfied" means, free to disagree with the assessment that produced the number, which is the exact class of drift the ISMS is built to prevent (`kubernetes/apps/security/isms-exporter/app/configmap-exporter.yaml`).

## Architecture at a glance
- **Depends on:** GitLab (namespace `gitlab`), specifically the internal Service `gitlab.gitlab.svc.cluster.local` to read the metrics file; SOPS-encrypted secret for the GitLab API token.
- **Depended on by:** Prometheus (namespace `monitoring`) scrapes `/metrics` every 5 minutes to populate the compliance dashboard.

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/security/isms-exporter/ks.yaml` | Flux Kustomization; no postBuild substitution (same reasoning as isms-collector: the Python script contains no `${VAR}` to be mistaken for a Flux variable, and keeping the two ISMS components symmetrical is worth more than the flexibility) |
| `kubernetes/apps/security/isms-exporter/app/deployment.yaml` | Deployment, Service, ServiceMonitor; runs `python:3.15-alpine` with the exporter script from the ConfigMap |
| `kubernetes/apps/security/isms-exporter/app/configmap-exporter.yaml` | The exporter script itself (`exporter.py`), stdlib-only Python |
| `kubernetes/apps/security/isms-exporter/app/secret.sops.yaml` | SOPS-encrypted GitLab API token |
| `kubernetes/apps/security/isms-exporter/app/ciliumnetworkpolicy.yaml` | Ingress from Prometheus only; egress to kube-dns and GitLab's Workhorse container port 8181 |
| `kubernetes/apps/security/isms-exporter/app/kustomization.yaml` | Kustomize resources list |

## Secrets
| Secret | Source | Consumer |
| --- | --- | --- |
| `isms-exporter-git` | SOPS-encrypted in `kubernetes/apps/security/isms-exporter/app/secret.sops.yaml`; contains a GitLab API token | Mounted as a file at `/secrets/token` in the exporter container; read on startup and used in the `PRIVATE-TOKEN` header for GitLab API requests |

The token is mounted as a file rather than passed as an environment variable because the cluster's own Kyverno policy `prefer-secrets-as-files` (CIS 5.4.1) requires it, and a long-running pod leaking its environment is a larger surface than a CronJob doing it (`kubernetes/apps/security/isms-exporter/app/configmap-exporter.yaml`).

## Routing & access
- **Internal-only:** No HTTPRoute; the Service is `ClusterIP` and reachable only within the cluster.
- **Ingress:** CiliumNetworkPolicy allows Prometheus (namespace `monitoring`, label `app.kubernetes.io/name: prometheus`) to reach port 9878 (`kubernetes/apps/security/isms-exporter/app/ciliumnetworkpolicy.yaml`). The policy comment notes that an egress rule here without a matching ingress rule in GitLab's own policy would fail silently—the connection would be dropped and the exporter would serve nothing but its own health metrics. That trap was already walked into once when the collector moved to this namespace, so the matching ingress rule is part of the same change.
- **Egress:** DNS (kube-dns) and GitLab's Workhorse container port 8181. Port 8181 is Workhorse's container port; GitLab's ingress is evaluated after DNAT, so the Service port 80 the exporter actually dials does not appear in the policy (`kubernetes/apps/security/isms-exporter/app/ciliumnetworkpolicy.yaml`).
- **SSO:** Not applicable (no user-facing interface).

## Storage
None. The exporter is stateless; it caches the fetched metrics file in memory and refreshes it every 5 minutes (configurable via `REFRESH_SECONDS`, default 300).

## Known quirks
- **Staleness is self-revealing by design.** Every freshness value in the metrics file is an absolute timestamp, never an age, so when GitLab is unreachable the exporter keeps serving the last good body and the dashboard's computed ages simply keep growing. A cached body cannot pretend to be fresh (`kubernetes/apps/security/isms-exporter/app/configmap-exporter.yaml`).
- **Health check is independent of GitLab reachability.** `/healthz` returns 200 even if GitLab is down. A failing fetch must not evict the pod from the Service, because Prometheus would then report the target as down and the dashboard would show no data at all, when the truth is "the last known assessment, getting older." That staleness is reported as a metric (`isms_exporter_last_fetch_success_timestamp_seconds`) and alerted on separately (`kubernetes/apps/security/isms-exporter/app/configmap-exporter.yaml`, `kubernetes/apps/security/isms-exporter/app/deployment.yaml`).
- **Deliberately stdlib-only.** Nothing is installed on top of `python:*-alpine`, so the container runs as nobody (UID 65534) with a read-only root filesystem and there is no dependency to keep current. Same reasoning as paperless-exporter (`kubernetes/apps/security/isms-exporter/app/configmap-exporter.yaml`, `kubernetes/apps/security/isms-exporter/app/deployment.yaml`).
- **Dedicated ServiceAccount with no API token.** The exporter talks to GitLab and to nothing else—it has no business holding a credential for the Kubernetes API. Also what the cluster's own Kyverno policies `restrict-default-serviceaccount` and `restrict-token-automount` (CIS 5.1.5, 5.1.6) require. An exporter for the compliance dashboard failing the cluster's compliance policies would be a poor start (`kubernetes/apps/security/isms-exporter/app/deployment.yaml`).
- **fsGroup required for secret readability.** Kubelet owns secret volume files as root and only applies fsGroup as their group, so without `fsGroup: 65534` the 0440 mount is root-only and the exporter dies on startup with a PermissionError (`kubernetes/apps/security/isms-exporter/app/deployment.yaml`).
- **Slow scrape interval by design.** The ServiceMonitor scrapes every 5 minutes with a 30-second timeout. The underlying file is regenerated once a day at most, after the collector commits a snapshot at 04:00 and the pipeline assesses it. A scrape is served from the exporter's cache either way, so a faster interval would buy nothing but series churn (`kubernetes/apps/security/isms-exporter/app/deployment.yaml`).

## Common operations
- **View logs:** `kubectl logs -n security deployment/isms-exporter`
- **Force a fetch:** The exporter refreshes every 5 minutes automatically. To force an immediate refresh, restart the pod: `kubectl rollout restart -n security deployment/isms-exporter`
- **Check what Prometheus sees:** `kubectl port-forward -n security svc/isms-exporter 9878:9878`, then `curl http://localhost:9878/metrics`
- **Rotate the GitLab token:** Update the SOPS-encrypted secret in `kubernetes/apps/security/isms-exporter/app/secret.sops.yaml`, commit, push, and restart the deployment (the token is read once on startup, not watched).
- **Pause reconciliation:** `flux suspend kustomization isms-exporter -n security`

## TODOs / unknowns
- The GitLab token's scope and expiration are not documented in the manifests. The token is SOPS-encrypted, so its actual value and metadata are not visible here. Verify in GitLab's UI or 1Password (if the token is also stored there) what permissions it has and whether it expires.

---

**Secret/IP scan:** Clean. The SOPS-encrypted token in `secret.sops.yaml` is cited but not resolved. The GitLab Service hostname `gitlab.gitlab.svc.cluster.local` is internal (`.cluster.local`) and meaningless outside this cluster. No public IPs, no resolved secrets, no account-identifying paths.
