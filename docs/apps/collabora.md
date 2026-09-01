# Collabora

> **Namespace**  nextcloud
> **Source**     `app-template` chart v5.1.0 via OCIRepository `oci://ghcr.io/bjw-s-labs/helm/app-template` (`kubernetes/apps/nextcloud/collabora/app/ocirepository.yaml`, `helmrelease.yaml`) — wraps the `collabora/code` and `collabora/languagetool` container images, not a dedicated Collabora Helm chart
> **Hostname**   `collabora.${SECRET_DOMAIN}` (public, via `envoy-external`)

## What it does here
WOPI-protocol Office document editing/rendering backend for Nextcloud's `richdocuments` app — Nextcloud's post-install job points `richdocuments`' `wopi_url` at `https://collabora.${SECRET_DOMAIN}`, and Collabora calls back into Nextcloud server-to-server for the WOPI `CheckFileInfo` handshake. Stateless: no PVC, no database — editing state lives in Nextcloud's own storage. Ships with a `languagetool` sidecar container in the same pod for spell/grammar-check.

## Architecture at a glance
- **Depends on:** ExternalSecret → 1Password item `collabora` (basic-auth creds), in-pod `languagetool` sidecar (reached via the `collabora-languagetool` Service on the same pod), CoreDNS + `envoy-internal` gateway for its outbound WOPI callback to Nextcloud (see Routing & access).
- **Depended on by:** `nextcloud`'s `richdocuments` app — a WOPI client relationship, not a hard startup dependency: if Collabora is down, Nextcloud itself keeps working, only in-browser document editing/preview breaks. Also polled by Gatus (`GET /hosting/discovery`).

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/nextcloud/collabora/app/ocirepository.yaml` | Chart source: `app-template` v5.1.0 |
| `kubernetes/apps/nextcloud/collabora/app/helmrelease.yaml` | `collabora/code` + `collabora/languagetool` image tags, env, probes, resources |
| `kubernetes/apps/nextcloud/collabora/app/externalsecret.yaml` | Basic-auth credentials for the Collabora container |
| `kubernetes/apps/nextcloud/collabora/app/httproute.yaml` | Public route via `envoy-external`, backend `collabora-app:9980` |
| `kubernetes/apps/nextcloud/collabora/app/ciliumnetworkpolicy.yaml` | Ingress (envoy, same-namespace WOPI calls, Gatus) and egress (DNS, sidecar, WOPI callback to Nextcloud) |
| `kubernetes/apps/nextcloud/collabora/ks.yaml` | Flux Kustomization — `dependsOn: external-secrets-stores` (security ns) |

## Secrets
| ExternalSecret | 1Password source | Consumed by |
| --- | --- | --- |
| `nextcloud-collabora-env` → `nextcloud-collabora-credentials` | Item `collabora` (`dataFrom.extract.key: collabora`), fields `COLLABORA_USERNAME` / `COLLABORA_PASSWORD` | Collabora `app` container's `username`/`password` env vars — the `collabora/code` image's built-in basic-auth credentials. Notably still wired up even though `--o:admin_console.enable=false` disables the web admin console itself. |

## Routing & access
- **Public HTTPRoute**: `collabora.${SECRET_DOMAIN}` on `envoy-external`, all paths to the `collabora-app` Service, port 9980. This is the path browser clients and the Nextcloud UI use to open the editor iframe.
- **WOPI CheckFileInfo callback — dual-gateway fix.** Collabora also calls back *into* Nextcloud server-to-server (WOPI host verification):
  - Originally this callback hairpinned out through the public Cloudflare tunnel to `cloud.${SECRET_DOMAIN}`, so Nextcloud saw the connection arrive from Cloudflare's edge IP and `richdocuments`' `wopi_allowlist` (RFC1918-only) rejected it with a 403 ("Unauthorised WOPI host") — per the fix commit `120f616` ("resolve OIDC admin split-identity and Collabora WOPI 403").
  - The fix (same commit) added a CoreDNS `hosts` override resolving `cloud.${SECRET_DOMAIN}` in-cluster to the `envoy-internal` Gateway ClusterIP and attached Nextcloud's HTTPRoute to `envoy-internal` too — the same split-horizon pattern already used for Authentik's `id.${SECRET_DOMAIN}`.
  - That alone wasn't sufficient: a follow-up fix (`05dc4ab`, "allow Collabora egress to internal gateway for WOPI") was needed because Collabora's own CiliumNetworkPolicy still only had a `toEntities: world` egress rule on :443, and `envoy-gateway`'s ingress allowlist for its DNAT'd `:10443` port didn't include Collabora — so traffic was silently dropped (surfacing as a 30s timeout instead of the earlier 403). The fix added an explicit egress rule to `envoy` (network ns) on port `10443`, and a matching ingress rule in envoy-gateway's own CNP.
  - The old `toEntities: world` egress rule on :443 is kept as an explicit "legacy fallback path (unused while the split-horizon DNS override above is in place, kept in case that ever needs to be rolled back)."
  - Collabora's own `net.proxy_allowed_hosts` extra param allow-lists `192.168.0.0/16,10.0.0.0/8,cloud.${SECRET_DOMAIN}` — the set of WOPI-source hosts Collabora itself is willing to fetch documents from, mirroring the same RFC1918 + internal-domain assumption on the client side.
- **CiliumNetworkPolicy ingress**: from `envoy` (network ns, port 9980, public path), from anything in the `nextcloud` namespace unrestricted (Nextcloud's own WOPI requests, and the `languagetool` sidecar hairpin), and from Gatus (`monitoring` ns, port 9980, health check).
- No SSO/OIDC on Collabora itself — auth is the basic-auth env vars above, not Authentik.

## Storage
No PVC — Collabora is stateless; document content and editing state live in Nextcloud's own storage. The `nextcloud` namespace (which Collabora runs in) is included in Velero's daily/weekly/monthly schedules, but since Collabora has no persistent volume this only captures its Kubernetes-object state (Secret, Deployment, etc.), not user data.

## Known quirks
- **WOPI 403 → silent timeout, two-stage fix.** See Routing & access above — the split-horizon DNS/gateway fix alone wasn't enough; CiliumNetworkPolicy had to catch up in a separate commit the next day, and the failure mode changed from an explicit 403 to a silent 30s timeout in between, which would otherwise read as a regression rather than "still fixing the same issue."
- **Single replica with `sessionAffinity: ClientIP`.** `helmrelease.yaml` sets `replicas: 1` but the Service still defines `sessionAffinity: ClientIP` with a 7200s timeout — only matters if this is ever scaled beyond one replica, since Collabora keeps live document-editing sessions in-memory per pod.
- **`languagetool` is a sidecar, not a separate Deployment.** Both `app` and `languagetool` are containers under the single `collabora` controller, each exposed via its own Service (`collabora-app` / `collabora-languagetool`) — easy to mistake for two independently-scalable workloads when skimming the values.

## Common operations
- Upgrade Collabora image: edit the `app` container's `image.tag` in `helmrelease.yaml` (Renovate opens these automatically, e.g. `89f1a85`), commit, push. `reloader.stakater.com/auto: "true"` means credential rotation also triggers a pod restart automatically.
- Rotate credentials: update the `collabora` 1Password item, then `kubectl annotate externalsecret nextcloud-collabora-env -n nextcloud force-sync=$(date +%s)` (or wait for the 1h `refreshInterval`).
- Pause reconciliation: `flux suspend kustomization collabora -n flux-system` / `flux suspend helmrelease collabora -n nextcloud`.
- Debugging a WOPI editing failure: check whether it's a 403 (allowlist/host-verification problem) vs. a timeout (network-policy/DNS problem) first — per the quirk above, both have happened here for related-but-distinct reasons.

## TODOs / unknowns
- Not verified from the repo: whether the `username`/`password` basic-auth env vars gate anything beyond the (disabled) admin console — e.g. whether they're also required for the `/hosting/discovery` endpoint Gatus polls unauthenticated. Not contradicted by any file read, just not explicitly confirmed either way.

---
_Cite every non-obvious claim with a repo-root-relative file path (e.g. `kubernetes/apps/security/authentik/app/helmrelease.yaml`), not a bare filename — this doc lives under `docs/apps/`, so relative paths must resolve from there._
