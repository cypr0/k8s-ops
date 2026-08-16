# Proxmox Ansible

> **Namespace**  automation
> **Source**     plain manifests — no HelmRelease/HelmRepository; a `CronJob` + `ConfigMap` (`kubernetes/apps/automation/proxmox-ansible/app/`)
> **Hostname**   none — no ingress; egress-only toward the physical Proxmox host

## What it does here
A daily `CronJob` that SSHes into the physical Proxmox VE host underlying this cluster and runs an Ansible playbook to harden and maintain it: OS package updates, fail2ban, CIS-flavored `sshd_config`, syslog forwarding to Fluent-Bit/OpenSearch, and vsftpd for a Brother scanner that drops files into Paperless's NFS consume share. It's the one app in this repo that reaches *outside* Kubernetes to manage the hypervisor hosting the cluster itself.

## Architecture at a glance
- **Depends on:** ExternalSecret `proxmox-ansible-credentials` (1Password item `proxmox`); ConfigMap `proxmox-ansible-playbook` (`kubernetes/apps/automation/proxmox-ansible/app/configmap-playbook.yaml`) which embeds the entire playbook, a key-fixup script, and the run wrapper; Flux `postBuild.substituteFrom: cluster-secrets` for the `SECRET_ALLOWED_IP` value (`kubernetes/apps/automation/proxmox-ansible/ks.yaml`).
- **Depended on by:** Paperless-ngx's scan-to-consume workflow — the vsftpd account and directory ownership this playbook maintains back the NFS path Paperless mounts as its consume share (`kubernetes/apps/paperless/paperless-ngx/app/pvc.yaml`). The cluster's Fluent-Bit/OpenSearch logging pipeline receives the Proxmox host's syslog/fail2ban output because this playbook configures the forwarding (`kubernetes/apps/automation/proxmox-ansible/app/configmap-playbook.yaml`, `hardening.yml` rsyslog tasks).

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/automation/proxmox-ansible/ks.yaml` | Flux Kustomization; `postBuild.substituteFrom: cluster-secrets`, `dependsOn: external-secrets-stores` |
| `kubernetes/apps/automation/proxmox-ansible/app/cronjob.yaml` | Schedule (`15 3 * * *` UTC), image (`python:3.14-alpine`), env wiring from the ExternalSecret, node affinity (workers only) |
| `kubernetes/apps/automation/proxmox-ansible/app/externalsecret.yaml` | Credentials pulled from 1Password |
| `kubernetes/apps/automation/proxmox-ansible/app/configmap-playbook.yaml` | `run.sh` (entrypoint), `fix_key.py` (SSH key repair), `hardening.yml` (the actual Ansible playbook) |
| `kubernetes/apps/automation/proxmox-ansible/app/ciliumnetworkpolicy.yaml` | Egress-only network policy for the Job's pods |

## Secrets
One ExternalSecret, `kubernetes/apps/automation/proxmox-ansible/app/externalsecret.yaml`, pulling from 1Password item `proxmox` (no `refreshInterval` set, unlike most other apps in this repo which set `1h` — falls back to the operator default):

| Key | Consumed by |
| --- | --- |
| `PROXMOX_SSH_HOST` | `PROXMOX_SSH_HOST` env var → written into the Ansible inventory as the target host |
| `PROXMOX_ROOT_SSH_KEY` | mounted read-only at `/tmp/ssh/id_rsa`, then repaired by `fix_key.py` before use (see Known quirks) |
| `PROXMOX_ANSIBLE_SSH_PUBKEY` | `PROXMOX_ANSIBLE_SSH_PUBKEY` env var → installed as the `ansible` OS user's `authorized_keys` by the playbook |
| `PROXMOX_ANSIBLE_SSH_KEY` | The `ansible` user's own private key, matching `PROXMOX_ANSIBLE_SSH_PUBKEY` — a genuinely separate keypair from root's, mounted read-only at `/tmp/ssh/ansible_id_rsa` and repaired by `fix_key.py` the same way as root's key |
| `FTP_SCANNER_PASSWORD` | `FTP_SCANNER_PASSWORD` env var → sets the local `scanner` FTP account's password (`chpasswd`, `no_log: true`) |

A fifth value, `ALLOWED_IP`, is not from this ExternalSecret — it comes from the cluster-wide SOPS-encrypted `cluster-secrets` via Flux's `postBuild.substituteFrom` (`${SECRET_ALLOWED_IP}` in `cronjob.yaml`), and feeds fail2ban's `ignoreip` allow-list in the playbook. Never restate its resolved value — cite the `SECRET_ALLOWED_IP` key in `kubernetes/components/sops/cluster-secrets.sops.yaml`.

## Routing & access
No HTTPRoute — this app is never reached from outside, only reaches out. The CiliumNetworkPolicy (`ciliumnetworkpolicy.yaml`) scopes to the Job's own pods (`batch.kubernetes.io/job-name` exists) and allows egress only to: `kube-dns` (UDP/TCP 53), `world` on TCP 22 (SSH to the Proxmox host — comment points at `SECRET_ALLOWED_IP` rather than a literal address), and `world` on TCP 80/443 (Alpine's `apk add ansible openssh-client` at container start). No SSO/OIDC — this is a backend batch job, not a service with a UI.

## Storage
None. No PVC in this app's directory, and the `automation` namespace is not in any Velero schedule (`kubernetes/apps/velero/schedules/schedule-daily.yaml` only covers `nextcloud`, `paperless`, `open-webui`, `hermes-agent`). The playbook does manage a directory on the Proxmox host's own filesystem (`/rpool/k8s-rwx/paperless-consume`, ownership `3000:3000` to match Paperless's `USERMAP_UID`/`GID`), but that's host config applied over SSH, not a cluster-backed volume — recovery is simply re-running the (idempotent) CronJob.

## Known quirks
- **1Password collapses the PEM private key's newlines to spaces.** `fix_key.py` (`kubernetes/apps/automation/proxmox-ansible/app/configmap-playbook.yaml`) reconstructs the key's line breaks (header/footer/base64 body handled separately) before `ansible-playbook` ever runs — without it, `PROXMOX_ROOT_SSH_KEY` is unusable as an SSH key.
- **`$${VAR}` double-escaping in `run.sh` is load-bearing, not decorative.** Flux's `postBuild.substituteFrom` scans the *entire* rendered manifest text, including what ends up inside the shell script, so a bare `${PROXMOX_SSH_HOST}` there would be treated as a substitution target — and since it isn't a `cluster-secrets` key, Flux would silently replace it with an empty string. Doubling to `$${VAR}` makes Flux pass through a literal `${VAR}` for the shell to expand at runtime. `ALLOWED_IP` is the one exception: it's substituted once, deliberately, at the `cronjob.yaml` env-var level, not doubled.
- **PVE's built-in firewall is deliberately disabled** (`hardening.yml`, "Disable cluster-level PVE firewall" / "Disable node-level PVE firewall" tasks) — per its inline comment, it "caused a full lockout when allowed_ip was stale." Protection is instead layered as iptables rules in `/etc/network/interfaces`, fail2ban, and OPNsense. Anyone looking for firewall rules in the PVE UI itself will find none by design.
- **A prior playbook version blackholed host→Kubernetes traffic (including NFS) after a Proxmox reboot**, by forcing a route via OPNsense that raced the kernel's own connected route on `vmbr1` and won. `hardening.yml`'s rsyslog section carries an explicit comment not to re-add that route.
- **Root→ansible-user handoff: implemented and verified live 2026-08-16.** The `ansible` OS user was always provisioned (pubkey, passwordless sudo) but never actually connected to — `run.sh`'s inventory used to hardcode `ansible_user=root` for every task, unconditionally, on every run. Fixed across three PRs, each surfaced by actually running the playbook against the real host rather than by inspection alone:
  - **[#70](https://github.com/cypr0/k8s-ops/pull/70)** split `hardening.yml` into a root-only bootstrap play (create/repair the `ansible` user) and a second play running everything else as `ansible` via sudo — a broken `ansible` account now fails that second play loudly instead of silently falling back to root. Its first draft wrongly assumed `PROXMOX_ANSIBLE_SSH_PUBKEY` was the public half of `PROXMOX_ROOT_SSH_KEY`; live inspection of `/home/ansible/.ssh/authorized_keys` disproved that (a distinct `ansible@proxmox` ed25519 key, unrelated to either of root's own `root@proxmox` keys) — corrected in the same PR by generating a real dedicated keypair and adding its private half as `PROXMOX_ANSIBLE_SSH_KEY`.
  - **[#71](https://github.com/cypr0/k8s-ops/pull/71)** — the first live test run authenticated successfully as `ansible` but then failed with "Failed to create temporary directory": years of root-only runs had left `/tmp/.ansible/tmp` `0700` root-owned, so the unprivileged `ansible` user couldn't create its own scratch subdirectory inside it. Fixed by removing the `ANSIBLE_REMOTE_TMP` override entirely and letting Ansible fall back to its own per-user default (`~/.ansible/tmp` — separate homes for root and ansible, no shared path to collide on).
  - **[#77](https://github.com/cypr0/k8s-ops/pull/77)** — the next test run got past authentication and temp-dir creation, then failed with `sudo: not found`: this host had always been administered as root directly, so the `sudo` package itself was never installed. Fixed by adding an `apt` task to the bootstrap play.
  - A fourth manual run (`kubectl create job --from=cronjob/proxmox-ansible ...`) completed with **zero failures on both plays** (`proxmox-host-root: ok=5, changed=1, failed=0`; `proxmox-host-ansible: ok=45, changed=6, failed=0`) — package updates, fail2ban, sshd hardening, syslog forwarding, and vsftpd all applied successfully running as the unprivileged `ansible` account. `hardening.yml`'s hardened `sshd_config` still keeps `PermitRootLogin prohibit-password` for the bootstrap play's sake (its own inline CIS 5.2.10 comment) — root access is now used for exactly four tasks per run, not the whole playbook.
- **The DNAT/routing `lineinfile` tasks in `hardening.yml`** (e.g. correcting a `192.10.100.1` typo, fixing `post-down` to use `-D` not `-A`) are one-time historical fixups now re-asserted idempotently on every run, not general-purpose network config — don't assume they document the "intended" network layout from scratch.

## Common operations
- Manually trigger a run outside the schedule: `kubectl create job --from=cronjob/proxmox-ansible proxmox-ansible-manual-$(date +%s) -n automation`.
- Suspend the schedule: `kubectl patch cronjob proxmox-ansible -n automation -p '{"spec":{"suspend":true}}'`, or pause Flux entirely with `flux suspend kustomization proxmox-ansible -n flux-system`.
- Update the playbook itself: edit `hardening.yml`/`run.sh`/`fix_key.py` in `configmap-playbook.yaml`, commit, push — the ConfigMap is mounted fresh into the next Job pod, no restart mechanism needed.
- Rotate a credential: update the 1Password item `proxmox`, then `kubectl annotate externalsecret proxmox-ansible-credentials -n automation force-sync=$(date +%s)` (no `refreshInterval` is set on this ExternalSecret, so don't assume it'll pick up the change on its own within a bounded window).
- Check the last run's result: `kubectl logs -n automation job/<latest-job-name>` — `run.sh` echoes key diagnostics (Ansible version, SSH key line count, inventory host) before invoking `ansible-playbook`.

## TODOs / unknowns
- Whether `sshd_config`'s `PermitRootLogin prohibit-password` could be tightened further now that root's role is reduced to four bootstrap tasks per run — not addressed by #70/#71/#77, left as a possible follow-up now that the handoff is confirmed stable in practice.
- The old, now-orphaned `/tmp/.ansible/tmp` directory (root-owned, `0700`) left behind by years of pre-#71 runs was never cleaned up on the host — harmless (nothing references it anymore), but noted in case it's ever confusing during manual troubleshooting.
- No `refreshInterval` is set on `externalsecret.yaml`, unlike most other apps' ExternalSecrets in this repo (which explicitly set `1h`) — not confirmed whether this is intentional or an oversight.
- Whether the CIS Debian 13 `sshd_config` in `hardening.yml` has been checked against a specific CIS benchmark control list the way the cluster's own nodes have (see the `project_cis_benchmark_review` memory) — no cross-reference found in this repo.
