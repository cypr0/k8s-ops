"""
title: Paperless-ngx (Full Access)
author: cypr0
version: 2.0.0
license: MIT
requirements: httpx
description: >
  FULL access to this cluster's Paperless-ngx REST API
  (https://docs.paperless-ngx.com/api/). Same reach as before, but exposed
  through a deliberately SMALL method surface -- see "Token budget" below.

  This is NOT read-only: it can create, modify, and PERMANENTLY DELETE
  documents and every other resource type in Paperless. There is no
  confirmation step beyond whatever Open WebUI's own tool-call approval UI
  provides -- treat every delete_*/bulk_edit("delete")/empty_* call as final.

  ── Token budget (why this file looks the way it does) ──────────────────
  v1.0.0 spelled out one typed method per API operation: 139 methods, each
  one a JSON schema injected into the model's context on every single chat
  turn the tool is enabled for, and (mirrored into paperless-mcp) 139 of
  the 232 MCP tools hermes-agent had to disambiguate between -- which is
  exactly why hermes-agent's paperless webhook prompt needed a paragraph
  warning about "get_document" vs. "get_document_type"/"_metadata"/
  "_notes"/"_version" (kubernetes/apps/hermes-agent/hermes-agent/app/
  configmap.yaml). v2.0.0 keeps 21 methods and gives up NO capability:

    * the ~20 operations that carry real day-to-day traffic stay typed,
      with their v1 names and parameter names UNCHANGED, because
      hermes-agent's document-pipeline prompt calls them by name
      (get_document, list_custom_fields, list_tags, list_correspondents,
      list_document_types, modify_documents_tags, create_tag,
      create_correspondent, set_documents_correspondent,
      set_documents_document_type, update_document) and a rename there is
      a silent breakage, not a compile error;
    * every bulk_edit variant that used to be its own method
      (add_tag/remove_tag/set_storage_path/modify_custom_fields/
      set_permissions/reprocess/rotate/merge/split/delete_pages/edit_pdf/
      remove_password) now goes through the one generic bulk_edit();
    * everything else -- saved views, mail accounts/rules, share links,
      workflows/triggers/actions, processed mail, users, groups, tasks,
      trash, config, logs, profile -- goes through raw_request();
    * capabilities() is the discovery path: it returns the endpoint paths,
      bulk_edit method names and filter params needed to drive bulk_edit()
      and raw_request(), on demand, so that reference material costs
      tokens only in the turn that actually asks for it instead of in
      every turn.

  Deliberately NOT reachable even via raw_request without meaning it:
  profile/generate_auth_token would rotate the very token this tool
  authenticates with -- and the same token backs the
  paperless-cronjob-fix-ownership CronJob -- locking both out. It is not
  blocked in code (raw_request is a genuine passthrough), just called out
  here and in capabilities("danger").

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
  NOT staff, so e.g. /api/status/ or /api/users/ via raw_request currently
  403 -- widen that account's Paperless permissions if you want those).

  MIRROR NOTICE: this file is duplicated byte-for-byte into
  kubernetes/apps/hermes-agent/paperless-mcp/app/paperless_full.py, where
  owui_tool_mcp_bridge.py serves it as a standalone MCP server. kustomize's
  configMapGenerator refuses file paths that escape its own kustomization
  directory, so a single shared copy isn't possible -- keep both in sync.
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Reference material for capabilities(). Lives here, NOT in the method
# docstrings, precisely so it stays out of the tool specs the model is fed
# on every turn -- it is fetched only when a call actually needs it.
_CAPABILITIES: dict[str, str] = {
    "filters": """list_documents() named params cover the common filters. Anything
else goes in extra_filters={...} as raw Paperless query params:
  title_content, more_like_id, storage_path__id, added__date__gte/lte,
  modified__date__gte/lte, is_tagged, is_in_inbox, archive_serial_number,
  owner__id, owner__isnull, id__in, tags__id__all, tags__id__none,
  correspondent__isnull, document_type__isnull, checksum__iexact.
Field lookups follow Django: __icontains __istartswith __iendswith __iexact
__gt __gte __lt __lte __isnull __in.
custom_field_query is a serialized expression, e.g.
  ["AND", [["1","exact","foo"], ["2","gte","2026-01-01"]]]
operators: exact in isnull exists icontains istartswith iendswith gt gte lt
lte range contains.
ordering: any document field, "-" prefix for descending (e.g. "-created").
Omit ordering when query= is set to keep relevance ranking.""",
    "bulk_edit": """bulk_edit(document_ids, method, parameters) -> POST /api/documents/bulk_edit/
method            parameters
set_correspondent {"correspondent": <id|null>}
set_document_type {"document_type": <id|null>}
set_storage_path  {"storage_path": <id|null>}
add_tag           {"tag": <id>}
remove_tag        {"tag": <id>}
modify_tags       {"add_tags": [ids], "remove_tags": [ids]}
modify_custom_fields {"add_custom_fields": [{"field": id, "value": v}],
                      "remove_custom_fields": [ids]}
set_permissions   {"owner": <id>, "merge": bool,
                   "set_permissions": {"view": {"users": [], "groups": []},
                                       "change": {"users": [], "groups": []}}}
delete            {}                       (-> trash, recoverable)
reprocess         {}                       (re-run OCR/consumption)
rotate            {"degrees": 90|180|270}
merge             {"metadata_document_id": <id>, "delete_originals": bool}
split             {"pages": "1-2,3", "delete_originals": bool}
delete_pages      {"pages": [1,2]}
edit_pdf          {"operations": [...], "update_document": bool}
remove_password   {"password": "..."}
Dedicated wrappers exist for the three the document pipeline uses most:
modify_documents_tags, set_documents_correspondent,
set_documents_document_type -- plus delete_documents for the destructive one.""",
    "endpoints": """raw_request(method, path, params, json_body) reaches any endpoint.
Standard DRF shape per resource: GET /api/<r>/ (list, ?page&page_size),
POST /api/<r>/ (create), GET/PATCH/DELETE /api/<r>/<id>/.
Resources: documents correspondents document_types tags storage_paths
  custom_fields saved_views mail_accounts mail_rules share_links
  share_link_bundles workflows workflow_triggers workflow_actions
  processed_mail users groups tasks
Document sub-resources: /api/documents/<id>/metadata/ /notes/ /versions/
  /preview/ /thumb/ /download/?original=true /suggestions/ /history/
Other: /api/search/ (omnibar) /api/search/autocomplete/?term=&limit=
  /api/statistics/ /api/status/ (staff only) /api/remote_version/
  /api/config/ /api/ui_settings/ /api/profile/ /api/logs/ /api/logs/<name>/
  /api/trash/ (GET), /api/trash/ (POST {"action":"restore"|"empty",
  "documents":[ids]}), /api/tasks/ /api/tasks/acknowledge/
  /api/documents/bulk_download/ (POST -- builds a zip server-side;
  this tool never inlines zip bytes, fetch it from the web UI)""",
    "danger": """Irreversible / high-blast-radius calls:
- bulk_edit(..., "delete") and delete_documents() -> trash (recoverable),
  but POST /api/trash/ {"action":"empty"} is permanent.
- DELETE on tags/correspondents/document_types/storage_paths/custom_fields
  is immediate and permanent; documents survive but lose the assignment.
- POST /api/profile/generate_auth_token/ ROTATES the token this tool
  authenticates with, which is also the token
  paperless-cronjob-fix-ownership uses -- it locks out both. Never call it.
- bulk_edit "merge"/"split"/"delete_pages"/"edit_pdf" with
  delete_originals=true destroys the source documents.""",
}


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
            description="Cap on how large a file download_document will "
            "inline as base64. Larger files return an error with the "
            "size instead.",
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
                        "note": "Non-JSON response; use download_document() "
                        "for document file/preview/thumbnail bytes.",
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

    def _page_params(self, page: int, page_size: int) -> dict[str, Any]:
        return {"page": page, "page_size": min(page_size, self.valves.MAX_PAGE_SIZE)}

    def _bulk_edit(self, document_ids: list[int], method: str, parameters: dict[str, Any]) -> str:
        return str(
            self._post(
                "/api/documents/bulk_edit/",
                {"documents": document_ids, "method": method, "parameters": parameters},
            )
        )

    # ==================================================================
    # Discovery -- the progressive-disclosure half of the token budget
    # ==================================================================
    def capabilities(self, topic: str = "") -> str:
        """Reference for driving bulk_edit()/raw_request(): endpoint paths, bulk_edit methods, filter params, destructive calls. Call this before reaching for raw_request().

        :param topic: One of "filters", "bulk_edit", "endpoints", "danger". Omit for all four.
        """
        key = topic.strip().lower()
        if not key:
            return "\n\n".join(f"## {k}\n{v}" for k, v in _CAPABILITIES.items())
        if key not in _CAPABILITIES:
            return f"Unknown topic '{topic}'. Available: {', '.join(_CAPABILITIES)}."
        return _CAPABILITIES[key]

    # ==================================================================
    # Documents
    # ==================================================================
    def list_documents(
        self,
        query: Optional[str] = None,
        tag_id: Optional[int] = None,
        correspondent_id: Optional[int] = None,
        document_type_id: Optional[int] = None,
        created_after: Optional[str] = None,
        created_before: Optional[str] = None,
        custom_field_query: Optional[str] = None,
        ordering: Optional[str] = None,
        page: int = 1,
        page_size: int = 25,
        extra_filters: Optional[dict[str, Any]] = None,
    ) -> str:
        """List/search Paperless documents.

        :param query: Full-text search over OCR content + title.
        :param created_after: ISO date YYYY-MM-DD, inclusive.
        :param created_before: ISO date YYYY-MM-DD, inclusive.
        :param custom_field_query: Serialized custom-field filter expression -- see capabilities("filters").
        :param ordering: Sort field, "-" prefix for descending, e.g. "-created". Omit when query is set to keep relevance ranking.
        :param extra_filters: Any other Paperless filter as raw query params -- see capabilities("filters").
        """
        params = self._page_params(page, page_size)
        if query:
            params["query"] = query
        if tag_id is not None:
            params["tags__id"] = tag_id
        if correspondent_id is not None:
            params["correspondent__id"] = correspondent_id
        if document_type_id is not None:
            params["document_type__id"] = document_type_id
        if created_after:
            params["created__date__gte"] = created_after
        if created_before:
            params["created__date__lte"] = created_before
        if custom_field_query:
            params["custom_field_query"] = custom_field_query
        if ordering:
            params["ordering"] = ordering
        if extra_filters:
            params.update(extra_filters)
        return str(self._get("/api/documents/", params=params))

    def get_document(self, document_id: int, full_perms: bool = False) -> str:
        """Get one document's full details: title, OCR content, tags, correspondent, document_type, custom_fields.

        :param full_perms: Include complete view/change permission details instead of just user_can_change.
        """
        params = {"full_perms": "true"} if full_perms else None
        return str(self._get(f"/api/documents/{document_id}/", params=params))

    def get_document_metadata(self, document_id: int) -> str:
        """Get file-level metadata for a document: mime type, checksums, page count, EXIF/PDF metadata."""
        return str(self._get(f"/api/documents/{document_id}/metadata/"))

    def download_document(self, document_id: int, original: bool = False) -> str:
        """Download a document's file base64-encoded, capped by the MAX_INLINE_DOWNLOAD_BYTES valve.

        :param original: Fetch the original upload instead of the archived (OCR'd PDF/A) version. For preview/thumbnail images use raw_request on "/api/documents/<id>/preview/" or "/thumb/".
        """
        suffix = "?original=true" if original else ""
        path = f"/api/documents/{document_id}/download/{suffix}"
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

    def upload_document(
        self,
        file_content_base64: str,
        filename: str,
        title: Optional[str] = None,
        created: Optional[str] = None,
        correspondent_id: Optional[int] = None,
        document_type_id: Optional[int] = None,
        tag_ids: Optional[list[int]] = None,
    ) -> str:
        """Upload a new document for consumption (OCR, classification). Returns a task UUID -- poll /api/tasks/ via raw_request to see when it finishes.

        :param file_content_base64: The file's raw bytes, base64-encoded.
        :param filename: Filename including extension, e.g. "invoice.pdf".
        :param created: ISO date or datetime the document itself is dated.
        """
        try:
            raw = base64.b64decode(file_content_base64)
        except Exception as e:  # noqa: BLE001
            return str({"error": f"file_content_base64 is not valid base64: {e}"})

        data: dict[str, Any] = {}
        if title is not None:
            data["title"] = title
        if created is not None:
            data["created"] = created
        if correspondent_id is not None:
            data["correspondent"] = correspondent_id
        if document_type_id is not None:
            data["document_type"] = document_type_id
        if tag_ids:
            data["tags"] = tag_ids
        return str(
            self._request(
                "POST",
                "/api/documents/post_document/",
                data=data,
                files={"document": (filename, raw)},
            )
        )

    def update_document(
        self,
        document_id: int,
        title: Optional[str] = None,
        content: Optional[str] = None,
        correspondent_id: Optional[int] = None,
        document_type_id: Optional[int] = None,
        storage_path_id: Optional[int] = None,
        tag_ids: Optional[list[int]] = None,
        created: Optional[str] = None,
        custom_fields: Optional[list[dict[str, Any]]] = None,
        owner_id: Optional[int] = None,
    ) -> str:
        """Update fields on one document. Only pass what should change.

        :param tag_ids: FULL replacement of the tag list -- use modify_documents_tags for additive changes.
        :param created: ISO datetime, e.g. "2026-01-15T00:00:00Z".
        :param custom_fields: FULL replacement list of {"field": <id>, "value": <v>} -- include every existing entry from get_document or its value is lost.
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
        if created is not None:
            body["created"] = created
        if custom_fields is not None:
            body["custom_fields"] = custom_fields
        if owner_id is not None:
            body["owner"] = owner_id
        return str(self._patch(f"/api/documents/{document_id}/", body))

    # ------------------------------------------------------------------
    # Documents: bulk_edit. One generic dispatcher plus the three wrappers
    # hermes-agent's document pipeline calls by name (see module docstring)
    # and the destructive one, which is worth being explicit about.
    # ------------------------------------------------------------------
    def bulk_edit(self, document_ids: list[int], method: str, parameters: dict[str, Any]) -> str:
        """Run any Paperless bulk_edit operation on a set of documents. Call capabilities("bulk_edit") first for the method names and their parameter shapes.

        :param method: e.g. "add_tag", "set_storage_path", "modify_custom_fields", "reprocess", "rotate", "merge", "split", "edit_pdf".
        :param parameters: Method-specific parameter object -- see capabilities("bulk_edit").
        """
        return self._bulk_edit(document_ids, method, parameters or {})

    def modify_documents_tags(
        self,
        document_ids: list[int],
        add_tag_ids: Optional[list[int]] = None,
        remove_tag_ids: Optional[list[int]] = None,
    ) -> str:
        """Add and/or remove tags on multiple documents in one call. Additive -- tags not listed stay untouched."""
        return self._bulk_edit(
            document_ids,
            "modify_tags",
            {"add_tags": add_tag_ids or [], "remove_tags": remove_tag_ids or []},
        )

    def set_documents_correspondent(
        self, document_ids: list[int], correspondent_id: Optional[int]
    ) -> str:
        """Set (or clear, with null) the correspondent on multiple documents at once."""
        return self._bulk_edit(document_ids, "set_correspondent", {"correspondent": correspondent_id})

    def set_documents_document_type(
        self, document_ids: list[int], document_type_id: Optional[int]
    ) -> str:
        """Set (or clear, with null) the document type on multiple documents at once."""
        return self._bulk_edit(document_ids, "set_document_type", {"document_type": document_type_id})

    def delete_documents(self, document_ids: list[int]) -> str:
        """Move documents to the trash. Recoverable via POST /api/trash/ {"action":"restore"} until the trash is emptied."""
        return self._bulk_edit(document_ids, "delete", {})

    # ==================================================================
    # Taxonomy: the four lists the document pipeline matches against, and
    # the two create_* calls it is allowed to make. Everything else
    # (document types, storage paths, custom fields, updates, deletes)
    # goes through raw_request -- see capabilities("endpoints").
    # ==================================================================
    def list_tags(self, name: Optional[str] = None, page: int = 1, page_size: int = 25) -> str:
        """List tags with their parent/children hierarchy and document_count.

        :param name: Case-insensitive substring filter on the tag name.
        """
        params = self._page_params(page, page_size)
        if name:
            params["name__icontains"] = name
        return str(self._get("/api/tags/", params=params))

    def list_correspondents(
        self, name: Optional[str] = None, page: int = 1, page_size: int = 25
    ) -> str:
        """List correspondents (who sent/received a document).

        :param name: Case-insensitive substring filter on the correspondent name.
        """
        params = self._page_params(page, page_size)
        if name:
            params["name__icontains"] = name
        return str(self._get("/api/correspondents/", params=params))

    def list_document_types(
        self, name: Optional[str] = None, page: int = 1, page_size: int = 25
    ) -> str:
        """List document types.

        :param name: Case-insensitive substring filter on the type name.
        """
        params = self._page_params(page, page_size)
        if name:
            params["name__icontains"] = name
        return str(self._get("/api/document_types/", params=params))

    def list_custom_fields(self, page: int = 1, page_size: int = 100) -> str:
        """List custom field definitions with their ids and data types. Look ids up here rather than assuming them."""
        return str(self._get("/api/custom_fields/", params=self._page_params(page, page_size)))

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
        """Create a tag. Check list_tags first -- a near-duplicate is worse than reusing an existing tag.

        :param color: Hex color, e.g. "#a6cee3".
        :param parent_id: Parent tag id, to nest this tag (hierarchical tags).
        :param match: Auto-matching text/regex, used with matching_algorithm to auto-assign this tag to future documents.
        :param matching_algorithm: 0=none 1=any word 2=all words 3=exact 4=regex 5=fuzzy 6=auto.
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

    def create_correspondent(
        self,
        name: str,
        match: Optional[str] = None,
        matching_algorithm: Optional[int] = None,
        is_insensitive: Optional[bool] = None,
        owner_id: Optional[int] = None,
    ) -> str:
        """Create a correspondent. Check list_correspondents first -- legal-form suffixes and abbreviations of the same company are the same correspondent, not two.

        :param match: Auto-matching text/regex, used with matching_algorithm to auto-assign this correspondent to future documents.
        :param matching_algorithm: 0=none 1=any word 2=all words 3=exact 4=regex 5=fuzzy 6=auto.
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

    # ==================================================================
    # Cross-cutting
    # ==================================================================
    def global_search(self, query: str) -> str:
        """Search across documents, correspondents, tags and the other object types at once -- the Paperless omnibar search."""
        return str(self._get("/api/search/", params={"query": query}))

    def get_statistics(self) -> str:
        """Library statistics: document/character counts, inbox count, counts per document type and tag."""
        return str(self._get("/api/statistics/"))

    def raw_request(
        self,
        method: str,
        path: str,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
    ) -> str:
        """Call any Paperless API endpoint not covered by the methods above (saved views, mail rules, share links, workflows, users, groups, tasks, trash, config, logs). Call capabilities("endpoints") first for the paths.

        :param method: GET, POST, PATCH, PUT or DELETE.
        :param path: API path starting with "/api/", e.g. "/api/workflows/".
        :param json_body: JSON request body for POST/PATCH/PUT.
        """
        if not path.startswith("/api/"):
            return str({"error": 'path must start with "/api/"'})
        return str(self._request(method.upper(), path, params=params, json_body=json_body))
