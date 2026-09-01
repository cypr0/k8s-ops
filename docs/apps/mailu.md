# Mailu

> **Namespace**  mail
> **Source**     Helm chart `mailu/mailu` v2.7.3 (app version `2024.06.55`) from `https://mailu.github.io/helm-charts/` — `kubernetes/apps/mail/mailu/app/helmrepository.yaml`, `helmrelease.yaml`; dedicated Redis via `bjw-s-labs/helm/app-template` — `helmrelease-redis.yaml`
> **Hostname**   `mail.${SECRET_DOMAIN}` (mail protocols, dedicated LoadBalancer IP) + `webmail.${SECRET_DOMAIN}` (webmail/admin, via the Cloudflare Tunnel) — see Routing & access below

## What it does here

[Mailu](https://mailu.io) is the cluster's mail server, replacing Stalwart (see git history / this file's previous revision for that deployment). It's a multi-component stack — `front` (nginx, the single public entrypoint for all mail protocols + HTTPS), `admin` (Flask app: domains/mailboxes/aliases/DKIM, its own database), `postfix` (SMTP), `dovecot` (IMAP/POP3/ManageSieve), `rspamd` (spam/DKIM-signing/DMARC/greylisting), `clamav` (antivirus), and `webmail` (Roundcube) — all deployed from one Helm release.

Unlike Stalwart, **domains/mailboxes/aliases are not infra-as-code**: there is no declarative-apply layer for mailbox content. They're managed through the Admin web UI (`https://mail.${SECRET_DOMAIN}/admin`) or `flask mailu ...` CLI inside the `admin` pod, after this platform-level deployment is up.

## Architecture at a glance

- **Depends on:** CNPG cluster `postgres` (namespace `database`) via two roles/databases — `mailuusr`/`mailudb` for the `admin` component, `roundcubeusr`/`roundcubedb` for Roundcube's own address-book/preferences store (`kubernetes/apps/database/cloudnative-pg/databases/database-mailu.yaml`, `database-roundcube.yaml`); a dedicated in-namespace Redis (`helmrelease-redis.yaml`) for rspamd greylisting/admin quota+ratelimit counters; `cert-manager`'s `letsencrypt-production` `ClusterIssuer` for TLS (`certificate.yaml`); ExternalSecret → 1Password item `mailu`.
- **Depended on by:** nothing else in the cluster. Authentik's own outbound mail (`kubernetes/apps/security/authentik/app/helmrelease.yaml`'s `authentik.email.host: ${SECRET_MAIL_SERVER}`) points at a *different*, pre-existing mail server, not at this Mailu instance — same as it did with Stalwart.
- **Not integrated with Authentik SSO** — Mailu has no OIDC-backed mail-client login path in general use (mainstream IMAP/SMTP clients don't do browser-redirect auth), so this uses Mailu's own internal accounts, same reasoning as Stalwart before it.
- **Not given a Cloudflare API token.** Deliberate: unlike Stalwart, Mailu's Helm chart has no DNS-provider integration to hand a token to in the first place, and DKIM/MX/SPF/DMARC record publication for each domain is a manual step (see DNS runbook below) — a smaller blast radius if this app is ever compromised.

## Repo layout

| File | Purpose |
| --- | --- |
| `kubernetes/apps/mail/mailu/ks.yaml` | Flux Kustomization — `dependsOn` `cloudnative-pg-databases`, `csi-driver-nfs`, `external-secrets-stores` |
| `kubernetes/apps/mail/mailu/app/helmrepository.yaml` | Classic Helm `HelmRepository` pointing at `https://mailu.github.io/helm-charts/` |
| `kubernetes/apps/mail/mailu/app/helmrelease.yaml` | The Mailu chart itself — hostnames, external DB/Redis wiring, TLS, front's LoadBalancer Service, per-component resources |
| `kubernetes/apps/mail/mailu/app/helmrelease-redis.yaml` | Dedicated unauthenticated Redis (`app-template` chart) for rspamd/admin |
| `kubernetes/apps/mail/mailu/app/externalsecret.yaml` | `mailu-secret` — Flask secret key, initial admin password, both CNPG role passwords |
| `kubernetes/apps/mail/mailu/app/certificate.yaml` | `cert-manager` `Certificate` for `mail.${SECRET_DOMAIN}` + `webmail.${SECRET_DOMAIN}` → `mailu-tls` Secret |
| `kubernetes/apps/mail/mailu/app/dnsendpoint.yaml` | Public DNS record for `webmail.${SECRET_DOMAIN}` (CNAME straight to the Cloudflare Tunnel) |
| `kubernetes/apps/mail/mailu/app/service-front-webmail-lan.yaml` | LAN-only path to `webmail.${SECRET_DOMAIN}` — second `LoadBalancer` Service on its own dedicated IP (`192.168.10.105`), carries the `coredns.io/hostname` annotation `k8s-gateway` needs to generate an internal DNS record |
| `kubernetes/apps/mail/mailu/app/ciliumnetworkpolicy.yaml` | Ingress from `world` on the enabled mail ports, from the `cloudflare-tunnel` pod (namespace `network`) on `443`, from `192.168.10.0/24`, `192.168.40.0/24`, and `10.10.0.2/32` (LAN) also on `443`; same-namespace trust for the chart's internal component-to-component traffic; egress to Postgres, DNS, and `world` (outbound SMTP delivery + ClamAV virus-DB updates) |
| `kubernetes/apps/mail/mailu/app/ciliumnetworkpolicy-redis.yaml` | Locks the unauthenticated Redis to same-namespace ingress only, zero egress |

## Secrets

One ExternalSecret (`mailu`, in `kubernetes/apps/mail/mailu/app/externalsecret.yaml`), pulling from 1Password item `mailu`, producing the `mailu-secret` Kubernetes Secret with:

- `secret-key` — Flask session-cookie signing key (`MAILU_SECRET_KEY` in 1Password).
- `initial-admin-password` — the `admin@${SECRET_DOMAIN}` mailbox's password, created once (`initialAccount.mode: ifmissing`, so a later password change via the UI survives reconciles).
- `mailu-db-database` / `mailu-db-username` / `mailu-db-password` — the `admin` component's CNPG role (`mailuusr`/`mailudb`); password sourced from `MAILU_POSTGRESQL_PASSWORD`.
- `roundcube-db-password` — Roundcube's CNPG role (`roundcubeusr`/`roundcubedb`); sourced from `ROUNDCUBE_POSTGRESQL_PASSWORD`.

The matching CNPG role passwords in the `database` namespace (`mailu-db-role`, `roundcube-db-role` Secrets) are separate ExternalSecrets reading the *same* 1Password item — see `kubernetes/apps/database/cloudnative-pg/databases/externalsecret-mailu.yaml` / `externalsecret-roundcube.yaml`. Required 1Password fields: `MAILU_SECRET_KEY`, `MAILU_INITIAL_ADMIN_PASSWORD`, `MAILU_POSTGRESQL_PASSWORD`, `ROUNDCUBE_POSTGRESQL_PASSWORD`.

## Routing & access

Two separate public paths, split by protocol — unlike Stalwart, which put everything (mail ports + webmail/admin) on one dedicated LoadBalancer IP:

- **Raw mail protocols (SMTP/IMAPS/Submission/SMTPS/ManageSieve) — dedicated LoadBalancer, no Envoy/Tunnel.** These aren't HTTP, so they can't go through Envoy Gateway or the Cloudflare Tunnel. `front` gets its own dedicated Cilium `LoadBalancer` IP (`192.168.10.104`, reused from Stalwart's removal — see `helmrelease.yaml`'s `front.externalService.annotations`), `externalTrafficPolicy: Local` (preserves real client source IPs for rspamd/postfix rate-limiting/RBL — an improvement over Stalwart's `Cluster` policy). Exposed ports are narrower than Stalwart's: `smtp` (25), `submission` (587), `smtps` (465), `imaps` (993), `manageSieve` (4190) — plaintext-capable `pop3`/`pop3s`/`imap` (110/995/143) are deliberately left disabled. `mail.${SECRET_DOMAIN}` has a plain (non-Cloudflare-proxied) DNS `A` record pointing at this cluster's public IP — required for the MX record, since Cloudflare's proxy can't carry raw SMTP.
- **Webmail/admin UI (443/HTTPS) — two parallel paths**, both terminating at the same `front` pod, on a *separate* hostname (`webmail.${SECRET_DOMAIN}`):
  - **From the public internet: the Cloudflare Tunnel**, exactly like every other web app in this cluster. `kubernetes/apps/network/cloudflare-tunnel/app/helmrelease.yaml`'s `config.yaml` routes that hostname straight to `mailu-front.mail.svc.cluster.local:443` (ahead of the `*.${SECRET_DOMAIN}` catch-all, which would otherwise send it to `envoy-external` — cloudflared matches top-to-bottom). Gets Cloudflare's DDoS/WAF protection in front of the admin UI and webmail login, and keeps this cluster's public IP out of DNS for that hostname. DNS record via `dnsendpoint.yaml` (see below).
  - **From the home/office LAN: directly**, via `service-front-webmail-lan.yaml` — a second `LoadBalancer` Service with its **own dedicated IP** (`192.168.10.105`, deliberately *not* shared with `mailu-front-ext`'s `192.168.10.104` — see that file's comment: Cilium's L2-announcement leader election runs independently per Service, so two Services sharing one VIP can end up announced by two different nodes at once, a genuine Layer-2 ARP conflict; confirmed live when an earlier version of this shared one IP via `io.cilium/lb-ipam-sharing-key`, and a previously-working port started failing right after the shared lease moved between nodes). Carries the `coredns.io/hostname: webmail.${SECRET_DOMAIN}` annotation so `k8s-gateway` (the in-cluster authoritative resolver for `${SECRET_DOMAIN}`, used by the LAN's split-DNS setup) generates an internal record for it — the way it already does for `mail.${SECRET_DOMAIN}`. Without this, LAN clients get `NXDOMAIN` for `webmail.${SECRET_DOMAIN}`: it otherwise has no Gateway API `HTTPRoute` or annotated Service anywhere for `k8s-gateway` to derive a record from. `ciliumnetworkpolicy.yaml` restricts this path's ingress to `192.168.10.0/24` (the Proxmox/VM LAN this cluster's nodes live on), `192.168.40.0/24` (the office LAN's own range), and `10.10.0.2/32` — not `world`, since nothing else stops that IP from being reachable from anywhere routable to `192.168.10.0/24`. `10.10.0.2/32` is the WireGuard tunnel interface's own address on the OPNsense side; OPNsense previously masqueraded office-LAN clients' traffic to it before entering the site-to-site tunnel into the Proxmox network (confirmed live via `cilium monitor` at the time — connections arrived as `10.10.0.2`, not `192.168.40.x`). That masquerade no longer happens: re-confirmed live via `hubble observe --ip 192.168.10.105` on 2026-09-01, office-LAN connections now arrive with their real `192.168.40.x` source, which is why that CIDR was added alongside `10.10.0.2/32` rather than replacing it. If OPNsense's WireGuard/NAT setup changes again, re-verify with hubble before assuming either form still applies.
  - Separately, `kubernetes/apps/kube-system/cilium/app/networks.yaml`'s cluster-wide `CiliumL2AnnouncementPolicy` excludes control-plane nodes from L2-announcement eligibility — they're `NoSchedule`-tainted and can never host the actual backend pod, so a control-plane node winning a Service's announcement lease silently blackholes all traffic to it (confirmed live, not Mailu-specific).
  - Both paths avoided routing through Envoy Gateway entirely: `front` terminates TLS itself (own cert, not Envoy's), and Envoy's `HTTPRoute` model expects plain-HTTP backends — proxying to an HTTPS backend needs an extra `BackendTLSPolicy`, not worth it for one hostname when a second Service is simpler.
- **TLS is cert-manager's, not Mailu's, for both paths.** One `Certificate` (`certificate.yaml`) with both hostnames as SANs (`mail.${SECRET_DOMAIN}`, `webmail.${SECRET_DOMAIN}`) issues via the existing `letsencrypt-production` `ClusterIssuer` (DNS-01/Cloudflare); `front` mounts that Secret directly via `ingress.existingSecret` + `ingress.tlsFlavorOverride: mail` — no chart-managed `Ingress` object, no ACME logic inside Mailu at all. Both hostnames are also listed in `helmrelease.yaml`'s `hostnames` so `front`'s nginx actually serves both Host headers.
- No SSO — internal directory only (see Architecture at a glance above).

## Public DNS / router configuration required

Since `mail.${SECRET_DOMAIN}` bypasses Cloudflare's proxy (see above), this app — unlike everything else in the cluster — needs manual router-level port forwarding: WAN ports `25`/`465`/`587`/`993`/`4190` → `192.168.10.104` on whatever edge router/firewall sits in front of this cluster (e.g. OPNsense's Destination NAT, one rule per port, same target port on both sides, with "Add associated filter rule" left on). Port `443` does **not** need forwarding — that's the Cloudflare Tunnel path. Outbound port 25 may also be blocked by the hosting/ISP provider (Hetzner does this by default for abuse prevention) — needs a support ticket to lift, or (not yet configured here) Mailu's `externalRelay` values block routing outbound mail through an authenticated third-party SMTP relay on port 587 instead.

## Storage

Single `ReadWriteMany` PVC (`persistence.single_pvc: true`, `zfs-nfs` StorageClass, 20Gi) shared via subPaths across postfix/dovecot/admin/rspamd/clamav/webmail — this is the chart's own default architecture, just with RWX instead of the chart's default RWO. The chart's default RWO would node-lock every Mailu pod to whichever node first claims the PVC; `zfs-nfs` already serves RWX to several other apps in this cluster (Nextcloud, Immich, Paperless, Open-WebUI), so this avoids that node-pinning without any affinity configuration.

## Known limitations (accepted trade-offs, not bugs)

- **No fail2ban-equivalent IP banning.** Containers can't touch host `iptables`. Brute-force mitigation is rspamd's rate-limiting/greylisting plus `limits.authRatelimit`/`limits.messageRatelimit` in `helmrelease.yaml` (chart defaults, not overridden — 60 auth attempts/hour/IP, 100/day/user, 200 messages/day/sender).
- **MTA-STS / DANE (TLSA) / TLS-RPT are not configured.** `${SECRET_DOMAIN}` (`cisotop.de`) is already DNSSEC-signed (verified live: `DNSKEY`/`DS` present, validating resolvers return the `AD` flag), which makes DANE feasible, but publishing `TLSA`/MTA-STS DNS records is additional manual work best done as a follow-up once base mail flow is confirmed working. MTA-STS specifically has a ready-made hook: `front.overrides` in `helmrelease.yaml` can inject an nginx location block serving `/.well-known/mta-sts.txt` (see the chart's own `values.yaml` comment for the exact snippet) — nothing else needs to change.
- **DNSSEC-validation pre-flight risk.** Mailu's `admin` component has been known (Mailu/Mailu#147) to refuse to start if it can't confirm DNSSEC validation through its configured DNS resolver, and this chart bundles no validating resolver of its own (unlike Mailu's docker-compose bundle, which ships an Unbound sidecar). This cluster's CoreDNS just forwards upstream without validating. If `admin`'s logs show a DNSSEC-validation failure on first deploy, `admin.dnsConfig` in `helmrelease.yaml` (currently unset) is the fix — point it at a validating resolver (e.g. `1.1.1.1`).

## DNS runbook (manual — not infra-as-code)

For each of the 4-5 domains Mailu will serve mail for, after creating the `Domain` object in the Admin UI:

1. **MX record**: `<domain> MX 10 mail.${SECRET_DOMAIN}`.
2. **SPF** (TXT on `<domain>`): `v=spf1 mx ~all` (adjust if any other systems send as that domain).
3. **DKIM**: Admin UI → Domain → DKIM shows the generated public key/selector — publish the TXT record it gives you at `<selector>._domainkey.<domain>`. Mailu generates and stores the private key itself; nothing publishes this automatically (see Architecture at a glance above — no Cloudflare token is given to Mailu, unlike Stalwart).
4. **DMARC** (TXT at `_dmarc.<domain>`): start permissive, e.g. `v=DMARC1; p=none; rua=mailto:postmaster@${SECRET_DOMAIN}`, tighten to `p=quarantine`/`p=reject` once SPF/DKIM alignment is confirmed working across all sending paths.
5. **PTR record** for `192.168.10.104`'s public-facing IP, matching `mail.${SECRET_DOMAIN}` — needed for deliverability regardless of how many domains route through this one server.

## Common operations

- Force reconcile: `flux reconcile kustomization mailu -n mail --with-source`.
- Add a domain/mailbox/alias: Admin UI (`https://mail.${SECRET_DOMAIN}/admin`) with the `admin@${SECRET_DOMAIN}` account, or `kubectl exec` into the `admin` pod and use `flask mailu ...`.
- Upgrade the chart: bump `spec.chart.spec.version` in `helmrelease.yaml`.

## TODOs / unknowns

- Whether `${SECRET_MAIL_SERVER}` (used by Authentik and other apps for outbound notification mail) should eventually be repointed at this Mailu instance's SMTP submission port has not been decided — inherited unresolved from the Stalwart deployment.
- `mail` is now in Velero's GFS schedules (`kubernetes/apps/velero/schedules/schedule-{daily,weekly,monthly}.yaml`), so the `zfs-nfs` PVC (mailbox Maildir data) gets kopia fs-backup like every other app's volume. The two Postgres databases (`mailudb`, `roundcubedb`) remain covered separately by the cluster-wide CNPG `postgres` cluster's Barman Cloud WAL archiving. `mailu-redis`'s `tmp` emptyDir (disposable greylisting/quota counters) is excluded via `backup.velero.io/backup-volumes-excludes` in `helmrelease-redis.yaml`. Not yet exercised by an actual restore-test run or verified against Velero's node-agent on this namespace's RWX PVC — worth confirming on the first `mail-restore-test` cycle.
- MTA-STS/DANE/TLS-RPT rollout (see Known limitations above) — deferred, not scheduled.

---
_Cite every non-obvious claim with a repo-root-relative file path (e.g. `kubernetes/apps/security/authentik/app/helmrelease.yaml`), not a bare filename — this doc lives under `docs/apps/`, so relative paths must resolve from there._
