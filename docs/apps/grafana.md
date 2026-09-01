# Grafana

> **Namespace**  monitoring
> **Source**     Helm chart `grafana` from the `grafana` HelmRepository (`https://grafana.github.io/helm-charts`), version `10.5.15` (`kubernetes/apps/monitoring/grafana/app/helmrelease.yaml`, `helmrepository.yaml`); Grafana image `grafana/grafana:13.2.0`, pinned by digest
> **Hostname**   `grafana.${SECRET_DOMAIN}` — internal-only (VPN/split-DNS), not exposed via the Cloudflare tunnel

## What it does here
Standalone dashboard/visualization frontend for the cluster's metrics and logs. This is **not** the `kube-prometheus-stack` chart's bundled Grafana — that subchart is explicitly disabled (`grafana.enabled: false` in `kubernetes/apps/monitoring/kube-prometheus-stack/app/helmrelease.yaml`) in favor of this dedicated deployment, which is a sibling app in the same namespace. It queries Prometheus, Loki and Alertmanager as datasources and ships a mix of community dashboards (Grafana.com `gnetId`s) and hand-authored ones — a Talos OS availability/security log dashboard, a cross-app health overview, and a Nextcloud metrics dashboard, all inlined as JSON in `helmrelease.yaml`. Login is SSO-only via Authentik OIDC, with role (Admin/Editor/Viewer) derived from Authentik group membership.

## Architecture at a glance
- **Depends on:** Prometheus and Alertmanager Services from `kube-prometheus-stack` (`kube-prometheus-stack-prometheus.monitoring.svc.cluster.local:9090`, `kube-prometheus-stack-alertmanager.monitoring.svc.cluster.local:9093`) and `loki.monitoring.svc.cluster.local:3100`, all wired as datasources in `helmrelease.yaml`; ExternalSecret → 1Password item `grafana` for admin credentials and OIDC client id/secret; Authentik for OIDC login. The Flux Kustomization also hard-`dependsOn` `kube-prometheus-stack`, `loki`, and `external-secrets-stores` (namespace `security`) — `kubernetes/apps/monitoring/grafana/ks.yaml`.
- **Depended on by:** Gatus, which polls `http://grafana.monitoring.svc.cluster.local/api/health` every minute and alerts via Pushover on failure (`kubernetes/apps/monitoring/gatus/app/configmap.yaml`). No other app's Service depends on Grafana — it's an operator/user-facing dashboard, not infrastructure other apps call into.

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/monitoring/grafana/app/helmrelease.yaml` | Chart version, image, datasources, dashboards, OIDC config, persistence |
| `kubernetes/apps/monitoring/grafana/app/helmrepository.yaml` | Upstream chart source (`grafana.github.io/helm-charts`) |
| `kubernetes/apps/monitoring/grafana/app/externalsecret.yaml` | Admin credentials + OIDC client id/secret, from 1Password |
| `kubernetes/apps/monitoring/grafana/app/httproute.yaml` | Internal-only Gateway routing |
| `kubernetes/apps/monitoring/grafana/app/ciliumnetworkpolicy.yaml` | Ingress (Envoy, kubelet, Gatus) and egress (datasources, DNS, OIDC, dashboard downloads) |
| `kubernetes/apps/monitoring/grafana/ks.yaml` | Flux Kustomization; `dependsOn` kube-prometheus-stack, loki, external-secrets-stores |

## Secrets
| ExternalSecret | 1Password item / fields | Consumed by |
| --- | --- | --- |
| `grafana-secret` (`kubernetes/apps/monitoring/grafana/app/externalsecret.yaml`) | Item `grafana`: `GRAFANA_ADMIN_USER` → `GF_SECURITY_ADMIN_USER`, `GRAFANA_ADMIN_PASSWORD` → `GF_SECURITY_ADMIN_PASSWORD`, `GRAFANA_OPENID_CLIENT_ID` → `GF_AUTH_GENERIC_OAUTH_CLIENT_ID`, `GRAFANA_OPENID_CLIENT_SECRET` → `GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET` | `admin.existingSecret`/`envFromSecret: grafana-secret` in `helmrelease.yaml`; reloaded on change via the `secret.reloader.stakater.com/reload: grafana-secret` pod annotation |

The same 1Password item `grafana` is read a second time, independently, by Authentik's own ExternalSecret `authentik-grafana-oidc` (`kubernetes/apps/security/authentik/app/externalsecret-grafana-oidc.yaml`) to populate the matching OAuth2 provider's client id/secret on the Authentik side (`kubernetes/apps/security/authentik/app/blueprints/01-grafana-oidc.yaml`). Both ExternalSecrets must stay in sync since they source the same credential pair for opposite ends of the OIDC handshake.

## Routing & access
- HTTPRoute `grafana.${SECRET_DOMAIN}` attaches only to `envoy-internal` (namespace `network`, `sectionName: https`) — internal-only, reachable via VPN + split-DNS, deliberately not attached to `envoy-external`/the Cloudflare tunnel (`kubernetes/apps/monitoring/grafana/app/httproute.yaml`, comment on the `parentRefs` block).
- SSO: OIDC via Authentik generic OAuth (`auth.generic_oauth` in `helmrelease.yaml`), backed by the Authentik blueprint `kubernetes/apps/security/authentik/app/blueprints/01-grafana-oidc.yaml`. Role (`Admin`/`Editor`/`Viewer`) is derived from Authentik group membership via `role_attribute_path`, using a custom `grafana` scope mapping the blueprint defines to expose `groups` in the token. `oauth_allow_insecure_email_lookup: "true"` is set — email-based account linking without Grafana's stricter lookup, a deliberate SSO tradeoff for this cluster.
- CiliumNetworkPolicy (`ciliumnetworkpolicy.yaml`): ingress restricted to Envoy Gateway pods (namespace `network`, port 3000), kubelet probes (`fromEntities: host`), and Gatus health checks (namespace `monitoring`); egress restricted to CoreDNS, the Prometheus/Loki/Alertmanager datasource ports, Envoy (namespace `network`) on **both** 443 and 10443 for OIDC calls to Authentik, and `world:443` for pulling dashboard JSON from grafana.com. The dual-port (443+10443) egress rule exists because Cilium evaluates egress policy *before* DNAT for Service traffic, so the Service port (443) has to be allowed even though the traffic actually lands on the container port (10443) after DNAT — documented inline in the policy file.

## Storage
5Gi PVC on the `zfs-nfs` StorageClass (`persistence.enabled`/`storageClassName`/`size` in `helmrelease.yaml`); `initChownData` init container fixes NFS ownership before Grafana starts.

**Not covered by Velero:** `monitoring` is absent from `includedNamespaces` in all three backup schedules. Dashboards themselves are reproducible from `helmrelease.yaml` (Grafana.com `gnetId`s + inlined custom JSON), but any PVC-resident state — alerting silences/history, per-user preferences, org/team settings created outside Git — is not backed up.

## Known quirks
- **Datasource provisioner is add/update-only.** When the `TradingDB` datasource was retired (hermes-agent trading-bot deprovisioning), simply removing it from the `datasources:` list in `helmrelease.yaml` did not remove it from the live instance — Grafana's file provisioner never deletes. An explicit `deleteDatasources:` block was added for one reconcile (commit `9f7d5c4`) then removed once confirmed gone (commit `9791862`). Worth remembering if any datasource is ever retired again.
- **kube-prometheus-stack's bundled Grafana subchart is intentionally disabled** (`grafana.enabled: false`, `kubernetes/apps/monitoring/kube-prometheus-stack/app/helmrelease.yaml`) in favor of this standalone app — don't look for Grafana config in the stack's HelmRelease.
- **Egress dual-port DNAT quirk** (443+10443, see Routing above) — the same Cilium egress-before-DNAT gotcha recurs for other apps that reach Authentik via `envoy-internal`.

## Common operations
- Upgrade chart version: edit `helmrelease.yaml`, commit, push, Flux reconciles within the 1h `interval` (or force with `flux reconcile helmrelease grafana -n monitoring`).
- Rotate a secret: update the `grafana` 1Password item, then `kubectl annotate externalsecret grafana-secret -n monitoring force-sync=$(date +%s)` — the `stakater.com/reload` annotation restarts the pod automatically once the Secret content changes. Remember to also update Authentik's `authentik-grafana-oidc` ExternalSecret consumer if rotating the OIDC client secret specifically, since both sides read the same 1Password item independently.
- Pause reconciliation: `flux suspend kustomization grafana -n monitoring` / `flux suspend helmrelease grafana -n monitoring`.

## TODOs / unknowns
- Whether excluding `monitoring` (and therefore Grafana's PVC) from the Velero schedules was a deliberate decision or an oversight is not documented anywhere in the repo — worth confirming with the operator.
- The full field list of the `grafana` 1Password item is not verified beyond the four keys the two ExternalSecrets actually read (`GRAFANA_ADMIN_USER`, `GRAFANA_ADMIN_PASSWORD`, `GRAFANA_OPENID_CLIENT_ID`, `GRAFANA_OPENID_CLIENT_SECRET`).

---
_Cite every non-obvious claim with a repo-root-relative file path (e.g. `kubernetes/apps/security/authentik/app/helmrelease.yaml`), not a bare filename — this doc lives under `docs/apps/`, so relative paths must resolve from there._
