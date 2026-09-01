# Whiteboard

> **Namespace**  nextcloud
> **Source**     `app-template` chart v5.1.0 via OCIRepository `oci://ghcr.io/bjw-s-labs/helm/app-template` (`kubernetes/apps/nextcloud/whiteboard/app/ocirepository.yaml`, `helmrelease.yaml`) — wraps the `ghcr.io/nextcloud-releases/whiteboard:v1.5.9` image, not a dedicated Whiteboard Helm chart
> **Hostname**   `cloud.${SECRET_DOMAIN}/whiteboard` (path-based, on the same hostname/gateway as Nextcloud itself, not its own subdomain — see Routing)

## What it does here
Real-time collaborative-drawing backend for Nextcloud's `whiteboard` app — a standalone Node.js service, not a PHP app, enabled by Nextcloud's post-install job (`kubernetes/apps/nextcloud/nextcloud/app/post-install-job.yaml:50,92-93`). The Nextcloud PHP app points its `collabBackendUrl` config at this service and the two sides authenticate each other with a shared JWT secret rather than session cookies. `STORAGE_STRATEGY: redis` (`kubernetes/apps/nextcloud/whiteboard/app/helmrelease.yaml:39`) means live session/document state lives in Dragonfly, not in-process — unlike Collabora's single-replica, in-memory sessions (`docs/apps/collabora.md`), a restart here doesn't drop active drawing sessions.

## Architecture at a glance
- **Depends on:** Dragonfly (`dragonfly.database.svc.cluster.local:6379/3`, `kubernetes/apps/nextcloud/whiteboard/app/externalsecret.yaml:19` — DB index 3, per `docs/apps/dragonfly.md`'s consumer table) as its only storage backend; the parent Nextcloud instance (`NEXTCLOUD_URL`, `helmrelease.yaml:32-33`) for its own callback validation; ExternalSecret → 1Password items `nextcloud` (JWT secret) and `dragonfly` (session-store password).
- **Depended on by:** `nextcloud` itself — and in an inverted direction from every other Dragonfly consumer. Nextcloud's own container env and post-install job read the Secret *this app's* ExternalSecret creates (`nextcloud-whiteboard-credentials`) directly via `secretKeyRef` (`kubernetes/apps/nextcloud/nextcloud/app/helmrelease.yaml:117-121`, `post-install-job.yaml:113-117`), rather than independently pulling its own copy of `WHITEBOARD_JWT_SECRET_KEY` from 1Password the way `dragonfly.md` describes every other consumer doing. See Known quirks for why this matters operationally.

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/nextcloud/whiteboard/app/ocirepository.yaml` | Chart source: `app-template` v5.1.0 |
| `kubernetes/apps/nextcloud/whiteboard/app/helmrelease.yaml` | Single `whiteboard` controller: image tag, env, resources, Service |
| `kubernetes/apps/nextcloud/whiteboard/app/externalsecret.yaml` | JWT secret + Redis URL, sourced from two 1Password items |
| `kubernetes/apps/nextcloud/whiteboard/app/httproute.yaml` | Path-based route on Nextcloud's own hostname (see Routing) |
| `kubernetes/apps/nextcloud/whiteboard/app/ciliumnetworkpolicy.yaml` | Ingress (envoy, same-namespace) and egress (DNS, Dragonfly, world:443) |
| `kubernetes/apps/nextcloud/whiteboard/ks.yaml` | Flux Kustomization — `dependsOn: external-secrets-stores` (security ns) only |

## Secrets
| ExternalSecret | 1Password source | Consumed by |
| --- | --- | --- |
| `nextcloud-whiteboard-env` → `nextcloud-whiteboard-credentials` | Item `nextcloud`, field `WHITEBOARD_JWT_SECRET_KEY` (moved here from the `collabora` item by fix commit `b236490`, per the inline comment at `externalsecret.yaml:21`); item `dragonfly`, field `DRAGONFLY_PASSWORD` templated into `REDIS_URL` (`externalsecret.yaml:19`) | This app's own `app` container (`JWT_SECRET_KEY`/`REDIS_URL` env, `helmrelease.yaml:34-45`) **and** Nextcloud's own container + post-install job, which read the same target Secret's `WHITEBOARD_JWT_SECRET_KEY` key directly (see Architecture above) |

## Routing & access
- **Path-based, not a subdomain.** `httproute.yaml` attaches to the same `cloud.${SECRET_DOMAIN}` hostname Nextcloud itself uses: an `Exact /whiteboard` match 301-redirects to `/whiteboard/`, then a `PathPrefix /whiteboard` match strips the prefix (`URLRewrite`/`ReplacePrefixMatch`) before proxying to the `whiteboard` Service on `:3002`.
- **Parented to `envoy-external`** (public, via the Cloudflare tunnel) — this took two reverted commits to land on: `872a447` moved it to `envoy-internal` believing that gateway was needed for LAN reachability, then `92705b3` moved it back to `envoy-external` once the real cause (a Cilium port-80 policy bug) was found and fixed, restoring the pattern of matching Nextcloud's own public listener.
- No SSO of its own — the trust relationship with Nextcloud is the shared JWT secret above, not Authentik.
- **CiliumNetworkPolicy** (`ciliumnetworkpolicy.yaml`): ingress from `envoy` (network ns, `:3002`) and unrestricted from any pod in the `nextcloud` namespace (Nextcloud's own callbacks — mirrored by `docs/apps/nextcloud.md`'s note on "same-namespace siblings (Collabora/Whiteboard callbacks)"). Egress: DNS, Dragonfly (`database` ns, `:6379`), and `toEntities: world` on `:443` — see Known quirks for a likely gap in that last rule.

## Storage
No PVC — stateless container; all session/document state lives in Dragonfly DB index 3 (`docs/apps/dragonfly.md` notes Dragonfly itself is in-memory only, no PVC of its own). The `nextcloud` namespace is included in Velero's daily/weekly/monthly schedules (`kubernetes/apps/velero/schedules/schedule-daily.yaml`, `schedule-weekly.yaml`, `schedule-monthly.yaml`), but with no PVC here this only captures this app's Kubernetes-object state (Secret, Deployment) — actual whiteboard/session data lives solely in Dragonfly, which `docs/apps/dragonfly.md` confirms is **not** in that backup schedule's `includedNamespaces` (`database` isn't listed).

## Known quirks
- **Nextcloud's pod depends on this app's ExternalSecret output with no Flux ordering edge to guarantee it exists first.** `kubernetes/apps/nextcloud/nextcloud/ks.yaml`'s `dependsOn` lists `cloudnative-pg-databases`, `csi-driver-nfs`, `external-secrets-stores` — not `whiteboard`. `kubernetes/apps/nextcloud/kustomization.yaml` lists `nextcloud/ks.yaml` before `whiteboard/ks.yaml`, but that file's resource order has no bearing on Flux's independent per-Kustomization reconciliation. On a from-scratch bootstrap (or if this app's Kustomization is ever suspended/deleted), Nextcloud's pod could come up before `nextcloud-whiteboard-credentials` exists, failing to resolve the `WHITEBOARD_JWT_SECRET_KEY` `secretKeyRef` (`CreateContainerConfigError`) until whiteboard's own Kustomization also reconciles.
- **Rotating the JWT secret restarts this app automatically but not Nextcloud.** This controller carries `reloader.stakater.com/auto: "true"` (`helmrelease.yaml:25`), so Stakater Reloader restarts it when `nextcloud-whiteboard-credentials` changes. Nextcloud's own `helmrelease.yaml` has **no** `reloader.stakater.com` annotation at all (confirmed by grep — zero matches, unlike `docs/apps/reloader.md`'s list of annotated apps, which includes `nextcloud/whiteboard` and `nextcloud/collabora` but not bare `nextcloud`), even though Nextcloud's own container reads the same secret key directly. After a JWT rotation, Nextcloud keeps running with the stale `WHITEBOARD_JWT_SECRET_KEY` value until it's restarted by hand — the shared-secret handshake between the two sides will fail silently until that happens.
- **The `toEntities: world` egress rule on `:443` may not actually cover this app's own callback traffic.** The only `:443` destination in this app's env is `NEXTCLOUD_URL` (`https://cloud.${SECRET_DOMAIN}`). CoreDNS resolves that hostname cluster-wide to an internal Envoy Gateway ClusterIP via a `hosts` override (`kubernetes/apps/kube-system/coredns/app/helmrelease.yaml:56-67` — added originally for Collabora's WOPI callback, but the override is unconditional for the hostname, not scoped to one calling app), which after Cilium's Service DNAT resolves to an `envoy` pod identity in the `network` namespace, not the `world` entity. Collabora hit exactly this gap: its CiliumNetworkPolicy's `world:443` rule alone wasn't sufficient for its own `cloud.${SECRET_DOMAIN}` callback, and needed an explicit `toEndpoints` egress rule to `network`/`envoy` on port `10443` (`kubernetes/apps/nextcloud/collabora/app/ciliumnetworkpolicy.yaml`) before its callback worked — the fix surfaced as a silent 30s timeout, not an error. This app's own `ciliumnetworkpolicy.yaml` has no equivalent `network`/`envoy`/`10443` egress rule, only the `world:443` one. Not confirmed as a live failure here (unlike Collabora's, this hasn't shown up in `docs/incidents/`), but structurally the same setup that broke Collabora before its two-stage fix — worth checking first if whiteboard-to-Nextcloud connectivity ever times out.

## Common operations
- Upgrade the image: edit `containers.app.image.tag` in `helmrelease.yaml` (currently `v1.5.9`), commit, push.
- Rotate the JWT secret or Dragonfly password: update the relevant 1Password item, then `kubectl annotate externalsecret nextcloud-whiteboard-env -n nextcloud force-sync=$(date +%s)` (or wait out the 1h `refreshInterval`) — **and manually restart Nextcloud's own pod too** (`kubectl rollout restart deployment/nextcloud -n nextcloud`), per the reloader gap noted above.
- Pause reconciliation: `flux suspend kustomization whiteboard -n flux-system` / `flux suspend helmrelease whiteboard -n nextcloud`.

## TODOs / unknowns
- Whether the `toEntities: world`-vs-DNAT egress gap described above has ever actually caused a silent failure for this app is unconfirmed — flagged as a structural risk by analogy with Collabora's documented incident, not an observed one for Whiteboard.
- No ServiceMonitor/PodMonitor or Gatus health check found for this app anywhere in the repo (grepped `kubernetes/apps/monitoring/`) — unlike Collabora and Nextcloud, this app appears unmonitored beyond Kubernetes' own pod readiness.
- Exact API surface the `whiteboard` container calls on `NEXTCLOUD_URL` is not stated anywhere in this repo (only the env var itself, `helmrelease.yaml:32-33`) — not verified further for this doc.

---
_Cite every non-obvious claim with a repo-root-relative file path (e.g. `kubernetes/apps/security/authentik/app/helmrelease.yaml`), not a bare filename — this doc lives under `docs/apps/`, so relative paths must resolve from there._
