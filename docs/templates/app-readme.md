# <App Name>

> **Namespace**  <namespace>
> **Source**     <upstream chart repo + name, from helmrepository.yaml, or "plain manifests" if no HelmRelease>
> **Hostname**   <public/internal hostname(s), if any>

## What it does here
1–3 sentences. What role does this app play in this cluster? Not generic
product marketing — the specific function (e.g., "SSO broker for every
OIDC-integrated app in the cluster; backs by CNPG postgres and Dragonfly").
If a sentence could appear on the vendor's website, rewrite it.

## Architecture at a glance
- **Depends on:** CNPG cluster `<name>` (namespace `database`), Dragonfly, ExternalSecret → 1Password item `<name>`, …
- **Depended on by:** <apps that break if this one is down>

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/<ns>/<app>/app/helmrelease.yaml` | Chart version, values |
| `kubernetes/apps/<ns>/<app>/app/externalsecret*.yaml` | What secrets are pulled, from where |
| `kubernetes/apps/<ns>/<app>/app/httproute.yaml` | Gateway/routing, if applicable |
| `kubernetes/apps/<ns>/<app>/app/ciliumnetworkpolicy.yaml` | Network policy, if applicable |

## Secrets
One row per ExternalSecret: name, 1Password item/field it pulls, which container/env-var/volume consumes it. Never restate the actual secret value — cite the ExternalSecret resource, not its resolved content.

## Routing & access
- Gateway/HTTPRoute, if this app is exposed (internal-only vs via cloudflare-tunnel)
- SSO: OIDC via Authentik, if applicable — link the blueprint file
- Any CiliumNetworkPolicy worth knowing about (what it allows/denies and why)

## Storage
PVCs, StorageClass, backup coverage (Velero/Kopia schedule this app is included in, if any — see `kubernetes/apps/velero/`).

## Known quirks
Things the code alone does not reveal — drawn from commits, inline comments, and past incidents (link `docs/incidents/*.md` where relevant). Mark clearly when the source is a memory/recollection rather than a citable file. Omit this section if the app genuinely has none — do not manufacture content.

## Common operations
- Upgrade chart version: edit `helmrelease.yaml`, commit, push, Flux reconciles within `interval` (or force with `flux reconcile helmrelease <name> -n <ns>`).
- Rotate a secret: update the 1Password item, then `kubectl annotate externalsecret <name> -n <ns> force-sync=$(date +%s)` (or wait for the refresh interval).
- Pause reconciliation: `flux suspend kustomization <name> -n <ns>` / `flux suspend helmrelease <name> -n <ns>`.

## TODOs / unknowns
Anything that could not be verified from the repo — mark clearly rather than guessing.

---
_Cite every non-obvious claim with a repo-root-relative file path (e.g. `kubernetes/apps/security/authentik/app/helmrelease.yaml`), not a bare filename — this doc lives under `docs/apps/`, so relative paths must resolve from there._
