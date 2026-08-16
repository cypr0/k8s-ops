# Falco & Falcosidekick

> **Namespace**  `falco` (the Flux Kustomization lives under `kubernetes/apps/security/falco/` for repo organization, but `targetNamespace: falco` — `kubernetes/apps/security/falco/ks.yaml:6-27`)
> **Source**     `falcosecurity` HelmRepository (`kubernetes/apps/security/falco/app/helmrepository.yaml`) — two charts from one Kustomization: `falco` v9.0.0 and `falcosidekick` v0.13.1
> **Hostname**   none — no HTTPRoute; both components are internal-only, reached via ClusterIP/eBPF, not the ingress path

## What it does here
Falco is the cluster's runtime intrusion-detection layer: a DaemonSet reading kernel syscalls via eBPF on every worker node, matched against a mix of upstream and homelab-specific rules (`kubernetes/apps/security/falco/app/helmrelease-falco.yaml:88-181`). Falcosidekick, deployed as a fully separate HelmRelease rather than the chart's built-in subchart (`falcosidekick.enabled: false` in `helmrelease-falco.yaml:52-53`, with the comment "deployed as separate HelmRelease"), fans each Falco event out to Loki, OpenSearch, and Pushover. This pairing is documented as one app because they're one Flux Kustomization and one operational unit — Falco without Falcosidekick just logs to stdout with nowhere to go.

## Architecture at a glance
- **Depends on:** Flux Kustomization `kube-prometheus-stack` and `loki` (namespace `monitoring`) and `external-secrets-stores` (namespace `security`), all via `dependsOn` (`kubernetes/apps/security/falco/ks.yaml:12-18`) — `loki` specifically because Falcosidekick needs it up before it can ship logs (also noted from the other side in `docs/apps/loki.md:14`); `external-secrets-stores` because the `onepassword` ClusterSecretStore must exist before the two ExternalSecrets below can sync.
- **Depended on by:** `loki` (log destination for warning+ events), the `opensearch-cluster` (SIEM index for warning+ events — `docs/apps/opensearch-cluster.md:12` lists Falco's falcosidekick as a consumer), Pushover (critical+ alerting), and `kube-prometheus-stack`'s Prometheus (scrapes both ServiceMonitors). At the Flux level, this Kustomization's own health gates nothing else, but `docs/apps/kube-prometheus-stack.md:12` and `:43` note that `falco` itself is one of six Kustomizations that stall if `kube-prometheus-stack` fails to converge. `docs/apps/metrics-server.md:12` also notes Falcosidekick's HPA (`helmrelease-sidekick.yaml:94-98`) depends on `metrics-server` for scaling decisions.

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/security/falco/ks.yaml` | Flux Kustomization — `dependsOn`, `targetNamespace: falco` |
| `kubernetes/apps/security/falco/app/helmrepository.yaml` | `falcosecurity` chart repo |
| `kubernetes/apps/security/falco/app/helmrelease-falco.yaml` | Falco DaemonSet: driver, resources, custom rules |
| `kubernetes/apps/security/falco/app/helmrelease-sidekick.yaml` | Falcosidekick Deployment: output routing, HPA |
| `kubernetes/apps/security/falco/app/externalsecret.yaml` | Pushover credentials for Falcosidekick |
| `kubernetes/apps/security/falco/app/externalsecret-opensearch.yaml` | OpenSearch admin password for Falcosidekick |
| `kubernetes/apps/security/falco/app/ciliumnetworkpolicy.yaml` | Two CiliumNetworkPolicies — one per component |
| `kubernetes/apps/security/falco/app/namespace.yaml` | `falco` namespace (prune disabled) |

## Secrets
| ExternalSecret | 1Password item / field | Consumed by |
| --- | --- | --- |
| `falcosidekick-pushover` (`kubernetes/apps/security/falco/app/externalsecret.yaml`) | item `pushover`, fields `ALERTMANAGER_PUSHOVER_API_TOKEN` → `PUSHOVER_APITOKEN`, `PUSHOVER_USER_KEY` → `PUSHOVER_USERKEY` | Falcosidekick container env, via `extraEnv` (`helmrelease-sidekick.yaml:57-67`) |
| `falcosidekick-opensearch` (`kubernetes/apps/security/falco/app/externalsecret-opensearch.yaml`) | item `opensearch`, field `OPENSEARCH_ADMIN_PASSWORD` → `ELASTICSEARCH_PASSWORD` | Falcosidekick container env, via `extraEnv` (`helmrelease-sidekick.yaml:68-72`), used to auth against the `admin` user for the `elasticsearch` output (`helmrelease-sidekick.yaml:44-52`) |

Both ExternalSecrets pull from the `onepassword` ClusterSecretStore (`externalsecret.yaml:8-9`, `externalsecret-opensearch.yaml:7-8`).

## Routing & access
No HTTPRoute — nothing here is meant to be reached from outside the cluster. Two CiliumNetworkPolicies in `kubernetes/apps/security/falco/app/ciliumnetworkpolicy.yaml`:
- **`falco`** (lines 2-37): egress-only — DNS to `kube-dns`, port 2801/TCP to `falcosidekick` (event forwarding), and port 443/TCP to `world` for the `falcoctl-artifact-install` init container, which fetches rule updates from `falcosecurity.github.io`.
- **`falcosidekick`** (lines 39-108): ingress from `falco` (2801, event intake), from `prometheus` in `monitoring` (2802, metrics scrape), and from the `host` entity (2801, kubelet probes); egress to DNS, `loki` (3100), the `opensearch-cluster` in `logging` (9200), and `world` (443, for the Pushover webhook).

No OIDC/SSO — neither component has a web UI exposed to end users in this deployment.

## Storage
No PVCs. Falco is a DaemonSet reading the host's eBPF/syscall stream directly; Falcosidekick is stateless (forward-only). Neither appears in `kubernetes/apps/velero/` schedules or restore-test config — nothing here needs backup coverage.

## Known quirks
- **Talos requires the modern eBPF driver plus `SYS_RESOURCE`.** `driver.kind: modern_ebpf` (`helmrelease-falco.yaml:28-29`) and the `SYS_RESOURCE` capability (`helmrelease-falco.yaml:193`) are both necessary on Talos — without the capability, Falco can't bump `RLIMIT_MEMLOCK` for the eBPF driver and crashes with "Operation not permitted" (commit `9dc0a0b`, corroborated by the auto-memory note on Talos compatibility).
- **Both components are pinned to worker nodes only.** `tolerations: []` plus a `requiredDuringSchedulingIgnoredDuringExecution` nodeAffinity excluding `node-role.kubernetes.io/control-plane` (`helmrelease-falco.yaml:31-39`) — control-plane nodes need their RAM for `kube-apiserver`/etcd (commit `095196d`).
- **`falco.grpc`/`falco.grpc_output` were removed as part of a Falco chart 9.0.0 breaking change** — the gRPC config moved out of `falco.falco` upstream; leaving the old keys in would have applied stale config (commit `87cfecc`).
- **The `falcoctl-artifact-install` egress rule was missing for a while and nobody noticed** — existing nodes had cached rule artifacts, so the gap in the `falco` CiliumNetworkPolicy only surfaced once a new node joined and its init container couldn't reach `falcosecurity.github.io` (commit `ba272af`). Worth re-checking this rule after any future node addition if Falco pods on the new node stay stuck initializing.
- **Two layered false-positive exceptions exist for CNPG's WAL archiving, added in separate incidents:**
  1. `cnpg_wal_archive` exception on the **"Drop and execute new binary in container"** rule (`helmrelease-falco.yaml:102-109`) — CNPG's barman-cloud WAL-archive `python3` process was firing CRITICAL roughly 1300x/week on one Falco pod alone (commit `613acfd`, added alongside a similar Dragonfly healthcheck exemption on the same rule pair).
  2. A `not proc.cmdline contains "wal-archive"` clause added directly into the **"Shell in Database Container"** rule's condition (`helmrelease-falco.yaml:131`), since that rule has no `exceptions:` block to extend. CNPG's operator invokes `sh -c /controller/manager wal-archive ...` on the Postgres pod roughly every 5 minutes as routine WAL archiving to barman-cloud; confirmed live on 2026-08-16 with 30 CRITICAL hits in 6 hours, all this exact command (commit `3214b3e`). Also cross-referenced from the backup side in `docs/apps/plugin-barman-cloud.md:52`.

  If either exclusion is ever reverted, expect an alert-fatigue storm rather than an actual intrusion — both are confirmed-benign, routine CNPG behavior, not open questions.
- **`Shell in Security Namespace` explicitly carves out `authentik-worker` pods** (`helmrelease-falco.yaml:147`) — Authentik's worker legitimately spawns shells for blueprint/task execution in the `security` namespace; without the exclusion this rule would fire on normal Authentik operation.

## Common operations
- Upgrade either chart: edit the relevant `version:` in `helmrelease-falco.yaml` or `helmrelease-sidekick.yaml`, commit, push, Flux reconciles within `interval: 1h` (or force with `flux reconcile helmrelease falco -n falco` / `flux reconcile helmrelease falcosidekick -n falco`).
- Add/adjust a custom rule or exception: edit the `customRules.homelab-rules.yaml` block in `helmrelease-falco.yaml`, commit, push — Flux applies the ConfigMap and the chart restarts the DaemonSet to pick it up.
- Rotate a secret: update the relevant 1Password item (`pushover` or `opensearch`), then `kubectl annotate externalsecret falcosidekick-pushover -n falco force-sync=$(date +%s)` (or the `-opensearch` one), or wait for the refresh interval.
- Pause reconciliation: `flux suspend kustomization falco -n security` / `flux suspend helmrelease falco -n falco` / `flux suspend helmrelease falcosidekick -n falco`.

## TODOs / unknowns
- The worker node count Falco's DaemonSet actually schedules onto isn't cited here — it's whatever `talos/talconfig.yaml` currently defines as worker nodes, and that count has changed at least once recently (`0872c96`, adding a 5th worker); no need to hardcode a number that will drift.
- No incident postmortem exists yet for the 2026-08-16 "Shell in Database Container" false positive — it was fixed directly (commit `3214b3e`) without a dedicated `docs/incidents/` entry. Worth a short postmortem if this pattern (new rule fires on a CNPG-internal command) recurs for a third rule.

---
_Cite every non-obvious claim with a repo-root-relative file path (e.g. `kubernetes/apps/security/authentik/app/helmrelease.yaml`), not a bare filename — this doc lives under `docs/apps/`, so relative paths must resolve from there._
