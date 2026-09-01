# 1Password Connect

> **Namespace**  security
> **Source**     Not a dedicated upstream chart — deployed via the generic `bjw-s-labs/app-template` chart (OCIRepository `app-template`, tag `5.1.0`, `kubernetes/apps/security/onepassword-connect/app/ocirepository.yaml`), wrapping the two official 1Password Connect images directly (`docker.io/1password/connect-api:1.8.2`, `docker.io/1password/connect-sync:1.8.2`, pinned by digest in `kubernetes/apps/security/onepassword-connect/app/helmrelease.yaml`).
> **Hostname**   Internal-only — `onepassword-connect.security.svc.cluster.local`. No `httproute.yaml` exists in this app's directory; it is never exposed outside the cluster.

## What it does here
This is the 1Password Connect server: the on-cluster proxy that turns 1Password vault items into a REST API. `external-secrets`' `ClusterSecretStore/onepassword` talks to it exclusively, and every `ExternalSecret` in the cluster — 51 resources across 28 app directories at last count (`grep -rl "name: onepassword" kubernetes/apps --include="externalsecret*.yaml"`) — resolves through this Service. It is the credential root for the entire GitOps secret pipeline documented across `docs/apps/`: if this app is gone, no ExternalSecret can create or refresh a Kubernetes Secret, though Secrets already synced continue to exist until something forces a re-sync.

## Architecture at a glance
- **Depends on:** 1Password's cloud service (SaaS) reached over egress — see Routing & access below. No in-cluster database, cache, or object storage dependency; it is stateless apart from a scratch `emptyDir`. Bootstraps from a raw SOPS-encrypted Kubernetes `Secret` committed in-repo (`kubernetes/apps/security/onepassword-connect/app/secret.sops.yaml`) rather than an `ExternalSecret` — it *is* the secret backend, so it cannot pull its own bootstrap credentials from itself.
- **Depended on by:** `external-secrets`' `ClusterSecretStore/onepassword` (`kubernetes/apps/security/external-secrets/stores/clustersecretstore.yaml`), which is in turn the `secretStoreRef` for every `ExternalSecret` in the repo. The `external-secrets` Flux `Kustomization` explicitly waits on this app via `dependsOn`. Also polled by Gatus for uptime status.

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/security/onepassword-connect/app/helmrelease.yaml` | app-template values: two containers (`api`, `sync`), image tags/digests, probes, resources |
| `kubernetes/apps/security/onepassword-connect/app/ocirepository.yaml` | Pins the `app-template` chart source (OCI, tag `5.1.0`) |
| `kubernetes/apps/security/onepassword-connect/app/secret.sops.yaml` | SOPS/age-encrypted raw `Secret` — the Connect credentials file + API token |
| `kubernetes/apps/security/onepassword-connect/app/ciliumnetworkpolicy.yaml` | Ingress locked to `security` namespace + Gatus + kubelet probes; egress to `world` (1Password cloud) + DNS |
| `kubernetes/apps/security/onepassword-connect/app/kustomization.yaml` | Includes the four files above — no `httproute.yaml` present |
| `kubernetes/apps/security/onepassword-connect/ks.yaml` | Flux `Kustomization`, `targetNamespace: security` |

## Secrets
No `ExternalSecret` exists in this app's directory — by design, since this app *is* the ExternalSecret backend and can't bootstrap from itself. Instead:

| Secret | Source | Consumed by |
| --- | --- | --- |
| `onepassword-secret` (raw SOPS Secret) | Encrypted in-repo with `age`/SOPS (`encrypted_regex: ^(data\|stringData)$`); decrypted at apply time by the cluster's global Flux controller decryption (moved off a per-`Kustomization` `sops` block in commit `196dc0f`, see Known quirks) | Key `onepassword-credentials.json` → mounted as `OP_SESSION` env var on both the `api` and `sync` containers. Key `token` → read by `external-secrets`' `ClusterSecretStore/onepassword` via `auth.secretRef.connectTokenSecretRef` |

No resolved values are reproduced here — only key names and consumers.

## Routing & access
- **No `HTTPRoute`** — this app is never reachable from outside the cluster.
- **CiliumNetworkPolicy**: ingress on port 80 allowed only from pods in the `security` namespace, plus Gatus pods in `monitoring` (added in commit `3cb104e` after Gatus's health check was initially blocked), plus kubelet liveness/readiness probes on ports 80 and 8081 via `fromEntities: host` (added in commit `ba3bce2` — see Known quirks). Egress is `world` (reaching 1Password's cloud service) plus DNS to `kube-dns`.
- The `external-secrets` app's own `CiliumNetworkPolicy` mirrors this from the other side, allowing its egress to `onepassword-connect` on port 80.
- No OIDC/SSO — not applicable, this app has no UI.

## Storage
No `PersistentVolumeClaim`. `persistence.config` is an `emptyDir` mounted at `/config` for both containers — scratch space only, wiped on pod restart. The `security` namespace is not in the `includedNamespaces` list of any Velero `Schedule`, so there is no Velero coverage for this namespace. The only durable state is the git-committed, SOPS-encrypted `secret.sops.yaml` itself — its durability is the repo's own history/backup, not Velero's.

## Known quirks
- **Port history:** the Service/containers listen on port 80 (api) and 8081 (sync), not 8080 — an early CiliumNetworkPolicy draft used 8080 and silently blocked new `ExternalSecret` syncs cluster-wide until fixed in commit `94aa519`.
- **Kubelet probes are a `host` entity, not `cluster`:** commit `ba3bce2` fixed a cluster-wide pattern (21 probe ports across 17 files, this app included) where CiliumNetworkPolicies restricting a probe port to specific namespaces/pods silently blocked the kubelet itself — Cilium classifies kubelet-originated probe traffic as the `host` entity, not the pod's own namespace.
- **SOPS decryption is global, not per-Kustomization:** commit `196dc0f` removed an explicit `spec.decryption.sops` block from `ks.yaml` in favor of controller-wide decryption config.
- **Gatus cross-namespace ingress** was a one-off fix (commit `3cb104e`) — if another `monitoring`-namespace tool ever needs to reach this Service, it will hit the same default-deny wall and need the same kind of explicit `fromEndpoints` entry.

## Common operations
- Upgrade Connect version: bump the `tag`/digest for both `api` and `sync` containers in `helmrelease.yaml` (keep them in lockstep), commit, push, Flux reconciles within the 1h `interval` (or force with `flux reconcile helmrelease onepassword-connect -n security`).
- Rotate the Connect API token or credentials file: re-encrypt the new value into `secret.sops.yaml` with `sops`, commit, push.
- Pause reconciliation: `flux suspend kustomization onepassword-connect -n security` / `flux suspend helmrelease onepassword-connect -n security`. Note `external-secrets`' `Kustomization` has a hard `dependsOn` on this one, so suspending this app blocks `external-secrets` reconciliation too, though already-running `external-secrets` pods keep serving from their last-synced state.

## TODOs / unknowns
- No incident postmortem in `docs/incidents/` currently references this app by name — the port-8080→80 and kubelet-probe fixes above were resolved as routine commits, not documented incidents. Worth a postmortem if a future outage traces back here, given the blast radius.
- Did not verify live pod health, Gatus dashboard status, or actual reachability to 1Password's cloud endpoint from this session.

---
_Cite every non-obvious claim with a repo-root-relative file path (e.g. `kubernetes/apps/security/authentik/app/helmrelease.yaml`), not a bare filename — this doc lives under `docs/apps/`, so relative paths must resolve from there._
