"""
title: Nextcloud (Full Access)
author: cypr0
version: 1.0.0
license: MIT
requirements: httpx
description: >
  FULL access to this cluster's Nextcloud instance, covering the "Client
  APIs" Nextcloud documents at docs.nextcloud.com/server/stable/
  developer_manual/client_apis/: WebDAV files (list/read/write/delete/move/
  copy, favorites, trashbin, versions, comments, search) and the OCS APIs
  (sharing incl. sharees + federated shares, user/group/app provisioning,
  notifications, activity, user status, preferences, out-of-office,
  recommendations, capabilities), plus a raw-request escape hatch for
  anything not covered by a named method below.

  This is NOT read-only: it can create, modify, and PERMANENTLY DELETE
  files, users, groups, shares, and every other resource type below.
  DELETE on a file moves it to Nextcloud's trashbin (recoverable via
  restore_trashed_item until the trash is emptied); DELETE on the trashbin
  itself, or on users/groups/shares/tags, is immediate and irreversible.
  There is no confirmation step beyond whatever Open WebUI's own tool-call
  approval UI provides.

  Auth model: HTTP Basic Auth with this cluster's actual Nextcloud
  ADMIN account (NEXTCLOUD_USERNAME/NEXTCLOUD_PASSWORD env vars on the Open
  WebUI pod -- see externalsecret-nextcloud-token.yaml, which reads the
  SAME admin credentials the nextcloud-credentials Secret already has).
  This was a deliberate choice over provisioning a separate dedicated
  account (like the read-only "openclaw-reader" user post-install-job.yaml
  creates): it means every OCS Provisioning endpoint below genuinely works
  (no non-staff-style 403s, unlike the paperless_full tool), but it also
  means this tool wields the SAME real super-admin credential as the human
  admin login -- there is no separate revoke path for just this tool.
  Because the admin account has no per-user WebDAV namespace restriction,
  every WebDAV/trashbin/versions method below takes an optional `user_id`
  (defaults to the admin account) so this tool can browse/manage ANY
  user's files, not just the admin's own -- that is a direct consequence
  of using the real admin account and is intentional.

  Scope decisions (documented so gaps don't need reverse-engineering
  later): built against the two Client-API doc pages linked above.
  Endpoints with a small, stable, well-documented shape (WebDAV basic ops,
  trashbin, versions, sharing, provisioning users/groups/apps, status,
  notifications, activity, capabilities) get explicit typed methods.
  systemtags (tags) and the comments POST body are NOT explicitly
  documented on those pages -- implemented here from Nextcloud's long-
  stable (if lightly documented) WebDAV extensions, flagged in their
  docstrings as best-effort. Preferences/out-of-office/recommendations are
  newer, less-traveled OCS endpoints -- also flagged best-effort.

  Deliberately NOT implemented: Remote Wipe API (it exists to let an MDM
  remotely erase a *lost device* -- letting a chat tool trigger that on a
  live device would be a straight-up destructive/foot-gun action, not a
  legitimate chat operation), Login Flow v2 / app-password minting (this
  tool already authenticates with a standing credential; minting new
  sessions from inside chat adds attack surface for no benefit), the Talk
  Integration API (the Talk/Spreed app is not installed on this instance
  -- see kubernetes/apps/nextcloud/nextcloud/app/post-install-job.yaml's
  app list), and every app-*specific* API (Deck, Tables, Forms,
  Groupfolders, Collabora/richdocuments, Whiteboard) -- those have their
  own separate API docs outside the two Client-API pages this tool was
  built against; ask for a dedicated tool if you want one of those. The
  Assistant/Translation/TextProcessing/Text2Image/TaskProcessing OCS APIs
  are also skipped: this Nextcloud instance has no AI backend configured
  for them (this cluster's actual AI stack is Open WebUI + OpenRouter).
"""

from __future__ import annotations

import base64
import logging
import os
import xml.etree.ElementTree as ET
from typing import Any, Optional
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_DAV_NS = "DAV:"
_OC_NS = "http://owncloud.org/ns"
_NC_NS = "http://nextcloud.org/ns"


def _localname(tag: str) -> str:
    return tag.split("}", 1)[1] if tag.startswith("{") else tag


class Tools:
    class Valves(BaseModel):
        """Admin-configured, shared by every user.

        NEXTCLOUD_USERNAME/PASSWORD default to env vars on the Open WebUI
        pod (populated from the same 1Password item the nextcloud-
        credentials Secret already reads -- see
        externalsecret-nextcloud-token.yaml). Users should never need to
        touch this; it exists here only as a manual override/rotation
        escape hatch.
        """

        NEXTCLOUD_BASE_URL: str = Field(
            default="http://nextcloud.nextcloud.svc.cluster.local:8080",
            description="Nextcloud base URL, in-cluster Service DNS "
            "(no trailing slash).",
        )
        NEXTCLOUD_USERNAME: str = Field(
            default_factory=lambda: os.getenv("NEXTCLOUD_USERNAME", ""),
        )
        NEXTCLOUD_PASSWORD: str = Field(
            default_factory=lambda: os.getenv("NEXTCLOUD_PASSWORD", ""),
        )
        REQUEST_TIMEOUT_SECONDS: int = Field(default=30)
        DEFAULT_PAGE_LIMIT: int = Field(default=50)
        MAX_INLINE_DOWNLOAD_BYTES: int = Field(
            default=3_000_000,
            description="Cap on how large a file download_file will "
            "inline as base64. Larger files return an error with the "
            "size instead.",
        )

    def __init__(self):
        self.valves = self.Valves()

    # ------------------------------------------------------------------
    # internal helpers (not exposed to the model)
    # ------------------------------------------------------------------
    def _auth(self) -> tuple[str, str]:
        if not self.valves.NEXTCLOUD_USERNAME or not self.valves.NEXTCLOUD_PASSWORD:
            raise RuntimeError(
                "No Nextcloud credentials configured. This should be "
                "auto-filled from the cluster secret -- if missing, check "
                "NEXTCLOUD_USERNAME/NEXTCLOUD_PASSWORD on the Open WebUI "
                "pod, or set them manually in this tool's Valves (gear icon)."
            )
        return (self.valves.NEXTCLOUD_USERNAME, self.valves.NEXTCLOUD_PASSWORD)

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.valves.NEXTCLOUD_BASE_URL.rstrip("/"),
            auth=self._auth(),
            headers={"OCS-APIRequest": "true"},
            timeout=self.valves.REQUEST_TIMEOUT_SECONDS,
        )

    def _me(self) -> str:
        return self.valves.NEXTCLOUD_USERNAME

    # -- OCS (JSON) ----------------------------------------------------
    def _ocs(
        self,
        method: str,
        ocs_path: str,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
    ) -> Any:
        """ocs_path e.g. "/ocs/v1.php/cloud/users" or "/ocs/v2.php/apps/files_sharing/api/v1/shares"."""
        params = dict(params or {})
        params["format"] = "json"
        try:
            with self._client() as client:
                resp = client.request(method, ocs_path, params=params, json=json_body)
                resp.raise_for_status()
                if not resp.content:
                    return {}
                body = resp.json()
                ocs = body.get("ocs", body)
                meta = ocs.get("meta", {})
                status_code = meta.get("statuscode")
                if status_code not in (100, 200, None):
                    return {
                        "error": f"Nextcloud OCS error (statuscode {status_code}): "
                        f"{meta.get('message', '')}"
                    }
                return ocs.get("data", ocs)
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status == 401:
                return {"error": "Nextcloud rejected the credentials (401). Check this tool's Valves."}
            if status == 403:
                return {"error": "Nextcloud denied access (403). The configured account likely lacks permission."}
            if status == 404:
                return {"error": "Not found (404)."}
            return {"error": f"Nextcloud OCS HTTP error {status}: {e.response.text[:400]}"}
        except httpx.RequestError as e:
            return {"error": f"Could not reach Nextcloud: {e}"}

    # -- WebDAV ----------------------------------------------------------
    def _dav_url_path(self, path: str) -> str:
        segments = [quote(seg) for seg in path.strip("/").split("/") if seg != ""]
        return "/".join(segments)

    def _dav_request(
        self,
        method: str,
        path: str,
        headers: Optional[dict[str, str]] = None,
        content: Optional[bytes] = None,
    ) -> httpx.Response:
        """`path` is relative to /remote.php/ (NOT /remote.php/dav/) -- most
        resources (files, trashbin, versions, systemtags) live under a
        "dav/" prefix, but comments notably does not (see list_file_comments'
        docstring), so callers pass the full path after /remote.php/ explicitly."""
        with self._client() as client:
            return client.request(
                method, "/remote.php/" + self._dav_url_path(path), headers=headers, content=content
            )

    _PROPFIND_BODY = (
        '<?xml version="1.0"?>'
        '<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns" xmlns:nc="http://nextcloud.org/ns">'
        "<d:prop>"
        "<d:getlastmodified/><d:getcontentlength/><d:getcontenttype/><d:resourcetype/><d:getetag/>"
        "<oc:id/><oc:fileid/><oc:size/><oc:favorite/><oc:permissions/><oc:owner-display-name/>"
        "<nc:has-preview/>"
        "</d:prop>"
        "</d:propfind>"
    ).encode()

    def _propfind(self, path: str, depth: str = "1") -> Any:
        resp = self._dav_request(
            "PROPFIND",
            path,
            headers={"Depth": depth, "Content-Type": "application/xml"},
            content=self._PROPFIND_BODY,
        )
        return self._handle_dav_response(resp, parse=True)

    def _handle_dav_response(self, resp: httpx.Response, parse: bool = False) -> Any:
        if resp.status_code in (200, 201, 204, 207):
            if parse:
                return self._parse_multistatus(resp.content)
            return {"status": "ok", "http_status": resp.status_code}
        if resp.status_code == 401:
            return {"error": "Nextcloud rejected the credentials (401)."}
        if resp.status_code == 403:
            return {"error": "Nextcloud denied access (403)."}
        if resp.status_code == 404:
            return {"error": "Not found (404)."}
        if resp.status_code == 405:
            return {"error": "Method not allowed (405) -- check the path/verb."}
        if resp.status_code == 412:
            return {"error": "Precondition failed (412) -- e.g. destination exists and Overwrite was disabled."}
        return {"error": f"Nextcloud WebDAV error HTTP {resp.status_code}: {resp.text[:400]}"}

    def _parse_multistatus(self, xml_bytes: bytes) -> list[dict[str, Any]]:
        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError as e:
            return [{"error": f"Could not parse WebDAV XML response: {e}"}]
        results = []
        for response_el in root.findall(f"{{{_DAV_NS}}}response"):
            href_el = response_el.find(f"{{{_DAV_NS}}}href")
            entry: dict[str, Any] = {"href": href_el.text if href_el is not None else None}
            for propstat in response_el.findall(f"{{{_DAV_NS}}}propstat"):
                status_el = propstat.find(f"{{{_DAV_NS}}}status")
                if status_el is not None and " 200 " not in f" {status_el.text} ":
                    continue
                prop_el = propstat.find(f"{{{_DAV_NS}}}prop")
                if prop_el is None:
                    continue
                for child in prop_el:
                    name = _localname(child.tag)
                    if name == "resourcetype":
                        entry["is_collection"] = any(
                            _localname(c.tag) == "collection" for c in child
                        )
                    else:
                        entry[name] = child.text
            results.append(entry)
        return results

    def _download_bytes(self, path: str) -> str:
        try:
            resp = self._dav_request("GET", path)
            if resp.status_code != 200:
                return str(self._handle_dav_response(resp))
            size = len(resp.content)
            if size > self.valves.MAX_INLINE_DOWNLOAD_BYTES:
                return str(
                    {
                        "error": f"File is {size} bytes, over the "
                        f"{self.valves.MAX_INLINE_DOWNLOAD_BYTES}-byte inline cap "
                        "(MAX_INLINE_DOWNLOAD_BYTES valve)."
                    }
                )
            return str(
                {
                    "content_type": resp.headers.get("content-type", ""),
                    "size_bytes": size,
                    "base64": base64.b64encode(resp.content).decode("ascii"),
                }
            )
        except httpx.RequestError as e:
            return str({"error": f"Could not reach Nextcloud: {e}"})

    # ==================================================================
    # WebDAV: files & folders
    # ==================================================================
    def list_files(self, path: str = "", user_id: Optional[str] = None) -> str:
        """List the contents of a folder (non-recursive) under a user's files root.

        :param path: Folder path relative to the user's files root, e.g. "" for the root, or "Documents/Invoices".
        :param user_id: Whose files to browse. Defaults to the configured admin account; as an admin account, any user_id works.
        """
        return str(self._propfind(f"dav/files/{user_id or self._me()}/{path}", depth="1"))

    def get_file_info(self, path: str, user_id: Optional[str] = None) -> str:
        """Get metadata for a single file or folder (fileid, size, mtime, etag, favorite, permissions, mimetype) without listing its children."""
        return str(self._propfind(f"dav/files/{user_id or self._me()}/{path}", depth="0"))

    def download_file(self, path: str, user_id: Optional[str] = None) -> str:
        """Download a file's content, base64-encoded (capped by MAX_INLINE_DOWNLOAD_BYTES)."""
        return self._download_bytes(f"dav/files/{user_id or self._me()}/{path}")

    def upload_file(self, path: str, file_content_base64: str, user_id: Optional[str] = None) -> str:
        """Upload (create or overwrite) a file.

        :param path: Destination path relative to the user's files root, e.g. "Documents/report.pdf". Parent folders must already exist (use create_folder first).
        :param file_content_base64: The file's raw bytes, base64-encoded.
        """
        try:
            data = base64.b64decode(file_content_base64)
        except Exception as e:  # noqa: BLE001
            return str({"error": f"Invalid base64 in file_content_base64: {e}"})
        resp = self._dav_request("PUT", f"dav/files/{user_id or self._me()}/{path}", content=data)
        return str(self._handle_dav_response(resp))

    def create_folder(self, path: str, user_id: Optional[str] = None) -> str:
        """Create a new (single-level) folder. The parent folder must already exist."""
        resp = self._dav_request("MKCOL", f"dav/files/{user_id or self._me()}/{path}")
        return str(self._handle_dav_response(resp))

    def delete_file(self, path: str, user_id: Optional[str] = None) -> str:
        """Delete a file or folder (recursively). This moves it to the trashbin -- recoverable via restore_trashed_item until the trash is emptied."""
        resp = self._dav_request("DELETE", f"dav/files/{user_id or self._me()}/{path}")
        return str(self._handle_dav_response(resp))

    def _destination_url(self, dest_path: str, user_id: str) -> str:
        base = self.valves.NEXTCLOUD_BASE_URL.rstrip("/")
        return f"{base}/remote.php/dav/{self._dav_url_path(f'files/{user_id}/{dest_path}')}"

    def move_file(
        self, source_path: str, destination_path: str, user_id: Optional[str] = None, overwrite: bool = False
    ) -> str:
        """Move/rename a file or folder within the same user's files root.

        :param overwrite: If true, replace an existing item at the destination; if false (default), fail with 412 if the destination already exists.
        """
        uid = user_id or self._me()
        resp = self._dav_request(
            "MOVE",
            f"dav/files/{uid}/{source_path}",
            headers={"Destination": self._destination_url(destination_path, uid), "Overwrite": "T" if overwrite else "F"},
        )
        return str(self._handle_dav_response(resp))

    def copy_file(
        self, source_path: str, destination_path: str, user_id: Optional[str] = None, overwrite: bool = False
    ) -> str:
        """Copy a file or folder within the same user's files root."""
        uid = user_id or self._me()
        resp = self._dav_request(
            "COPY",
            f"dav/files/{uid}/{source_path}",
            headers={"Destination": self._destination_url(destination_path, uid), "Overwrite": "T" if overwrite else "F"},
        )
        return str(self._handle_dav_response(resp))

    def search_files(
        self,
        term: str,
        user_id: Optional[str] = None,
        scope_path: str = "",
        search_property: str = "displayname",
    ) -> str:
        """Search for files/folders by a LIKE pattern on a DAV property (default: displayname, i.e. filename).

        :param term: Search term. Wrapped as "%term%" for a substring match.
        :param scope_path: Folder to search within (relative to the user's files root); empty = whole files root.
        :param search_property: DAV property to match against, e.g. "displayname" (filename) or "getcontenttype" (MIME type, e.g. "text/%").
        """
        uid = user_id or self._me()
        scope_href = f"/files/{uid}/{scope_path.strip('/')}" if scope_path else f"/files/{uid}"
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<d:searchrequest xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
            "<d:basicsearch>"
            "<d:select><d:prop><d:displayname/><d:getcontenttype/><d:getcontentlength/>"
            "<oc:fileid/><oc:size/></d:prop></d:select>"
            f"<d:from><d:scope><d:href>{scope_href}</d:href><d:depth>infinity</d:depth></d:scope></d:from>"
            f"<d:where><d:like><d:prop><d:{search_property}/></d:prop><d:literal>%{term}%</d:literal></d:like></d:where>"
            "<d:orderby/>"
            "</d:basicsearch>"
            "</d:searchrequest>"
        ).encode()
        resp = self._dav_request("SEARCH", "dav", headers={"Content-Type": "text/xml"}, content=body)
        return str(self._handle_dav_response(resp, parse=True))

    # ==================================================================
    # WebDAV: favorites
    # ==================================================================
    def list_favorites(self, user_id: Optional[str] = None) -> str:
        """List all files/folders the user has marked as favorite."""
        body = (
            '<?xml version="1.0"?>'
            '<oc:filter-files xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
            "<d:prop><oc:fileid/><d:displayname/><d:getcontentlength/><d:getlastmodified/><d:resourcetype/></d:prop>"
            "<oc:filter-rules><oc:favorite>1</oc:favorite></oc:filter-rules>"
            "</oc:filter-files>"
        ).encode()
        resp = self._dav_request(
            "REPORT", f"dav/files/{user_id or self._me()}/", headers={"Content-Type": "text/xml"}, content=body
        )
        return str(self._handle_dav_response(resp, parse=True))

    def _set_favorite(self, path: str, user_id: Optional[str], value: bool) -> str:
        body = (
            '<?xml version="1.0"?>'
            '<d:propertyupdate xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
            f"<d:set><d:prop><oc:favorite>{1 if value else 0}</oc:favorite></d:prop></d:set>"
            "</d:propertyupdate>"
        ).encode()
        resp = self._dav_request(
            "PROPPATCH", f"dav/files/{user_id or self._me()}/{path}", headers={"Content-Type": "text/xml"}, content=body
        )
        return str(self._handle_dav_response(resp))

    def add_favorite(self, path: str, user_id: Optional[str] = None) -> str:
        """Mark a file or folder as a favorite."""
        return self._set_favorite(path, user_id, True)

    def remove_favorite(self, path: str, user_id: Optional[str] = None) -> str:
        """Remove a file or folder's favorite mark."""
        return self._set_favorite(path, user_id, False)

    # ==================================================================
    # WebDAV: trashbin
    # ==================================================================
    def list_trash(self, user_id: Optional[str] = None) -> str:
        """List items currently in a user's trashbin."""
        return str(self._propfind(f"dav/trashbin/{user_id or self._me()}/trash", depth="1"))

    def restore_trashed_item(self, trash_filename: str, user_id: Optional[str] = None) -> str:
        """Restore an item out of the trashbin back to its original location.

        :param trash_filename: The item's filename exactly as shown in list_trash (Nextcloud restores it to its recorded original path automatically).
        """
        uid = user_id or self._me()
        base = self.valves.NEXTCLOUD_BASE_URL.rstrip("/")
        dest = f"{base}/remote.php/dav/{self._dav_url_path(f'trashbin/{uid}/restore/{trash_filename}')}"
        resp = self._dav_request(
            "MOVE", f"dav/trashbin/{uid}/trash/{trash_filename}", headers={"Destination": dest}
        )
        return str(self._handle_dav_response(resp))

    def delete_trashed_item(self, trash_filename: str, user_id: Optional[str] = None) -> str:
        """PERMANENTLY delete one item from the trashbin. Irreversible."""
        resp = self._dav_request("DELETE", f"dav/trashbin/{user_id or self._me()}/trash/{trash_filename}")
        return str(self._handle_dav_response(resp))

    def empty_trash(self, user_id: Optional[str] = None) -> str:
        """PERMANENTLY empty a user's entire trashbin. Irreversible."""
        resp = self._dav_request("DELETE", f"dav/trashbin/{user_id or self._me()}/trash")
        return str(self._handle_dav_response(resp))

    # ==================================================================
    # WebDAV: versions
    # ==================================================================
    def list_file_versions(self, file_id: str, user_id: Optional[str] = None) -> str:
        """List all stored versions of a file, given its Nextcloud fileid (from get_file_info/list_files)."""
        return str(self._propfind(f"dav/versions/{user_id or self._me()}/versions/{file_id}", depth="1"))

    def restore_file_version(self, file_id: str, version_id: str, user_id: Optional[str] = None) -> str:
        """Restore a specific older version as the current version of a file.

        :param version_id: The version's own id (the last path segment of a version's href from list_file_versions).
        """
        uid = user_id or self._me()
        base = self.valves.NEXTCLOUD_BASE_URL.rstrip("/")
        dest = f"{base}/remote.php/dav/{self._dav_url_path(f'versions/{uid}/restore/target')}"
        resp = self._dav_request(
            "COPY", f"dav/versions/{uid}/versions/{file_id}/{version_id}", headers={"Destination": dest}
        )
        return str(self._handle_dav_response(resp))

    # ==================================================================
    # WebDAV: comments
    # ==================================================================
    def list_file_comments(self, file_id: str) -> str:
        """List comments on a file, given its Nextcloud fileid. (Comments are the one WebDAV resource here NOT mounted under /remote.php/dav/ -- they live directly at /remote.php/comments/files/{fileId}, an older Sabre plugin path that predates the rest of the /dav/ tree.)"""
        return str(self._propfind(f"comments/files/{file_id}", depth="1"))

    def add_file_comment(self, file_id: str, message: str) -> str:
        """Add a comment to a file. Best-effort: the exact POST body isn't published in Nextcloud's docs; this uses the shape Nextcloud's own web client sends."""
        try:
            with self._client() as client:
                resp = client.post(
                    f"/remote.php/comments/files/{file_id}",
                    json={"actorType": "users", "verb": "comment", "message": message},
                )
                return str(self._handle_dav_response(resp))
        except httpx.RequestError as e:
            return str({"error": f"Could not reach Nextcloud: {e}"})

    def delete_file_comment(self, file_id: str, comment_id: str) -> str:
        """PERMANENTLY delete a comment. Irreversible."""
        resp = self._dav_request("DELETE", f"comments/files/{file_id}/{comment_id}")
        return str(self._handle_dav_response(resp))

    # ==================================================================
    # WebDAV: tags (systemtags) -- best-effort, see module docstring.
    # ==================================================================
    def list_tags(self) -> str:
        """List all system tags known to this Nextcloud instance."""
        return str(self._propfind("dav/systemtags", depth="1"))

    def create_tag(self, name: str, user_visible: bool = True, user_assignable: bool = True) -> str:
        """Create a new system tag."""
        try:
            with self._client() as client:
                resp = client.post(
                    "/remote.php/dav/systemtags",
                    json={"name": name, "userVisible": user_visible, "userAssignable": user_assignable},
                )
                if resp.status_code in (200, 201):
                    return str({"status": "ok", "location": resp.headers.get("Content-Location", "")})
                return str(self._handle_dav_response(resp))
        except httpx.RequestError as e:
            return str({"error": f"Could not reach Nextcloud: {e}"})

    def delete_tag(self, tag_id: str) -> str:
        """PERMANENTLY delete a system tag (unassigned from every file it was on). Irreversible."""
        resp = self._dav_request("DELETE", f"dav/systemtags/{tag_id}")
        return str(self._handle_dav_response(resp))

    def list_file_tags(self, file_id: str) -> str:
        """List system tags assigned to a specific file."""
        return str(self._propfind(f"dav/systemtags-relations/files/{file_id}", depth="1"))

    def assign_tag_to_file(self, file_id: str, tag_id: str) -> str:
        """Assign an existing system tag to a file."""
        resp = self._dav_request("PUT", f"dav/systemtags-relations/files/{file_id}/{tag_id}")
        return str(self._handle_dav_response(resp))

    def unassign_tag_from_file(self, file_id: str, tag_id: str) -> str:
        """Remove a system tag from a file (the tag itself is not deleted)."""
        resp = self._dav_request("DELETE", f"dav/systemtags-relations/files/{file_id}/{tag_id}")
        return str(self._handle_dav_response(resp))

    # ==================================================================
    # OCS: Sharing (files_sharing app)
    # ==================================================================
    def list_shares(self, path: Optional[str] = None, reshares: bool = False, subfiles: bool = False) -> str:
        """List shares. Without `path`, lists all shares the authenticated account created.

        :param path: Optional path (relative to that user's files root) to scope to one file/folder.
        :param reshares: If true with `path` set, also include shares of that item made by other users.
        :param subfiles: If true with `path` set on a folder, include shares of everything inside it.
        """
        params: dict[str, Any] = {}
        if path is not None:
            params["path"] = path
        if reshares:
            params["reshares"] = "true"
        if subfiles:
            params["subfiles"] = "true"
        return str(self._ocs("GET", "/ocs/v2.php/apps/files_sharing/api/v1/shares", params=params))

    def get_share(self, share_id: int) -> str:
        """Get details of a single share by id."""
        return str(self._ocs("GET", f"/ocs/v2.php/apps/files_sharing/api/v1/shares/{share_id}"))

    def create_share(
        self,
        path: str,
        share_type: int,
        share_with: Optional[str] = None,
        permissions: Optional[int] = None,
        password: Optional[str] = None,
        expire_date: Optional[str] = None,
        note: Optional[str] = None,
        label: Optional[str] = None,
        public_upload: Optional[bool] = None,
        send_mail: Optional[bool] = None,
    ) -> str:
        """Create a new share.

        :param path: Path to the file/folder to share, relative to the authenticated account's files root.
        :param share_type: 0=user, 1=group, 3=public link, 4=email, 6=federated (remote), 7=circle, 10=Talk conversation.
        :param share_with: Recipient user/group id (required for share_type 0/1/4/6/7/10; omit for a plain public link, type 3).
        :param permissions: Bitmask: 1=read, 2=update, 4=create, 8=delete, 16=share, 31=all.
        :param password: Password to protect a public link share.
        :param expire_date: "YYYY-MM-DD".
        :param public_upload: Allow uploads into a public-link-shared folder.
        :param send_mail: Email the recipient about this new share.
        """
        body: dict[str, Any] = {"path": path, "shareType": share_type}
        if share_with is not None:
            body["shareWith"] = share_with
        if permissions is not None:
            body["permissions"] = permissions
        if password is not None:
            body["password"] = password
        if expire_date is not None:
            body["expireDate"] = expire_date
        if note is not None:
            body["note"] = note
        if label is not None:
            body["label"] = label
        if public_upload is not None:
            body["publicUpload"] = "true" if public_upload else "false"
        if send_mail is not None:
            body["sendMail"] = "true" if send_mail else "false"
        return str(self._ocs("POST", "/ocs/v2.php/apps/files_sharing/api/v1/shares", json_body=body))

    def update_share(
        self,
        share_id: int,
        permissions: Optional[int] = None,
        password: Optional[str] = None,
        expire_date: Optional[str] = None,
        note: Optional[str] = None,
        public_upload: Optional[bool] = None,
    ) -> str:
        """Update an existing share. Only pass the fields you want to change."""
        body: dict[str, Any] = {}
        if permissions is not None:
            body["permissions"] = permissions
        if password is not None:
            body["password"] = password
        if expire_date is not None:
            body["expireDate"] = expire_date
        if note is not None:
            body["note"] = note
        if public_upload is not None:
            body["publicUpload"] = "true" if public_upload else "false"
        return str(self._ocs("PUT", f"/ocs/v2.php/apps/files_sharing/api/v1/shares/{share_id}", json_body=body))

    def delete_share(self, share_id: int) -> str:
        """PERMANENTLY revoke a share. Irreversible."""
        return str(self._ocs("DELETE", f"/ocs/v2.php/apps/files_sharing/api/v1/shares/{share_id}"))

    def search_sharees(self, search: str, item_type: str = "file", limit: int = 25) -> str:
        """Search for potential share recipients (users/groups/etc) by name, for use as `share_with` in create_share.

        :param item_type: "file" or "folder".
        """
        return str(
            self._ocs(
                "GET",
                "/ocs/v2.php/apps/files_sharing/api/v1/sharees",
                params={"search": search, "itemType": item_type, "perPage": limit},
            )
        )

    def list_federated_shares(self) -> str:
        """List accepted federated (remote-server) shares."""
        return str(self._ocs("GET", "/ocs/v2.php/apps/files_sharing/api/v1/remote_shares"))

    def list_pending_federated_shares(self) -> str:
        """List pending (not yet accepted/declined) federated share invitations."""
        return str(self._ocs("GET", "/ocs/v2.php/apps/files_sharing/api/v1/remote_shares/pending"))

    def accept_federated_share(self, share_id: int) -> str:
        """Accept a pending federated share invitation."""
        return str(self._ocs("POST", f"/ocs/v2.php/apps/files_sharing/api/v1/remote_shares/pending/{share_id}"))

    def decline_federated_share(self, share_id: int) -> str:
        """Decline a pending federated share invitation."""
        return str(self._ocs("DELETE", f"/ocs/v2.php/apps/files_sharing/api/v1/remote_shares/pending/{share_id}"))

    def delete_federated_share(self, share_id: int) -> str:
        """Remove an already-accepted federated share. Irreversible for this end's copy of it."""
        return str(self._ocs("DELETE", f"/ocs/v2.php/apps/files_sharing/api/v1/remote_shares/{share_id}"))

    # ==================================================================
    # OCS: User provisioning
    # ==================================================================
    def list_users(self, search: Optional[str] = None, limit: Optional[int] = None, offset: Optional[int] = None) -> str:
        """List Nextcloud user ids."""
        params: dict[str, Any] = {}
        if search:
            params["search"] = search
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        return str(self._ocs("GET", "/ocs/v1.php/cloud/users", params=params))

    def get_user(self, user_id: str) -> str:
        """Get full profile details of a single user."""
        return str(self._ocs("GET", f"/ocs/v1.php/cloud/users/{user_id}"))

    def create_user(
        self,
        user_id: str,
        password: Optional[str] = None,
        email: Optional[str] = None,
        display_name: Optional[str] = None,
        groups: Optional[list[str]] = None,
        quota: Optional[str] = None,
    ) -> str:
        """Create a new Nextcloud user account.

        :param password: Required unless `email` is given (Nextcloud can email the user a set-password link instead).
        :param quota: e.g. "5 GB", "500 MB", or "none" for unlimited.
        """
        body: dict[str, Any] = {"userid": user_id}
        if password is not None:
            body["password"] = password
        if email is not None:
            body["email"] = email
        if display_name is not None:
            body["displayName"] = display_name
        if groups is not None:
            body["groups"] = groups
        if quota is not None:
            body["quota"] = quota
        return str(self._ocs("POST", "/ocs/v1.php/cloud/users", json_body=body))

    def update_user_field(self, user_id: str, key: str, value: str) -> str:
        """Update a single field on a user's profile.

        :param key: One of: email, quota, display (display name), password, phone, address, website, twitter, fediverse, organisation, role, headline, biography, additional_mail, additional_phone, language, locale, notify_email, twofactor_auth_disabled.
        """
        return str(self._ocs("PUT", f"/ocs/v1.php/cloud/users/{user_id}", json_body={"key": key, "value": value}))

    def enable_user(self, user_id: str) -> str:
        """Re-enable a disabled user account."""
        return str(self._ocs("PUT", f"/ocs/v1.php/cloud/users/{user_id}/enable"))

    def disable_user(self, user_id: str) -> str:
        """Disable a user account (blocks login, does not delete anything)."""
        return str(self._ocs("PUT", f"/ocs/v1.php/cloud/users/{user_id}/disable"))

    def delete_user(self, user_id: str) -> str:
        """PERMANENTLY delete a user account and all their data. Irreversible."""
        return str(self._ocs("DELETE", f"/ocs/v1.php/cloud/users/{user_id}"))

    def resend_welcome_email(self, user_id: str) -> str:
        """Resend the welcome/set-password email to a user."""
        return str(self._ocs("POST", f"/ocs/v1.php/cloud/users/{user_id}/welcome"))

    def get_user_groups(self, user_id: str) -> str:
        """List the groups a user belongs to."""
        return str(self._ocs("GET", f"/ocs/v1.php/cloud/users/{user_id}/groups"))

    def add_user_to_group(self, user_id: str, group_id: str) -> str:
        """Add a user to a group."""
        return str(self._ocs("POST", f"/ocs/v1.php/cloud/users/{user_id}/groups", json_body={"groupid": group_id}))

    def remove_user_from_group(self, user_id: str, group_id: str) -> str:
        """Remove a user from a group."""
        return str(self._ocs("DELETE", f"/ocs/v1.php/cloud/users/{user_id}/groups", json_body={"groupid": group_id}))

    def get_user_subadmin_groups(self, user_id: str) -> str:
        """List the groups a user is a subadmin of."""
        return str(self._ocs("GET", f"/ocs/v1.php/cloud/users/{user_id}/subadmins"))

    def promote_user_to_subadmin(self, user_id: str, group_id: str) -> str:
        """Make a user a subadmin of a group."""
        return str(self._ocs("POST", f"/ocs/v1.php/cloud/users/{user_id}/subadmins", json_body={"groupid": group_id}))

    def demote_user_subadmin(self, user_id: str, group_id: str) -> str:
        """Remove a user's subadmin status for a group."""
        return str(self._ocs("DELETE", f"/ocs/v1.php/cloud/users/{user_id}/subadmins", json_body={"groupid": group_id}))

    # ==================================================================
    # OCS: Group provisioning
    # ==================================================================
    def list_groups(self, search: Optional[str] = None, limit: Optional[int] = None, offset: Optional[int] = None) -> str:
        """List Nextcloud group ids."""
        params: dict[str, Any] = {}
        if search:
            params["search"] = search
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        return str(self._ocs("GET", "/ocs/v1.php/cloud/groups", params=params))

    def create_group(self, group_id: str) -> str:
        """Create a new group."""
        return str(self._ocs("POST", "/ocs/v1.php/cloud/groups", json_body={"groupid": group_id}))

    def get_group_members(self, group_id: str) -> str:
        """List the members of a group."""
        return str(self._ocs("GET", f"/ocs/v1.php/cloud/groups/{group_id}"))

    def get_group_subadmins(self, group_id: str) -> str:
        """List the subadmins of a group."""
        return str(self._ocs("GET", f"/ocs/v1.php/cloud/groups/{group_id}/subadmins"))

    def delete_group(self, group_id: str) -> str:
        """PERMANENTLY delete a group (members are not deleted, just lose membership). Irreversible."""
        return str(self._ocs("DELETE", f"/ocs/v1.php/cloud/groups/{group_id}"))

    # ==================================================================
    # OCS: App provisioning
    # ==================================================================
    def list_apps(self, filter_status: Optional[str] = None) -> str:
        """List installed apps.

        :param filter_status: Optional "enabled" or "disabled" to filter.
        """
        params = {"filter": filter_status} if filter_status else None
        return str(self._ocs("GET", "/ocs/v1.php/cloud/apps", params=params))

    def get_app(self, app_id: str) -> str:
        """Get details of a single app."""
        return str(self._ocs("GET", f"/ocs/v1.php/cloud/apps/{app_id}"))

    def enable_app(self, app_id: str) -> str:
        """Enable an installed app."""
        return str(self._ocs("POST", f"/ocs/v1.php/cloud/apps/{app_id}"))

    def disable_app(self, app_id: str) -> str:
        """Disable an app (does not uninstall it)."""
        return str(self._ocs("DELETE", f"/ocs/v1.php/cloud/apps/{app_id}"))

    # ==================================================================
    # OCS: Notifications
    # ==================================================================
    def list_notifications(self) -> str:
        """List the authenticated account's notifications."""
        return str(self._ocs("GET", "/ocs/v2.php/apps/notifications/api/v2/notifications"))

    def get_notification(self, notification_id: int) -> str:
        """Get a single notification by id."""
        return str(self._ocs("GET", f"/ocs/v2.php/apps/notifications/api/v2/notifications/{notification_id}"))

    def delete_notification(self, notification_id: int) -> str:
        """Dismiss/delete a single notification."""
        return str(self._ocs("DELETE", f"/ocs/v2.php/apps/notifications/api/v2/notifications/{notification_id}"))

    def delete_all_notifications(self) -> str:
        """Dismiss/delete all of the authenticated account's notifications."""
        return str(self._ocs("DELETE", "/ocs/v2.php/apps/notifications/api/v2/notifications"))

    # ==================================================================
    # OCS: Activity
    # ==================================================================
    def list_activities(
        self,
        since: Optional[int] = None,
        limit: Optional[int] = None,
        object_type: Optional[str] = None,
        object_id: Optional[int] = None,
        sort: Optional[str] = None,
    ) -> str:
        """List activity feed entries for the authenticated account.

        :param since: Only activities with an id greater (or, with sort="asc", lower) than this.
        :param object_type: Optional filter, e.g. "files", scoped together with object_id.
        :param sort: "asc" or "desc" (default desc/newest first).
        """
        params: dict[str, Any] = {}
        if since is not None:
            params["since"] = since
        if limit is not None:
            params["limit"] = limit
        if sort:
            params["sort"] = sort
        if object_type and object_id is not None:
            return str(
                self._ocs(
                    "GET",
                    f"/ocs/v2.php/apps/activity/api/v2/activity/filter",
                    params={**params, "object_type": object_type, "object_id": object_id},
                )
            )
        return str(self._ocs("GET", "/ocs/v2.php/apps/activity/api/v2/activity", params=params))

    # ==================================================================
    # OCS: User status
    # ==================================================================
    def get_own_status(self) -> str:
        """Get the authenticated account's own current status."""
        return str(self._ocs("GET", "/ocs/v2.php/apps/user_status/api/v1/user_status"))

    def set_own_status_type(self, status_type: str) -> str:
        """Set the predefined online-status type.

        :param status_type: One of "online", "away", "dnd", "invisible", "offline".
        """
        return str(
            self._ocs("PUT", "/ocs/v2.php/apps/user_status/api/v1/user_status/status", json_body={"statusType": status_type})
        )

    def set_own_status_message(self, message: str, status_icon: Optional[str] = None, clear_at: Optional[int] = None) -> str:
        """Set a custom status message.

        :param status_icon: An emoji, e.g. "🏖️".
        :param clear_at: Unix timestamp when the message should auto-clear; omit for no auto-clear.
        """
        body: dict[str, Any] = {"message": message}
        if status_icon:
            body["statusIcon"] = status_icon
        if clear_at is not None:
            body["clearAt"] = clear_at
        return str(self._ocs("PUT", "/ocs/v2.php/apps/user_status/api/v1/user_status/message/custom", json_body=body))

    def clear_own_status_message(self) -> str:
        """Clear the current custom status message (keeps the online-status type)."""
        return str(self._ocs("DELETE", "/ocs/v2.php/apps/user_status/api/v1/user_status/message"))

    def get_user_status(self, user_id: str) -> str:
        """Get another user's current status."""
        return str(self._ocs("GET", f"/ocs/v2.php/apps/user_status/api/v1/statuses/{user_id}"))

    def list_all_statuses(self, limit: Optional[int] = None, offset: Optional[int] = None) -> str:
        """List statuses for all users who have one set."""
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        return str(self._ocs("GET", "/ocs/v2.php/apps/user_status/api/v1/statuses", params=params))

    # ==================================================================
    # OCS: Preferences / config (best-effort, see module docstring)
    # ==================================================================
    def get_own_preference(self, app_id: str, config_key: str) -> str:
        """Get one of the authenticated account's own app preference values."""
        return str(self._ocs("GET", f"/ocs/v2.php/apps/provisioning_api/api/v1/preferences/{app_id}/{config_key}"))

    def set_own_preference(self, app_id: str, config_key: str, value: str) -> str:
        """Set one of the authenticated account's own app preference values."""
        return str(
            self._ocs(
                "POST",
                f"/ocs/v2.php/apps/provisioning_api/api/v1/preferences/{app_id}/{config_key}",
                json_body={"configValue": value},
            )
        )

    def delete_own_preference(self, app_id: str, config_key: str) -> str:
        """Delete (reset to default) one of the authenticated account's own app preference values."""
        return str(self._ocs("DELETE", f"/ocs/v2.php/apps/provisioning_api/api/v1/preferences/{app_id}/{config_key}"))

    def get_user_config_value(self, user_id: str, app_id: str, config_key: str) -> str:
        """Admin-level: get another user's app config value."""
        return str(
            self._ocs("GET", f"/ocs/v2.php/apps/provisioning_api/api/v1/config/users/{user_id}/{app_id}/{config_key}")
        )

    def set_user_config_value(self, user_id: str, app_id: str, config_key: str, value: str) -> str:
        """Admin-level: set another user's app config value."""
        return str(
            self._ocs(
                "POST",
                f"/ocs/v2.php/apps/provisioning_api/api/v1/config/users/{user_id}/{app_id}/{config_key}",
                json_body={"configValue": value},
            )
        )

    def delete_user_config_value(self, user_id: str, app_id: str, config_key: str) -> str:
        """Admin-level: delete another user's app config value."""
        return str(
            self._ocs("DELETE", f"/ocs/v2.php/apps/provisioning_api/api/v1/config/users/{user_id}/{app_id}/{config_key}")
        )

    # ==================================================================
    # OCS: Out-of-office (best-effort, see module docstring)
    # ==================================================================
    def get_out_of_office(self, user_id: str) -> str:
        """Get a user's currently configured out-of-office/vacation auto-reply, if any."""
        return str(self._ocs("GET", f"/ocs/v2.php/apps/dav/api/v1/outOfOffice/{user_id}"))

    def set_out_of_office(
        self, user_id: str, first_day: str, last_day: str, status: str, message: str
    ) -> str:
        """Configure a user's out-of-office auto-reply.

        :param first_day: "YYYY-MM-DD".
        :param last_day: "YYYY-MM-DD".
        :param status: Short status line shown next to the user's name.
        :param message: The auto-reply message body.
        """
        body = {"firstDay": first_day, "lastDay": last_day, "status": status, "message": message}
        return str(self._ocs("POST", f"/ocs/v2.php/apps/dav/api/v1/outOfOffice/{user_id}", json_body=body))

    def clear_out_of_office(self, user_id: str) -> str:
        """Remove a user's out-of-office auto-reply configuration."""
        return str(self._ocs("DELETE", f"/ocs/v2.php/apps/dav/api/v1/outOfOffice/{user_id}"))

    # ==================================================================
    # OCS: Recommendations & capabilities
    # ==================================================================
    def get_file_recommendations(self) -> str:
        """Get the authenticated account's recommended-files list (Nextcloud's own "recently relevant files" suggestions)."""
        return str(self._ocs("GET", "/ocs/v2.php/apps/recommendations/api/v1/recommendations"))

    def get_capabilities(self) -> str:
        """Get the server's capabilities (enabled apps, version, feature flags)."""
        return str(self._ocs("GET", "/ocs/v1.php/cloud/capabilities"))

    # ==================================================================
    # Escape hatches: anything not covered above, or an endpoint added in
    # a Nextcloud release after this file was written.
    # ==================================================================
    def raw_ocs_request(
        self,
        method: str,
        ocs_path: str,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
    ) -> str:
        """Advanced/last-resort: call any OCS API endpoint directly.

        :param method: HTTP method, one of GET, POST, PUT, DELETE.
        :param ocs_path: Full OCS path starting with "/ocs/", e.g. "/ocs/v2.php/apps/files_sharing/api/v1/shares".
        """
        if not ocs_path.startswith("/ocs/"):
            return str({"error": 'ocs_path must start with "/ocs/"'})
        return str(self._ocs(method.upper(), ocs_path, params=params, json_body=json_body))

    def raw_webdav_request(self, method: str, path: str, body_xml: Optional[str] = None) -> str:
        """Advanced/last-resort: call any WebDAV method/path directly under /remote.php/.

        :param method: WebDAV/HTTP method, e.g. PROPFIND, PROPPATCH, REPORT, SEARCH, MKCOL, MOVE, COPY, GET, PUT, DELETE.
        :param path: Path relative to /remote.php/, e.g. "dav/files/admin/Documents" (most resources live under a "dav/" prefix) or "comments/files/123" (the one exception -- see list_file_comments' docstring).
        :param body_xml: Optional raw XML request body (e.g. for PROPFIND/PROPPATCH/REPORT).
        """
        resp = self._dav_request(
            method.upper(),
            path,
            headers={"Content-Type": "application/xml"} if body_xml else None,
            content=body_xml.encode() if body_xml else None,
        )
        parse = method.upper() in ("PROPFIND", "REPORT", "SEARCH")
        return str(self._handle_dav_response(resp, parse=parse))
