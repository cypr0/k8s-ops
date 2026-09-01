# Dragonfly

> **Namespace**  database
> **Source**     dragonfly-operator Helm chart v1.6.1, pulled via `OCIRepository` `ghcr.io/dragonflydb/dragonfly-operator/helm/dragonfly-operator` (`kubernetes/apps/database/dragonfly/app/ocirepository.yaml`, `helmrelease.yaml`) — installs the CRD controller only; the actual cache is a `Dragonfly` custom resource with its own independently-pinned image `ghcr.io/dragonflydb/dragonfly:v1.40.1` (`kubernetes/apps/database/dragonfly/cluster/cluster.yaml`)
> **Hostname**   none — internal only, `dragonfly.database.svc.cluster.local:6379`

## What it does here
Shared Redis-compatible cache/session store for the whole cluster, not a per-app instance — one 3-replica Dragonfly instance, partitioned by Redis DB index per consumer. Backs Authentik's session store, Nextcloud's core cache and the Whiteboard collab sub-app, Open WebUI's websocket/session manager, Paperless-ngx's task queue, and hermes-agent's Firecrawl job queue/rate-limiter (see Architecture below for the full consumer/index table).

## Architecture at a glance
- **Depends on:** dragonfly-operator (manages the `Dragonfly` CRD lifecycle), 1Password (`ClusterSecretStore/onepassword`) for the single shared `dragonfly` item (`DRAGONFLY_PASSWORD`). No CNPG/S3 dependency of its own.
- **Depended on by** (all via `dragonfly.database.svc.cluster.local:6379`, cross-referenced by grepping the repo for that hostname):

  | Consumer | Redis DB index | Source |
  | --- | --- | --- |
  | Authentik (`security` ns) | 1 | `kubernetes/apps/security/authentik/app/helmrelease.yaml:90`, `externalsecret.yaml:23-25` |
  | Nextcloud core (`nextcloud` ns) | 1 | `kubernetes/apps/nextcloud/nextcloud/app/helmrelease.yaml:97-99` |
  | Nextcloud Whiteboard (`nextcloud` ns) | 3 | `kubernetes/apps/nextcloud/whiteboard/app/externalsecret.yaml:19` |
  | Open WebUI (`open-webui` ns) | 0 | `kubernetes/apps/open-webui/open-webui/app/externalsecret.yaml:23-24` |
  | Paperless-ngx (`paperless` ns) | 0 (no index in URL → client default) | `kubernetes/apps/paperless/paperless-ngx/app/externalsecret.yaml:29` |
  | hermes-agent/Firecrawl (`hermes-agent` ns) | 4 | `kubernetes/apps/hermes-agent/firecrawl/app/externalsecret.yaml:26-27` |
  | Gatus (`monitoring` ns) | n/a — plain TCP connect check, no AUTH, monitoring only | `kubernetes/apps/monitoring/gatus/app/configmap.yaml:98-101` |

  See Known quirks below for the index-collision observation this table surfaces.

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/database/dragonfly/app/ocirepository.yaml` | dragonfly-operator chart source + version pin |
| `kubernetes/apps/database/dragonfly/app/helmrelease.yaml` | Operator install — minimal resources (`requests.cpu: 10m`, `limits.memory: 128Mi`), no chart values beyond that |
| `kubernetes/apps/database/dragonfly/cluster/cluster.yaml` | The actual `Dragonfly` CR — image tag, replica count, launch args, resources, topology spread |
| `kubernetes/apps/database/dragonfly/cluster/externalsecret.yaml` | Shared `dragonfly-secret` (`DRAGONFLY_PASSWORD`) from 1Password |
| `kubernetes/apps/database/dragonfly/cluster/ciliumnetworkpolicy.yaml` | Ingress from any cluster pod on 6379, egress to cluster |
| `kubernetes/apps/database/dragonfly/ks.yaml` | Two Kustomizations — `dragonfly` (operator, `app/`) and `dragonfly-cluster` (`cluster/`), the latter `dependsOn` the former |

## Secrets
| ExternalSecret | 1Password source | Consumed by |
| --- | --- | --- |
| `dragonfly` (`kubernetes/apps/database/dragonfly/cluster/externalsecret.yaml`) | `dragonfly` item, field `DRAGONFLY_PASSWORD` → target secret `dragonfly-secret` | The `Dragonfly` CR itself, via `env.DRAGONFLY_PASSWORD` → `--requirepass=$(DRAGONFLY_PASSWORD)` arg (`cluster/cluster.yaml`) |

There is no fan-out of a single K8s Secret to consumers. Every consuming app instead independently extracts the **same** 1Password item (`dataFrom: - extract: key: dragonfly`) into its own ExternalSecret to build its own connection string — confirmed in `kubernetes/apps/security/authentik/app/externalsecret.yaml:36`, `kubernetes/apps/nextcloud/nextcloud/app/externalsecret.yaml:30`, `kubernetes/apps/nextcloud/whiteboard/app/externalsecret.yaml`, `kubernetes/apps/open-webui/open-webui/app/externalsecret.yaml`, `kubernetes/apps/paperless/paperless-ngx/app/externalsecret.yaml:61`, and `kubernetes/apps/hermes-agent/firecrawl/app/externalsecret.yaml`. Rotating the password means updating the one 1Password item, then force-syncing (or waiting out the refresh interval on) every one of those ExternalSecrets separately — there's no cascade.

## Routing & access
- **ClusterIP only, no HTTPRoute.** No `service.yaml` exists in this app's repo layout — the Service is created by the dragonfly-operator from the `Dragonfly` CR name, not hand-defined here.
- **CiliumNetworkPolicy** (`kubernetes/apps/database/dragonfly/cluster/ciliumnetworkpolicy.yaml`) allows ingress on port 6379 from `fromEntities: cluster` — i.e. any pod anywhere in the cluster, not scoped to a namespace or label — with egress restricted to `toEntities: cluster`. Access control here is by password (`requirepass`), not network segmentation; firecrawl's own CiliumNetworkPolicy comment makes this explicit (`kubernetes/apps/hermes-agent/firecrawl/app/ciliumnetworkpolicy.yaml:61-65`).
- No SSO — this is a backend cache, no user-facing surface.

## Storage
No PersistentVolumeClaim — `cluster/cluster.yaml` sets no storage/volume spec on the `Dragonfly` CR, so it's in-memory only across all 3 replicas. `MAX_MEMORY` (and the `--maxmemory` arg) is derived at runtime from the container's own `resources.limits.memory` via a `resourceFieldRef` (divisor `1Mi`), not a separately-maintained literal — bumping the memory limit is the only step needed to raise the cache ceiling.

Not included in any Velero backup schedule: `kubernetes/apps/velero/schedules/schedule-daily.yaml` only lists `nextcloud`, `paperless`, `open-webui`, `hermes-agent` under `includedNamespaces` — the `database` namespace isn't in it. Consistent with this being a pure cache (session/queue data, not source of truth) rather than an oversight, but it does mean a full loss of all 3 replicas loses everything cached, by design.

## Known quirks
- **Two independent version knobs.** The operator chart version (`app/ocirepository.yaml` tag, currently `v1.6.1`) and the actual Dragonfly workload image (`cluster/cluster.yaml` `spec.image`, currently `v1.40.1`) are pinned separately, each with its own `renovate:` comment — bumping one does not bump the other, and Renovate will open separate PRs for each.
- **Redis DB index has no enforced registry, and two collisions currently exist.** The only place indices are tracked at all is an inline comment in `kubernetes/apps/hermes-agent/firecrawl/app/externalsecret.yaml` ("key db index /4 is currently unused, see whiteboard's /3 for the established pattern"). Cross-referencing every consumer (table above) shows **index 1 used by both Authentik and Nextcloud core**, and **index 0 used by both Open WebUI and Paperless-ngx** (the latter via an omitted index in its connection string, which defaults to 0). Not confirmed to have caused a live incident — each app presumably key-prefixes its own cache/session data — but there's nothing in the repo preventing a future accidental key collision when a new consumer is added without checking this doc's table first.
- **The password is passed as a literal CLI arg** (`--requirepass=$(DRAGONFLY_PASSWORD)`, `cluster/cluster.yaml`), which the Dragonfly operator interpolates from the pod's env at container start. This means the resolved value is visible in the pod spec's `args` to anyone with `describe`/`get pod -o yaml` access in the `database` namespace — an inherent property of how this operator consumes the password, not a leak in this repo, but worth knowing before assuming `kubectl describe` output is safe to share.
- **`cluster_mode=emulated` + `--lock_on_hashtags`, 3 replicas, one-per-node `topologySpreadConstraints` (`whenUnsatisfiable: DoNotSchedule`)** (`cluster/cluster.yaml`) — this is a single logical instance with HA replicas for failover, not a real sharded Redis Cluster from the client's point of view.

## Common operations
- **Upgrade the operator chart:** edit the `tag` in `kubernetes/apps/database/dragonfly/app/ocirepository.yaml` (not `helmrelease.yaml` — this HelmRelease uses `chartRef` to the `OCIRepository`, there's no `spec.chart.spec.version` field to edit here), commit, push.
- **Upgrade the Dragonfly image itself:** edit `spec.image` in `kubernetes/apps/database/dragonfly/cluster/cluster.yaml` — independent of the operator chart version above.
- **Rotate the password:** update the `dragonfly` 1Password item's `DRAGONFLY_PASSWORD` field, then force-sync (or wait out `refreshInterval`) on **every** consumer's ExternalSecret individually (see Secrets above) — not just this app's own.
- **Pause reconciliation:** `flux suspend kustomization dragonfly -n database` (operator) and/or `flux suspend kustomization dragonfly-cluster -n database` (the CR) — the latter `dependsOn` the former, so suspend both if pausing the whole app.

## TODOs / unknowns
- Whether the DB-index overlaps noted above (index 0: Open WebUI + Paperless-ngx; index 1: Authentik + Nextcloud) have ever caused an observed key collision — not verified from the repo, flagged here so a future consumer picks an unused index deliberately rather than relying on the informal comment-only registry.
- No ServiceMonitor/PodMonitor for Dragonfly found anywhere in the repo — whether metrics are exposed/scraped at all (dashboards, alerting) is unconfirmed.
- Whether the operator-created Service routes only to the primary replica or load-balances across all 3 — not stated anywhere in this repo (no `service.yaml` exists to inspect), and not verified against the chart/operator source for this doc.
