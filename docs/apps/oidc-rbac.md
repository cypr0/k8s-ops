# OIDC RBAC

> **Namespace**  security
> **Source**     plain manifests (no HelmRelease) — `kubernetes/apps/security/oidc-rbac/app/`
> **Hostname**   n/a — this app is a single RBAC object, not a workload

## What it does here
Binds Authentik's `k8s-admins` OIDC group to Kubernetes' built-in `cluster-admin` ClusterRole via one `ClusterRoleBinding` (`kubernetes/apps/security/oidc-rbac/app/clusterrolebinding.yaml`). It's the RBAC-side half of human SSO login to the Kubernetes API server — the other half is the apiserver's own OIDC flags and the Authentik OAuth2 provider that issues the tokens. This is the only `ClusterRoleBinding` in the whole repo whose subject is a `kind: Group` (confirmed via repo-wide grep), so it's currently the sole gate standing between "member of an Authentik group" and full cluster-admin.

## Architecture at a glance
- **Depends on:**
  - kube-apiserver's OIDC auth flags (`talos/patches/controller/cluster.yaml`, `cluster.apiServer.extraArgs`: `oidc-issuer-url`, `oidc-client-id: kubernetes`, `oidc-username-claim: preferred_username`, `oidc-username-prefix: "oidc:"`, `oidc-groups-claim: groups`, `oidc-groups-prefix: "oidc:"`) — this is what turns a validated ID token's `groups` claim into the literal `oidc:<name>` Group identity Kubernetes RBAC sees.
  - Authentik's `kubernetes-oidc` OAuth2Provider and `k8s-admins` Group, provisioned by the `kubernetes-oidc` blueprint (`kubernetes/apps/security/authentik/app/blueprints/08-kubernetes-oidc.yaml`) — the `kubernetes-scope-mapping` custom scope in that blueprint is what puts `groups = [group.name for group in request.user.ak_groups.all()]` into the token in the first place.
- **Depended on by:** nothing in-cluster — this is a leaf manifest consumed only by kube-apiserver's RBAC authorizer at request time, not referenced by any other app.

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/security/oidc-rbac/app/clusterrolebinding.yaml` | The entire app: one `ClusterRoleBinding` named `oidc-k8s-admins` |
| `kubernetes/apps/security/oidc-rbac/app/kustomization.yaml` | Kustomize resource list (single entry) |
| `kubernetes/apps/security/oidc-rbac/ks.yaml` | Flux `Kustomization`, `interval: 1h`, `prune: true` |

## Secrets
None. No `ExternalSecret` in this app's directory — the OIDC client itself is a *public* client (no client secret; PKCE-based), configured entirely on the Authentik side (`kubernetes/apps/security/authentik/app/blueprints/08-kubernetes-oidc.yaml`, `client_type: public`).

## Routing & access
No `HTTPRoute` or `CiliumNetworkPolicy` — this app exposes nothing over the network itself. The access path it grants:
1. A user authenticates to Authentik and requests a token from the `kubernetes` application (`meta_launch_url: https://k8s.${SECRET_DOMAIN}:6443` per the blueprint, though the actual sign-in typically happens via a `kubectl`/`kubelogin`-style OIDC plugin rather than a browser hitting that URL directly).
2. The ID token's `groups` claim includes every Authentik group the user belongs to (from the `kubernetes-scope-mapping` scope mapping).
3. kube-apiserver validates the token against issuer `https://id.${SECRET_DOMAIN}/application/o/kubernetes/` and client-id `kubernetes` (`talos/patches/controller/cluster.yaml`), then prefixes the username and each group with `oidc:`.
4. If the resulting identity includes group `oidc:k8s-admins`, this `ClusterRoleBinding`'s subject match grants `cluster-admin` — cluster-wide, no scoping.

This is explicitly **additive**, not a replacement: the comment in `talos/patches/controller/cluster.yaml` (next to `oidc-issuer-url`) states the Talos-generated client-cert admin kubeconfig keeps working as a break-glass fallback.

Membership in the `k8s-admins` Authentik group is **not managed by this app or by GitOps at all** — the blueprint's own comment says membership is added manually via the Authentik UI (Directory > Groups > k8s-admins > Users) after the blueprint reconciles. Anyone added there gets full `cluster-admin` the next time they authenticate — this repo has no lower-privileged tier to fall back to.

## Storage
None.

## Known quirks
- **Only one privilege tier exists.** This is the only `Group`-subject `ClusterRoleBinding` in the repo, and it binds straight to `cluster-admin` — there's no intermediate role (e.g., a `view`/`edit`-bound group for less-trusted accounts) wired up anywhere yet.
- **Access grant lives outside git.** Because Authentik group membership is UI-managed, reviewing "who currently has cluster-admin via OIDC" requires checking Authentik directly — it's invisible to `git log` or `flux diff`.
- **Short-lived tokens.** The `kubernetes-oidc-provider` in the blueprint sets `access_token_validity: minutes=10` / `refresh_token_validity: days=30` — an interactive `kubectl` session will exercise the OIDC refresh flow frequently.

## Common operations
- **Grant/revoke cluster-admin:** add or remove the user from Authentik's `k8s-admins` group (Directory > Groups > k8s-admins > Users in the Authentik UI) — no change to this repo required.
- **Add a lower-privileged tier:** would require a second Authentik group (added to the `08-kubernetes-oidc.yaml` blueprint or a new one) plus a second `ClusterRoleBinding` here bound to a less powerful `ClusterRole` — neither exists today.
- **Force reconcile:** `flux reconcile kustomization oidc-rbac -n security` (`interval: 1h` per `ks.yaml`).

## TODOs / unknowns
- No `kubectl`/`kubelogin` client-side OIDC exec-plugin configuration was found anywhere in this repo — it presumably lives on the operator's local machine, outside git.
- Whether a lower-privileged (non-cluster-admin) RBAC tier is planned is unknown — flagged above as a gap, not confirmed as a plan.
- Single commit in this app's history (`fef017b`, 2026-07-04) — no evidence yet of this binding being exercised against a live login or touched by any incident.

---
_Cite every non-obvious claim with a repo-root-relative file path (e.g. `kubernetes/apps/security/authentik/app/helmrelease.yaml`), not a bare filename — this doc lives under `docs/apps/`, so relative paths must resolve from there._
