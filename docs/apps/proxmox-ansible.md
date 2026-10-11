# Proxmox Ansible

> **Namespace**  automation
> **Source**     plain manifests — no HelmRelease/HelmRepository; a `CronJob` + `ConfigMap` (`kubernetes/apps/automation/proxmox-ansible/app/`)
> **Hostname**   none — no ingress; egress-only toward the physical Proxmox host

## What it does here
A daily `CronJob` that SSHes into the physical Proxmox VE host underlying this cluster and runs an Ansible playbook to harden and maintain it: OS package updates, fail2ban, CIS-flavored `sshd_config`, a Wazuh agent reporting to the in-cluster manager, auditd with command and credential-file auditing for that agent, and vsftpd for a Brother scanner that drops files into Paperless's NFS consume share. A staged CIS Debian 13 rollout runs after all of that ([CIS hardening](#cis-hardening-debian-13-benchmark)). It's the one app in this repo that reaches *outside* Kubernetes to manage the hypervisor hosting the cluster itself.

## Architecture at a glance
- **Depends on:** ExternalSecret `proxmox-ansible-credentials` (1Password item `proxmox`); ConfigMap `proxmox-ansible-playbook` (`kubernetes/apps/automation/proxmox-ansible/app/configmap-playbook.yaml`) which embeds the entire playbook, a key-fixup script, and the run wrapper; Flux `postBuild.substituteFrom: cluster-secrets` for the `SECRET_ALLOWED_IP` value (`kubernetes/apps/automation/proxmox-ansible/ks.yaml`).
- **Depended on by:** Paperless-ngx's scan-to-consume workflow — the vsftpd account and directory ownership this playbook maintains back the NFS path Paperless mounts as its consume share (`kubernetes/apps/paperless/paperless-ngx/app/pvc.yaml`). Wazuh's visibility into the hypervisor also comes from here: the playbook installs the agent, pins it to the manager's version, and registers it as `proxmox.${SECRET_DOMAIN}` (`kubernetes/apps/automation/proxmox-ansible/app/configmap-playbook.yaml`). It previously forwarded raw syslog to Fluent Bit instead; that was removed on 2026-09-20 once the agent shipped the same events decoded — rsyslog itself stays installed, because it writes the `/var/log/syslog` and `/var/log/auth.log` files the agent tails.

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/automation/proxmox-ansible/ks.yaml` | Flux Kustomization; `postBuild.substituteFrom: cluster-secrets`, `dependsOn: external-secrets-stores` |
| `kubernetes/apps/automation/proxmox-ansible/app/cronjob.yaml` | Schedule (`15 3 * * *` UTC), image (`python:3.14-alpine`), env wiring from the ExternalSecret, node affinity (workers only) |
| `kubernetes/apps/automation/proxmox-ansible/app/externalsecret.yaml` | Credentials pulled from 1Password |
| `kubernetes/apps/automation/proxmox-ansible/app/configmap-playbook.yaml` | `run.sh` (entrypoint), `fix_key.py` (SSH key repair), `hardening.yml` (the actual Ansible playbook), `cis.yml` (CIS play, imported at the end of `hardening.yml`), `cis_wave1.yml` (wave 1 tasks, included by `cis.yml`) |
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
- Dry run (reports what would change, touches nothing): same, but with `ANSIBLE_CHECK=1` added to the Job's environment, which makes `run.sh` pass `--check --diff`: `kubectl create job --from=cronjob/proxmox-ansible proxmox-ansible-dryrun-$(date +%s) -n automation --dry-run=client -o json | jq '.spec.template.spec.containers[0].env += [{"name":"ANSIBLE_CHECK","value":"1"}]' | kubectl apply -f -`. Check mode skips `command`/`shell` tasks, so it covers the file, copy, lineinfile and apt changes but not what the shell tasks do (the PVE firewall files, `agent-auth`, `chpasswd`); the read-only commands whose output later tasks read carry `check_mode: false` so the dry run does not trip over its own skips.
- Give `phiros-cmd` its password (once, after the first run that created the account): `ssh root@<host> passwd phiros-cmd`. The playbook creates the account locked and never manages the password; it is only used by `sudo`, SSH accepts keys only.
- Suspend the schedule: `kubectl patch cronjob proxmox-ansible -n automation -p '{"spec":{"suspend":true}}'`, or pause Flux entirely with `flux suspend kustomization proxmox-ansible -n flux-system`.
- Update the playbook itself: edit `hardening.yml`/`run.sh`/`fix_key.py` in `configmap-playbook.yaml`, commit, push — the ConfigMap is mounted fresh into the next Job pod, no restart mechanism needed.
- Rotate a credential: update the 1Password item `proxmox`, then `kubectl annotate externalsecret proxmox-ansible-credentials -n automation force-sync=$(date +%s)` (no `refreshInterval` is set on this ExternalSecret, so don't assume it'll pick up the change on its own within a bounded window).
- Check the last run's result: `kubectl logs -n automation job/<latest-job-name>` — `run.sh` echoes key diagnostics (Ansible version, SSH key line count, inventory host) before invoking `ansible-playbook`.

## CIS hardening (Debian 13 benchmark)
Staged, so that each step can be checked before the next one is applied. `cis.yml` (a play for the `ansible` connection, imported at the end of `hardening.yml`) holds the wave switch `cis_waves` and the admin account; each wave is its own task file. It runs only after the maintenance play succeeded, so a problem in a wave cannot stop the nightly updates, and the Job still fails loudly (visible in `kubectl get jobs`).

**Where it comes from and how it is scored.** The control selection follows the `ansible-debian-cis` role (`roles/cis_debian13`, Nextcloud *Repos* folder), but that role is **not run** here: on a hypervisor several of its remediations would break the host (UFW's forward-deny against the iptables NAT in `/etc/network/interfaces`, removing NFS/FTP, an sshd drop-in that this host's own `sshd_config` would ignore, wholesale rewrites of `/etc/pam.d/common-*`). Each wave is a hand-picked, Proxmox-adapted port. The scorer is the **Wazuh SCA policy `cis_debian13`** on the agent `proxmox.${SECRET_DOMAIN}`, not Ansible: it is independent of what the playbook believes it did. Control numbers in the playbook comments are the Wazuh policy's; the role's differ in places (e.g. `PermitRootLogin` is 5.1.20 in Wazuh, 5.1.21 in the role).

Baseline before any wave, scan of 2026-10-11 03:16 UTC: **91 passed, 102 failed, 14 not applicable (47 %)**. Expect the score to move only after the agent's next SCA scan.

| Wave | Content | Status |
| --- | --- | --- |
| 1 | Cron permissions (2.4.1.2-2.4.1.7), `opasswd`/`opasswd.old` (7.1.10), telnet client removal (2.2.4), sudo `logfile` and `timestamp_timeout` (5.2.3, 5.2.6), `su` restricted via `pam_wheel` (5.2.7), no `/etc/motd` and a clean `/etc/issue.net` (1.6.1, 1.6.3), chrony user (2.3.3.2), sshd `DisableForwarding` (5.1.8, in `hardening.yml`). Touches none of authentication, SSH access, PAM or the network. Expected to flip 15 checks. | written, tested on Debian 13 in a container, **not yet applied** |
| 2 | auditd rules and AIDE restricted to configuration paths | not written |
| 3 | SSH `AllowUsers`, PAM (`faillock`, `pwquality`, `pwhistory`), password aging for new accounts | not written |

**Rule for every wave:** read the SCA result of the previous one first; run the dry run (see Common operations), then one manual real run, then let the nightly schedule have it.

### Accepted exceptions (decided 2026-10-11)
| Control (Wazuh) | Why it stays failed |
| --- | --- |
| Firewall 4.2-4.4 (ufw / nftables / iptables policy and base chains) | The PVE firewall is off on purpose (a stale `ALLOWED_IP` once caused a full lockout) and the packet filter is the iptables rules in `/etc/network/interfaces` plus fail2ban and OPNsense. A second filter such as UFW, whose forward policy is deny, would cut the NAT path to OPNsense. |
| 5.1.20 `PermitRootLogin no` | `prohibit-password` is kept: the bootstrap play connects as root by key to repair the `ansible` user, which is the only way back if that account breaks. Revisit once the root handoff has been stable for a few weeks. |
| 5.2.4 sudo `NOPASSWD` | The `ansible` automation user needs it. Compensating controls: key-only SSH, no password set, every command audited. `phiros-cmd` has no `NOPASSWD`. |

### Proposed exceptions (not yet decided)
| Control | Reason to skip |
| --- | --- |
| 1.1.2.x separate partitions and mount options | ZFS root; the role's bind-mount remediation writes `fstab` and has already turned an NFS `/home` into an empty local directory on another host. |
| 1.3.1.x AppArmor in the bootloader, 1.4.1 GRUB password | Needs a kernel command-line change and a reboot of the hypervisor; a boot password blocks unattended reboots. |
| 2.1.6 / 2.1.9 / 2.1.12 (ftp, nfs, rpcbind "not in use") | vsftpd serves the scanner, NFS and rpcbind serve the cluster's `k8s-rwx` share. |
| 5.4.3.1 `nologin` not in `/etc/shells` | vsftpd's `pam_shells` rejects the `scanner` account without it; fixing it properly means a dedicated PAM service file for vsftpd, to be tested against the scanner. |
| 6.2.3.20 audit configuration immutable (`-e 2`) | Rules could then only change by reboot, which conflicts with the `augenrules --load` handler. |
| 6.3 AIDE over the whole filesystem, 6.1.2.1 `systemd-journal-remote` | Wazuh FIM and the agent already cover these; wave 2 does AIDE on configuration paths only. |

### Quirks of the Wazuh policy that wave 1 mirrors on purpose
The checks follow the policy's literal rules, which differ from the CIS text in places, so a "correct" configuration can still score failed:
- **5.2.6** needs `timestamp_timeout` in `/etc/sudoers` *and* in a `sudoers.d` file (condition `all`); both get the same value.
- **5.2.7** needs `pam_wheel.so` with both `use_uid` and an explicit `group=`; the role writes a bare `pam_wheel.so`, which would not pass.
- **7.1.10** stats `opasswd` *and* `opasswd.old`; a missing file fails, so both are created.
- **1.6.1** contains a bare `\s` in its regex, so any `/etc/motd` with a space in it fails; only an absent file passes (the CIS text allows that). `/etc/issue` (1.6.2) is left alone: `pvebanner` rewrites it at every boot.
- **2.3.3.2** needs `user _chrony` in `chrony.conf` *and* in a `conf.d` file.

### Admin account
`cis.yml` creates `phiros-cmd` (key-only SSH, `sudo` group) once at least one public key is listed in `cis_admin_ssh_pubkeys`. Public keys are not secret, so they are in Git, not 1Password. Listed today: the ED25519 key of `cypr0` (fingerprint `SHA256:iw1v8EdDCpznuAFmlTfjBIuzUiLi7U7PyImao7ni60E`). The account is created with a locked password; set it once by hand (see Common operations). SSH never uses that password, `sudo` does, which keeps 5.2.4 true for a human. Until `AllowUsers` arrives in wave 3 nothing restricts who may log in, so the account is an additional way in, not a replacement for `root` and `ansible`.

## TODOs / unknowns
- Whether `sshd_config`'s `PermitRootLogin prohibit-password` could be tightened further now that root's role is reduced to four bootstrap tasks per run — not addressed by #70/#71/#77, left as a possible follow-up now that the handoff is confirmed stable in practice.
- The old, now-orphaned `/tmp/.ansible/tmp` directory (root-owned, `0700`) left behind by years of pre-#71 runs was never cleaned up on the host — harmless (nothing references it anymore), but noted in case it's ever confusing during manual troubleshooting.
- No `refreshInterval` is set on `externalsecret.yaml`, unlike most other apps' ExternalSecrets in this repo (which explicitly set `1h`) — not confirmed whether this is intentional or an oversight.
- `sshd_config` is now cross-checked against the Wazuh SCA policy (baseline 2026-10-11): every 5.1.x check passes except 5.1.4 (`AllowUsers`, wave 3), 5.1.8 (`DisableForwarding`, added with wave 1) and 5.1.20 (accepted exception above).
- `cis.yml` asserts Debian 13 before doing anything, because the waves were checked against Trixie only; a distribution upgrade of the host will stop them until someone re-reads them.
- 7.2.1 ("accounts use shadowed passwords") fails in SCA although no username in Wazuh's inventory trips its pattern; wave 1 only prints the offending `/etc/passwd` lines (usernames, never the second field) into the Job log instead of running `pwconv` blind.
- **Internet exposure of the RPC/NFS ports** (111, 20048, 20049; requirement: reachable from the LAN and the WireGuard networks only, not from the internet). Probed from outside on 2026-10-11 over IPv4: TCP 111/2049/20048-20050 get no answer (the playbook's PREROUTING DNAT hands everything but 22 and 8006 to OPNsense) and UDP 111 gets no reply; 22 and 8006 do answer. **IPv6 is unverified**: rpcbind, mountd, statd, pveproxy and spiceproxy listen on `[::]`, the uplink and `vmbr0` carry a global IPv6 address, and no `ip6tables` rule is managed by this playbook. Every run of `cis.yml` prints the live listening sockets and both rule sets (task "Exposure report", read only, also in dry runs); the restriction is to be written against that output rather than against the rules in Git.
