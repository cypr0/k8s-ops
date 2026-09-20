# dmarc-exporter

> **Namespace**  mail
> **Source**     plain manifests; image `jgosmann/dmarc-metrics-exporter` ([upstream](https://github.com/jgosmann/dmarc-metrics-exporter))
> **Hostname**   none — metrics only, scraped in-cluster

## What it does here
Turns DMARC aggregate reports into Prometheus metrics. Receivers (Google, Microsoft, GMX and others) send one XML report per day per domain, summarising how mail claiming to be from that domain fared against SPF, DKIM and the published DMARC policy. This polls the `dmarc@${SECRET_DOMAIN}` mailbox over IMAP, parses what it finds, and exposes the counts.

### Why it exists
Until 2026-09-20 all five mail domains published the strictest policy DMARC offers — `p=reject` with `adkim=s; aspf=s` — and **no `rua=` at all**. Without a reporting address no receiver sends reports, so there was no feedback whatsoever on a policy that instructs the world to discard non-conforming mail.

That is not theoretical. The SPF failure found the same day (`mail.${SECRET_DOMAIN}` had been recreated as a Cloudflare-proxied record, so `a:mail.${SECRET_DOMAIN}` in the secondary domains' SPF authorised Cloudflare's IPs rather than the real sender) would have appeared in the first aggregate report. Instead it surfaced when a message failed to arrive — see `docs/apps/mailu.md`.

## Architecture at a glance
- **Depends on:** the `dmarc@${SECRET_DOMAIN}` mailbox in Mailu; `external-secrets-stores` for the IMAP password; `csi-driver-nfs` for the deduplication-state PVC. Deliberately **no** `dependsOn` on `mailu` — this only reads a mailbox, and coupling it would remove DMARC visibility exactly when the mail stack is unhealthy.
- **Depended on by:** nothing. Prometheus scrapes it; losing it costs visibility, not mail.

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/mail/dmarc-exporter/app/deployment.yaml` | Deployment, PVC, Service, ServiceMonitor |
| `kubernetes/apps/mail/dmarc-exporter/app/externalsecret.yaml` | Templates the exporter's whole JSON config, plus `imap-password` for the pruning job |
| `kubernetes/apps/mail/dmarc-exporter/app/cronjob-prune.yaml` | Weekly retention of processed reports |
| `kubernetes/apps/mail/dmarc-exporter/app/ciliumnetworkpolicy.yaml` | Two policies — exporter and pruning job |
| `kubernetes/apps/mail/dmarc-exporter/ks.yaml` | Flux Kustomization |

The DNS side lives elsewhere: the `rua=` tags are in `kubernetes/apps/mail/mailu/app/dnsendpoint.yaml`, together with the `<domain>._report._dmarc.${SECRET_DOMAIN}` cross-domain authorisations that let four of the five domains send their reports to an address under a different domain.

## Secrets
| ExternalSecret | 1Password source | Consumed by |
| --- | --- | --- |
| `dmarc-exporter-config` | item `mailu`, field `DMARC_MAILBOX_PASSWORD` | Templated into the exporter's config file *and* exposed as key `imap-password` for the pruning CronJob |

The password appears inline in the config file because the exporter has no way to read it from an environment variable. The file is mounted `0400` from the Secret, never written to a ConfigMap.

## Routing & access
No HTTPRoute — metrics only, on port 9797.

- **IMAP target is `mail.${SECRET_DOMAIN}:993`, not the Service name**, and that is deliberate. CoreDNS's `hosts` plugin resolves that name in-cluster to `mailu-front`'s ClusterIP (`kubernetes/apps/kube-system/coredns/app/helmrelease.yaml`), and the serving certificate's SAN names it — so the connection is encrypted **and** certificate-verified, with no `verify_certificate: false`.
- That matters more than it might look: **Cilium encrypts nothing here** — neither WireGuard nor IPsec is enabled — so plain IMAP on 143 would put the mailbox password on the pod network in clear text.
- `CiliumNetworkPolicy`: exporter takes ingress from Prometheus and the kubelet on 9797, egress to DNS and `mailu-front:993`. The pruning job carries its own policy with DNS and 993 only. Both need the matching ingress rule on the `mailu` policy — Cilium requires both halves.

## Storage
One 1 Gi `zfs-nfs` PVC holding only the deduplication state (report IDs seen in the last 7 days), so a redelivered report is not counted twice. `strategy: Recreate` and a single replica, because the volume is RWO and two pods would double-count.

## Report retention
`cronjob-prune.yaml`, Sundays at 04:40. The exporter moves each processed report into `Archive` and anything unparseable into `Invalid`; neither is ever emptied by the exporter itself.

| Folder | Retained | Reasoning |
| --- | ---: | --- |
| `Archive` | 90 days | Outlives Prometheus' own 30-day retention, so the raw XML is still there when a metric raises a question |
| `Invalid` | 30 days | Only useful while someone might still investigate why a report would not parse |

It searches on IMAP `BEFORE`, which matches the message's **internal date** — when this server received it, not the period the report covers. That is the right question here: how long have we held it.

## Known quirks
- **The exporter will not start until `DMARC_MAILBOX_PASSWORD` exists in 1Password.** Its ExternalSecret sits in `SecretSyncedError` and the pod stays in `ContainerCreating`. That is the intended behaviour, not a fault.
- **Reports arrive once a day, not on demand.** After enabling `rua=` expect nothing for roughly 24 hours, then a steady trickle — one message per reporting provider per domain per day. An empty dashboard on day one is normal.
- **`p=reject` with strict alignment means every misconfiguration shows up as a rejection, not a warning.** `dmarc_reject_total` climbing is the signal to act on; with this policy those messages were discarded, not quarantined.
- The exporter parses the aggregate (`rua`) format only. Forensic reports (`ruf`) are a different format and are not requested by any of these records.

## Common operations
- Force a credential re-sync: `kubectl annotate externalsecret dmarc-exporter-config -n mail force-sync=$(date +%s)`, then `kubectl rollout restart deploy/dmarc-exporter -n mail` — the config is read at start-up.
- Run the pruning manually: `kubectl create job --from=cronjob/dmarc-prune dmarc-prune-manual -n mail`.
- Look at the raw reports: log into `dmarc@${SECRET_DOMAIN}` via webmail; processed ones are in `Archive`.
- Check what is being parsed: `kubectl logs -n mail -l app.kubernetes.io/name=dmarc-exporter`.

## TODOs / unknowns
- No Grafana dashboard yet — deliberately deferred until real reports have arrived, so it can be built against the labels that are actually populated rather than against the documentation.
- No alert on `dmarc_reject_total` rising. Worth adding once a baseline exists; on a domain that sends this little, any rejection is probably worth knowing about.
- `ruf` (forensic) reporting is not enabled. It carries message-level detail and therefore personal data; whether that is wanted has not been decided.

---
_Cite every non-obvious claim with a repo-root-relative file path (e.g. `kubernetes/apps/security/authentik/app/helmrelease.yaml`), not a bare filename — this doc lives under `docs/apps/`, so relative paths must resolve from there._
