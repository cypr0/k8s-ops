# Stalwart

> **Namespace**  mail
> **Source**     `bjw-s-labs/helm/app-template` (OCI, `oci://ghcr.io/bjw-s-labs/helm/app-template`, tag `5.1.0`) — `kubernetes/apps/mail/stalwart/app/ocirepository.yaml`, `helmrelease.yaml`; runs `ghcr.io/stalwartlabs/stalwart:v0.16.18`
> **Hostname**   `mail.${SECRET_DOMAIN}` — public, own dedicated LoadBalancer IP (not Envoy Gateway, not the Cloudflare Tunnel)

## What it does here
[Stalwart](https://stalw.art/) is the cluster's mail server: SMTP (send/receive), IMAP/POP3 (mailbox access), JMAP + a WebUI (`/admin`), and CalDAV/CardDAV/WebDAV, all as a single binary. It's the only app in this cluster that speaks raw mail protocols rather than HTTP, which drives most of its unusual deployment shape (see Routing & access below).

## Architecture at a glance
- **Depends on:** CNPG cluster `postgres` (namespace `database`, role `stalwartusr`/db `stalwartdb`) for its DataStore/BlobStore/SearchStore/InMemoryStore — see `kubernetes/apps/mail/stalwart-bootstrap/app/configmap.yaml`'s `config.json`; a Cloudflare API token (`STALWART_CLOUDFLARE_API_TOKEN`) for the DNS-01 ACME challenge and MX/SPF/DKIM/DMARC record publication; ExternalSecret → 1Password item `stalwart`.
- **Depended on by:** nothing else in the cluster (no other app relays mail through it) — Authentik's own outbound mail (`kubernetes/apps/security/authentik/app/helmrelease.yaml`'s `authentik.email.host: ${SECRET_MAIL_SERVER}`) points at a *different*, pre-existing mail server referenced by the cluster-wide `SECRET_MAIL_SERVER` secret, not at this Stalwart instance — the two are unrelated as of this writing.
- **Not integrated with:** Authentik SSO. Stalwart's OIDC directory type only validates `OAUTHBEARER` SASL tokens that a mail client already obtained from an IdP itself (see [stalw.art/docs/auth/backend/oidc](https://stalw.art/docs/auth/backend/oidc)) — it is not a browser-redirect login like every other app's Authentik blueprint in this cluster. Most mainstream mail clients (Thunderbird, Outlook, Apple Mail) don't support `OAUTHBEARER` with third-party IdPs at all. Deliberately deployed with Stalwart's **internal directory** instead (accounts/passwords managed directly in Stalwart) — no Authentik blueprint exists for this app.

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/mail/stalwart-bootstrap/ks.yaml` | Flux Kustomization: runs *before* the main `stalwart` Kustomization (see `dependsOn` in `kubernetes/apps/mail/stalwart/ks.yaml`) |
| `kubernetes/apps/mail/stalwart-bootstrap/app/configmap.yaml` | The single `config.json` Stalwart reads from disk — just the PostgreSQL DataStore connection details; shared by both Kustomizations |
| `kubernetes/apps/mail/stalwart-bootstrap/app/configmap-bootstrap-plan.yaml` | The declarative `stalwart-cli apply` NDJSON plan: DnsServer, AcmeProvider, Domain, every NetworkListener, SystemSettings, the admin Account |
| `kubernetes/apps/mail/stalwart-bootstrap/app/job-bootstrap.yaml` | One-shot Job: runs a throwaway Stalwart instance in recovery mode as a sidecar, then applies the plan above against it |
| `kubernetes/apps/mail/stalwart-bootstrap/app/externalsecret.yaml` | `stalwart-secret` — Postgres password, recovery credentials, admin mailbox password, Cloudflare API token |
| `kubernetes/apps/mail/stalwart/app/helmrelease.yaml` | The long-running server: image, LoadBalancer Service (all mail ports + 443), probes, resources |
| `kubernetes/apps/mail/stalwart/app/ciliumnetworkpolicy.yaml` | Ingress from `world` on every mail port + 443; egress to Postgres, DNS, and the public internet (ACME/DNS API/outbound SMTP) |

## Secrets
One ExternalSecret (`stalwart`, in `kubernetes/apps/mail/stalwart-bootstrap/app/externalsecret.yaml`), pulling from 1Password item `stalwart`, producing the `stalwart-secret` Kubernetes Secret with:
- `STALWART_POSTGRESQL_PASSWORD` — the `stalwartusr` CNPG role password, consumed by `config.json`'s `authSecret` (`EnvironmentVariable` type).
- `STALWART_RECOVERY_PASSWORD` / `STALWART_RECOVERY_ADMIN` (derived as `admin:<password>`) — the recovery-mode backdoor credential (see [stalw.art/docs/configuration/recovery-mode](https://stalw.art/docs/configuration/recovery-mode)), used by `job-bootstrap.yaml`'s `stalwart-cli` container and set permanently on the main server so the bootstrap Job can be safely re-run after a future plan change.
- `STALWART_ADMIN_PASSWORD` — the real mailbox admin account's password (`admin@${SECRET_DOMAIN}`), created by the bootstrap plan.
- `STALWART_CLOUDFLARE_API_TOKEN` — Zone:DNS:Edit token for the `DnsServer` object (DNS-01 challenge + record publication); a separate token from the one `cert-manager`/`cloudflare-dns` use, same permission shape.

## Routing & access
- **No Envoy Gateway, no Cloudflare Tunnel.** SMTP/IMAP/POP3/ManageSieve are raw TCP protocols that neither of this cluster's normal ingress paths can carry (both are HTTP-only). Stalwart therefore gets its own dedicated Cilium `LoadBalancer` IP (`192.168.10.104`, see `helmrelease.yaml`'s `service.app.annotations`) with every mail port plus `443` (WebUI/JMAP/CalDAV/CardDAV/WebDAV) exposed directly to `world`.
- **TLS/ACME is fully self-managed.** Stalwart runs its own ACME client against Let's Encrypt using the DNS-01 challenge (`AcmeProvider` + `DnsServer` objects, provisioned by the bootstrap plan) — independent of `cert-manager`'s `ClusterIssuer` used by every other app.
- No SSO — internal directory only (see Architecture at a glance above).
- `CiliumNetworkPolicy` (`kubernetes/apps/mail/stalwart/app/ciliumnetworkpolicy.yaml`) allows `world` ingress on every mail port + `443`, kubelet TCP probes, in-namespace access (the bootstrap Job's `stalwart-cli`), and Prometheus scraping; egress is DNS, Postgres, and `world` on `443`/`25` (ACME/DNS API/outbound mail delivery).

## Storage
No PVC. The Postgres `DataStore`/`BlobStore`/`SearchStore`/`InMemoryStore` all point at the shared CNPG `postgres` cluster (database `stalwartdb`, role `stalwartusr`) — see `kubernetes/apps/database/cloudnative-pg/databases/database-stalwart.yaml`. RocksDB's own `/var/lib/stalwart` data directory is mounted as an `emptyDir` since it's unused (the binary still expects the path to exist).

## Known quirks
- **Config.json always present ⇒ setup wizard never appears ⇒ zero listeners on a fresh DB.** Stalwart's interactive setup wizard (which normally creates the first `Domain`/`NetworkListener`/admin `Account`) only triggers when `config.json` is *absent* on first start (see [stalw.art/docs/configuration/bootstrap-mode](https://stalw.art/docs/configuration/bootstrap-mode)). Since this repo's GitOps model always ships `config.json`, the wizard is permanently skipped, and a brand-new Postgres-backed deployment starts with zero `NetworkListener` rows — meaning the *normal* server exposes no reachable management endpoint at all. Solved with the separate `stalwart-bootstrap` Kustomization: a Job runs a throwaway Stalwart instance in recovery mode (`STALWART_RECOVERY_MODE=1`, which always serves the JMAP management API on `:8080` regardless of listener config) as a native init-container sidecar, then a `stalwart-cli apply` container feeds it the declarative NDJSON plan. See `configmap-bootstrap-plan.yaml`'s header comment for the full rationale.
- **`matchOn` keys in the bootstrap plan are best-effort.** `NetworkListener.name` is server-set (read-only), so listeners are matched on `bind` instead; `AcmeProvider` has no description field, so it's matched on `contact`. Verify against a live server (`stalwart-cli describe <Object>`) if a re-apply of the plan ever behaves unexpectedly.
- **Recovery admin credential is left set permanently** on the main server (unlike Stalwart's own docs, which recommend removing `STALWART_RECOVERY_ADMIN` once initial setup is done) — a deliberate trade-off so the bootstrap Job can be re-run after a future plan change without a manual unlock step.

## Common operations
- Upgrade the Stalwart image: bump the tag in both `kubernetes/apps/mail/stalwart/app/helmrelease.yaml` (main server) and `kubernetes/apps/mail/stalwart-bootstrap/app/job-bootstrap.yaml` (bootstrap sidecar) — keep them in sync.
- Change domain/DKIM/DNS/listener config: edit `kubernetes/apps/mail/stalwart-bootstrap/app/configmap-bootstrap-plan.yaml`'s NDJSON plan and push — the `kustomize.toolkit.fluxcd.io/force: "enabled"` annotation on `job-bootstrap.yaml` makes Flux recreate the Job so the change re-applies automatically.
- Force reconcile: `flux reconcile kustomization stalwart-bootstrap -n mail --with-source` then `flux reconcile kustomization stalwart -n mail`.
- Manage day-to-day (add mailboxes, check DNS zone file, rotate DKIM, etc.): sign in to `https://mail.${SECRET_DOMAIN}/admin` with the `admin@${SECRET_DOMAIN}` account, or use `stalwart-cli` directly against the same URL.

## TODOs / unknowns
- Whether `${SECRET_MAIL_SERVER}` (used by Authentik and other apps for outbound notification mail) should eventually be repointed at this Stalwart instance's SMTP submission port, replacing whatever external mail server it currently references, has not been decided.
- No Velero backup schedule includes the `mail` namespace (no PVC to back up either — see Storage above). `stalwartdb`'s data is covered indirectly by the cluster-wide CNPG `postgres` cluster's Barman Cloud WAL archiving/backups (`kubernetes/apps/database/cloudnative-pg/cluster/cluster.yaml`, `scheduledbackup.yaml`), same as every other app's database on that shared cluster.

---
_Cite every non-obvious claim with a repo-root-relative file path (e.g. `kubernetes/apps/security/authentik/app/helmrelease.yaml`), not a bare filename — this doc lives under `docs/apps/`, so relative paths must resolve from there._
