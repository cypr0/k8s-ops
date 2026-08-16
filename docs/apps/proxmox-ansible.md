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
- **The `ansible` OS user is provisioned but never actually used — the intended root→ansible-user handoff isn't implemented.** Per the operator's own design intent: root should exist only to create the `ansible` user and hand off to it; once that's confirmed working, all future runs should connect exclusively as `ansible`, not root. `hardening.yml`'s "Ensure ansible user exists" / "Install SSH public key for ansible user" / "Grant ansible user passwordless sudo" tasks (`configmap-playbook.yaml`) build exactly the target end-state for that handoff — but `run.sh`'s generated `inventory.ini` hardcodes `ansible_user=root` unconditionally, on every single run, with no code path that ever switches to `ansible_user=ansible` or even verifies the `ansible` account's connection works. So today: every run still authenticates as root via `PROXMOX_ROOT_SSH_KEY`, and the `ansible` user/`PROXMOX_ANSIBLE_SSH_PUBKEY` setup is idempotently re-applied but sits unused every time — the cutover step was simply never written. Separately, `hardening.yml`'s hardened `sshd_config` keeps `PermitRootLogin prohibit-password` specifically "to keep key-based root access for the Ansible CronJob" (its own inline CIS 5.2.10 comment) — i.e. as of this playbook's current form, continued root access is treated as a requirement to preserve, not just a leftover to remove. Implementing the intended handoff would mean flipping the inventory to `ansible_user: ansible` (with `become: yes` for the sudo tasks) once connectivity is confirmed, and only then reconsidering whether `PermitRootLogin` still needs to stay `prohibit-password` for this Job's sake.
- **The DNAT/routing `lineinfile` tasks in `hardening.yml`** (e.g. correcting a `192.10.100.1` typo, fixing `post-down` to use `-D` not `-A`) are one-time historical fixups now re-asserted idempotently on every run, not general-purpose network config — don't assume they document the "intended" network layout from scratch.

## Common operations
- Manually trigger a run outside the schedule: `kubectl create job --from=cronjob/proxmox-ansible proxmox-ansible-manual-$(date +%s) -n automation`.
- Suspend the schedule: `kubectl patch cronjob proxmox-ansible -n automation -p '{"spec":{"suspend":true}}'`, or pause Flux entirely with `flux suspend kustomization proxmox-ansible -n flux-system`.
- Update the playbook itself: edit `hardening.yml`/`run.sh`/`fix_key.py` in `configmap-playbook.yaml`, commit, push — the ConfigMap is mounted fresh into the next Job pod, no restart mechanism needed.
- Rotate a credential: update the 1Password item `proxmox`, then `kubectl annotate externalsecret proxmox-ansible-credentials -n automation force-sync=$(date +%s)` (no `refreshInterval` is set on this ExternalSecret, so don't assume it'll pick up the change on its own within a bounded window).
- Check the last run's result: `kubectl logs -n automation job/<latest-job-name>` — `run.sh` echoes key diagnostics (Ansible version, SSH key line count, inventory host) before invoking `ansible-playbook`.

## TODOs / unknowns
- **Root→ansible-user cutover is not yet implemented** (see Known quirks above) — the target state (create `ansible`, verify it, then connect only as `ansible` going forward) is design intent confirmed by the operator, not yet code. Doing this needs: (1) a way to verify the `ansible` account's SSH connectivity from within the same run that creates it (e.g. a second `ansible-playbook`/`ansible ping` pass against `ansible_user: ansible` before declaring success), (2) then switching `run.sh`'s inventory generation to `ansible_user=ansible` for all subsequent runs, and (3) revisiting whether `sshd_config`'s `PermitRootLogin prohibit-password` can be tightened further (e.g. `no`) once root access is no longer needed by this Job. None of that exists in `configmap-playbook.yaml` today.
- No `refreshInterval` is set on `externalsecret.yaml`, unlike most other apps' ExternalSecrets in this repo (which explicitly set `1h`) — not confirmed whether this is intentional or an oversight.
- Whether the CIS Debian 13 `sshd_config` in `hardening.yml` has been checked against a specific CIS benchmark control list the way the cluster's own nodes have (see the `project_cis_benchmark_review` memory) — no cross-reference found in this repo.
