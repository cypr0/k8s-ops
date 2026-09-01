# Echo

> **Namespace**  `echo`
> **Source**     `app-template` chart v5.1.0 via `OCIRepository` `oci://ghcr.io/bjw-s-labs/helm/app-template` (`kubernetes/apps/echo/echo/app/ocirepository.yaml`, `helmrelease.yaml`)
> **Hostname**   `echo.${SECRET_DOMAIN}` — public, via `envoy-external` Gateway

## What it does here
A deliberate connectivity-test/diagnostic app, not a real workload: it runs `ghcr.io/mendhak/http-https-echo:41` (`kubernetes/apps/echo/echo/app/helmrelease.yaml`), which just echoes back whatever request it receives. Its only job in this cluster is to be a known-good external endpoint — something to curl or have Gatus poll to prove the public ingress path (DNS → Envoy Gateway → Cilium → pod) is actually working end-to-end, independent of any real app's own bugs.

## Architecture at a glance
- **Depends on:** nothing but CoreDNS (`kube-dns`) for its own DNS egress — no database, cache, or object storage (`ciliumnetworkpolicy.yaml` egress rule, port 53 only).
- **Depended on by:** Gatus's synthetic "Echo (Connectivity Test)" check, which polls `https://echo.${SECRET_DOMAIN}` every 5 minutes as an infra-health signal (`kubernetes/apps/monitoring/gatus/app/configmap.yaml`). This app's hostname was also the original, single-host case that motivated CoreDNS's cluster-wide AAAA-suppression `template` rule, later generalized to all external names (`kubernetes/apps/kube-system/coredns/app/helmrelease.yaml`); see Known quirks.

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/echo/echo/ks.yaml` | Flux Kustomization: `targetNamespace: echo`, `postBuild.substituteFrom` → `cluster-secrets` (resolves `${SECRET_DOMAIN}`) |
| `kubernetes/apps/echo/echo/app/ocirepository.yaml` | Chart source: `app-template` v5.1.0 |
| `kubernetes/apps/echo/echo/app/helmrelease.yaml` | Image, probes, resources, Service/Route/ServiceMonitor values (chart-generated, no separate manifest files) |
| `kubernetes/apps/echo/echo/app/ciliumnetworkpolicy.yaml` | Ingress from Envoy + kubelet probes, egress to CoreDNS only |

## Secrets
None — no ExternalSecret in this app's directory. The only external value is `${SECRET_DOMAIN}`, a cluster-wide postBuild substitution (not per-app) sourced from the SOPS-encrypted `kubernetes/components/sops/cluster-secrets.sops.yaml`, referenced via `cluster-secrets` in `ks.yaml`.

## Routing & access
- Exposed through `envoy-external` Gateway (`network` namespace, `https` section) at `echo.${SECRET_DOMAIN}` — chart-generated HTTPRoute from the `route.app` block in `helmrelease.yaml`, not a standalone `httproute.yaml`.
- No SSO/OIDC — deliberately unauthenticated, since its purpose is to prove reachability, not gate access.
- `ciliumnetworkpolicy.yaml`: ingress allowed only from `envoy` pods in the `network` namespace on port 80, plus the `host` entity for kubelet liveness/readiness probes; egress restricted to CoreDNS (port 53 UDP/TCP) — no other egress is permitted.

## Storage
None — stateless echo server, no PVC.

## Known quirks
- **The CNP shipped with the wrong ingress port for over a month.** It allowed Envoy ingress only on `:8080`, but the container (`HTTP_PORT` env, Service port, and HTTPRoute backendRef all agree) listens on `:80` — every request, whether from a real client or Gatus's own health check, was silently dropped by Cilium. Fixed in commit `0b646d8` (`fix(echo): correct CNP ingress port from 8080 to the real container port 80`). If this app is ever unreachable again, checking the CNP's allowed port against the actual container port is the first thing to verify.
- **Moved out of the `default` namespace for CIS compliance.** Originally lived in `default`; commit `86a24ba` relocated it to its own `echo` namespace/app-group because CIS 5.6.4 reserves `default` for system-managed resources only.
- **This app's hostname was the original trigger for a cluster-wide CoreDNS rule.** CoreDNS added a `template ANY AAAA .` rule (`kubernetes/apps/kube-system/coredns/app/helmrelease.yaml`) because this app's own external hostname publishes both A and AAAA records and this cluster has no working IPv6 egress — the rule was later generalized to all external names after unrelated hosts (Velero/Kopia, Flux artifact mirrors, Pushover) hit the same symptom. A subsequent regression of that same rule caused an unrelated internal-DNS outage — see `docs/incidents/2026-08-16-coredns-aaaa-nxdomain-breaks-internal-dns.md` — though that incident's root cause was the rule's effect on `cluster.local` names, not this app directly.

## Common operations
- Upgrade the echo image: bump `spec.values.controllers.echo.containers.app.image.tag` in `helmrelease.yaml`, commit, push.
- Upgrade the chart itself: bump `spec.ref.tag` in `ocirepository.yaml`.
- Force reconcile: `flux reconcile helmrelease echo -n echo`.
- Manual reachability test: `curl -s https://echo.${SECRET_DOMAIN}` (resolve `${SECRET_DOMAIN}` from your own 1Password/env, don't hardcode it) — a healthy response echoes back the request headers/body as JSON.

## TODOs / unknowns
- No incident doc exists yet specifically for the CNP port-8080-vs-80 outage (commit `0b646d8`) — the fix commit itself explains root cause, but whether it merits a standalone SEV-rated postmortem (vs. just this quirks entry) hasn't been decided.
