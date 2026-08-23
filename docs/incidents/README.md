# Incident Postmortems

One file per notable incident: `YYYY-MM-DD-<slug>.md`, skeleton at
[`../templates/incident.md`](../templates/incident.md). Written per the
contract in [`../prompts/app-docs-campaign.md`](../prompts/app-docs-campaign.md).

Severity scale (sized for a one-operator homelab, no on-call rotation):

| Level | Meaning |
| --- | --- |
| **SEV1** | Full outage of something cluster-wide-depended-on (SSO, DNS, the apiserver itself), or real data-loss risk |
| **SEV2** | A single app degraded/down, or a workaround exists, no data loss |
| **SEV3** | Cosmetic, caught before user impact, or a near-miss worth recording anyway |

## Index

| Date | Severity | Incident |
| --- | --- | --- |
| 2026-08-16 | SEV1 | [CoreDNS AAAA template rule breaking internal service resolution](2026-08-16-coredns-aaaa-nxdomain-breaks-internal-dns.md) |
| 2026-08-16 | SEV1 | [Authentik GeoIP sidecar crash-loop causing full SSO outage](2026-08-16-authentik-geoip-sidecar-sso-outage.md) |
| 2026-08-16 | SEV2 | [hermes-agent restore-PVC ownership fix broke production](2026-08-16-hermes-agent-restore-pvc-chown-permission-denied.md) |
| 2026-08-16 | SEV3 | [Immich VectorChord extension-image GLIBC mismatch on the shared CNPG cluster](2026-08-16-immich-vectorchord-glibc-mismatch.md) |
