# Open Terminal

> **Namespace**  open-webui
> **Source**     `oci://ghcr.io/bjw-s-labs/helm/app-template` (chart `app-template` v5.1.0) — `kubernetes/apps/open-webui/open-terminal/app/ocirepository.yaml`, `kubernetes/apps/open-webui/open-terminal/app/helmrelease.yaml`
> **Hostname**   none — not externally exposed; reachable only from the `open-webui` backend pod in-cluster

## What it does here
A single-container shell sandbox (`ghcr.io/open-webui/open-terminal`) that Open WebUI's backend proxies terminal sessions to, giving users a Linux shell reachable from the WebUI chat. It runs with `OPEN_TERMINAL_MULTI_USER: "true"`, which creates a separate Linux account per WebUI user inside the *same* container — a deliberate choice over the upstream Enterprise-licensed Terminals orchestrator, which would give real per-user kernel/network isolation instead of Unix-permission isolation within a shared pod. The image's inline comment spells this out: "acceptable here, real isolation would need the (Enterprise-licensed) Terminals orchestrator".

## Architecture at a glance
- **Depends on:** ExternalSecret `open-terminal` → 1Password item `open-terminal`; PVC `open-terminal-home-pvc` on `zfs-nfs`; CoreDNS for name resolution and unrestricted internet egress for in-terminal package installs.
- **Depended on by:** `open-webui/open-webui` — its CiliumNetworkPolicy explicitly allows egress to `open-terminal` on port 8000 ("the backend proxies user terminal requests to it"), so a user's in-chat terminal feature breaks if this app is down, though the rest of Open WebUI keeps working.

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/open-webui/open-terminal/app/ocirepository.yaml` | Pins the `app-template` chart to v5.1.0 |
| `kubernetes/apps/open-webui/open-terminal/app/helmrelease.yaml` | Container image/digest, env, probes, root securityContext, persistence |
| `kubernetes/apps/open-webui/open-terminal/app/externalsecret.yaml` | Shared bearer token pulled from 1Password |
| `kubernetes/apps/open-webui/open-terminal/app/ciliumnetworkpolicy.yaml` | Ingress from `open-webui` backend only; broad egress for package installs |
| `kubernetes/apps/open-webui/open-terminal/app/pvc.yaml` | `/home` persistence, `zfs-nfs`, RWX |
| `kubernetes/apps/open-webui/open-terminal/app/kustomization.yaml` | Wires the above into one Kustomization |
| `kubernetes/apps/open-webui/open-terminal/ks.yaml` | Flux Kustomization: depends on `external-secrets-stores` (namespace `security`), 1h interval |

## Secrets
| ExternalSecret | 1Password source | Consumed by |
| --- | --- | --- |
| `open-terminal` → target Secret `open-terminal-secret` | Item `open-terminal`, field `API_KEY`, templated into key `OPEN_TERMINAL_API_KEY` | Injected via `envFrom` into the `app` container. Per the ExternalSecret's own comment, the *same* value must also be pasted manually, once, into Open WebUI's Admin Settings → Integrations → Open Terminal — this is not automated anywhere in the repo, so it's a manual step to redo if the 1Password value ever rotates. |

## Routing & access
- No HTTPRoute — this app is not reachable from outside the cluster at all, by design (confirmed by the CiliumNetworkPolicy's ingress rule restricting to `open-webui`/`open-webui` pods only).
- No SSO/OIDC of its own — access control is entirely "only the Open WebUI backend can reach port 8000," so whoever can open a terminal session inside Open WebUI (itself behind Authentik, per Open WebUI's own CNP) can reach this container.
- CiliumNetworkPolicy: ingress limited to pods labeled `app.kubernetes.io/name: open-webui` in the `open-webui` namespace on TCP/8000; egress allows DNS to `kube-dns` plus full `world` egress on TCP/80 and TCP/443 — the comment notes this is deliberate so users can `apt`/`pip`/`npm` install and `curl`/`git clone` arbitrary hosts from inside their terminal session.

## Storage
- `open-terminal-home-pvc`: 20Gi, `storageClassName: zfs-nfs`, `ReadWriteMany`, mounted at `/home`. `/tmp` is a plain `emptyDir`, not persisted.
- The HelmRelease pins `replicas: 1` and `strategy: Recreate` with an explicit comment: "Single writer to the shared /home PVC — never run two pods at once" — scaling this up would risk concurrent writers on the same RWX volume.
- Backup coverage: the `open-webui` namespace (which includes this PVC) is included in Velero's daily (14-day retention), weekly (90-day), and monthly GFS schedules, and in the automated restore-test job.

## Known quirks
- **Runs as root by design.** `securityContext.runAsNonRoot: false` / `runAsUser: 0` is explicit, not an oversight — multi-user mode manages Linux accounts and uses `sudo` for runtime package installs inside the container, which needs root. The same comment reiterates the container is not externally exposed as the compensating control.
- **Image tagging is unusual.** Only the `latest`/full upstream image variant supports `OPEN_TERMINAL_MULTI_USER` (slim/alpine/openshift variants don't), and that full image publishes *only* `latest` and `sha-<commit>` tags — no semver. The repo pins by digest instead (`tag: latest@sha256:...`) for reproducibility, with a `renovate:` comment so Renovate still bumps the digest automatically. Git history confirms this was a deliberate follow-up fix, not the initial state: commit `c928171` ("pin open-terminal by digest (full image has no semver tags)") came right after the initial `e0b6a1d` ("add Open Terminal multi-user shell sandbox").
- **Isolation is Unix-permission-level only, not per-user network/kernel isolation** — all WebUI users who get a terminal share this one pod. This is documented upstream as suitable for "small trusted groups only," which the helmrelease.yaml comment calls acceptable for this cluster's single-operator use case.
- **Manual, undocumented-in-code admin step.** The `OPEN_TERMINAL_API_KEY` value pulled by the ExternalSecret must also be pasted once into Open WebUI's own Admin Settings UI (Integrations → Open Terminal) for the two apps to authenticate to each other — this pairing isn't expressed as code anywhere, so it will silently break if the 1Password `open-terminal`/`API_KEY` value is ever rotated without redoing the UI step.

## Common operations
- Upgrade chart version: edit the `tag:` in `ocirepository.yaml`, commit, push; Flux reconciles within the 1h `interval` (or force with `flux reconcile helmrelease open-terminal -n open-webui`).
- Bump the container image: Renovate manages the digest automatically; a manual bump means updating the `sha256:` digest in `helmrelease.yaml`.
- Rotate the shared API key: update the `open-terminal` 1Password item's `API_KEY` field, let the ExternalSecret's 1h `refreshInterval` pick it up (or force with `kubectl annotate externalsecret open-terminal -n open-webui force-sync=$(date +%s)`) — **and remember to re-enter the same value in Open WebUI's Admin Settings → Integrations → Open Terminal**, since that pairing is manual.
- Pause reconciliation: `flux suspend kustomization open-terminal -n flux-system` / `flux suspend helmrelease open-terminal -n open-webui`.
- Never scale `replicas` above 1 — the shared `/home` PVC is single-writer by design.

## TODOs / unknowns
- No CPU/memory usage baseline is recorded anywhere in the repo for how close real terminal sessions run to the `2 CPU` / `2Gi` limit — unverified from the repo.
- No incident in `docs/incidents/` currently references this app.

---
_Cite every non-obvious claim with a repo-root-relative file path (e.g. `kubernetes/apps/security/authentik/app/helmrelease.yaml`), not a bare filename — this doc lives under `docs/apps/`, so relative paths must resolve from there._
