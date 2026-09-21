#!/usr/bin/env python3
"""
Sync Open WebUI groups and workspace models from Git-managed YAML into the
Open WebUI database.

Same problem and same solution as ../tools/sync_tools.py: workspace objects
live only in the DB -- there is no file mount or env autoload for a group's
permission set or a workspace model's system prompt -- so this Job is the
GitOps bridge. It runs as a post-reconcile Flux Job, inside the Open WebUI
image itself, using open_webui's own SQLAlchemy models, because this
instance is OIDC-only (no password login) and the admin HTTP API's
password/API-key auth paths don't work here.

Idempotent: create-or-update by name (groups) and by id (models), safe to
re-run on every Flux reconcile.

WHAT THIS DELIBERATELY DOES NOT TOUCH
  * group MEMBERSHIP (the group_member table). Open WebUI rebuilds it from
    the OIDC `groups` claim on every login and removes users from groups
    not in the claim (open_webui/utils/oauth.py), so anything written here
    would be reverted on the next sign-in. Membership is Authentik's job.
  * a group's permissions ONCE IT EXISTS, unless the YAML declares them.
    A group with no `permissions:` key is left exactly as found -- that is
    what lets owui-family keep Open WebUI's instance defaults, including
    any later change to those defaults, instead of freezing today's copy
    into git.
  * a model's `is_active` flag and anything an admin edited that the YAML
    has no opinion about.

Environment:
  WORKSPACE_FILE        path to the mounted workspace.yaml
  WORKSPACE_OWNER_EMAIL email of an admin user to own created objects
  LOG_LEVEL             default INFO
(DATABASE_URL / WEBUI_SECRET_KEY come from open-webui-secret, same as the
app itself -- see job-sync-workspace.yaml.)
"""

from __future__ import annotations

import logging
import os
import sys
import time
import uuid

# Importing open_webui.env (inside main()) reconfigures the root logger --
# logging.basicConfig(..., force=True) plus a loguru intercept handler --
# which silently swallows everything this script logs from that point on.
# Own the handler and switch off propagation so our output always survives.
# Same reason as sync_tools.py's identical block.
log = logging.getLogger("sync-workspace")
log.setLevel(os.getenv("LOG_LEVEL", "INFO"))
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log.addHandler(_handler)
log.propagate = False


def _env(name: str, required: bool = True, default: str = "") -> str:
    val = os.getenv(name, default)
    if required and not val:
        log.error("Missing required env var: %s", name)
        sys.exit(1)
    return val


def _deep_merge(base: dict, override: dict) -> dict:
    """Merge `override` into a copy of `base`, recursing into nested dicts.

    Used so a group's declared permissions only have to spell out the keys
    they actually change: Open WebUI's permission dict is a fixed
    two-level shape (workspace/sharing/chat/features/...), and a shallow
    update would drop every sibling key inside a section that the YAML
    happens to touch -- e.g. declaring only `features.web_search: false`
    would otherwise delete `features.notes` and the rest of that section,
    which Open WebUI then reads as "permission absent".
    """
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def main() -> None:
    import yaml

    workspace_file = _env("WORKSPACE_FILE")
    owner_email = _env("WORKSPACE_OWNER_EMAIL")

    try:
        with open(workspace_file, "r", encoding="utf-8") as f:
            spec = yaml.safe_load(f) or {}
    except OSError as e:
        log.error("Cannot read %s: %s", workspace_file, e)
        sys.exit(1)
    except yaml.YAMLError as e:
        log.error("%s is not valid YAML: %s", workspace_file, e)
        sys.exit(1)

    # Open WebUI's own models. Require the app's env (DATABASE_URL) and are
    # only importable from inside the open-webui image.
    from open_webui.config import DEFAULT_USER_PERMISSIONS
    from open_webui.internal.db import get_db
    from open_webui.models.access_grants import AccessGrant
    from open_webui.models.groups import Group
    from open_webui.models.models import Model
    from open_webui.models.users import User

    now = int(time.time())

    with get_db() as db:
        owner = db.query(User).filter(User.email == owner_email).first()
        if not owner:
            log.error("Owner user %s not found", owner_email)
            sys.exit(1)
        if owner.role != "admin":
            log.error("Owner %s is not an admin (role=%s)", owner_email, owner.role)
            sys.exit(1)

        group_ids = _sync_groups(
            db, Group, owner.id, spec.get("groups") or [], DEFAULT_USER_PERMISSIONS, now
        )
        _sync_models(db, Model, AccessGrant, owner.id, spec.get("models") or [], group_ids, now)
        db.commit()

    log.info("Workspace sync OK")


def _sync_groups(db, Group, owner_id: str, groups: list, defaults: dict, now: int) -> dict[str, str]:
    """Create-or-update each declared group, matched by NAME (which is also
    how Open WebUI's own OAuth group creation matches). Returns
    {name: group_id} for the model grants to resolve against."""
    ids: dict[str, str] = {}
    for entry in groups:
        name = (entry or {}).get("name")
        if not name:
            log.error("A groups[] entry has no name: %r", entry)
            sys.exit(1)
        description = entry.get("description", "")
        declared = entry.get("permissions")

        group = db.query(Group).filter(Group.name == name).first()
        if group is None:
            group = Group(
                id=str(uuid.uuid4()),
                user_id=owner_id,
                name=name,
                description=description,
                data={},
                meta={},
                # No declared permissions -> start from the instance
                # defaults, exactly as Open WebUI itself would.
                permissions=_deep_merge(defaults, declared) if declared else dict(defaults),
                created_at=now,
                updated_at=now,
            )
            db.add(group)
            action = "created"
        else:
            group.description = description
            if declared:
                # Merge onto the CURRENT stored permissions, not onto the
                # defaults: an admin may have adjusted something in the UI
                # that the YAML has no opinion about, and re-running this
                # Job should not silently revert it.
                group.permissions = _deep_merge(group.permissions or dict(defaults), declared)
            group.updated_at = now
            action = "updated"

        ids[name] = group.id
        log.info(
            "Group '%s' (%s) %s%s",
            name,
            group.id,
            action,
            "" if declared else " [permissions left at instance defaults]",
        )
    return ids


def _sync_models(
    db, Model, AccessGrant, owner_id: str, models: list, group_ids: dict[str, str], now: int
) -> None:
    for entry in models:
        model_id = (entry or {}).get("id")
        base_model_id = entry.get("base_model_id")
        if not model_id or not base_model_id:
            log.error("A models[] entry is missing id or base_model_id: %r", entry)
            sys.exit(1)

        params = dict(entry.get("params") or {})
        meta = {
            "description": entry.get("description", ""),
            "capabilities": entry.get("capabilities") or {},
            "tags": [{"name": t} for t in (entry.get("tags") or [])],
            "suggestion_prompts": entry.get("suggestion_prompts") or None,
            # Marker so it is obvious in the UI that editing this model by
            # hand will be overwritten on the next reconcile.
            "managed_by": "GitOps (job-sync-workspace)",
        }

        model = db.query(Model).filter(Model.id == model_id).first()
        if model:
            model.name = entry.get("name", model_id)
            model.base_model_id = base_model_id
            model.params = params
            model.meta = meta
            model.updated_at = now
            action = "updated"
        else:
            model = Model(
                id=model_id,
                user_id=owner_id,
                base_model_id=base_model_id,
                name=entry.get("name", model_id),
                params=params,
                meta=meta,
                is_active=True,
                created_at=now,
                updated_at=now,
            )
            db.add(model)
            action = "created"

        desired: set[tuple[str, str]] = set()
        for group_name in entry.get("grant_to_groups") or []:
            group_id = group_ids.get(group_name)
            if group_id is None:
                # Unlike a missing tool grant this is worth failing on: a
                # workspace model nobody can see is not a partial success,
                # it is a typo. Every group a model grants to is declared
                # in the same file, so this can only be a mismatch.
                log.error(
                    "Model '%s' grants to group '%s', which is not declared in groups[].",
                    model_id,
                    group_name,
                )
                sys.exit(1)
            desired.add(("group", group_id))

        _reconcile_model_grants(db, AccessGrant, model_id, desired, now)
        log.info(
            "Model '%s' (base=%s) %s, readable by %d group(s)",
            model_id,
            base_model_id,
            action,
            len(desired),
        )


def _reconcile_model_grants(
    db, AccessGrant, model_id: str, desired: set[tuple[str, str]], now: int
) -> None:
    """Make the 'read' access grants for this model match `desired` exactly:
    insert missing, delete stale. Mirrors sync_tools.py's equivalent."""
    existing_rows = (
        db.query(AccessGrant)
        .filter(
            AccessGrant.resource_type == "model",
            AccessGrant.resource_id == model_id,
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
                id=f"{model_id}:{principal_type}:{principal_id}:read",
                resource_type="model",
                resource_id=model_id,
                principal_type=principal_type,
                principal_id=principal_id,
                permission="read",
                created_at=now,
            )
        )


if __name__ == "__main__":
    main()
