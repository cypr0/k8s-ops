# MIRROR NOTICE: this file is a byte-for-byte duplicate of
# kubernetes/apps/open-webui/open-webui/app/tools/paperless_full.py
# (kustomize's configMapGenerator refuses file paths that escape its own
# kustomization directory, so this can't be a single shared file --
# see owui_tool_mcp_bridge.py's module docstring). Keep both copies in
# sync when editing either -- the Tools class itself is 100% Open-WebUI-
# runtime-independent, so the exact same file works unmodified as both an
# Open WebUI Tool and (via the bridge script) a standalone MCP server.
"""
title: Paperless-ngx (Full Access)
author: cypr0
version: 1.0.0
license: MIT
requirements: httpx
description: >
  FULL access to this cluster's Paperless-ngx REST API
  (https://docs.paperless-ngx.com/api/): documents (search, read, update,
  delete, upload, notes, versions, bulk operations incl. reprocess/rotate/
  merge/split/edit_pdf/remove_password/delete), correspondents, document
  types, tags (incl. hierarchical parent/child), storage paths, custom
  fields, saved views, mail accounts/rules, share links, workflows/triggers/
  actions, processed mail, users, groups, tasks, trash, system status/
  statistics/config/logs/remote_version, and a raw-request escape hatch for
  anything not covered by a named method below.

  This is NOT read-only: it can create, modify, and PERMANENTLY DELETE
  documents and every other resource type in Paperless. There is no
  confirmation step beyond whatever Open WebUI's own tool-call approval UI
  provides -- treat every delete_*/remove_*/empty_* call as final.

  Scope decisions (so behaviour here doesn't need reverse-engineering
  later): resources with a small, well-documented field set (documents,
  correspondents, document_types, tags, storage_paths, custom_fields) get
  explicit typed parameters. Resources whose schema is large, version-
  dependent, or not confirmed against the actual paperless-ngx source at
  authoring time (saved_views, mail_accounts, mail_rules, share_links,
  workflows/triggers/actions, users, groups) take a plain `fields: dict`
  for create/update instead of guessing named kwargs that might not match
  your version -- pass whatever field names the Paperless API/admin UI
  documents for that resource. `raw_request()` at the very end is a last-
  resort passthrough to any endpoint/query-param combination not covered
  above (e.g. a filter this file didn't anticipate, or an endpoint added in
  a paperless-ngx release after this file was written).

  Deliberately NOT implemented: profile/generate_auth_token (would rotate
  the very token this tool authenticates with -- and the same token backs
  the paperless-cronjob-fix-ownership CronJob -- locking both out),
  TOTP/2FA setup, social-account-provider management, and the raw
  document download/preview/thumbnail bytes for large files (capped by
  MAX_INLINE_DOWNLOAD_BYTES below; oversized files return an error instead
  of flooding the chat with base64).

  Auth model: a single shared Paperless API token -- the SAME token
  paperless-cronjob-fix-ownership (kubernetes/apps/paperless/paperless-ngx/
  app/jobs.yaml) already uses. No new Paperless credential is provisioned
  for this tool. Its default is pre-filled from an env var on the Open
  WebUI pod (see PAPERLESS_API_TOKEN in this repo's
  kubernetes/apps/open-webui/open-webui/app/externalsecret-paperless-token.yaml),
  itself pulled from the same 1Password "paperless" item paperless-secret
  already reads -- so users never need to enter or even see a credential.
  Whatever this token's underlying Paperless user account can and cannot do
  is the real ceiling on this tool (confirmed live: this token's account is
  NOT staff, so e.g. get_system_status()/list_users() currently 403 even
  though the methods exist below -- widen that account's Paperless
  permissions if you want those to work).

  NOTE: get_profile() echoes the configured token's own `auth_token` field
  back verbatim (that's what Paperless's /api/profile/ returns) -- handle
  its output with the same care as the token itself.
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class Tools:
    class Valves(BaseModel):
        """Admin-configured, shared by every user.

        API_TOKEN defaults to the PAPERLESS_API_TOKEN environment variable
        on the Open WebUI pod (populated from the same 1Password item the
        paperless-secret ExternalSecret already reads -- see
        externalsecret-paperless-token.yaml). Users should never need to
        touch this; it exists here only as a manual override/rotation
        escape hatch.
        """

        PAPERLESS_BASE_URL: str = Field(
            default="http://paperless.paperless.svc.cluster.local",
            description="Paperless-ngx base URL, in-cluster Service DNS "
            "(no trailing slash, no /api suffix).",
        )
        API_TOKEN: str = Field(
            default_factory=lambda: os.getenv("PAPERLESS_API_TOKEN", ""),
            description="Paperless API token (Authorization: Token <..>) "
            "-- auto-filled from the cluster secret; override only to "
            "rotate/test.",
        )
        REQUEST_TIMEOUT_SECONDS: int = Field(default=30)
        DEFAULT_PAGE_SIZE: int = Field(default=25)
        MAX_PAGE_SIZE: int = Field(
            default=200,
            description="Safety cap on page_size for list_* methods, so a "
            "single call can't flood the chat with the entire document "
            "library.",
        )
        MAX_INLINE_DOWNLOAD_BYTES: int = Field(
            default=3_000_000,
            description="Cap on how large a file download_document/"
            "get_document_preview/get_document_thumbnail will inline as "
            "base64. Larger files return an error with the size instead.",
        )

    def __init__(self):
        self.valves = self.Valves()

    # ------------------------------------------------------------------
    # internal helpers (not exposed to the model)
    # ------------------------------------------------------------------
    def _auth_header(self) -> dict[str, str]:
        if not self.valves.API_TOKEN:
            raise RuntimeError(
                "No Paperless API token configured. This should be "
                "auto-filled from the cluster secret -- if missing, check "
                "PAPERLESS_API_TOKEN on the Open WebUI pod, or set it "
                "manually in this tool's Valves (gear icon)."
            )
        return {"Authorization": f"Token {self.valves.API_TOKEN}"}

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.valves.PAPERLESS_BASE_URL.rstrip("/"),
            headers={**self._auth_header(), "Accept": "application/json"},
            timeout=self.valves.REQUEST_TIMEOUT_SECONDS,
        )

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
        data: Optional[dict[str, Any]] = None,
        files: Optional[dict[str, Any]] = None,
    ) -> Any:
        try:
            with self._client() as client:
                resp = client.request(
                    method, path, params=params, json=json_body, data=data, files=files
                )
                if resp.status_code == 204:
                    return {"status": "deleted"}
                resp.raise_for_status()
                if not resp.content:
                    return {}
                ctype = resp.headers.get("content-type", "")
                if "application/json" not in ctype:
                    return {
                        "content_type": ctype,
                        "content_length": len(resp.content),
                        "note": "Non-JSON response; use a *_bytes helper "
                        "if this is a document download/preview/thumbnail.",
                    }
                return resp.json()
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            detail = ""
            try:
                detail = str(e.response.json())
            except Exception:  # noqa: BLE001
                detail = e.response.text[:500]
            if status == 401:
                return {
                    "error": "Paperless rejected the API token (401). Check "
                    "the token configured in this tool's Valves."
                }
            if status == 403:
                return {
                    "error": "Paperless denied access (403). The configured "
                    "token's Paperless user likely lacks permission for "
                    "this action (e.g. it may not be a staff user). "
                    "Detail: " + detail
                }
            if status == 404:
                return {"error": "Not found (404). Check the id/path."}
            if status in (400, 422):
                return {"error": f"Validation error ({status}): {detail}"}
            return {"error": f"Paperless API error: HTTP {status}. {detail}"}
        except httpx.RequestError as e:
            return {"error": f"Could not reach Paperless: {e}"}

    def _get(self, path: str, params: Optional[dict[str, Any]] = None) -> Any:
        return self._request("GET", path, params=params)

    def _post(self, path: str, json_body: Optional[dict[str, Any]] = None) -> Any:
        return self._request("POST", path, json_body=json_body or {})

    def _patch(self, path: str, json_body: dict[str, Any]) -> Any:
        return self._request("PATCH", path, json_body=json_body)

    def _delete(self, path: str, json_body: Optional[dict[str, Any]] = None) -> Any:
        return self._request("DELETE", path, json_body=json_body)

    def _page_params(self, page: int, page_size: int) -> dict[str, Any]:
        return {"page": page, "page_size": min(page_size, self.valves.MAX_PAGE_SIZE)}

    def _download_bytes(self, path: str) -> str:
        """Fetch a binary sub-resource (download/preview/thumb) and return
        it base64-encoded, capped by MAX_INLINE_DOWNLOAD_BYTES."""
        try:
            with self._client() as client:
                resp = client.get(path)
                resp.raise_for_status()
                size = len(resp.content)
                if size > self.valves.MAX_INLINE_DOWNLOAD_BYTES:
                    return str(
                        {
                            "error": f"File is {size} bytes, over the "
                            f"{self.valves.MAX_INLINE_DOWNLOAD_BYTES}-byte "
                            "inline cap (MAX_INLINE_DOWNLOAD_BYTES valve). "
                            "Use the Paperless web UI to download this one."
                        }
                    )
                return str(
                    {
                        "content_type": resp.headers.get("content-type", ""),
                        "size_bytes": size,
                        "base64": base64.b64encode(resp.content).decode("ascii"),
                    }
                )
        except httpx.HTTPStatusError as e:
            return str({"error": f"HTTP {e.response.status_code}: {e.response.text[:300]}"})
        except httpx.RequestError as e:
            return str({"error": f"Could not reach Paperless: {e}"})

    # ==================================================================
    # Documents
    # ==================================================================
    def list_documents(
        self,
        query: Optional[str] = None,
        title_content: Optional[str] = None,
        more_like_id: Optional[int] = None,
        tag_id: Optional[int] = None,
        correspondent_id: Optional[int] = None,
        document_type_id: Optional[int] = None,
        storage_path_id: Optional[int] = None,
        created_after: Optional[str] = None,
        created_before: Optional[str] = None,
        added_after: Optional[str] = None,
        added_before: Optional[str] = None,
        custom_field_query: Optional[str] = None,
        ordering: Optional[str] = None,
        page: int = 1,
        page_size: int = 25,
        extra_filters: Optional[dict[str, Any]] = None,
    ) -> str:
        """List/search Paperless documents.

        :param query: Full-text search query (searches OCR content + title).
        :param title_content: Filter by title/content substring (title_content__icontains style shortcut).
        :param more_like_id: Return documents similar to this document id (Paperless "more like this").
        :param tag_id: Filter by tag id.
        :param correspondent_id: Filter by correspondent id.
        :param document_type_id: Filter by document type id.
        :param storage_path_id: Filter by storage path id.
        :param created_after: ISO date (YYYY-MM-DD), documents created on/after.
        :param created_before: ISO date, documents created on/before.
        :param added_after: ISO date, documents added to Paperless on/after.
        :param added_before: ISO date, documents added to Paperless on/before.
        :param custom_field_query: Advanced custom-field filter expression, e.g. ["AND", [["1", "exact", "foo"]]] serialized as documented at docs.paperless-ngx.com/api (operators: exact, in, isnull, exists, icontains, istartswith, iendswith, gt, gte, lt, lte, range, contains).
        :param ordering: Sort field, e.g. "-created" (default desc by best match/relevance when query is set).
        :param extra_filters: Any other Paperless list filter as raw query params not covered above, e.g. {"is_tagged": "false", "archive_serial_number": 123}.
        """
        params = self._page_params(page, page_size)
        if query:
            params["query"] = query
        if title_content:
            params["title_content"] = title_content
        if more_like_id is not None:
            params["more_like_id"] = more_like_id
        if tag_id is not None:
            params["tags__id"] = tag_id
        if correspondent_id is not None:
            params["correspondent__id"] = correspondent_id
        if document_type_id is not None:
            params["document_type__id"] = document_type_id
        if storage_path_id is not None:
            params["storage_path__id"] = storage_path_id
        if created_after:
            params["created__date__gte"] = created_after
        if created_before:
            params["created__date__lte"] = created_before
        if added_after:
            params["added__date__gte"] = added_after
        if added_before:
            params["added__date__lte"] = added_before
        if custom_field_query:
            params["custom_field_query"] = custom_field_query
        if ordering:
            params["ordering"] = ordering
        if extra_filters:
            params.update(extra_filters)
        return str(self._get("/api/documents/", params=params))

    def get_document(self, document_id: int, full_perms: bool = False) -> str:
        """Get full details of a single Paperless document.

        :param full_perms: If true, include complete view/change permission details instead of just the user_can_change flag.
        """
        params = {"full_perms": "true"} if full_perms else None
        return str(self._get(f"/api/documents/{document_id}/", params=params))

    def get_document_metadata(self, document_id: int) -> str:
        """Get file-level metadata (mime type, checksums, page count, EXIF/PDF metadata, etc.) for a document."""
        return str(self._get(f"/api/documents/{document_id}/metadata/"))

    def download_document(self, document_id: int, original: bool = False) -> str:
        """Download a document's file, base64-encoded (capped by the MAX_INLINE_DOWNLOAD_BYTES valve).

        :param original: If true, fetch the original uploaded file instead of the archived (usually OCR'd PDF/A) version.
        """
        params = "?original=true" if original else ""
        return self._download_bytes(f"/api/documents/{document_id}/download/{params}")

    def get_document_preview(self, document_id: int) -> str:
        """Get a document's preview image, base64-encoded (capped by MAX_INLINE_DOWNLOAD_BYTES)."""
        return self._download_bytes(f"/api/documents/{document_id}/preview/")

    def get_document_thumbnail(self, document_id: int) -> str:
        """Get a document's small thumbnail image, base64-encoded (usually well under the inline cap)."""
        return self._download_bytes(f"/api/documents/{document_id}/thumb/")

    def update_document(
        self,
        document_id: int,
        title: Optional[str] = None,
        content: Optional[str] = None,
        correspondent_id: Optional[int] = None,
        document_type_id: Optional[int] = None,
        storage_path_id: Optional[int] = None,
        tag_ids: Optional[list[int]] = None,
        archive_serial_number: Optional[int] = None,
        created: Optional[str] = None,
        custom_fields: Optional[list[dict[str, Any]]] = None,
        owner_id: Optional[int] = None,
        set_permissions: Optional[dict[str, Any]] = None,
    ) -> str:
        """Update fields on an existing Paperless document. Only pass the fields you want to change.

        :param correspondent_id: Correspondent id, or omit to leave unchanged (pass -1 semantics not supported -- use bulk_edit's set_correspondent with null to clear).
        :param tag_ids: FULL replacement list of tag ids (not additive -- use add_tag_to_documents/remove_tag_from_documents for additive changes).
        :param created: ISO datetime, e.g. "2026-01-15T00:00:00Z".
        :param custom_fields: List of {"field": <custom_field_id>, "value": <value>} objects -- FULL replacement of the document's custom field values.
        :param set_permissions: {"view": {"users": [...], "groups": [...]}, "change": {"users": [...], "groups": [...]}}.
        """
        body: dict[str, Any] = {}
        if title is not None:
            body["title"] = title
        if content is not None:
            body["content"] = content
        if correspondent_id is not None:
            body["correspondent"] = correspondent_id
        if document_type_id is not None:
            body["document_type"] = document_type_id
        if storage_path_id is not None:
            body["storage_path"] = storage_path_id
        if tag_ids is not None:
            body["tags"] = tag_ids
        if archive_serial_number is not None:
            body["archive_serial_number"] = archive_serial_number
        if created is not None:
            body["created"] = created
        if custom_fields is not None:
            body["custom_fields"] = custom_fields
        if owner_id is not None:
            body["owner"] = owner_id
        if set_permissions is not None:
            body["set_permissions"] = set_permissions
        return str(self._patch(f"/api/documents/{document_id}/", body))

    def delete_document(self, document_id: int) -> str:
        """Move a single Paperless document to the trash (recoverable via restore_documents_from_trash until it's emptied). Prefer this over bulk delete for a single document."""
        return str(self._delete(f"/api/documents/{document_id}/"))

    def upload_document(
        self,
        file_content_base64: str,
        filename: str,
        title: Optional[str] = None,
        created: Optional[str] = None,
        correspondent_id: Optional[int] = None,
        document_type_id: Optional[int] = None,
        storage_path_id: Optional[int] = None,
        tag_ids: Optional[list[int]] = None,
        archive_serial_number: Optional[int] = None,
        custom_fields: Optional[list[int]] = None,
    ) -> str:
        """Upload a new document into Paperless for consumption (OCR, classification, etc.). Returns a task UUID -- poll get_task() with it to see when consumption finishes.

        :param file_content_base64: The file's raw bytes, base64-encoded.
        :param filename: Filename including extension, e.g. "invoice.pdf".
        :param custom_fields: List of custom_field ids to pre-populate (values are filled in afterwards via update_document).
        """
        try:
            file_bytes = base64.b64decode(file_content_base64)
        except Exception as e:  # noqa: BLE001
            return str({"error": f"Invalid base64 in file_content_base64: {e}"})
        data: dict[str, Any] = {}
        if title:
            data["title"] = title
        if created:
            data["created"] = created
        if correspondent_id is not None:
            data["correspondent"] = correspondent_id
        if document_type_id is not None:
            data["document_type"] = document_type_id
        if storage_path_id is not None:
            data["storage_path"] = storage_path_id
        if tag_ids:
            data["tags"] = tag_ids
        if archive_serial_number is not None:
            data["archive_serial_number"] = archive_serial_number
        if custom_fields:
            data["custom_fields"] = custom_fields
        return str(
            self._request(
                "POST",
                "/api/documents/post_document/",
                data=data,
                files={"document": (filename, file_bytes)},
            )
        )

    def list_document_notes(self, document_id: int) -> str:
        """List notes attached to a Paperless document."""
        return str(self._get(f"/api/documents/{document_id}/notes/"))

    def add_document_note(self, document_id: int, note: str) -> str:
        """Add a note to a Paperless document."""
        return str(self._post(f"/api/documents/{document_id}/notes/", {"note": note}))

    def delete_document_note(self, document_id: int, note_id: int) -> str:
        """PERMANENTLY delete a note from a document. Irreversible."""
        return str(self._request("DELETE", f"/api/documents/{document_id}/notes/", params={"id": note_id}))

    def list_document_versions(self, document_id: int) -> str:
        """List all versions (root + any additional versions) of a document."""
        return str(self._get(f"/api/documents/{document_id}/versions/"))

    def get_document_version(self, document_id: int, version_id: int) -> str:
        """Get metadata for a single version of a document."""
        return str(self._get(f"/api/documents/{document_id}/versions/{version_id}/"))

    def update_document_version_label(self, document_id: int, version_id: int, version_label: str) -> str:
        """Rename (relabel) an existing document version."""
        return str(self._patch(f"/api/documents/{document_id}/versions/{version_id}/", {"version_label": version_label}))

    def delete_document_version(self, document_id: int, version_id: int) -> str:
        """PERMANENTLY delete one version of a document (not the whole document). Irreversible."""
        return str(self._delete(f"/api/documents/{document_id}/versions/{version_id}/"))

    def add_document_version(
        self, document_id: int, file_content_base64: str, filename: str, version_label: Optional[str] = None
    ) -> str:
        """Upload a new file as an additional version of an existing document (keeps the same title/tags/correspondent/etc, which are shared across all versions)."""
        try:
            file_bytes = base64.b64decode(file_content_base64)
        except Exception as e:  # noqa: BLE001
            return str({"error": f"Invalid base64 in file_content_base64: {e}"})
        data: dict[str, Any] = {"version_label": version_label} if version_label else {}
        return str(
            self._request(
                "POST",
                f"/api/documents/{document_id}/update_version/",
                data=data,
                files={"document": (filename, file_bytes)},
            )
        )

    def get_documents_selection_data(self, document_ids: list[int]) -> str:
        """Get aggregate metadata (common tags/correspondents/types/etc) for a set of selected documents -- what the Paperless UI uses to populate its bulk-edit panel."""
        return str(self._post("/api/documents/selection_data/", {"documents": document_ids}))

    def bulk_download_documents(
        self,
        document_ids: list[int],
        content: str = "both",
        compression: Optional[str] = None,
    ) -> str:
        """Prepare a zip download of multiple documents. Returns binary zip data info -- for actual retrieval, use the Paperless web UI; this reports success/size only since zip bytes aren't practical to inline into chat.

        :param content: "both", "originals", or "archive".
        :param compression: Optional zip compression level/method if your Paperless version supports it.
        """
        body: dict[str, Any] = {"documents": document_ids, "content": content}
        if compression:
            body["compression"] = compression
        try:
            with self._client() as client:
                resp = client.post("/api/documents/bulk_download/", json=body)
                resp.raise_for_status()
                return str(
                    {
                        "status": "ok",
                        "size_bytes": len(resp.content),
                        "note": "Zip built successfully server-side; download it directly "
                        "via the Paperless web UI (this tool does not inline zip bytes).",
                    }
                )
        except httpx.HTTPStatusError as e:
            return str({"error": f"HTTP {e.response.status_code}: {e.response.text[:300]}"})
        except httpx.RequestError as e:
            return str({"error": f"Could not reach Paperless: {e}"})

    # ------------------------------------------------------------------
    # Documents: bulk_edit (one Paperless endpoint, one method per
    # "method" value it supports -- see module docstring for why this file
    # spells each out explicitly instead of one generic dispatcher).
    # ------------------------------------------------------------------
    def _bulk_edit(self, document_ids: list[int], method: str, parameters: dict[str, Any]) -> str:
        return str(
            self._post(
                "/api/documents/bulk_edit/",
                {"documents": document_ids, "method": method, "parameters": parameters},
            )
        )

    def set_documents_correspondent(self, document_ids: list[int], correspondent_id: Optional[int]) -> str:
        """Set (or clear, if correspondent_id is None) the correspondent on multiple documents at once."""
        return self._bulk_edit(document_ids, "set_correspondent", {"correspondent": correspondent_id})

    def set_documents_document_type(self, document_ids: list[int], document_type_id: Optional[int]) -> str:
        """Set (or clear) the document type on multiple documents at once."""
        return self._bulk_edit(document_ids, "set_document_type", {"document_type": document_type_id})

    def set_documents_storage_path(self, document_ids: list[int], storage_path_id: Optional[int]) -> str:
        """Set (or clear) the storage path on multiple documents at once."""
        return self._bulk_edit(document_ids, "set_storage_path", {"storage_path": storage_path_id})

    def add_tag_to_documents(self, document_ids: list[int], tag_id: int) -> str:
        """Add one tag to multiple documents at once (additive, keeps existing tags)."""
        return self._bulk_edit(document_ids, "add_tag", {"tag": tag_id})

    def remove_tag_from_documents(self, document_ids: list[int], tag_id: int) -> str:
        """Remove one tag from multiple documents at once."""
        return self._bulk_edit(document_ids, "remove_tag", {"tag": tag_id})

    def modify_documents_tags(
        self, document_ids: list[int], add_tag_ids: Optional[list[int]] = None, remove_tag_ids: Optional[list[int]] = None
    ) -> str:
        """Add and/or remove multiple tags on multiple documents in one call."""
        return self._bulk_edit(
            document_ids, "modify_tags", {"add_tags": add_tag_ids or [], "remove_tags": remove_tag_ids or []}
        )

    def modify_documents_custom_fields(
        self,
        document_ids: list[int],
        add_custom_fields: Optional[Any] = None,
        remove_custom_field_ids: Optional[list[int]] = None,
    ) -> str:
        """Add/update and/or remove custom field values on multiple documents in one call.

        :param add_custom_fields: List of {"field": id, "value": val} to set/overwrite, or a {field_id: value} dict depending on your Paperless version.
        :param remove_custom_field_ids: List of custom_field ids to remove from these documents.
        """
        params: dict[str, Any] = {}
        if add_custom_fields is not None:
            params["add_custom_fields"] = add_custom_fields
        if remove_custom_field_ids is not None:
            params["remove_custom_fields"] = remove_custom_field_ids
        return self._bulk_edit(document_ids, "modify_custom_fields", params)

    def set_documents_permissions(
        self,
        document_ids: list[int],
        owner_id: Optional[int] = None,
        set_permissions: Optional[dict[str, Any]] = None,
        merge: bool = False,
    ) -> str:
        """Set owner and/or view/change permissions on multiple documents at once.

        :param set_permissions: {"view": {"users": [...], "groups": [...]}, "change": {"users": [...], "groups": [...]}}.
        :param merge: If true, merge with each document's existing permissions instead of replacing them.
        """
        params: dict[str, Any] = {"merge": merge}
        if owner_id is not None:
            params["owner"] = owner_id
        if set_permissions is not None:
            params["set_permissions"] = set_permissions
        return self._bulk_edit(document_ids, "set_permissions", params)

    def delete_documents(self, document_ids: list[int]) -> str:
        """Move multiple documents to the trash at once (recoverable until trash is emptied)."""
        return self._bulk_edit(document_ids, "delete", {})

    def reprocess_documents(self, document_ids: list[int]) -> str:
        """Re-run OCR/consumption on multiple existing documents (e.g. after changing OCR language settings)."""
        return self._bulk_edit(document_ids, "reprocess", {})

    def rotate_documents(self, document_ids: list[int], degrees: float, source_mode: Optional[str] = None) -> str:
        """Rotate multiple documents' pages by a fixed angle.

        :param degrees: Rotation angle, e.g. 90, 180, 270 (or -90).
        :param source_mode: Optional Paperless rotate source mode, if your version distinguishes original vs archive rotation.
        """
        params: dict[str, Any] = {"degrees": degrees}
        if source_mode:
            params["source_mode"] = source_mode
        return self._bulk_edit(document_ids, "rotate", params)

    def merge_documents(
        self, document_ids: list[int], delete_originals: bool = False, archive_fallback: bool = False
    ) -> str:
        """Merge multiple documents (in the given order) into a single new document.

        :param delete_originals: If true, move the source documents to the trash after merging.
        :param archive_fallback: If true, fall back to each document's archive version if the original can't be used for merging.
        """
        return self._bulk_edit(
            document_ids,
            "merge",
            {"delete_originals": delete_originals, "archive_fallback": archive_fallback},
        )

    def split_document(self, document_id: int, pages: str, delete_originals: bool = False) -> str:
        """Split a single document into multiple new documents by page ranges.

        :param pages: Page ranges, e.g. "1-2,3-5,6" (parsed by Paperless server-side).
        :param delete_originals: If true, move the source document to the trash after splitting.
        """
        return self._bulk_edit([document_id], "split", {"pages": pages, "delete_originals": delete_originals})

    def delete_document_pages(self, document_id: int, page_numbers: list[int]) -> str:
        """PERMANENTLY delete specific pages from a document (creates a new version without them). Irreversible for the removed pages."""
        return self._bulk_edit([document_id], "delete_pages", {"pages": page_numbers})

    def edit_pdf_documents(self, document_id: int, operations: list[dict[str, Any]], update_document: bool = True) -> str:
        """Apply a sequence of PDF page edit operations (e.g. per-page rotation) to a document.

        :param operations: List of {"page": n, "rotate": degrees, ...} operation objects as documented by Paperless's edit_pdf bulk-edit method.
        :param update_document: If true, apply the edits to the existing document; if false, create a new document instead.
        """
        return self._bulk_edit(
            [document_id], "edit_pdf", {"operations": operations, "update_document": update_document}
        )

    def remove_document_password(self, document_id: int, password: str) -> str:
        """Remove password protection from an encrypted PDF document, given its current password."""
        return self._bulk_edit([document_id], "remove_password", {"password": password})

    # ==================================================================
    # Search
    # ==================================================================
    def search_documents(self, query: str, page: int = 1, page_size: int = 25) -> str:
        """Full-text search over documents (title + OCR content). Equivalent to list_documents(query=...) but kept as its own method for a clearer model-facing name."""
        params = self._page_params(page, page_size)
        params["query"] = query
        return str(self._get("/api/documents/", params=params))

    def global_search(self, query: str) -> str:
        """Search across multiple Paperless object types at once (documents, correspondents, tags, etc) -- the same search the Paperless UI's omnibar uses."""
        return str(self._get("/api/search/", params={"query": query}))

    def autocomplete_search_terms(self, term: str, limit: int = 10) -> str:
        """Get autocomplete suggestions for a partial search term, ordered by frequency."""
        return str(self._get("/api/search/autocomplete/", params={"term": term, "limit": limit}))

    # ==================================================================
    # Correspondents
    # ==================================================================
    def list_correspondents(self, name: Optional[str] = None, page: int = 1, page_size: int = 25) -> str:
        """List Paperless correspondents (who sent/received a document)."""
        params = self._page_params(page, page_size)
        if name:
            params["name__icontains"] = name
        return str(self._get("/api/correspondents/", params=params))

    def get_correspondent(self, correspondent_id: int) -> str:
        """Get a single correspondent by id."""
        return str(self._get(f"/api/correspondents/{correspondent_id}/"))

    def create_correspondent(
        self,
        name: str,
        match: Optional[str] = None,
        matching_algorithm: Optional[int] = None,
        is_insensitive: Optional[bool] = None,
        owner_id: Optional[int] = None,
    ) -> str:
        """Create a new correspondent.

        :param match: Auto-matching text/regex, used with matching_algorithm to auto-assign this correspondent to future documents.
        :param matching_algorithm: 0=none, 1=any word, 2=all words, 3=exact, 4=regex, 5=fuzzy, 6=auto.
        """
        body: dict[str, Any] = {"name": name}
        if match is not None:
            body["match"] = match
        if matching_algorithm is not None:
            body["matching_algorithm"] = matching_algorithm
        if is_insensitive is not None:
            body["is_insensitive"] = is_insensitive
        if owner_id is not None:
            body["owner"] = owner_id
        return str(self._post("/api/correspondents/", body))

    def update_correspondent(self, correspondent_id: int, **fields: Any) -> str:
        """Update an existing correspondent. Pass any correspondent fields to change, e.g. name, match, matching_algorithm, is_insensitive, owner."""
        return str(self._patch(f"/api/correspondents/{correspondent_id}/", fields))

    def delete_correspondent(self, correspondent_id: int) -> str:
        """PERMANENTLY delete a correspondent (documents keep existing, just lose this correspondent). Irreversible."""
        return str(self._delete(f"/api/correspondents/{correspondent_id}/"))

    # ==================================================================
    # Document types
    # ==================================================================
    def list_document_types(self, name: Optional[str] = None, page: int = 1, page_size: int = 25) -> str:
        """List Paperless document types."""
        params = self._page_params(page, page_size)
        if name:
            params["name__icontains"] = name
        return str(self._get("/api/document_types/", params=params))

    def get_document_type(self, document_type_id: int) -> str:
        """Get a single document type by id."""
        return str(self._get(f"/api/document_types/{document_type_id}/"))

    def create_document_type(
        self,
        name: str,
        match: Optional[str] = None,
        matching_algorithm: Optional[int] = None,
        is_insensitive: Optional[bool] = None,
        owner_id: Optional[int] = None,
    ) -> str:
        """Create a new document type. See create_correspondent's docstring for the matching_algorithm scale."""
        body: dict[str, Any] = {"name": name}
        if match is not None:
            body["match"] = match
        if matching_algorithm is not None:
            body["matching_algorithm"] = matching_algorithm
        if is_insensitive is not None:
            body["is_insensitive"] = is_insensitive
        if owner_id is not None:
            body["owner"] = owner_id
        return str(self._post("/api/document_types/", body))

    def update_document_type(self, document_type_id: int, **fields: Any) -> str:
        """Update an existing document type. Pass any fields to change, e.g. name, match, matching_algorithm, is_insensitive, owner."""
        return str(self._patch(f"/api/document_types/{document_type_id}/", fields))

    def delete_document_type(self, document_type_id: int) -> str:
        """PERMANENTLY delete a document type. Irreversible."""
        return str(self._delete(f"/api/document_types/{document_type_id}/"))

    # ==================================================================
    # Tags (hierarchical: support parent/children)
    # ==================================================================
    def list_tags(self, name: Optional[str] = None, page: int = 1, page_size: int = 25) -> str:
        """List Paperless tags, including their parent/children hierarchy and document_count."""
        params = self._page_params(page, page_size)
        if name:
            params["name__icontains"] = name
        return str(self._get("/api/tags/", params=params))

    def get_tag(self, tag_id: int) -> str:
        """Get a single tag by id."""
        return str(self._get(f"/api/tags/{tag_id}/"))

    def create_tag(
        self,
        name: str,
        color: Optional[str] = None,
        is_inbox_tag: Optional[bool] = None,
        parent_id: Optional[int] = None,
        match: Optional[str] = None,
        matching_algorithm: Optional[int] = None,
        is_insensitive: Optional[bool] = None,
    ) -> str:
        """Create a new tag.

        :param color: Hex color string, e.g. "#a6cee3".
        :param is_inbox_tag: If true, documents get this tag automatically on consumption and it marks them as "inbox"/unprocessed.
        :param parent_id: Optional parent tag id, to nest this tag under another (hierarchical tags).
        """
        body: dict[str, Any] = {"name": name}
        if color is not None:
            body["color"] = color
        if is_inbox_tag is not None:
            body["is_inbox_tag"] = is_inbox_tag
        if parent_id is not None:
            body["parent"] = parent_id
        if match is not None:
            body["match"] = match
        if matching_algorithm is not None:
            body["matching_algorithm"] = matching_algorithm
        if is_insensitive is not None:
            body["is_insensitive"] = is_insensitive
        return str(self._post("/api/tags/", body))

    def update_tag(self, tag_id: int, **fields: Any) -> str:
        """Update an existing tag. Pass any fields to change, e.g. name, color, is_inbox_tag, parent, match, matching_algorithm, is_insensitive."""
        return str(self._patch(f"/api/tags/{tag_id}/", fields))

    def delete_tag(self, tag_id: int) -> str:
        """PERMANENTLY delete a tag (removed from every document that had it; child tags are NOT cascade-deleted). Irreversible."""
        return str(self._delete(f"/api/tags/{tag_id}/"))

    # ==================================================================
    # Storage paths
    # ==================================================================
    def list_storage_paths(self, name: Optional[str] = None, page: int = 1, page_size: int = 25) -> str:
        """List Paperless storage paths (folder-naming templates for the archive)."""
        params = self._page_params(page, page_size)
        if name:
            params["name__icontains"] = name
        return str(self._get("/api/storage_paths/", params=params))

    def get_storage_path(self, storage_path_id: int) -> str:
        """Get a single storage path by id."""
        return str(self._get(f"/api/storage_paths/{storage_path_id}/"))

    def create_storage_path(
        self,
        name: str,
        path: str,
        match: Optional[str] = None,
        matching_algorithm: Optional[int] = None,
        is_insensitive: Optional[bool] = None,
    ) -> str:
        """Create a new storage path.

        :param path: Paperless storage path template, e.g. "{correspondent}/{document_type}/{created_year}/{title}".
        """
        body: dict[str, Any] = {"name": name, "path": path}
        if match is not None:
            body["match"] = match
        if matching_algorithm is not None:
            body["matching_algorithm"] = matching_algorithm
        if is_insensitive is not None:
            body["is_insensitive"] = is_insensitive
        return str(self._post("/api/storage_paths/", body))

    def update_storage_path(self, storage_path_id: int, **fields: Any) -> str:
        """Update an existing storage path. Pass any fields to change, e.g. name, path, match, matching_algorithm, is_insensitive."""
        return str(self._patch(f"/api/storage_paths/{storage_path_id}/", fields))

    def delete_storage_path(self, storage_path_id: int) -> str:
        """PERMANENTLY delete a storage path (documents using it keep their current file location, they just lose the assignment). Irreversible."""
        return str(self._delete(f"/api/storage_paths/{storage_path_id}/"))

    # ==================================================================
    # Custom fields (definitions)
    # ==================================================================
    def list_custom_fields(self, page: int = 1, page_size: int = 25) -> str:
        """List Paperless custom field definitions."""
        return str(self._get("/api/custom_fields/", params=self._page_params(page, page_size)))

    def get_custom_field(self, custom_field_id: int) -> str:
        """Get a single custom field definition by id."""
        return str(self._get(f"/api/custom_fields/{custom_field_id}/"))

    def create_custom_field(self, name: str, data_type: str, extra_data: Optional[dict[str, Any]] = None) -> str:
        """Create a new custom field definition.

        :param data_type: One of: string, url, date, boolean, integer, float, monetary, document_link, select.
        :param extra_data: Type-specific extra data, e.g. {"select_options": ["A", "B"]} for data_type "select", or {"default_currency": "EUR"} for "monetary".
        """
        body: dict[str, Any] = {"name": name, "data_type": data_type}
        if extra_data:
            body["extra_data"] = extra_data
        return str(self._post("/api/custom_fields/", body))

    def update_custom_field(self, custom_field_id: int, **fields: Any) -> str:
        """Update an existing custom field definition. Pass any fields to change, e.g. name, extra_data."""
        return str(self._patch(f"/api/custom_fields/{custom_field_id}/", fields))

    def delete_custom_field(self, custom_field_id: int) -> str:
        """PERMANENTLY delete a custom field definition and every document's stored value for it. Irreversible."""
        return str(self._delete(f"/api/custom_fields/{custom_field_id}/"))

    # ==================================================================
    # Saved views, mail accounts/rules, share links, workflows, users,
    # groups, processed mail: schemas here are larger/less certain across
    # Paperless versions, so create/update take a plain `fields` dict --
    # see the module docstring's "Scope decisions" section for why.
    # ==================================================================
    def list_saved_views(self, page: int = 1, page_size: int = 25) -> str:
        """List saved document views (filter presets)."""
        return str(self._get("/api/saved_views/", params=self._page_params(page, page_size)))

    def get_saved_view(self, saved_view_id: int) -> str:
        """Get a single saved view by id."""
        return str(self._get(f"/api/saved_views/{saved_view_id}/"))

    def create_saved_view(self, name: str, fields: Optional[dict[str, Any]] = None) -> str:
        """Create a new saved view. `fields` merges with {"name": name}; see the Paperless web UI's "save current view" dialog or API schema for available keys (show_on_dashboard, show_in_sidebar, sort_field, sort_reverse, page_size, filter_rules: [{"rule_type": int, "value": str}])."""
        body = {"name": name, **(fields or {})}
        return str(self._post("/api/saved_views/", body))

    def update_saved_view(self, saved_view_id: int, fields: dict[str, Any]) -> str:
        """Update an existing saved view. `fields` is applied as-is as the PATCH body."""
        return str(self._patch(f"/api/saved_views/{saved_view_id}/", fields))

    def delete_saved_view(self, saved_view_id: int) -> str:
        """PERMANENTLY delete a saved view. Irreversible."""
        return str(self._delete(f"/api/saved_views/{saved_view_id}/"))

    def list_mail_accounts(self, page: int = 1, page_size: int = 25) -> str:
        """List configured IMAP mail accounts (for mail-fetch consumption). Note: passwords are write-only and never returned."""
        return str(self._get("/api/mail_accounts/", params=self._page_params(page, page_size)))

    def get_mail_account(self, mail_account_id: int) -> str:
        """Get a single mail account by id."""
        return str(self._get(f"/api/mail_accounts/{mail_account_id}/"))

    def create_mail_account(self, name: str, fields: dict[str, Any]) -> str:
        """Create a new IMAP mail account. `fields` merges with {"name": name} -- see the Paperless web UI's mail account form or API schema for required keys (imap_server, imap_port, imap_security, username, password, ...)."""
        body = {"name": name, **fields}
        return str(self._post("/api/mail_accounts/", body))

    def update_mail_account(self, mail_account_id: int, fields: dict[str, Any]) -> str:
        """Update an existing mail account. `fields` is applied as-is as the PATCH body (include "password" to rotate it -- it's write-only)."""
        return str(self._patch(f"/api/mail_accounts/{mail_account_id}/", fields))

    def delete_mail_account(self, mail_account_id: int) -> str:
        """PERMANENTLY delete a mail account and its mail rules. Irreversible."""
        return str(self._delete(f"/api/mail_accounts/{mail_account_id}/"))

    def list_mail_rules(self, page: int = 1, page_size: int = 25) -> str:
        """List mail rules (what to do with mail fetched via a mail account)."""
        return str(self._get("/api/mail_rules/", params=self._page_params(page, page_size)))

    def get_mail_rule(self, mail_rule_id: int) -> str:
        """Get a single mail rule by id."""
        return str(self._get(f"/api/mail_rules/{mail_rule_id}/"))

    def create_mail_rule(self, name: str, account_id: int, fields: Optional[dict[str, Any]] = None) -> str:
        """Create a new mail rule. `fields` merges with {"name": name, "account": account_id} -- see the Paperless web UI's mail rule form or API schema for available keys (folder, filter_from, filter_subject, action, assign_tags, assign_correspondent, ...)."""
        body = {"name": name, "account": account_id, **(fields or {})}
        return str(self._post("/api/mail_rules/", body))

    def update_mail_rule(self, mail_rule_id: int, fields: dict[str, Any]) -> str:
        """Update an existing mail rule. `fields` is applied as-is as the PATCH body."""
        return str(self._patch(f"/api/mail_rules/{mail_rule_id}/", fields))

    def delete_mail_rule(self, mail_rule_id: int) -> str:
        """PERMANENTLY delete a mail rule. Irreversible."""
        return str(self._delete(f"/api/mail_rules/{mail_rule_id}/"))

    def list_share_links(self, document_id: Optional[int] = None, page: int = 1, page_size: int = 25) -> str:
        """List public share links, optionally filtered to one document."""
        params = self._page_params(page, page_size)
        if document_id is not None:
            params["document__id"] = document_id
        return str(self._get("/api/share_links/", params=params))

    def get_share_link(self, share_link_id: int) -> str:
        """Get a single share link by id."""
        return str(self._get(f"/api/share_links/{share_link_id}/"))

    def create_share_link(
        self, document_id: int, expiration: Optional[str] = None, file_version: str = "archive"
    ) -> str:
        """Create a public, unauthenticated share link for a document.

        :param expiration: Optional ISO datetime when the link expires; omit for no expiration.
        :param file_version: "archive" or "original".
        """
        body: dict[str, Any] = {"document": document_id, "file_version": file_version}
        if expiration:
            body["expiration"] = expiration
        return str(self._post("/api/share_links/", body))

    def update_share_link(self, share_link_id: int, fields: dict[str, Any]) -> str:
        """Update an existing share link. `fields` is applied as-is as the PATCH body."""
        return str(self._patch(f"/api/share_links/{share_link_id}/", fields))

    def delete_share_link(self, share_link_id: int) -> str:
        """PERMANENTLY revoke a share link. Irreversible."""
        return str(self._delete(f"/api/share_links/{share_link_id}/"))

    def list_share_link_bundles(self, page: int = 1, page_size: int = 25) -> str:
        """List share link bundles (a single public link covering multiple documents)."""
        return str(self._get("/api/share_link_bundles/", params=self._page_params(page, page_size)))

    def get_share_link_bundle(self, bundle_id: int) -> str:
        """Get a single share link bundle by id."""
        return str(self._get(f"/api/share_link_bundles/{bundle_id}/"))

    def create_share_link_bundle(self, document_ids: list[int], fields: Optional[dict[str, Any]] = None) -> str:
        """Create a new share link bundle covering multiple documents. `fields` merges with {"documents": document_ids}."""
        body = {"documents": document_ids, **(fields or {})}
        return str(self._post("/api/share_link_bundles/", body))

    def delete_share_link_bundle(self, bundle_id: int) -> str:
        """PERMANENTLY revoke a share link bundle. Irreversible."""
        return str(self._delete(f"/api/share_link_bundles/{bundle_id}/"))

    def list_workflows(self, page: int = 1, page_size: int = 25) -> str:
        """List consumption workflows (trigger -> actions automation rules)."""
        return str(self._get("/api/workflows/", params=self._page_params(page, page_size)))

    def get_workflow(self, workflow_id: int) -> str:
        """Get a single workflow by id."""
        return str(self._get(f"/api/workflows/{workflow_id}/"))

    def create_workflow(
        self,
        name: str,
        trigger_ids: Optional[list[int]] = None,
        action_ids: Optional[list[int]] = None,
        enabled: bool = True,
        fields: Optional[dict[str, Any]] = None,
    ) -> str:
        """Create a new workflow linking existing triggers and actions (create those first via create_workflow_trigger/create_workflow_action)."""
        body: dict[str, Any] = {"name": name, "enabled": enabled, **(fields or {})}
        if trigger_ids is not None:
            body["triggers"] = trigger_ids
        if action_ids is not None:
            body["actions"] = action_ids
        return str(self._post("/api/workflows/", body))

    def update_workflow(self, workflow_id: int, fields: dict[str, Any]) -> str:
        """Update an existing workflow. `fields` is applied as-is as the PATCH body (e.g. name, enabled, order, triggers, actions)."""
        return str(self._patch(f"/api/workflows/{workflow_id}/", fields))

    def delete_workflow(self, workflow_id: int) -> str:
        """PERMANENTLY delete a workflow (its triggers/actions are not deleted, just unlinked). Irreversible."""
        return str(self._delete(f"/api/workflows/{workflow_id}/"))

    def list_workflow_triggers(self, page: int = 1, page_size: int = 25) -> str:
        """List workflow triggers (the "when" half of a workflow)."""
        return str(self._get("/api/workflow_triggers/", params=self._page_params(page, page_size)))

    def get_workflow_trigger(self, trigger_id: int) -> str:
        """Get a single workflow trigger by id."""
        return str(self._get(f"/api/workflow_triggers/{trigger_id}/"))

    def create_workflow_trigger(self, fields: dict[str, Any]) -> str:
        """Create a new workflow trigger. See the Paperless web UI's workflow editor or API schema for required keys (type, sources, filter_path, filter_filename, filter_mailrule, ...)."""
        return str(self._post("/api/workflow_triggers/", fields))

    def update_workflow_trigger(self, trigger_id: int, fields: dict[str, Any]) -> str:
        """Update an existing workflow trigger. `fields` is applied as-is as the PATCH body."""
        return str(self._patch(f"/api/workflow_triggers/{trigger_id}/", fields))

    def delete_workflow_trigger(self, trigger_id: int) -> str:
        """PERMANENTLY delete a workflow trigger. Irreversible; also unlinks it from any workflow using it."""
        return str(self._delete(f"/api/workflow_triggers/{trigger_id}/"))

    def list_workflow_actions(self, page: int = 1, page_size: int = 25) -> str:
        """List workflow actions (the "then" half of a workflow)."""
        return str(self._get("/api/workflow_actions/", params=self._page_params(page, page_size)))

    def get_workflow_action(self, action_id: int) -> str:
        """Get a single workflow action by id."""
        return str(self._get(f"/api/workflow_actions/{action_id}/"))

    def create_workflow_action(self, fields: dict[str, Any]) -> str:
        """Create a new workflow action. See the Paperless web UI's workflow editor or API schema for required keys (type, assign_title, assign_tags, assign_correspondent, ...)."""
        return str(self._post("/api/workflow_actions/", fields))

    def update_workflow_action(self, action_id: int, fields: dict[str, Any]) -> str:
        """Update an existing workflow action. `fields` is applied as-is as the PATCH body."""
        return str(self._patch(f"/api/workflow_actions/{action_id}/", fields))

    def delete_workflow_action(self, action_id: int) -> str:
        """PERMANENTLY delete a workflow action. Irreversible; also unlinks it from any workflow using it."""
        return str(self._delete(f"/api/workflow_actions/{action_id}/"))

    def list_processed_mail(self, page: int = 1, page_size: int = 25) -> str:
        """List the log of previously processed inbound emails (which mail rule/account handled each, and the outcome)."""
        return str(self._get("/api/processed_mail/", params=self._page_params(page, page_size)))

    def get_processed_mail(self, processed_mail_id: int) -> str:
        """Get a single processed-mail log entry by id."""
        return str(self._get(f"/api/processed_mail/{processed_mail_id}/"))

    def delete_processed_mail(self, processed_mail_id: int) -> str:
        """PERMANENTLY delete a processed-mail log entry, allowing Paperless to re-fetch and reprocess that email on the next mail-check run. Irreversible for the log entry itself."""
        return str(self._delete(f"/api/processed_mail/{processed_mail_id}/"))

    # ==================================================================
    # Bulk edit objects (tags / correspondents / document_types /
    # storage_paths -- permissions/ownership or bulk delete)
    # ==================================================================
    def bulk_edit_objects(
        self,
        object_type: str,
        object_ids: list[int],
        operation: str,
        owner_id: Optional[int] = None,
        permissions: Optional[dict[str, Any]] = None,
        merge: bool = False,
    ) -> str:
        """Bulk-set permissions/ownership, or bulk-delete, tags/correspondents/document_types/storage_paths.

        :param object_type: One of "tags", "correspondents", "document_types", "storage_paths".
        :param operation: "set_permissions" or "delete".
        :param permissions: {"view": {"users": [...], "groups": [...]}, "change": {"users": [...], "groups": [...]}} -- only for operation="set_permissions".
        :param merge: If true, merge with each object's existing permissions instead of replacing them.
        """
        body: dict[str, Any] = {
            "objects": object_ids,
            "object_type": object_type,
            "operation": operation,
            "merge": merge,
        }
        if owner_id is not None:
            body["owner"] = owner_id
        if permissions is not None:
            body["permissions"] = permissions
        return str(self._post("/api/bulk_edit_objects/", body))

    # ==================================================================
    # Users & groups (admin) -- requires the configured token's Paperless
    # user to be staff/superuser, see module docstring.
    # ==================================================================
    def list_users(self, page: int = 1, page_size: int = 25) -> str:
        """List Paperless user accounts. Requires the configured API token's user to be staff."""
        return str(self._get("/api/users/", params=self._page_params(page, page_size)))

    def get_user(self, user_id: int) -> str:
        """Get a single Paperless user by id. Requires staff."""
        return str(self._get(f"/api/users/{user_id}/"))

    def create_user(self, username: str, fields: Optional[dict[str, Any]] = None) -> str:
        """Create a new Paperless user account. `fields` merges with {"username": username} (e.g. password, email, is_staff, is_active, is_superuser, groups). Requires staff/superuser."""
        body = {"username": username, **(fields or {})}
        return str(self._post("/api/users/", body))

    def update_user(self, user_id: int, fields: dict[str, Any]) -> str:
        """Update an existing Paperless user. `fields` is applied as-is as the PATCH body. Requires staff/superuser."""
        return str(self._patch(f"/api/users/{user_id}/", fields))

    def delete_user(self, user_id: int) -> str:
        """PERMANENTLY delete a Paperless user account. Irreversible. Requires superuser."""
        return str(self._delete(f"/api/users/{user_id}/"))

    def list_groups(self, page: int = 1, page_size: int = 25) -> str:
        """List Paperless permission groups. Requires staff."""
        return str(self._get("/api/groups/", params=self._page_params(page, page_size)))

    def get_group(self, group_id: int) -> str:
        """Get a single group by id. Requires staff."""
        return str(self._get(f"/api/groups/{group_id}/"))

    def create_group(self, name: str, permissions: Optional[list[str]] = None) -> str:
        """Create a new permission group. Requires staff/superuser.

        :param permissions: List of Django permission codenames, e.g. ["add_document", "change_document"].
        """
        body: dict[str, Any] = {"name": name}
        if permissions is not None:
            body["permissions"] = permissions
        return str(self._post("/api/groups/", body))

    def update_group(self, group_id: int, fields: dict[str, Any]) -> str:
        """Update an existing group. `fields` is applied as-is as the PATCH body. Requires staff/superuser."""
        return str(self._patch(f"/api/groups/{group_id}/", fields))

    def delete_group(self, group_id: int) -> str:
        """PERMANENTLY delete a permission group. Irreversible. Requires superuser."""
        return str(self._delete(f"/api/groups/{group_id}/"))

    # ==================================================================
    # Tasks (consumption/processing task tracking)
    # ==================================================================
    def list_tasks(self, page: int = 1, page_size: int = 25) -> str:
        """List Paperless background tasks (document consumption, etc), most recent first."""
        return str(self._get("/api/tasks/", params=self._page_params(page, page_size)))

    def get_task(self, task_id: str) -> str:
        """Get a single task's status/result by its UUID (returned by upload_document)."""
        return str(self._get(f"/api/tasks/{task_id}/"))

    def acknowledge_tasks(self, task_ids: list[int]) -> str:
        """Mark tasks as acknowledged/dismissed (clears them from the Paperless UI's notification bell)."""
        return str(self._post("/api/tasks/acknowledge/", {"tasks": task_ids}))

    def get_task_summary(self) -> str:
        """Get aggregate task counts/summary."""
        return str(self._get("/api/tasks/summary/"))

    def get_task_status_counts(self) -> str:
        """Get task counts grouped by status (pending/started/success/failure)."""
        return str(self._get("/api/tasks/status_counts/"))

    def list_active_tasks(self) -> str:
        """List currently running (not yet finished) tasks."""
        return str(self._get("/api/tasks/active/"))

    def run_task(self, task_name: str) -> str:
        """Manually dispatch a supported maintenance task (e.g. "index_optimize", "train_classifier", "sanity_check", "check_sanity" -- exact names depend on your Paperless version's admin UI "Manage" page). Requires the configured token's user to be privileged (staff/superuser)."""
        return str(self._post("/api/tasks/run/", {"task_name": task_name}))

    # ==================================================================
    # UI settings / config / status / statistics / logs / remote version
    # ==================================================================
    def get_ui_settings(self) -> str:
        """Get the current UI settings for the configured API token's user."""
        return str(self._get("/api/ui_settings/"))

    def update_ui_settings(self, fields: dict[str, Any]) -> str:
        """Update UI settings for the configured API token's user. `fields` is applied as-is as the PATCH body."""
        return str(self._patch("/api/ui_settings/", fields))

    def list_config(self, page: int = 1, page_size: int = 25) -> str:
        """List Paperless application configuration profile(s) (OCR/AI/global settings overridable at runtime)."""
        return str(self._get("/api/config/", params=self._page_params(page, page_size)))

    def get_config(self, config_id: int) -> str:
        """Get a single application configuration profile by id."""
        return str(self._get(f"/api/config/{config_id}/"))

    def update_config(self, config_id: int, fields: dict[str, Any]) -> str:
        """Update an application configuration profile. `fields` is applied as-is as the PATCH body. Requires staff/superuser."""
        return str(self._patch(f"/api/config/{config_id}/", fields))

    def get_system_status(self) -> str:
        """Get overall Paperless system status (versions, DB/index/classifier health, storage). Requires staff."""
        return str(self._get("/api/status/"))

    def get_statistics(self) -> str:
        """Get document/tag/correspondent usage statistics."""
        return str(self._get("/api/statistics/"))

    def get_remote_version(self) -> str:
        """Check the latest available Paperless-ngx release vs. the currently running version."""
        return str(self._get("/api/remote_version/"))

    def list_log_files(self) -> str:
        """List available Paperless log files (e.g. "paperless", "mail"). Requires staff."""
        return str(self._get("/api/logs/"))

    def get_log(self, log_name: str) -> str:
        """Get the contents of a specific Paperless log file (by name from list_log_files). Requires staff."""
        return str(self._get(f"/api/logs/{log_name}/"))

    # ==================================================================
    # Trash
    # ==================================================================
    def list_trash(self, page: int = 1, page_size: int = 25) -> str:
        """List documents currently in the trash (deleted but not yet permanently purged)."""
        return str(self._get("/api/trash/", params=self._page_params(page, page_size)))

    def restore_documents_from_trash(self, document_ids: list[int]) -> str:
        """Restore documents out of the trash back into the active document library."""
        return str(self._post("/api/trash/", {"documents": document_ids, "action": "restore"}))

    def empty_trash(self, document_ids: list[int]) -> str:
        """PERMANENTLY and irreversibly purge documents from the trash. This cannot be undone -- the files and all their metadata are gone for good."""
        return str(self._delete("/api/trash/", {"documents": document_ids}))

    # ==================================================================
    # Profile (the API token's own account)
    # ==================================================================
    def get_profile(self) -> str:
        """Get the profile of the Paperless user backing the configured API token. NOTE: the response includes that user's current auth_token verbatim -- handle the output with the same care as a credential."""
        return str(self._get("/api/profile/"))

    def update_profile(self, fields: dict[str, Any]) -> str:
        """Update the profile of the Paperless user backing the configured API token (e.g. first_name, last_name, email). `fields` is applied as-is as the PATCH body. Deliberately does NOT support rotating the auth token itself -- see the module docstring."""
        return str(self._patch("/api/profile/", fields))

    # ==================================================================
    # Escape hatch: anything not covered above, or an endpoint/parameter
    # added in a paperless-ngx release after this file was written.
    # ==================================================================
    def raw_request(
        self,
        method: str,
        path: str,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
    ) -> str:
        """Advanced/last-resort: call any Paperless API endpoint directly with the configured token, for anything the named methods above don't cover.

        :param method: HTTP method, one of GET, POST, PATCH, PUT, DELETE.
        :param path: API path starting with "/api/", e.g. "/api/documents/123/".
        :param params: Optional query string parameters.
        :param json_body: Optional JSON request body (for POST/PATCH/PUT).
        """
        if not path.startswith("/api/"):
            return str({"error": 'path must start with "/api/"'})
        return str(self._request(method.upper(), path, params=params, json_body=json_body))
