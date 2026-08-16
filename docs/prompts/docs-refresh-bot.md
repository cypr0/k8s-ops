# Docs Refresh Bot

Automated follow-on to the interactive campaign in [`app-docs-campaign.md`](app-docs-campaign.md). Runs as a GitHub Actions workflow (`.github/workflows/docs-refresh.yaml` + `.github/scripts/docs_refresh.py`), triggered on:

- push to `main` touching `kubernetes/apps/**`
- a weekly schedule (Mondays 06:00 UTC) as a backstop
- manual `workflow_dispatch`

## What it does

1. Diffs the app directories under `kubernetes/apps/` against the docs already present in `docs/apps/`.
2. For any app with **no existing doc**, gathers its YAML manifests and drafts a README via an LLM call to OpenRouter, using the same template (`docs/templates/app-readme.md`) and operator contract (`app-docs-campaign.md`) as the interactive campaign.
3. Runs the draft through an automated secret/IP scanner (private-key markers, common API-key shapes, any literal IPv4) before doing anything else with it.
4. If the scan is clean: writes the file, updates `docs/apps/README.md`'s index, opens a **pull request** for human review.
5. If the scan flags something, or the OpenRouter call fails: opens a **GitHub Issue** instead, naming the app and the reason — never a PR with unreviewed content that failed the scan.

Every PR/Issue this bot opens carries the `docs-bot` label (`.github/labels.yaml`), so bot-authored content is identifiable at a glance, the same way Renovate's own PRs carry `renovate/*`. PRs additionally pick up `area/docs` automatically via the existing labeler workflow (`.github/labeler.yaml` was extended to match `docs/**/*`, not just the root `README.md`).

**It never auto-merges.** Every draft — clean scan or not — is a first draft meant to be checked against the actual manifests before merging, same as the interactive campaign's own citation discipline. It also only handles the "genuinely new, undocumented app" case; it does not attempt to detect staleness in existing docs (see Known limitations).

## One-time setup required

Before this workflow can run successfully:

1. **Add a repo secret `OPENROUTER_API_KEY`.** Reuses the same OpenRouter account already used by several apps in the cluster (see `docs/apps/hermes-agent.md`, `docs/apps/paperless-ngx.md`, `docs/apps/open-webui.md`) — pull the value from the 1Password `openrouter` item and set it via:
   ```
   gh secret set OPENROUTER_API_KEY --repo cypr0/k8s-ops
   ```
2. **Enable "Allow GitHub Actions to create and approve pull requests"** under repo Settings → Actions → General → Workflow permissions. Without this, `gh pr create` inside the workflow fails even though the workflow's own `permissions:` block grants `pull-requests: write` — this is a separate, repo-level toggle.

Nothing else is required — the workflow's `GITHUB_TOKEN` (scoped per-job to `contents: write`, `pull-requests: write`, `issues: write`) handles the rest.

## Model choice

Defaults to `anthropic/claude-sonnet-4.5` via OpenRouter — overridable per-run via the `workflow_dispatch` `model` input, or by editing the default in `docs-refresh.yaml`/`docs_refresh.py`. Check `https://openrouter.ai/models` for the current slug if this ever starts 404ing (OpenRouter's catalog changes over time).

## Known limitations

- Only catches **missing** docs (a new `kubernetes/apps/<ns>/<app>/` directory with nothing in `docs/apps/`), not staleness in existing docs — an app whose manifests changed significantly still needs the interactive campaign, or a future extension of this bot, to pick up.
- The secret/IP scanner is a blunt regex pass (private-key markers, common API-key shapes, any literal IPv4) — a real defense-in-depth backstop given the PR still needs human review, not a replacement for the interactive campaign's own judgment (`app-docs-campaign.md` §3).
- One shared branch/PR per workflow run — if multiple apps are newly undocumented at once, they land in a single PR with one commit per app, not separate PRs.
