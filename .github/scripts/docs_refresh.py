#!/usr/bin/env python3
"""Detect apps under kubernetes/apps/ with no docs/apps/<app>.md and draft one via
OpenRouter, opening a PR for human review. Never auto-merges. Any draft that fails
an automated secret/IP scan is skipped and reported via a GitHub Issue instead of
being opened as a PR."""
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

REPO_ROOT = subprocess.run(
    ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
).stdout.strip()
APPS_DIR = os.path.join(REPO_ROOT, "kubernetes", "apps")
DOCS_DIR = os.path.join(REPO_ROOT, "docs", "apps")
TEMPLATE_PATH = os.path.join(REPO_ROOT, "docs", "templates", "app-readme.md")
CAMPAIGN_PROMPT_PATH = os.path.join(REPO_ROOT, "docs", "prompts", "app-docs-campaign.md")

OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "anthropic/claude-sonnet-4.5")
MAX_MANIFEST_BYTES = 60_000  # keep the prompt bounded for very large apps

# Mirrors judgment calls the interactive campaign already made: some apps
# split one logical doc across multiple kubernetes/apps/ directories (velero),
# and some share a bare directory name ("tika") across unrelated apps that
# must NOT collapse into a single doc.
DOC_NAME_OVERRIDES = {
    ("velero", "app"): "velero",
    ("velero", "restore-test"): "velero",
    ("velero", "schedules"): "velero",
    ("open-webui", "tika"): "open-webui-tika",
    ("paperless", "tika"): "paperless-tika",
}

SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key shape
    re.compile(r"\bghp_[A-Za-z0-9]{36}\b"),  # GitHub PAT shape
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),  # OpenAI/OpenRouter-style key shape
    re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),  # any literal IPv4 - flag all, let a human judge
]


def discover_apps():
    """Map doc_name -> list of repo-relative kubernetes/apps/<ns>/<app> dirs."""
    apps = {}
    for ns in sorted(os.listdir(APPS_DIR)):
        ns_path = os.path.join(APPS_DIR, ns)
        if not os.path.isdir(ns_path):
            continue
        for app in sorted(os.listdir(ns_path)):
            app_path = os.path.join(ns_path, app)
            if not os.path.isdir(app_path):
                continue
            doc_name = DOC_NAME_OVERRIDES.get((ns, app), app)
            rel = os.path.join("kubernetes", "apps", ns, app)
            apps.setdefault(doc_name, []).append(rel)
    return apps


def missing_docs(apps):
    existing = {f[:-3] for f in os.listdir(DOCS_DIR) if f.endswith(".md") and f != "README.md"}
    return {name: dirs for name, dirs in apps.items() if name not in existing}


def scan_for_secrets(text):
    hits = []
    for pat in SECRET_PATTERNS:
        hits.extend(m.group(0) for m in pat.finditer(text))
    return hits


def gather_manifests(dirs):
    chunks = []
    total = 0
    for d in dirs:
        abs_d = os.path.join(REPO_ROOT, d)
        for root, _dirnames, files in os.walk(abs_d):
            for fn in sorted(files):
                if not fn.endswith((".yaml", ".yml")):
                    continue
                p = os.path.join(root, fn)
                rel = os.path.relpath(p, REPO_ROOT)
                try:
                    content = open(p, "r", errors="replace").read()
                except OSError:
                    continue
                block = f"--- {rel} ---\n{content}\n"
                if total + len(block) > MAX_MANIFEST_BYTES:
                    continue
                chunks.append(block)
                total += len(block)
    return "\n".join(chunks)


def build_prompt(doc_name, manifests, template, campaign_prompt):
    return f"""You are drafting the operational README for the "{doc_name}" app in a \
homelab Kubernetes GitOps repo (Talos Linux + Flux + Cilium). Follow the campaign \
contract below exactly, especially its secret/sensitive-info gate (search it for the \
section on secrets and sensitive info). Use the template skeleton as your structure. \
Base every claim strictly on the manifests provided below - do not invent details, \
and cite file paths for non-obvious claims. This draft will be reviewed by a human \
before merging, so mark anything you are unsure of as a TODO rather than guessing.

# Campaign contract
{campaign_prompt}

# Template skeleton
{template}

# Manifests for this app
{manifests}

Output ONLY the final markdown document - no preamble, no surrounding code fence.
"""


def call_openrouter(prompt):
    body = json.dumps(
        {
            "model": OPENROUTER_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
        }
    ).encode()
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/cypr0/k8s-ops",
            "X-Title": "k8s-ops docs-refresh bot",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.load(resp)
    return data["choices"][0]["message"]["content"]


def parse_title(draft, fallback):
    m = re.search(r"^# (.+)$", draft, re.MULTILINE)
    return m.group(1).strip() if m else fallback


def parse_namespace(draft):
    m = re.search(r"^> \*\*Namespace\*\*\s*(.+)$", draft, re.MULTILINE)
    if not m:
        return "?"
    ns = re.sub(r"`", "", m.group(1))
    ns = re.sub(r"\(.*", "", ns).strip()
    return ns


def update_index(new_rows):
    """new_rows: list of (title, namespace, doc_name). Inserts into the existing
    index table in docs/apps/README.md, keeping alphabetical order by title."""
    readme_path = os.path.join(DOCS_DIR, "README.md")
    lines = open(readme_path).read().splitlines()
    header_idx = next(i for i, l in enumerate(lines) if l.strip().startswith("| App "))
    row_start = header_idx + 2  # skip the header row and the |---|---|---| separator
    row_end = row_start
    while row_end < len(lines) and lines[row_end].strip().startswith("|"):
        row_end += 1
    rows = lines[row_start:row_end]
    for title, ns, name in new_rows:
        rows.append(f"| {title} | {ns} | [{name}.md]({name}.md) |")
    rows.sort(key=lambda r: r.split("|")[1].strip().lower())
    new_lines = lines[:row_start] + rows + lines[row_end:]
    open(readme_path, "w").write("\n".join(new_lines) + "\n")


def sh(*args, **kwargs):
    print("+", " ".join(args))
    subprocess.run(args, check=True, cwd=REPO_ROOT, **kwargs)


def main():
    todo = missing_docs(discover_apps())
    if not todo:
        print("No undocumented apps found.")
        return

    template = open(TEMPLATE_PATH).read()
    campaign_prompt = open(CAMPAIGN_PROMPT_PATH).read()

    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    branch = f"docs-bot/refresh-{run_id}"
    sh("git", "config", "user.name", "docs-refresh-bot")
    sh("git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    sh("git", "checkout", "-b", branch)

    drafted = []  # (title, namespace, doc_name)
    flagged = []  # (doc_name, reason)

    for doc_name, dirs in sorted(todo.items()):
        print(f"Drafting docs/apps/{doc_name}.md from {dirs}")
        manifests = gather_manifests(dirs)
        if not manifests.strip():
            print("  no YAML manifests found, skipping")
            continue

        try:
            draft = call_openrouter(build_prompt(doc_name, manifests, template, campaign_prompt))
        except (urllib.error.URLError, KeyError, json.JSONDecodeError) as exc:
            print(f"  OpenRouter call failed: {exc}")
            flagged.append((doc_name, f"OpenRouter API call failed: {exc}"))
            continue

        hits = scan_for_secrets(draft)
        if hits:
            print(f"  secret scan flagged {len(hits)} hit(s) - not opening a PR for this app")
            flagged.append(
                (
                    doc_name,
                    f"Automated secret/IP scan flagged {len(hits)} pattern(s) in the drafted "
                    "content, so no PR was opened. Needs manual drafting via the interactive "
                    "campaign (docs/prompts/app-docs-campaign.md).",
                )
            )
            continue

        doc_path = os.path.join(DOCS_DIR, f"{doc_name}.md")
        with open(doc_path, "w") as f:
            f.write(draft.rstrip() + "\n")
        sh("git", "add", f"docs/apps/{doc_name}.md")
        sh("git", "commit", "-m", f"docs({doc_name}): auto-draft app README")
        drafted.append((parse_title(draft, doc_name), parse_namespace(draft), doc_name))

    if flagged:
        body = "\n".join(f"- **{name}**: {reason}" for name, reason in flagged)
        sh(
            "gh",
            "issue",
            "create",
            "--title",
            "docs-refresh: app(s) needing manual documentation",
            "--body",
            f"The docs-refresh workflow could not safely auto-draft docs for:\n\n{body}",
            "--label",
            "docs-bot",
        )

    if not drafted:
        print("Nothing safely drafted this run.")
        return

    update_index(drafted)
    sh("git", "add", "docs/apps/README.md")
    sh("git", "commit", "-m", "docs(apps): update index for auto-drafted app(s)")

    sh("git", "push", "origin", branch)
    names = ", ".join(name for _, _, name in drafted)
    pr_body = (
        "Auto-drafted by `.github/workflows/docs-refresh.yml` for newly-added app(s) "
        f"with no existing doc:\n\n"
        + "\n".join(f"- `{name}`" for _, _, name in drafted)
        + "\n\n**This is an unattended first draft, not a substitute for the interactive "
        "review the rest of docs/apps/ went through.** Before merging: verify no secrets "
        "or real IPs leaked through despite the automated scan, and check that claims are "
        "grounded in the cited files, per docs/prompts/app-docs-campaign.md's secret/"
        "sensitive-info gate.\n\n"
        "🤖 Generated by the docs-refresh workflow"
    )
    sh(
        "gh",
        "pr",
        "create",
        "--title",
        f"docs: auto-draft README for {names}",
        "--body",
        pr_body,
        "--base",
        "main",
        "--head",
        branch,
        "--label",
        "docs-bot",
    )


if __name__ == "__main__":
    main()
