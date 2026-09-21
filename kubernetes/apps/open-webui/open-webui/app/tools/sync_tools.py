#!/usr/bin/env python3
"""
Sync Open WebUI Tools from Git-managed source into the Open WebUI database.

Tools live only in the DB -- there is no file mount or env autoload -- so
this Job is the GitOps bridge, run as a post-reconcile Flux Job (same
"force annotation + hashed ConfigMap" trick used elsewhere in this repo,
e.g. kubernetes/apps/paperless/paperless-ngx/config/cronjob-stats-exporter.yaml,
to make Flux re-run it whenever the mounted source actually changes).

Runs inside the Open WebUI image itself: it uses open_webui's own
SQLAlchemy models (Tool, User, AccessGrant) and its
load_tool_module_by_id()/get_tool_specs() helpers to compute `specs`
exactly as the running instance would if you pasted this into the UI, and
because this instance is OIDC-only (no password login), the admin HTTP
API's auth paths don't work here -- direct DB access via the app's own
models is the supported way in.

Idempotent: create-or-update by id, safe to re-run on every Flux
reconcile. `valves` and per-user valves are preserved across re-syncs
(only content and metadata are refreshed) so any admin/user configuration
set in the UI survives a `git push`.

Environment:
  TOOL_DIR              directory holding the mounted *.py tool sources
  TOOL_OWNER_EMAIL      email of an admin user to own the tools
  LOG_LEVEL             default INFO
(DATABASE_URL / WEBUI_SECRET_KEY come from open-webui-secret, same as the
app itself -- see job-sync-tools.yaml.)

TOOL_ACCESS_GRANTS below declares, per tool id (== filename stem), who
besides the owner can use it. Three principal forms are accepted:

  ("user", "*")            every signed-in Open WebUI user
  ("group", "<group.id>")  one group, by its literal DB id (a UUID)
  ("group_name", "<name>") one group, resolved to its id at sync time

`group_name` exists because group *ids* are UUIDs that only exist once
the group does, and the groups here are created by Open WebUI itself on
OIDC login (ENABLE_OAUTH_GROUP_MANAGEMENT/ENABLE_OAUTH_GROUP_CREATION in
helmrelease.yaml, fed by Authentik's groups claim) -- so there is no id to
hardcode in git ahead of time. A `group_name` that matches no group yet
resolves to NOTHING and is logged as a warning: the grant set for that
tool ends up empty, meaning owner-only. That is fail-CLOSED on purpose --
a typo'd or not-yet-created group must never degrade into "everyone".

An id with an empty list stays owner-only.
"""

from __future__ import annotations

import logging
import os
import sys
import time

# Importing open_webui.env (inside main()) reconfigures the root logger --
# logging.basicConfig(..., force=True) plus a loguru intercept handler --
# which silently swallows everything this script logs from that point on.
# Own the handler and switch off propagation so our output always survives.
log = logging.getLogger("sync-tools")
log.setLevel(os.getenv("LOG_LEVEL", "INFO"))
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log.addHandler(_handler)
log.propagate = False

# id (== filename stem) -> list of (principal_type, principal_id) read grants.
# The owner (TOOL_OWNER_EMAIL) always has implicit write access and needs
# no entry here.
# Authentik group whose members get the full-access tools. Deliberately a
# NAME, not an id -- see the module docstring. This group does not exist
# yet at the time of writing: create it in Authentik and add the adult
# accounts to it, and Open WebUI mirrors it on their next login.
#
# Until then every tool below is owner-only, which is the correct
# behaviour while kid accounts are being added: the previous
# [("user", "*")] grants meant "every signed-in user", and that stops
# being an acceptable default the moment this instance stops being
# single-user. A child account must not be able to invoke a tool that
# authenticates as the Nextcloud super-admin, reads either parent's
# mailbox, or deletes Paperless documents.
ADULTS_GROUP = "owui-family"

# id (== filename stem) -> list of (principal_type, principal_id) read grants.
# The owner (TOOL_OWNER_EMAIL) always has implicit write access and needs
# no entry here.
TOOL_ACCESS_GRANTS: dict[str, list[tuple[str, str]]] = {
    # FULL read/write/DESTRUCTIVE Paperless-ngx access, with a token whose
    # Paperless account is the real ceiling (see paperless_full.py).
    "paperless_full": [("group_name", ADULTS_GROUP)],
    # FULL read/write/DESTRUCTIVE Nextcloud access, authenticated as the
    # REAL Nextcloud super-admin account (see nextcloud_full.py's module
    # docstring) -- the highest-stakes credential of the five, with no
    # revoke path that doesn't also affect the human admin login.
    "nextcloud_full": [("group_name", ADULTS_GROUP)],
    # Reads either parent's actual mailbox and writes their real calendar
    # and contacts, using their own account passwords -- Mailu has no
    # narrower credential (see mailu_full.py). Strictly adults.
    "mailu_full": [("group_name", ADULTS_GROUP)],
    # Read-mostly and refuses DELETE outright (see immich_photos.py), so
    # this is the one tool whose blast radius would survive a wider grant.
    # Still adults-only for now: the API key sees a whole personal photo
    # library, which is not the same question as whether it can damage it.
    "immich_photos": [("group_name", ADULTS_GROUP)],
    # Hands a task to hermes-agent, which runs with a read-only Kubernetes
    # terminal and its own full-access MCP servers -- so this tool is a
    # gateway to everything above plus more, regardless of how small its
    # own method surface looks.
    "hermes_agent": [("group_name", ADULTS_GROUP)],
}


def _env(name: str, required: bool = True, default: str = "") -> str:
    val = os.getenv(name, default)
    if required and not val:
        log.error("Missing required env var: %s", name)
        sys.exit(1)
    return val


def main() -> None:
    import glob

    tool_dir = _env("TOOL_DIR")
    owner_email = _env("TOOL_OWNER_EMAIL")

    files = sorted(glob.glob(os.path.join(tool_dir, "*.py")))
    files = [f for f in files if os.path.basename(f) != "sync_tools.py"]
    if not files:
        log.error("No .py tools found in %s", tool_dir)
        sys.exit(1)

    # Open WebUI's own models/utilities. Require the app's env (DATABASE_URL)
    # and are only importable from inside the open-webui image.
    import asyncio

    from open_webui.internal.db import get_db
    from open_webui.models.access_grants import AccessGrant
    from open_webui.models.tools import Tool
    from open_webui.models.users import User
    from open_webui.utils.plugin import load_tool_module_by_id
    from open_webui.utils.tools import get_tool_specs

    now = int(time.time())

    with get_db() as db:
        owner = db.query(User).filter(User.email == owner_email).first()
        if not owner:
            log.error("Owner user %s not found", owner_email)
            sys.exit(1)
        if owner.role != "admin":
            log.error("Owner %s is not an admin (role=%s)", owner_email, owner.role)
            sys.exit(1)

        synced = 0
        for path in files:
            tool_id = os.path.splitext(os.path.basename(path))[0]
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
            except OSError as e:
                log.error("Cannot read %s: %s", path, e)
                sys.exit(1)

            if "class Tools" not in content:
                log.error(
                    "%s has no 'class Tools' -- refusing to sync as a Tool",
                    path,
                )
                sys.exit(1)

            # Load the module the same way Open WebUI does at chat time, so
            # `specs` and `has_user_valves` match exactly what the running
            # instance would compute if you pasted this into the UI.
            try:
                module, frontmatter = asyncio.run(
                    load_tool_module_by_id(tool_id, content=content)
                )
            except Exception as e:  # noqa: BLE001
                log.error("Failed to load tool module %s: %s", tool_id, e)
                sys.exit(1)

            specs = get_tool_specs(module)
            has_user_valves = hasattr(module, "UserValves")
            name = _title_from_frontmatter(content, default=tool_id)
            meta = {
                "description": "Managed by GitOps (job-sync-tools).",
                "manifest": frontmatter,
                "has_user_valves": has_user_valves,
            }

            tool = db.query(Tool).filter(Tool.id == tool_id).first()
            if tool:
                # Update code + metadata + specs only; leave `valves` (admin)
                # and each user's settings['tools']['valves'][id] (per-user)
                # untouched so configuration set in the UI is preserved.
                tool.name = name
                tool.content = content
                tool.specs = specs
                tool.meta = meta
                tool.updated_at = now
                action = "updated"
            else:
                tool = Tool(
                    id=tool_id,
                    user_id=owner.id,
                    name=name,
                    content=content,
                    specs=specs,
                    meta=meta,
                    valves={},
                    updated_at=now,
                    created_at=now,
                )
                db.add(tool)
                action = "created"

            _reconcile_access_grants(db, AccessGrant, tool_id, now)

            log.info(
                "Tool '%s' (%s, user_valves=%s, %d spec(s)) %s",
                name,
                tool_id,
                has_user_valves,
                len(specs),
                action,
            )
            synced += 1
        db.commit()
        log.info("Synced %d tool(s) OK", synced)


def _resolve_principals(db, tool_id: str) -> set[tuple[str, str]]:
    """Turn TOOL_ACCESS_GRANTS' declared principals into the (type, id)
    pairs the access_grant table stores, resolving ("group_name", <name>)
    against the live `group` table.

    An unresolvable group name yields NOTHING rather than raising: the sync
    of the tool code itself must not be blocked on a group somebody hasn't
    created yet. It is logged at WARNING so it shows up in the Job's logs,
    and the consequence -- that tool becoming owner-only -- is the safe
    direction to fail in.
    """
    from open_webui.models.groups import Group

    resolved: set[tuple[str, str]] = set()
    for principal_type, principal_id in TOOL_ACCESS_GRANTS.get(tool_id, []):
        if principal_type != "group_name":
            resolved.add((principal_type, principal_id))
            continue
        group = db.query(Group).filter(Group.name == principal_id).first()
        if group is None:
            log.warning(
                "Tool '%s': group '%s' does not exist (yet) -- granting nobody "
                "but the owner. Create it in Authentik and have a member sign "
                "in to Open WebUI, then re-run this Job.",
                tool_id,
                principal_id,
            )
            continue
        resolved.add(("group", group.id))
    return resolved


def _reconcile_access_grants(db, AccessGrant, tool_id: str, now: int) -> None:
    """Make the 'read' access grants for `tool_id` match TOOL_ACCESS_GRANTS
    exactly: insert missing, delete stale. Idempotent."""
    desired = _resolve_principals(db, tool_id)
    existing_rows = (
        db.query(AccessGrant)
        .filter(
            AccessGrant.resource_type == "tool",
            AccessGrant.resource_id == tool_id,
            AccessGrant.permission == "read",
        )
        .all()
    )
    existing = {(row.principal_type, row.principal_id) for row in existing_rows}

    for row in existing_rows:
        if (row.principal_type, row.principal_id) not in desired:
            db.delete(row)

    for principal_type, principal_id in desired - existing:
        db.add(
            AccessGrant(
                id=f"{tool_id}:{principal_type}:{principal_id}:read",
                resource_type="tool",
                resource_id=tool_id,
                principal_type=principal_type,
                principal_id=principal_id,
                permission="read",
                created_at=now,
            )
        )


def _title_from_frontmatter(content: str, default: str) -> str:
    """Extract the 'title:' from the module docstring frontmatter."""
    import re

    m = re.search(r'^\s*title:\s*(.+?)\s*$', content, re.MULTILINE)
    return m.group(1).strip() if m else default


if __name__ == "__main__":
    main()
