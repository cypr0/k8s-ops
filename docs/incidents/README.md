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

_(none yet — campaign in progress)_
