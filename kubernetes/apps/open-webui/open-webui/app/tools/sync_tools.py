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
besides the owner can use it: `[("user", "*")]` makes it visible/usable by
every signed-in Open WebUI user; `[("group", "<group.id>")]` restricts it
to one Open WebUI group (verify the id via `SELECT id, name FROM
"group"` against the live DB -- group *names* are not valid principal_ids).
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
TOOL_ACCESS_GRANTS: dict[str, list[tuple[str, str]]] = {
    # FULL read/write/DESTRUCTIVE Paperless-ngx access. This is currently a
    # single-admin-user instance (mail@cisotop.de) -- visible to any
    # signed-in user for now since there is no one else to restrict it
    # from; narrow this to a specific group id if/when more users are
    # added and not all of them should get Paperless write access.
    "paperless_full": [("user", "*")],
    # FULL read/write/DESTRUCTIVE Nextcloud access, authenticated as the
    # REAL Nextcloud super-admin account (see nextcloud_full.py's module
    # docstring) -- higher stakes than paperless_full's dedicated API
    # token. Same single-admin-user reasoning applies for now; revisit
    # this grant (and consider a less-privileged dedicated account
    # instead) before adding more Open WebUI users.
    "nextcloud_full": [("user", "*")],
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


def _reconcile_access_grants(db, AccessGrant, tool_id: str, now: int) -> None:
    """Make the 'read' access grants for `tool_id` match TOOL_ACCESS_GRANTS
    exactly: insert missing, delete stale. Idempotent."""
    desired = set(TOOL_ACCESS_GRANTS.get(tool_id, []))
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
