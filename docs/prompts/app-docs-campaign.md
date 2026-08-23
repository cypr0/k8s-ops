# App & Incident Documentation Campaign — Operator Prompt

You are documenting `k8s-ops`, a single-cluster Talos/Flux GitOps homelab repo (one operator, no on-call rotation, no multi-environment matrix — everything here is the one live cluster). Output is two kinds of file, both under `docs/`:

- **Per-app README**: `docs/apps/<app>.md`, one per app, skeleton at `docs/templates/app-readme.md`.
- **Incident postmortem**: `docs/incidents/YYYY-MM-DD-<slug>.md`, one per notable incident, skeleton at `docs/templates/incident.md`.

This prompt is the contract for both. Read it in full before writing anything.

---

## 1. Mode: interactive, not autonomous

This campaign runs **with the operator in the loop**, not as an unattended pipeline. For each app or incident:

1. **Announce** what you're about to document and your discovery plan if anything about it looks unusual.
2. **Discover** (§4 for apps, §5 for incidents). Don't narrate raw discovery output unless something surprised you.
3. **Draft** the full file in chat, in a single Markdown code fence, ready to save. All file citations inside the doc must be repo-root-relative (`kubernetes/apps/security/authentik/app/helmrelease.yaml`, not a bare filename).
4. **Self-scan (§3).** Run the secret/sensitive-info gate against your own draft before showing it. This is not optional and not covered by "the operator will catch it" — run it every time, including on edits to an already-committed doc.
5. **Wait.** Do not write the file or commit. The operator approves, requests changes, or asks questions.
6. **Commit** once approved: one file per commit, message `docs(<app|incident>): <slug>`.
7. **Next.** Move to the next item in the queue — don't skip ahead unprompted.

If the operator says "do the next N without stopping," relax step 4 but still commit one-file-per-commit.

**Why interactive-first, and what changes later:** an unattended GitHub-runner pipeline was considered and deliberately deferred (2026-08-16) — an LLM committing docs without review risks both hallucinated claims and accidentally restating sensitive live-cluster details (real IPs, secret paths) that a security audit had just flagged as a risk to avoid. Once the house style is calibrated and a full pass exists, a **staleness-checker** automation (flag `docs/apps/<app>.md` as possibly-stale when its HelmRelease chart version changes, open an issue/PR *suggesting* an update — never an autonomous commit) is the intended next step, not a full autonomous rewrite pipeline.

---

## 2. Non-negotiable rules (apps and incidents alike)

1. **Ground every non-obvious claim in a file path.** One citation per claim is enough — don't turn the doc into a footnote farm.
2. **If you don't know, say so.** Both templates have a TODOs/unknowns or open-action-items section. Use it.
3. **No generic product marketing.** Start from what role this app plays *in this cluster*, not what the vendor's homepage says.
4. **Repo facts only, unless explicitly marked.** Upstream chart defaults are only quoted when the repo overrides them, or clearly marked as "from the chart's values.yaml at version X."
5. **Never restate secret values, real public IPs, or account-identifying paths.** See §3 for the full mandatory gate — cite the ExternalSecret resource and the 1Password item/field name it pulls, never a resolved value, even if you saw it live while debugging.
6. **Do not invent hostnames, IPs, or paths.** These come from the repo or from the operator, never guessed.
7. **One file per commit.** If you notice a bug while documenting, surface it in the session's closing summary — do not fix it inline in the same branch.

---

## 3. Secret & sensitive-info gate (mandatory — every create AND every update)

A 2026-08-16 security audit of this repo found exactly one leak in three years of history: a real public IP in a plain YAML comment, present precisely because a comment isn't code and so drifted past the review habits that catch everything else. Documentation is the same risk surface, arguably worse — it's prose, written to be dense and specific, and specific is exactly what a secret-shaped value looks like right before it's pasted in. Treat this gate as equal in weight to the "ground every claim in a file path" rule, not a nice-to-have.

**Run this scan on the full drafted file — never skip it because "it's just a small edit" to an existing doc:**

1. **No resolved secret values, ever.** If a claim needs a credential, cite the `ExternalSecret` resource + the 1Password item/field name (`kubernetes/apps/<ns>/<app>/app/externalsecret-*.yaml`), never the value — not even a value you saw live in a debug session, not even truncated/partially redacted. This holds for API keys, passwords, tokens, client secrets, connection strings with embedded credentials, and private keys.
2. **No real public IPs or externally-resolvable hostnames beyond what's already public by necessity.** A hostname the cluster deliberately exposes (e.g. an app's own public HTTPRoute hostname) is fine — that's already public by design. A real public IP address, a home/office network's WAN IP, an ISP's identifying block, or any address the repo itself treats as sensitive elsewhere (check: is this value SOPS-encrypted or 1Password-sourced anywhere else in the repo? If yes, that's your answer — don't restate it in plaintext in a doc). RFC1918 internal IPs (`192.168.x.x`, `10.x.x.x`) and `*.cluster.local` names are fine — those are meaningless outside this LAN.
3. **No secret paths or identifiers that only make sense with insider knowledge of the account they belong to** — e.g. a literal 1Password vault UUID, a cloud account ID, a device serial. Field *names* are fine ("pulls `AUTHENTIK_SECRET_KEY`"); account-identifying values are not.
4. **When in doubt, cite structure, not content.** "The ExternalSecret pulls 6 keys from the `authentik-secret` 1Password item" is always safe. Never resolve that sentence further to see what those 6 values actually are.
5. **State the check explicitly when you present the draft** — one line, e.g. "Secret/IP scan: clean" or "Secret/IP scan: flagged and removed `<what>` before this draft." The operator should never have to ask whether this ran.

If something fails this gate, fix it in the same draft before showing it — don't show a draft with a flagged value and ask permission to keep it. The one exception: if you're genuinely unsure whether a value is sensitive (e.g., is this hostname actually public already?), ask the operator directly rather than guessing either way.

---

## 4. Discovery protocol — apps

Run for every app in `docs/apps/`:

- **Scope:** `ls kubernetes/apps/<ns>/<app>/app/` — what files exist (HelmRelease vs plain manifests, ExternalSecrets, HTTPRoute, CiliumNetworkPolicy).
- **Source:** read `helmrepository.yaml` (if present) + `helmrelease.yaml` — chart name/version, upstream repo, values that diverge from chart defaults.
- **Secrets:** `ls kubernetes/apps/<ns>/<app>/app/externalsecret*.yaml` — for each, the 1Password item/field it pulls (from the ExternalSecret spec, not by reading the actual secret) and what consumes it.
- **Routing:** `httproute.yaml` / `ciliumnetworkpolicy.yaml` if present — hostname, path rules, what's allowed in/out and why.
- **Dependencies:** grep the app's files for CNPG (`cnpg.io/`), Dragonfly/Redis, S3/MinIO, OpenSearch, OIDC issuer/client_id (→ Authentik blueprint under the same app's `blueprints/` or `security/authentik/app/blueprints/`), storageClassName. For reverse dependencies, grep the rest of the repo for this app's Service DNS name.
- **Quirks:** commit history (`git log --oneline -- kubernetes/apps/<ns>/<app>/`), inline `# NOTE:`/`# HACK:`/`# WORKAROUND:` comments, and `docs/incidents/` for anything referencing this app.
- **Self-critique before writing:** Could this README have been written without reading any of the app's files? If so, rewrite with citations. Would "Known quirks" save a future 2am debugging session 20 minutes? If not, it's noise — omit rather than pad.

## 5. Discovery protocol — incidents

Write a postmortem for anything SEV1/SEV2, or a SEV3 that was genuinely non-obvious to diagnose:

- Pull the actual timeline from what's available: `kubectl get events`, Flux `status.conditions` transition timestamps, pod logs, git commit timestamps for the fix.
- Write the "what did NOT work" section honestly — dead ends are as valuable as the fix.
- If the incident already has an auto-memory entry (`~/.claude/projects/.../memory/project_*.md`), you may draw structure from it, but the postmortem must still cite live evidence (commands + output), not just restate the memory's prose.
- Distinguish trigger from underlying cause explicitly (see `docs/templates/incident.md`).

---

## 6. Git workflow

- **Branch:** one long-lived branch `docs/app-and-incident-docs` off `main`. No per-item branches.
- **Commit format:** `docs(<app>): add app README` or `docs(incident): <slug>` — one commit per file.
- **Checkpoint pushes:** after every 5 commits, push and tell the operator.
- **No fixes to app manifests on this branch.** Bugs noticed in passing go into the closing summary with file + line, not a same-branch fix.
- **Every update to an already-committed doc re-runs §3** before the diff is shown — "it's already reviewed once" is not an exemption; edits are exactly where a stray value slips in unnoticed.
- **Closing summary** (after a batch or the full queue): list of files committed, TODOs opened, bugs surfaced in passing, and any cross-cutting pattern worth a follow-up.

---

## 7. Calibration

Before the first full app README or incident postmortem, produce one of each as a calibration sample and show them to the operator. If the density/register isn't right, iterate on the sample alone before touching the rest of the queue — cheaper than rewriting N files after the fact.

---

## 8. The queue (seed — extend as needed)

**Apps**, prioritized by what got the most attention in the 2026-08-16 cluster health-check session:
1. `authentik` (security)
2. `coredns` (kube-system)
3. `nextcloud` (nextcloud)
4. `hermes-agent` (hermes-agent)
... then the remaining ~50 apps, no fixed order — pick by what's most likely to be touched again soon, or ask the operator.

**Incidents**, same date:
1. `2026-08-16-authentik-geoip-sidecar-sso-outage.md`
2. `2026-08-16-coredns-aaaa-nxdomain-breaks-internal-dns.md`
3. `2026-08-16-hermes-agent-restore-pvc-chown-permission-denied.md`

If an app or incident isn't in this seed list, add it and continue — this queue is not exhaustive, just a starting point.
