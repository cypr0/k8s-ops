# Envoy Gateway

> **Namespace**  `network`
> **Source**     OCI Helm chart `oci://mirror.gcr.io/envoyproxy/gateway-helm`, tag `1.9.1` (`kubernetes/apps/network/envoy-gateway/app/ocirepository.yaml`)
> **Hostname**   None of its own — it's the cluster's Gateway API controller. It defines the two Gateways every other app's `HTTPRoute` attaches to: `envoy-external` (public) and `envoy-internal` (in-cluster-only) (`kubernetes/apps/network/envoy-gateway/app/envoy.yaml:56,86`).

## What it does here
This is the cluster's sole Gateway API implementation: one `GatewayClass`/`EnvoyProxy` pair plus the two `Gateway` resources (`envoy-external`, `envoy-internal`) that every `HTTPRoute` in the repo attaches to. The `envoy-gateway` HelmRelease deploys only the *controller*; the controller in turn creates the Envoy proxy data-plane Deployments/Services itself (`provider.kubernetes.deploy.type: GatewayNamespace`) — there's no `deployment.yaml` for the proxy pods in this repo because they're generated, not authored.

## Architecture at a glance
- **Depends on:** cert-manager's wildcard `Certificate` for the TLS secret both Gateways' HTTPS listeners reference (`kubernetes/apps/network/envoy-gateway/app/certificate.yaml` — see `docs/apps/cert-manager.md`); Cloudflare Tunnel as the public ingress path into `envoy-external`; kube-apiserver, which the controller watches for Gateway API resources. No ExternalSecret of its own.
- **Depended on by:** every app with an `HTTPRoute`/embedded chart route pointing at `envoy-external` or `envoy-internal` — 12 confirmed: `flux-instance`, `opensearch-cluster`, `gatus`, `grafana`, `collabora`, `nextcloud`, `whiteboard`, `open-webui`, `paperless-ngx`, `immich`, `authentik` (all via standalone `httproute.yaml` files), plus `echo`, whose `app-template` chart embeds its route directly in `helmrelease.yaml`. If the controller or its `EnvoyProxy` data-plane pods go down, every one of these loses ingress simultaneously. New backends must be added to the shared `envoy-proxy` `CiliumNetworkPolicy` (`ciliumnetworkpolicy.yaml`) on *both* sides — egress (envoy → backend) and ingress (backend → envoy, for the backend's own OIDC discovery/token calls) — commit `e82f544` documents Immich being missed on first rollout as the concrete example.

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/network/envoy-gateway/app/envoy.yaml` | `EnvoyProxy` (data-plane sizing/HPA), `GatewayClass`, both `Gateway`s, `BackendTrafficPolicy`, `ClientTrafficPolicy`, and the cluster-wide HTTP→HTTPS `https-redirect` `HTTPRoute` |
| `kubernetes/apps/network/envoy-gateway/app/helmrelease.yaml` | Controller chart values: resources, `GatewayNamespace` deploy mode |
| `kubernetes/apps/network/envoy-gateway/app/ocirepository.yaml` | Pins chart to `1.9.1`, 15m poll |
| `kubernetes/apps/network/envoy-gateway/app/certificate.yaml` | The one wildcard `Certificate` both Gateways' HTTPS listeners reference |
| `kubernetes/apps/network/envoy-gateway/app/ciliumnetworkpolicy.yaml` | Two `CiliumNetworkPolicy` objects: `envoy-gateway` (controller, xDS server) and `envoy-proxy` (data plane, per-backend allow-list) |
| `kubernetes/apps/network/envoy-gateway/app/podmonitor.yaml` | Scrapes proxy pods' `/stats/prometheus` |

## Secrets
None. No `externalsecret*.yaml` exists under this app's directory. The only secret-shaped object involved is the `${SECRET_DOMAIN/./-}-production-tls` `Secret`, produced by cert-manager's `Certificate` and referenced by name only — never its content — in both Gateways' `listeners[].tls.certificateRefs`.

## Routing & access
- **`envoy-external`**: public Gateway, `LoadBalancer` IP pinned via `lbipam.cilium.io/ips`, `allowedRoutes.namespaces.from: All`. Reached from outside via Cloudflare Tunnel forwarding to `envoy-external.network.svc:443`, or directly against the LB IP as a fallback path.
- **`envoy-internal`**: in-cluster-only Gateway. Used by in-cluster OIDC clients (OpenSearch Dashboards/masters, Grafana, Gatus, Paperless, Open WebUI, Nextcloud, Collabora, kube-apiserver) that need to reach Authentik or another backend without bouncing off the public Cloudflare hairpin — CoreDNS split-horizon resolves the relevant hostnames straight to this Gateway's ClusterIP in-cluster.
- **`https-redirect` `HTTPRoute`**: attached to the `http` listener of *both* Gateways, 301-redirects everything to HTTPS.
- **CiliumNetworkPolicy `envoy-gateway`** (controller): ingress only from `envoy` data-plane pods on `:18000` (xDS gRPC — proxy pods pull their listener/cluster config from the controller; without this rule every proxy pod is `1/2 Ready` and both Gateways have zero healthy backends) and from Prometheus on `:19001`.
- **CiliumNetworkPolicy `envoy-proxy`** (data plane) is enforced **post-DNAT**: the `Service`s map `80→10080` and `443→10443` on the container, so every ingress rule allows container ports `10080`/`10443`, not `80`/`443` — using the service ports "silently dropped everything." LB traffic arrives via SNAT (`loadBalancer.mode: snat`), so Cilium classifies it as `remote-node`/`kube-apiserver`, not `world` — both entities are explicitly allowed for this reason. `world` is also allowed directly as a fallback/health-check path, including UDP on `10443` for HTTP/3.
- **`ClientTrafficPolicy`**: TLS floor `1.2`, ALPN `h2`/`http/1.1`, HTTP/3 enabled (explains the UDP `10443` CNP rule above), trusts `X-Forwarded-For` only from the pod CIDR.
- **`BackendTrafficPolicy`**: response compression (Zstd/Brotli/Gzip), 2 retries on connection reset, and **`requestTimeout: 0s` — no request timeout at all**, applied cluster-wide to every route through either Gateway.

## Storage
No PVCs — the controller and proxy pods are stateless; all Gateway API config is reconciled live from the `Gateway`/`HTTPRoute`/`EnvoyProxy` CRs via xDS. `HTTPRoute` and `CiliumNetworkPolicy` CRDs (cluster-wide, not scoped to this app) are included as resource types in every Velero schedule.

## Known quirks
- **Stale `openclaw` egress rule.** The `envoy-proxy` `CiliumNetworkPolicy` still has an egress rule allowing traffic to the `openclaw` namespace on port `18789`, but OpenClaw was decommissioned (`445ed29`) and its namespace later replaced by `hermes-agent` (`5b8b47f`) — neither commit touched this file. `hermes-agent` has no `httproute.yaml` and its own `CiliumNetworkPolicy` has no reference to Envoy, so it isn't served through either Gateway; this rule is dead weight, not a currently-relied-on path. Not fixed here per the campaign's no-inline-fixes rule — flagged for a follow-up.
- **`requestTimeout: 0s` is cluster-wide, not per-route.** Every request through either Gateway has no server-side timeout unless a future per-route `BackendTrafficPolicy` overrides it — a hung backend won't be cut off by Envoy itself.
- **Envoy proxy pods have no Deployment manifest in this repo.** `provider.kubernetes.deploy.type: GatewayNamespace` means the controller creates them dynamically per-`GatewayClass`; anything you'd normally check in a `deployment.yaml` (replicas, resource overrides) instead lives in the `EnvoyProxy` CR — HPA 2–4 replicas at 70% CPU, memory capped at 1Gi.
- **`shutdown.drainTimeout: 180s`** — proxy pods wait up to 3 minutes to drain in-flight connections before terminating; relevant when reasoning about how long a node drain or rolling restart of the proxy Deployment takes.
- **The TLS cert both Gateways use is deliberately short-lived** (`duration: 160h`, ~6.7 days) as a fail-fast canary for the ACME pipeline, not a normal 90-day cert — full rationale in `certificate.yaml` and `docs/apps/cert-manager.md`. Any expiry monitoring on this cert must use that short window.
- **No explicit Flux `dependsOn` from this Kustomization onto `cert-manager`**, despite both HTTPS listeners requiring cert-manager's `Certificate` to exist first — same gap noted from the other side in `docs/apps/cert-manager.md`. Ordering currently relies on cert-manager reconciling first in practice.

## Common operations
- Upgrade chart version: bump `tag` in `ocirepository.yaml`, commit, push (`OCIRepository` polls every 15m, or force with `flux reconcile ocirepository envoy-gateway -n network` then `flux reconcile helmrelease envoy-gateway -n network`).
- Change data-plane sizing/HPA bounds: edit `EnvoyProxy.spec.provider.kubernetes.envoyHpa`/`envoyDeployment` in `envoy.yaml`, commit, push.
- Pause reconciliation: `flux suspend kustomization envoy-gateway -n flux-system` / `flux suspend helmrelease envoy-gateway -n network`.
- Diagnose a proxy pod stuck `1/2 Ready`: check it can reach the controller on `:18000` (xDS) — this is the #1 cause per the in-file CNP comment.

## TODOs / unknowns
- Whether `hermes-agent` is meant to be reachable through `envoy-internal` at all (replacing OpenClaw's old exposure) or is deliberately not Gateway-routed is unconfirmed — no `HTTPRoute` exists for it today, and the only remaining trace is the stale CNP rule above.
- The controller (`envoyGateway`) Deployment's replica count/HPA isn't set in `helmrelease.yaml` (only `resources` is overridden) — presumably the chart default, not verified against the chart's `values.yaml` for this doc.
- Whether the missing `dependsOn` between `envoy-gateway` and `cert-manager` has ever caused a real bootstrap-ordering failure is unverified — no incident doc references it as of this writing.

---
_Cite every non-obvious claim with a repo-root-relative file path (e.g. `kubernetes/apps/security/authentik/app/helmrelease.yaml`), not a bare filename — this doc lives under `docs/apps/`, so relative paths must resolve from there._
