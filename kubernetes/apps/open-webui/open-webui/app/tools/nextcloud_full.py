"""
title: Nextcloud (Full Access)
author: cypr0
version: 2.0.0
license: MIT
requirements: httpx
description: >
  FULL access to this cluster's Nextcloud instance, covering the "Client
  APIs" Nextcloud documents at docs.nextcloud.com/server/stable/
  developer_manual/client_apis/: WebDAV files (list/read/write/delete/move/
  copy, trashbin, versions, search) and the OCS APIs (sharing, user/group
  provisioning, and -- through raw_ocs_request -- notifications, activity,
  user status, preferences, out-of-office, recommendations, capabilities).

  This is NOT read-only: it can create, modify, and PERMANENTLY DELETE
  files, users, groups, shares, and every other resource type reachable
  here. DELETE on a file moves it to Nextcloud's trashbin (recoverable via
  restore_trashed_item until the trash is emptied); DELETE on the trashbin
  itself, or on users/groups/shares/tags, is immediate and irreversible.
  There is no confirmation step beyond whatever Open WebUI's own tool-call
  approval UI provides.

  ── Token budget (why this file looks the way it does) ──────────────────
  v1.0.0 spelled out one typed method per API operation: 85 methods, each
  one a JSON schema injected into the model's context on every chat turn
  this tool is enabled for, and (mirrored into nextcloud-mcp) 85 of the 232
  MCP tools hermes-agent had to pick between. v2.0.0 keeps 22 methods and
  gives up NO reach:

    * the file, trash, version, share and directory operations that carry
      real traffic stay typed, with their v1 names and parameter names
      unchanged;
    * everything else -- favorites, comments, systemtags, federated
      shares, user/group mutation, subadmins, apps, notifications,
      activity, user status, preferences, out-of-office, recommendations,
      server capabilities -- goes through raw_ocs_request() (OCS, JSON) or
      raw_webdav_request() (WebDAV, XML), which were already the documented
      escape hatches in v1;
    * capabilities() is the discovery path: it returns the OCS/WebDAV paths
      and body shapes needed to drive those two, on demand, so that
      reference material costs tokens only in the turn that asks for it.

  Note that v1's get_capabilities() is gone on purpose rather than folded
  in: a get_capabilities() (server feature flags) sitting next to a
  capabilities() (this tool's own cheat sheet) is exactly the near-name
  collision that made hermes-agent's paperless prompt need a disambiguation
  paragraph. Server capabilities now live at capabilities("endpoints") as a
  documented raw_ocs_request path.

  Auth model: HTTP Basic Auth with this cluster's actual Nextcloud
  ADMIN account (NEXTCLOUD_USERNAME/NEXTCLOUD_PASSWORD env vars on the Open
  WebUI pod -- see externalsecret-nextcloud-token.yaml, which reads the
  SAME admin credentials the nextcloud-credentials Secret already has).
  This was a deliberate choice over provisioning a separate dedicated
  account (like the read-only "openclaw-reader" user post-install-job.yaml
  creates): it means every OCS Provisioning endpoint genuinely works (no
  non-staff-style 403s, unlike the paperless_full tool), but it also means
  this tool wields the SAME real super-admin credential as the human admin
  login -- there is no separate revoke path for just this tool. Because the
  admin account has no per-user WebDAV namespace restriction, every
  WebDAV/trashbin/versions method below takes an optional `user_id`
  (defaults to the admin account) so this tool can browse/manage ANY user's
  files, not just the admin's own -- that is a direct consequence of using
  the real admin account and is intentional.

  Deliberately NOT implemented, and NOT to be reached via the escape
  hatches either: the Remote Wipe API (it exists to let an MDM remotely
  erase a *lost device* -- letting a chat tool trigger that on a live
  device is a straight-up destructive foot-gun, not a legitimate chat
  operation) and Login Flow v2 / app-password minting (this tool already
  authenticates with a standing credential; minting new sessions from
  inside chat adds attack surface for no benefit). The Talk Integration API
  is absent because the Talk/Spreed app is not installed on this instance
  (see kubernetes/apps/nextcloud/nextcloud/app/post-install-job.yaml's app
  list), and every app-*specific* API (Deck, Tables, Forms, Groupfolders,
  Collabora/richdocuments, Whiteboard) has its own API docs outside the two
  Client-API pages this tool was built against -- ask for a dedicated tool
  if you want one of those. The Assistant/Translation/TextProcessing/
  Text2Image/TaskProcessing OCS APIs are skipped too: this Nextcloud
  instance has no AI backend configured for them (this cluster's actual AI
  stack is Open WebUI + OpenRouter).

  MIRROR NOTICE: this file is duplicated byte-for-byte into
  kubernetes/apps/hermes-agent/nextcloud-mcp/app/nextcloud_full.py, where
  owui_tool_mcp_bridge.py serves it as a standalone MCP server. kustomize's
  configMapGenerator refuses file paths that escape its own kustomization
  directory, so a single shared copy isn't possible -- keep both in sync.
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

# Reference material for capabilities(). Lives here, NOT in the method
# docstrings, precisely so it stays out of the tool specs the model is fed
# on every turn -- it is fetched only when a call actually needs it.
_CAPABILITIES: dict[str, str] = {
    "files": """WebDAV paths are passed to raw_webdav_request(method, path, body_xml)
relative to /remote.php/ -- note most resources need an explicit "dav/"
prefix, comments being the one exception (an older Sabre plugin path):
  dav/files/<user>/<path>            files and folders
  dav/trashbin/<user>/trash          trashbin listing
  dav/trashbin/<user>/restore/<name> MOVE destination to restore
  dav/versions/<user>/versions/<fileid>
  dav/systemtags                     tag definitions (PROPFIND/POST)
  dav/systemtags-relations/files/<fileid>   tags on one file (PUT/DELETE)
  comments/files/<fileid>            comments (NO "dav/" prefix)
Useful verbs: PROPFIND (list/metadata, Depth header), REPORT (favorites,
filter-files), SEARCH (basicsearch), PROPPATCH, MKCOL, MOVE, COPY.
Favorites REPORT body on dav/files/<user>:
  <oc:filter-files xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">
    <oc:filter-rules><oc:favorite>1</oc:favorite></oc:filter-rules>
  </oc:filter-files>
Set a favorite: PROPPATCH dav/files/<user>/<path> with
  <d:propertyupdate xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">
    <d:set><d:prop><oc:favorite>1</oc:favorite></d:prop></d:set>
  </d:propertyupdate>
PERMANENT deletes (no trash behind them): DELETE on
dav/trashbin/<user>/trash/<name> (one item) or on
dav/trashbin/<user>/trash (the whole trashbin).""",
    "endpoints": """raw_ocs_request(method, ocs_path, params, json_body) reaches any OCS
endpoint. All of these already get format=json and the OCS-APIRequest
header; responses are unwrapped to their "data" payload.
  /ocs/v1.php/cloud/capabilities              server version/feature flags
  /ocs/v1.php/cloud/users[/<id>]              list/get/create/edit/delete
  /ocs/v1.php/cloud/users/<id>/enable|disable POST
  /ocs/v1.php/cloud/users/<id>/groups         GET/POST/DELETE {groupid}
  /ocs/v1.php/cloud/users/<id>/subadmins      GET/POST/DELETE
  /ocs/v1.php/cloud/groups[/<id>]             list/create/members/delete
  /ocs/v1.php/cloud/groups/<id>/subadmins     GET
  /ocs/v1.php/cloud/apps[/<app>]              list/get/enable/disable
  /ocs/v2.php/apps/files_sharing/api/v1/shares[/<id>]
  /ocs/v2.php/apps/files_sharing/api/v1/sharees?search=&itemType=
  /ocs/v2.php/apps/files_sharing/api/v1/remote_shares[/pending][/<id>]
  /ocs/v2.php/apps/notifications/api/v2/notifications[/<id>]
  /ocs/v2.php/apps/activity/api/v2/activity[/filter]?since=&limit=
  /ocs/v2.php/apps/user_status/api/v1/user_status  (GET/PUT status,message)
  /ocs/v2.php/apps/user_status/api/v1/statuses[/<user>]
  /ocs/v2.php/apps/provisioning_api/api/v1/config/users/<app>/<key>
  /ocs/v2.php/apps/dav/api/v1/outOfOffice/<user>  (GET/POST/DELETE)
  /ocs/v2.php/apps/recommendations/api/v1/recommendations
Editing a user: PUT /ocs/v1.php/cloud/users/<id> with {"key":..,"value":..}
-- one field per call, that is the API's own shape, not a limitation here.""",
    "shares": """create_share(path, share_type, ...) share_type values:
  0=user 1=group 3=public link 4=email 6=federated 7=circle 10=Talk room
permissions bitmask: 1=read 2=update 4=create 8=delete 16=share 31=all
Updating a share is PUT /ocs/v2.php/apps/files_sharing/api/v1/shares/<id>
with any of permissions, password, expireDate, note, publicUpload, label.
Federated: GET .../remote_shares (accepted), .../remote_shares/pending
(POST <id> to accept, DELETE <id> to decline).""",
    "danger": """Irreversible / high-blast-radius calls:
- delete_file() moves to the trashbin; DELETE on dav/trashbin/... via
  raw_webdav_request does NOT -- that is permanent.
- DELETE /ocs/v1.php/cloud/users/<id> deletes the account AND its files.
- delete_share() revokes access immediately, with no undo.
- The credential in use is the real Nextcloud super-admin, so every
  provisioning call genuinely succeeds -- there is no permission backstop
  catching a mistake here the way there is on the paperless tool.
- Do not use the Remote Wipe API (/ocs/v2.php/apps/settings/api/v1/wipe)
  or Login Flow v2 / app-password minting from this tool -- see the module
  docstring for why both are out of scope.""",
}

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
    # Discovery -- the progressive-disclosure half of the token budget
    # ==================================================================
    def capabilities(self, topic: str = "") -> str:
        """Reference for driving raw_ocs_request()/raw_webdav_request(): OCS and WebDAV paths, XML body shapes, share types, destructive calls. Call this before reaching for either escape hatch.

        :param topic: One of "files", "endpoints", "shares", "danger". Omit for all four.
        """
        key = topic.strip().lower()
        if not key:
            return "\n\n".join(f"## {k}\n{v}" for k, v in _CAPABILITIES.items())
        if key not in _CAPABILITIES:
            return f"Unknown topic '{topic}'. Available: {', '.join(_CAPABILITIES)}."
        return _CAPABILITIES[key]

    # ==================================================================
    # WebDAV: files & folders
    # ==================================================================
    def list_files(self, path: str = "", user_id: Optional[str] = None) -> str:
        """List a folder's contents, non-recursive.

        :param path: Folder path relative to the user's files root, e.g. "" for the root, or "Documents/Invoices".
        :param user_id: Whose files to browse. Defaults to the configured admin account; as an admin account, any user_id works.
        """
        return str(self._propfind(f"dav/files/{user_id or self._me()}/{path}", depth="1"))

    def get_file_info(self, path: str, user_id: Optional[str] = None) -> str:
        """Get one file or folder's metadata (fileid, size, mtime, etag, favorite, permissions, mimetype) without listing children."""
        return str(self._propfind(f"dav/files/{user_id or self._me()}/{path}", depth="0"))

    def download_file(self, path: str, user_id: Optional[str] = None) -> str:
        """Download a file's content base64-encoded, capped by the MAX_INLINE_DOWNLOAD_BYTES valve."""
        return self._download_bytes(f"dav/files/{user_id or self._me()}/{path}")

    def upload_file(self, path: str, file_content_base64: str, user_id: Optional[str] = None) -> str:
        """Upload (create or overwrite) a file.

        :param path: Destination path relative to the user's files root, e.g. "Documents/report.pdf". Parent folders must already exist -- use create_folder first.
        :param file_content_base64: The file's raw bytes, base64-encoded.
        """
        try:
            data = base64.b64decode(file_content_base64)
        except Exception as e:  # noqa: BLE001
            return str({"error": f"Invalid base64 in file_content_base64: {e}"})
        resp = self._dav_request("PUT", f"dav/files/{user_id or self._me()}/{path}", content=data)
        return str(self._handle_dav_response(resp))

    def create_folder(self, path: str, user_id: Optional[str] = None) -> str:
        """Create one folder level. The parent folder must already exist."""
        resp = self._dav_request("MKCOL", f"dav/files/{user_id or self._me()}/{path}")
        return str(self._handle_dav_response(resp))

    def delete_file(self, path: str, user_id: Optional[str] = None) -> str:
        """Delete a file or folder recursively. Moves it to the trashbin -- recoverable via restore_trashed_item until the trash is emptied."""
        resp = self._dav_request("DELETE", f"dav/files/{user_id or self._me()}/{path}")
        return str(self._handle_dav_response(resp))

    def _destination_url(self, dest_path: str, user_id: str) -> str:
        base = self.valves.NEXTCLOUD_BASE_URL.rstrip("/")
        return f"{base}/remote.php/dav/{self._dav_url_path(f'files/{user_id}/{dest_path}')}"

    def move_file(
        self,
        source_path: str,
        destination_path: str,
        user_id: Optional[str] = None,
        overwrite: bool = False,
    ) -> str:
        """Move or rename a file or folder within one user's files root.

        :param overwrite: Replace an existing item at the destination. Default false fails with 412 instead.
        """
        uid = user_id or self._me()
        resp = self._dav_request(
            "MOVE",
            f"dav/files/{uid}/{source_path}",
            headers={
                "Destination": self._destination_url(destination_path, uid),
                "Overwrite": "T" if overwrite else "F",
            },
        )
        return str(self._handle_dav_response(resp))

    def copy_file(
        self,
        source_path: str,
        destination_path: str,
        user_id: Optional[str] = None,
        overwrite: bool = False,
    ) -> str:
        """Copy a file or folder within one user's files root."""
        uid = user_id or self._me()
        resp = self._dav_request(
            "COPY",
            f"dav/files/{uid}/{source_path}",
            headers={
                "Destination": self._destination_url(destination_path, uid),
                "Overwrite": "T" if overwrite else "F",
            },
        )
        return str(self._handle_dav_response(resp))

    def search_files(
        self,
        term: str,
        user_id: Optional[str] = None,
        scope_path: str = "",
        search_property: str = "displayname",
    ) -> str:
        """Search files/folders by a LIKE pattern on a DAV property.

        :param term: Search term, wrapped as "%term%" for a substring match.
        :param scope_path: Folder to search within, relative to the user's files root. Empty searches the whole root.
        :param search_property: DAV property to match, e.g. "displayname" (filename) or "getcontenttype" (MIME type, e.g. "text/%").
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
    # WebDAV: trashbin & versions -- the recovery half. The PERMANENT
    # variants (delete one trashed item, empty the trash) are deliberately
    # left to raw_webdav_request; see capabilities("files").
    # ==================================================================
    def list_trash(self, user_id: Optional[str] = None) -> str:
        """List the items currently in a user's trashbin."""
        return str(self._propfind(f"dav/trashbin/{user_id or self._me()}/trash", depth="1"))

    def restore_trashed_item(self, trash_filename: str, user_id: Optional[str] = None) -> str:
        """Restore an item out of the trashbin back to its original location.

        :param trash_filename: The item's filename exactly as list_trash shows it. Nextcloud restores it to its recorded original path automatically.
        """
        uid = user_id or self._me()
        base = self.valves.NEXTCLOUD_BASE_URL.rstrip("/")
        dest = f"{base}/remote.php/dav/{self._dav_url_path(f'trashbin/{uid}/restore/{trash_filename}')}"
        resp = self._dav_request(
            "MOVE", f"dav/trashbin/{uid}/trash/{trash_filename}", headers={"Destination": dest}
        )
        return str(self._handle_dav_response(resp))

    def list_file_versions(self, file_id: str, user_id: Optional[str] = None) -> str:
        """List a file's stored versions, given its Nextcloud fileid from get_file_info/list_files."""
        return str(self._propfind(f"dav/versions/{user_id or self._me()}/versions/{file_id}", depth="1"))

    def restore_file_version(self, file_id: str, version_id: str, user_id: Optional[str] = None) -> str:
        """Restore an older version as the current version of a file.

        :param version_id: The version's own id -- the last path segment of a version's href from list_file_versions.
        """
        uid = user_id or self._me()
        base = self.valves.NEXTCLOUD_BASE_URL.rstrip("/")
        dest = f"{base}/remote.php/dav/{self._dav_url_path(f'versions/{uid}/restore/target')}"
        resp = self._dav_request(
            "COPY", f"dav/versions/{uid}/versions/{file_id}/{version_id}", headers={"Destination": dest}
        )
        return str(self._handle_dav_response(resp))

    # ==================================================================
    # OCS: sharing
    # ==================================================================
    def list_shares(self, path: Optional[str] = None, reshares: bool = False, subfiles: bool = False) -> str:
        """List shares. Without path, lists every share the authenticated account created.

        :param path: Path relative to that user's files root, to scope to one file/folder.
        :param reshares: With path set, also include shares of that item made by other users.
        :param subfiles: With path set on a folder, include shares of everything inside it.
        """
        params: dict[str, Any] = {}
        if path is not None:
            params["path"] = path
        if reshares:
            params["reshares"] = "true"
        if subfiles:
            params["subfiles"] = "true"
        return str(self._ocs("GET", "/ocs/v2.php/apps/files_sharing/api/v1/shares", params=params))

    def create_share(
        self,
        path: str,
        share_type: int,
        share_with: Optional[str] = None,
        permissions: Optional[int] = None,
        password: Optional[str] = None,
        expire_date: Optional[str] = None,
        note: Optional[str] = None,
        public_upload: Optional[bool] = None,
        send_mail: Optional[bool] = None,
    ) -> str:
        """Create a share.

        :param path: File/folder to share, relative to the authenticated account's files root.
        :param share_type: 0=user 1=group 3=public link 4=email 6=federated 7=circle 10=Talk room.
        :param share_with: Recipient user/group id. Required for types 0/1/4/6/7/10, omit for a plain public link (type 3).
        :param permissions: Bitmask: 1=read 2=update 4=create 8=delete 16=share 31=all.
        :param expire_date: "YYYY-MM-DD".
        :param public_upload: Allow uploads into a public-link-shared folder.
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
        if public_upload is not None:
            body["publicUpload"] = "true" if public_upload else "false"
        if send_mail is not None:
            body["sendMail"] = "true" if send_mail else "false"
        return str(self._ocs("POST", "/ocs/v2.php/apps/files_sharing/api/v1/shares", json_body=body))

    def delete_share(self, share_id: int) -> str:
        """PERMANENTLY revoke a share. Irreversible."""
        return str(self._ocs("DELETE", f"/ocs/v2.php/apps/files_sharing/api/v1/shares/{share_id}"))

    def search_sharees(self, search: str, item_type: str = "file", limit: int = 25) -> str:
        """Search potential share recipients (users, groups, ...) by name, for use as share_with in create_share.

        :param item_type: "file" or "folder".
        """
        return str(
            self._ocs(
                "GET",
                "/ocs/v2.php/apps/files_sharing/api/v1/sharees",
                params={"search": search, "itemType": item_type, "perPage": limit},
            )
        )

    # ==================================================================
    # OCS: directory. Read-only here on purpose -- creating, editing,
    # disabling and deleting users/groups all go through raw_ocs_request,
    # so those calls have to be written out deliberately rather than being
    # one tab-completion away. See capabilities("endpoints").
    # ==================================================================
    def list_users(
        self, search: Optional[str] = None, limit: Optional[int] = None, offset: Optional[int] = None
    ) -> str:
        """List Nextcloud user ids.

        :param search: Substring filter on the user id.
        """
        params: dict[str, Any] = {}
        if search:
            params["search"] = search
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        return str(self._ocs("GET", "/ocs/v1.php/cloud/users", params=params))

    def list_groups(
        self, search: Optional[str] = None, limit: Optional[int] = None, offset: Optional[int] = None
    ) -> str:
        """List Nextcloud group ids.

        :param search: Substring filter on the group id.
        """
        params: dict[str, Any] = {}
        if search:
            params["search"] = search
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        return str(self._ocs("GET", "/ocs/v1.php/cloud/groups", params=params))

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
        """Call any OCS API endpoint directly -- user/group provisioning, apps, notifications, activity, user status, preferences, out-of-office, server capabilities. Call capabilities("endpoints") first for the paths.

        :param method: GET, POST, PUT or DELETE.
        :param ocs_path: Full OCS path starting with "/ocs/", e.g. "/ocs/v1.php/cloud/capabilities".
        """
        if not ocs_path.startswith("/ocs/"):
            return str({"error": 'ocs_path must start with "/ocs/"'})
        return str(self._ocs(method.upper(), ocs_path, params=params, json_body=json_body))

    def raw_webdav_request(self, method: str, path: str, body_xml: Optional[str] = None) -> str:
        """Call any WebDAV method/path directly under /remote.php/ -- favorites, comments, systemtags, permanent trash deletion. Call capabilities("files") first for the paths and XML bodies.

        :param method: PROPFIND, PROPPATCH, REPORT, SEARCH, MKCOL, MOVE, COPY, GET, PUT or DELETE.
        :param path: Path relative to /remote.php/, e.g. "dav/files/admin/Documents". Most resources need the "dav/" prefix; comments ("comments/files/123") is the exception.
        :param body_xml: Raw XML request body, for PROPFIND/PROPPATCH/REPORT/SEARCH.
        """
        resp = self._dav_request(
            method.upper(),
            path,
            headers={"Content-Type": "application/xml"} if body_xml else None,
            content=body_xml.encode() if body_xml else None,
        )
        parse = method.upper() in ("PROPFIND", "REPORT", "SEARCH")
        return str(self._handle_dav_response(resp, parse=parse))
