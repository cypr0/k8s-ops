# GitLab

> **Namespace**  `gitlab`
> **Source**     `app-template` chart v5.3.0 via `OCIRepository` `oci://ghcr.io/bjw-s-labs/helm/app-template` (`kubernetes/apps/gitlab/gitlab/app/ocirepository.yaml`, `helmrelease.yaml`), image `docker.io/gitlab/gitlab-ce:19.5.1-ce.0` (Omnibus)
> **Hostname**   `gitlab.${SECRET_DOMAIN}` (HTTPS) and `gitlab-ssh.${SECRET_DOMAIN}` (Git over SSH, `192.168.10.112`). Internal only: the `HTTPRoute` attaches to `envoy-internal` alone, nothing in `envoy-external` or cloudflare-tunnel.

## What it does here
Single-user GitLab CE for IaC and compliance-as-code that must not leave the LAN. One Omnibus pod runs Puma, Workhorse, Sidekiq, Gitaly, gitlab-shell/sshd and an embedded Redis. PostgreSQL is **not** bundled: it is a database on the shared CNPG cluster. Login is OIDC via Authentik. CI runs on [gitlab-runner](gitlab-runner.md).

## Architecture at a glance
- **Depends on:** CNPG cluster `postgres` (database `gitlabdb`, role `gitlabusr`), Authentik (OIDC), ExternalSecret → 1Password item `gitlab`, `zfs-nfs` StorageClass, envoy-internal.
- **Depended on by:** [gitlab-runner](gitlab-runner.md) (polls the API, clones over the Service), the Grafana dashboard `GitLab`, Gatus.
- **Not used:** Dragonfly (GitLab does not support it as Redis), bundled nginx, Let's Encrypt, KAS, container registry, bundled Prometheus.

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/gitlab/gitlab/app/helmrelease.yaml` | Pod: root, `drop: ALL` plus the capabilities Omnibus needs, exec probes on loopback, 3Gi request / 6Gi limit |
| `kubernetes/apps/gitlab/gitlab/app/externalsecret.yaml` | Renders the whole `gitlab.rb` (DB, OIDC, tuning) into Secret `gitlab-config`, mounted at `/etc/gitlab-config/gitlab.rb` and loaded through `GITLAB_OMNIBUS_CONFIG=from_file(...)` |
| `kubernetes/apps/gitlab/gitlab/app/pvc.yaml` | `gitlab-data` 30Gi RWX, subPaths `config` → `/etc/gitlab`, `data` → `/var/opt/gitlab` |
| `kubernetes/apps/gitlab/gitlab/app/httproute.yaml` | `envoy-internal` only; Envoy overwrites `X-Forwarded-For` |
| `kubernetes/apps/gitlab/gitlab/app/service-ssh.yaml` | LoadBalancer `192.168.10.112:22`, `coredns.io/hostname` for the internal A record |
| `kubernetes/apps/gitlab/gitlab/app/podmonitor.yaml` | Four exporters: Rails `/-/metrics` :8181, Workhorse :9229, Sidekiq :8082, Gitaly :9236 |
| `kubernetes/apps/gitlab/gitlab/app/ciliumnetworkpolicy.yaml` | Ingress: Envoy, runner namespace, Gatus, Prometheus, SSH from LAN CIDRs. Egress: DNS, CNPG :5432, Envoy :10443 (OIDC), world :443 |
| `kubernetes/apps/database/cloudnative-pg/databases/{database,externalsecret}-gitlab.yaml`, `cluster/cluster.yaml` | Database (extensions `pg_trgm`, `btree_gist`, `amcheck`), role `gitlabusr` and its password |
| `kubernetes/apps/security/authentik/app/blueprints/11-gitlab-oidc.yaml`, `externalsecret-gitlab-oidc.yaml` | OIDC provider/application, group `gitlab-users` |
| `kubernetes/apps/monitoring/grafana/dashboards/apps/gitlab.json` | Dashboard `GitLab` in the folder "Anwendungen" |

## Secrets
One 1Password item `gitlab` feeds four ExternalSecrets.

| Field | Used by |
| --- | --- |
| `GITLAB_DBPASS` | `gitlab-db-role` (CNPG role) and `gitlab-config` (`db_password`) |
| `GITLAB_ROOT_PASSWORD` | `gitlab-config` (`initial_root_password`) |
| `GITLAB_OIDC_CLIENT_ID`, `GITLAB_OIDC_CLIENT_SECRET` | `gitlab-config` (omniauth provider) and `authentik-gitlab-oidc` (Authentik blueprint, `optional: true` in the Authentik pods) |
| `GITLAB_RUNNER_TOKEN` | `gitlab-runner-secret` in [gitlab-runner](gitlab-runner.md) |

Values go into single-quoted Ruby strings: use alphanumeric values without quotes or backslashes.

## Routing & access
- **Gateway:** `envoy-internal`, `sectionName: https`. `externalTrafficPolicy: Local` on the SSH Service with its own IP, same reasoning as `kubernetes/apps/mail/mailu/app/service-front-webmail-lan.yaml`.
- **TLS:** ends at Envoy. Workhorse listens on plain `0.0.0.0:8181`; `nginx['enable'] = false`, `letsencrypt['enable'] = false`.
- **SSO:** OIDC via Authentik, issuer `https://id.${SECRET_DOMAIN}/application/o/gitlab/`, blueprint `11-gitlab-oidc.yaml`. Only members of the Authentik group `gitlab-users` pass the application policy; membership is not managed in git.
- **Metrics exposure:** `/-/metrics` is reachable through the route but answers 404 to LAN clients: the whitelist is matched against the client IP that Rails derives from `X-Forwarded-For` via the trusted Envoy, so only in-cluster scrapers (pod CIDR `10.42.0.0/16`) get through. Checked on a trial instance with a spoofed LAN `X-Forwarded-For`.
- **CiliumNetworkPolicy:** the Envoy side needs matching entries (`kubernetes/apps/network/envoy-gateway/app/ciliumnetworkpolicy.yaml`: OIDC ingress from `gitlab`, backend egress to :8181), and each Prometheus port must be listed in `kubernetes/apps/monitoring/kube-prometheus-stack/app/ciliumnetworkpolicy.yaml`, otherwise the targets silently time out (`docs/incidents/2026-09-20-cilium-cnp-scrape-target-drop.md`).

## Storage
- `gitlab-data` (`zfs-nfs`, RWX, 30Gi): repositories (Gitaly), uploads, artifacts, embedded Redis, `gitlab-secrets.json`, SSH host keys. Logs (`/var/log/gitlab`) and `/dev/shm` are `emptyDir`s.
- Backup: namespace `gitlab` is in the Velero daily/weekly/monthly schedules (`kubernetes/apps/velero/schedules/`). The database is backed up separately by Barman via CNPG. The two are not captured at the same instant; for a restore, take the PVC and the database from the closest points in time.
- **`gitlab-secrets.json` is the part you cannot recreate.** It holds the keys that encrypt CI variables, runner tokens and 2FA secrets in the database. Keep a copy outside the cluster.
- GitLab advises against NFS for Gitaly. On a trial instance with the same StorageClass, repository creation, HTTP and SSH clone/push, hooks, Unix sockets and CI worked. Watch the "Git command time" panel if it gets slow.

## Known quirks
- **Runs as root, deliberately.** Omnibus (runit, chef, sshd) starts as root and drops to its service users. Expect audit hits from `pod-security-baseline`, `pod-security-restricted` and `require-security-context`. The container does run with `allowPrivilegeEscalation: false` and `drop: ALL`, adding back only `CHOWN, DAC_OVERRIDE, FOWNER, FSETID, KILL, SETGID, SETUID, SETPCAP, NET_BIND_SERVICE, SYS_CHROOT, AUDIT_WRITE` (verified on the trial instance). `ulimit`/`file-max` warnings at startup are harmless.
- **The initial root password must pass GitLab's password policy** (a long random alphanumeric string does; word-like passwords do not). If it is rejected, the seed aborts after the schema is loaded, there is no root user, and the CI job token signing key is never written (jobs then fail with `CI job token signing key is not set`, HTTP 500 on trace upload). Recovery: set a valid password, create the admin with `gitlab-rails runner`, then run `gitlab-rake db:seed_fu`. Dropping and recreating `gitlabdb` before the first start is the cleaner route.
- **First start takes 5-10 minutes** (reconfigure plus schema load). The startup probe allows 30 minutes; the Flux Kustomization timeout is 30m.
- **Schema load needs `max_locks_per_transaction`** ≥ 64 with `max_connections` ≥ 200. The cluster has 200 connections and the default 64, which was enough on a trial; with 100 connections it failed with `out of shared memory`. Do not lower `max_connections` on the CNPG cluster without re-checking this.
- **Redis is embedded**, with data on the NFS volume. Dragonfly is not supported by GitLab.
- **Sign-up is not disabled declaratively.** After the first login switch it off under Admin → Settings → General → Sign-up restrictions. Authentik's group policy still gates SSO.
- **E-mail is off** (`gitlab_email_enabled = false`); wire it to Mailu if notifications are wanted.
- **Version policy:** Renovate will propose new `gitlab-ce` tags. GitLab does not allow skipping required upgrade stops; check the upgrade path before merging a major or minor jump, and keep the [runner](gitlab-runner.md) on the same minor.

## Common operations
- **First deployment order:** (1) create the 1Password item `gitlab` with the fields above (generate `GITLAB_ROOT_PASSWORD` and `GITLAB_DBPASS` as random alphanumerics; client ID/secret are free to choose, they are shared with Authentik); (2) merge, Flux creates DB, Authentik provider and the app; (3) in Authentik add your user to group `gitlab-users`; (4) sign in as `root` with the initial password at `/users/sign_in?auto_sign_in=false`, change the root e-mail to your Authentik e-mail, then sign in with the "Authentik" button, which auto-links to root; (5) create the runner (see [gitlab-runner](gitlab-runner.md)).
- **SSH:** add your key in GitLab, clone with `ssh://git@gitlab-ssh.${SECRET_DOMAIN}/<group>/<project>.git`.
- **Upgrade:** edit the image tag in `helmrelease.yaml`, commit, push, or `flux reconcile helmrelease gitlab -n gitlab`.
- **Rotate a secret:** update the 1Password item, then `kubectl annotate externalsecret <name> -n gitlab force-sync=$(date +%s)`; Reloader restarts the pod on `gitlab-config` changes. Rotating `GITLAB_DBPASS` also needs the `gitlab-db-role` ExternalSecret in `database`.
- **Pause reconciliation:** `flux suspend kustomization gitlab -n gitlab` / `flux suspend helmrelease gitlab -n gitlab`.

## TODOs / unknowns
- The OIDC login itself (Authentik provider, redirect, auto-link) is not covered by the trial: it ran with dummy client credentials, only the presence of the login button was checked.
- Not verified under real load or with the production Kyverno/Cilium policies in place (the trial namespace had none); the CNP is modelled on the existing apps.
- Alerts (PrometheusRule) are not defined; the dashboard and the Gatus check are the only monitoring.
