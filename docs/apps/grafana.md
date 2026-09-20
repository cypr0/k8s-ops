# Grafana

> **Namespace**  monitoring
> **Source**     Helm chart `grafana` from the `grafana` HelmRepository (`https://grafana.github.io/helm-charts`), version `10.5.15` (`kubernetes/apps/monitoring/grafana/app/helmrelease.yaml`, `helmrepository.yaml`); Grafana image `grafana/grafana:13.2.0`, pinned by digest
> **Hostname**   `grafana.${SECRET_DOMAIN}` — internal-only (VPN/split-DNS), not exposed via the Cloudflare tunnel

## What it does here
Standalone dashboard/visualization frontend for the cluster's metrics and logs. This is **not** the `kube-prometheus-stack` chart's bundled Grafana — that subchart is explicitly disabled (`grafana.enabled: false` in `kubernetes/apps/monitoring/kube-prometheus-stack/app/helmrelease.yaml`) in favor of this dedicated deployment, which is a sibling app in the same namespace. It queries Prometheus, Loki and Alertmanager as datasources. Login is SSO-only via Authentik OIDC, with role (Admin/Editor/Viewer) derived from Authentik group membership.

The dashboards were rebuilt from scratch on 2026-09-20: **29 dashboards across 7 folders** (Cluster, Netzwerk, Storage & Datenbanken, Plattform, Sicherheit, Observability, Anwendungen). Eighteen are community dashboards imported by pinned `gnetId`/`revision`; eleven live in `kubernetes/apps/monitoring/grafana/dashboards/`, of which four are upstream dashboards adapted to this cluster's metrics and five exist because no community equivalent does (Flux, Falco, Gatus, Paperless, Open WebUI) — plus Alertmanager and the Talos log dashboard.

See **Dashboards** below for how they are wired and, more importantly, how to check that one actually works before adding it.

## Architecture at a glance
- **Depends on:** Prometheus and Alertmanager Services from `kube-prometheus-stack` (`kube-prometheus-stack-prometheus.monitoring.svc.cluster.local:9090`, `kube-prometheus-stack-alertmanager.monitoring.svc.cluster.local:9093`) and `loki.monitoring.svc.cluster.local:3100`, all wired as datasources in `helmrelease.yaml`; ExternalSecret → 1Password item `grafana` for admin credentials and OIDC client id/secret; Authentik for OIDC login. The Flux Kustomization also hard-`dependsOn` `kube-prometheus-stack`, `loki`, and `external-secrets-stores` (namespace `security`) — `kubernetes/apps/monitoring/grafana/ks.yaml`.
- **Depended on by:** Gatus, which polls `http://grafana.monitoring.svc.cluster.local/api/health` every minute and alerts via Pushover on failure (`kubernetes/apps/monitoring/gatus/app/configmap.yaml`). No other app's Service depends on Grafana — it's an operator/user-facing dashboard, not infrastructure other apps call into.

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/monitoring/grafana/app/helmrelease.yaml` | Chart version, image, datasources, dashboard providers + `gnetId` imports, OIDC config, persistence |
| `kubernetes/apps/monitoring/grafana/dashboards/` | The dashboards kept in this repo, one directory per folder, assembled into ConfigMaps by `configMapGenerator` |
| `kubernetes/apps/monitoring/grafana/app/helmrepository.yaml` | Upstream chart source (`grafana.github.io/helm-charts`) |
| `kubernetes/apps/monitoring/grafana/app/externalsecret.yaml` | Admin credentials + OIDC client id/secret, from 1Password |
| `kubernetes/apps/monitoring/grafana/app/httproute.yaml` | Internal-only Gateway routing |
| `kubernetes/apps/monitoring/grafana/app/ciliumnetworkpolicy.yaml` | Ingress (Envoy, kubelet, Gatus) and egress (datasources, DNS, OIDC, dashboard downloads) |
| `kubernetes/apps/monitoring/grafana/ks.yaml` | **Two** Flux Kustomizations: `grafana` (the app, with `postBuild.substituteFrom`) and `grafana-dashboards` (the ConfigMaps, deliberately without it — see Known quirks) |

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

## Dashboards

### How they get in

Two mechanisms, deliberately kept apart:

| | Mechanism | Where |
| --- | --- | --- |
| Community, unmodified | `dashboards.<provider>` with a pinned `gnetId` + `revision`; the chart's init container downloads them at pod start | `app/helmrelease.yaml` |
| Adapted or hand-written | `dashboardsConfigMaps`, fed by `configMapGenerator` | `dashboards/<folder>/*.json` |

Each folder therefore has **two providers** (`<folder>` and `<folder>-repo`) pointing at the same Grafana folder from different paths. The chart cannot mix its own downloads and a user ConfigMap in one directory — they would collide on the mount point.

### Verifying a dashboard before adding it

This matters more than it sounds. The set replaced in 2026-09 contained two dashboards querying `pod_name` and `container_name`, cAdvisor labels **removed in Kubernetes 1.16**, and two Cilium dashboards written for **1.12** against a cluster running 1.20. None of that is visible in the UI: a panel with no matching metric renders as an empty graph, indistinguishable from a quiet system.

The check used was mechanical, and is worth repeating for anything new:

1. Fetch the dashboard JSON: `https://grafana.com/api/dashboards/<id>/revisions/<rev>/download`
2. Extract every `expr`, strip label selectors, `by(...)`/`without(...)` clauses and `$vars` — what remains is metric names
3. Diff those against `curl -s localhost:9090/api/v1/label/__name__/values`
4. Execute every query; a query that parses but returns nothing is as useless as one that errors

Coverage figures measured this way are recorded inline next to each `gnetId` in `helmrelease.yaml`, with the reason whenever they are below 100%.

### When a metric genuinely cannot exist

Three outcomes, in order of preference — the adapted dashboards in `dashboards/` each carry a `__patched` key recording which was applied:

- **Rename.** The metric exists under a different name. `kube_hpa_labels` → `kube_horizontalpodautoscaler_labels`, `kube_endpoint_info` → `kube_endpointslice_info`, `coredns_forward_*` → `coredns_proxy_*`.
- **Rewrite.** Different metric, same question. This kubelet exposes no `container_cpu_cfs_throttled_seconds_total`, so throttling is expressed as a ratio of `_periods_total` — arguably the better figure anyway.
- **Remove.** Nothing here can produce it: `kube_ingress_info` (this cluster uses Gateway API), `kube_networkpolicy_labels` (CiliumNetworkPolicy), `alertmanager_cluster_*` (single replica), `node_cpu_core_throttles_total` (no thermal data in a VM).

Removal is not just a delete. Where one query among several is dead, only that query goes and the panel keeps the rest. Where a whole panel goes, the rows beneath are pulled up so no gap is left behind.

### Dashboards without a community equivalent

`Flux`, `Falco`, `Gatus`, `Paperless-ngx`, `Open WebUI` and a replacement `Alertmanager`. The Flux one is worth calling out: the dashboard everyone links to (`gnetId` 16714) queries `gotk_reconcile_condition`, which current Flux no longer emits. `flux_resource_info` from flux-operator does exist and carries `ready`, `reason`, `suspended` and `revision` per object — which is what makes "has anything stopped following Git" answerable at a glance.

## Known quirks
- **Datasource provisioner is add/update-only.** When the `TradingDB` datasource was retired (hermes-agent trading-bot deprovisioning), simply removing it from the `datasources:` list in `helmrelease.yaml` did not remove it from the live instance — Grafana's file provisioner never deletes. An explicit `deleteDatasources:` block was added for one reconcile (commit `9f7d5c4`) then removed once confirmed gone (commit `9791862`). Worth remembering if any datasource is ever retired again.
- **kube-prometheus-stack's bundled Grafana subchart is intentionally disabled** (`grafana.enabled: false`, `kubernetes/apps/monitoring/kube-prometheus-stack/app/helmrelease.yaml`) in favor of this standalone app — don't look for Grafana config in the stack's HelmRelease.
- **Dashboard ConfigMaps must be built without `postBuild`.** Flux runs envsubst over every manifest it builds, and dashboard JSON is full of `${...}` — `${datasource}`, `${__field.labels.pod}`. The first attempt failed outright with `envsubst error: variable substitution failed: missing closing brace`, which was the *lucky* outcome: with slightly different syntax envsubst succeeds and silently replaces Grafana's own variables with empty strings, leaving dashboards that load and render but show nothing. Hence the separate `grafana-dashboards` Kustomization with no `postBuild` at all (`ks.yaml`).
- **The repo ConfigMaps must not be named `grafana-dashboards-<provider>`.** That is exactly the name the chart generates for every entry under `dashboards:`. Using it left six objects carrying both `app.kubernetes.io/managed-by: Helm` and `kustomize.toolkit.fluxcd.io/name`, with Helm and Flux overwriting each other on every reconcile — and looking correct in between, depending on who applied last. They are `grafana-repo-dashboards-*` for that reason.
- **`disableNameSuffixHash: true` is required** on the generator (`dashboards/kustomization.yaml`): a content hash in the name would rename the ConfigMap on every dashboard edit and leave `dashboardsConfigMaps` pointing at nothing. The cost is that Grafana does not restart on a dashboard change — its provisioning loop re-reads each directory every 60s instead.
- **Egress dual-port DNAT quirk** (443+10443, see Routing above) — the same Cilium egress-before-DNAT gotcha recurs for other apps that reach Authentik via `envoy-internal`.

## Common operations
- Upgrade chart version: edit `helmrelease.yaml`, commit, push, Flux reconciles within the 1h `interval` (or force with `flux reconcile helmrelease grafana -n monitoring`).
- Rotate a secret: update the `grafana` 1Password item, then `kubectl annotate externalsecret grafana-secret -n monitoring force-sync=$(date +%s)` — the `stakater.com/reload` annotation restarts the pod automatically once the Secret content changes. Remember to also update Authentik's `authentik-grafana-oidc` ExternalSecret consumer if rotating the OIDC client secret specifically, since both sides read the same 1Password item independently.
- Pause reconciliation: `flux suspend kustomization grafana -n monitoring` / `flux suspend helmrelease grafana -n monitoring`. Note there is a second Kustomization, `grafana-dashboards`, which must be suspended separately.
- Add or edit a dashboard kept in this repo: drop the JSON into `dashboards/<folder>/`, add it to that folder's `files:` list in `dashboards/kustomization.yaml`, commit. It appears within ~60s of the ConfigMap updating; no pod restart.
- Add a community dashboard: verify coverage first (see Dashboards above), then add a pinned `gnetId` + `revision` under the right provider in `helmrelease.yaml` with the measured figure in a comment.
- List what is actually loaded, by folder:
  ```sh
  kubectl port-forward -n monitoring svc/grafana 3000:80 &
  curl -s -u "$(kubectl get secret -n monitoring grafana-secret -o jsonpath='{.data.GF_SECURITY_ADMIN_USER}' | base64 -d)":"$(kubectl get secret -n monitoring grafana-secret -o jsonpath='{.data.GF_SECURITY_ADMIN_PASSWORD}' | base64 -d)" \
    'http://127.0.0.1:3000/api/search?type=dash-db&limit=200' | jq -r '.[] | "\(.folderTitle // "General")\t\(.title)"' | sort
  ```

## TODOs / unknowns
- Whether excluding `monitoring` (and therefore Grafana's PVC) from the Velero schedules was a deliberate decision or an oversight is not documented anywhere in the repo — worth confirming with the operator.
- The full field list of the `grafana` 1Password item is not verified beyond the four keys the two ExternalSecrets actually read (`GRAFANA_ADMIN_USER`, `GRAFANA_ADMIN_PASSWORD`, `GRAFANA_OPENID_CLIENT_ID`, `GRAFANA_OPENID_CLIENT_SECRET`).

---
_Cite every non-obvious claim with a repo-root-relative file path (e.g. `kubernetes/apps/security/authentik/app/helmrelease.yaml`), not a bare filename — this doc lives under `docs/apps/`, so relative paths must resolve from there._
