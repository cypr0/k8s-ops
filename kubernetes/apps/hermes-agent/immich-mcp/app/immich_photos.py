"""
title: Immich (Photos)
author: cypr0
version: 1.0.0
license: MIT
requirements: httpx
description: >
  Access to this cluster's Immich photo library
  (kubernetes/apps/immich/immich/): CLIP-backed natural-language photo
  search, metadata search, albums, people, and asset/thumbnail download.

  Built against the routes of the ACTUALLY DEPLOYED Immich version, read
  off the running container's compiled controllers rather than from
  upstream docs, because Immich v3 reshaped the search API: every search
  endpoint now accepts BOTH the legacy flat body (personIds, takenAfter,
  city, ...) and a new nested {filter, orderBy, cursor} body, and the two
  are mutually exclusive per request -- mixing a flat field with `filter`
  is a validation error, not a merge (server/dist/dtos/search.dto.js,
  `withShapeExclusivity`). This tool sends the flat shape throughout; if
  you need the nested one, build it yourself and send it via raw_request.

  ── Read-mostly by design ───────────────────────────────────────────────
  Writes are limited to things that are cheap to undo: creating an album,
  adding assets to one, and toggling favorite/description on an asset.
  There is NO delete method, and raw_request REFUSES the DELETE verb
  outright -- not because the API lacks it, but because a chat tool that
  can delete someone's photos is a different risk class from one that can
  delete a Paperless document (which goes to a recoverable trash and can
  be re-scanned) or a Nextcloud file (trashbin, versions). Immich's own
  trash exists, but "the model misread the request and trashed 400 photos"
  is not a mistake worth making reachable from a chat turn. Deleting
  photos stays a deliberate act in the Immich UI.

  ── Token budget ────────────────────────────────────────────────────────
  Two deliberate choices, both about volume rather than method count:
    * 14 methods, with capabilities() as the discovery path for everything
      else (tags, stacks, memories, shared links, timeline buckets, the
      server admin endpoints) via raw_request -- same pattern as
      paperless_full.py/nextcloud_full.py v2.
    * search results are SLIMMED by default. An Immich asset response is
      large (full EXIF block, people with face bounding boxes, tags,
      stack, owner, checksums) and smart search returns up to 1000 of them
      -- a single unslimmed `size=100` search is tens of thousands of
      tokens of context for what is almost always "which photos are
      these". _slim_asset() projects each hit down to id, filename, type,
      date, place, favorite and people names. Pass verbose=True when the
      full record genuinely matters.

  Auth model: an Immich API key sent as the `x-api-key` header
  (IMMICH_API_KEY env var on the Open WebUI pod -- see
  externalsecret-immich-token.yaml). Unlike the Nextcloud and Paperless
  tools, this credential is NOT reused from somewhere else in the cluster:
  Immich has no provisionable service credential, API keys can only be
  minted by a signed-in user in the web UI (Account Settings > API Keys),
  and this instance is OIDC-only with passwordLogin disabled. So the key
  has to be created by hand once and stored in the 1Password "immich" item
  as IMMICH_API_KEY -- see docs/apps/open-webui.md. Immich API keys carry
  a granular permission list; grant the key asset.read, asset.view,
  asset.update, album.read, album.create, album.update, person.read and
  server.about, and nothing else. The key inherits its owning user's
  library scope, so it sees that user's photos plus what is shared with
  them -- there is no "whole instance" key.

  MIRROR NOTICE: this file is duplicated byte-for-byte into
  kubernetes/apps/hermes-agent/immich-mcp/app/immich_photos.py, where
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
    "search": """Flat search body fields, shared by /api/search/smart and
/api/search/metadata (this tool's search_photos / search_by_metadata):
  query            natural language, smart search only (CLIP)
  queryAssetId     find assets similar to this one, smart search only
  personIds[]      restrict to photos containing these people (AND)
  tagIds[]  albumIds[]  libraryId
  takenAfter takenBefore   ISO datetime, when the photo was taken
  createdAfter createdBefore updatedAfter   ISO datetime, upload/change time
  city state country make model lensModel   exact strings
  type             IMAGE | VIDEO | AUDIO | OTHER
  visibility       timeline | hidden | archive | locked
  isFavorite isMotion isOffline isEncoded isNotInAlbum   booleans
  rating           -1..5
  withExif withDeleted withStacked withPeople   booleans
  size             1..1000 (default 100), page  1-based
metadata-only extras: id description checksum originalFileName originalPath
Immich v3 also accepts a nested {filter, orderBy, cursor} body instead --
NOT combinable with any flat field above in the same request. Build it
yourself and POST it through raw_request if you need it.
Response: {"assets": {"items": [...], "total": n, "count": n, "nextPage": n}}""",
    "endpoints": """raw_request(method, path, params, json_body) reaches any endpoint.
DELETE is refused by this tool -- see capabilities("danger").
  /api/search/smart          POST  CLIP natural-language search
  /api/search/metadata       POST  structured search
  /api/search/random         POST  random sample, same filter fields
  /api/search/statistics     POST  counts for a filter, no asset payload
  /api/search/person?name=   GET   find people by name
  /api/search/places?name=   GET   find places
  /api/search/cities         GET   cities that have photos
  /api/search/explore        GET   the "explore" clusters
  /api/search/suggestions?type=  country|state|city|camera-make|camera-model
  /api/assets/{id}                GET/PUT   one asset, update favorite etc.
  /api/assets/{id}/thumbnail?size=thumbnail|preview
  /api/assets/{id}/original       GET  full-resolution bytes
  /api/assets/{id}/ocr            GET  recognised text in the image
  /api/assets/statistics          GET
  /api/albums  /api/albums/{id}   GET/POST/PATCH
  /api/albums/{id}/assets         PUT (add) -- DELETE refused here
  /api/albums/{id}/users          PUT  share an album with a user
  /api/people  /api/people/{id}   GET/PUT, /{id}/statistics, /{id}/thumbnail
  /api/tags  /api/tags/{id}/assets       /api/stacks
  /api/memories  /api/memories/{id}/assets
  /api/timeline/buckets?timeBucket=      month-bucketed timeline
  /api/shared-links               GET/POST
  /api/server/about  /statistics  /storage  /version  /features
  /api/view/folder  /api/view/folder/unique-paths""",
    "danger": """This tool is read-mostly on purpose:
- There is NO delete method, and raw_request REFUSES DELETE for any path.
  Removing photos, albums or album members stays a deliberate act in the
  Immich UI. See the module docstring for the reasoning.
- create_album / add_assets_to_album / update_asset are the only writes,
  all cheap to undo by hand.
- /api/server/statistics and the other admin endpoints only answer if the
  API key's owning user is an admin AND the key carries that permission;
  a 403 here means the key is scoped too narrowly, not that the endpoint
  is wrong.
- Downloaded originals are inlined as base64 and capped by the
  MAX_INLINE_DOWNLOAD_BYTES valve -- videos will almost always exceed it.
  Prefer get_asset_thumbnail() for anything the model just needs to look
  at.""",
}


class Tools:
    class Valves(BaseModel):
        """Admin-configured, shared by every user.

        API_KEY defaults to the IMMICH_API_KEY environment variable on the
        Open WebUI pod (populated from the 1Password "immich" item -- see
        externalsecret-immich-token.yaml). Users should never need to touch
        this; it exists here only as a manual override/rotation escape
        hatch.
        """

        IMMICH_BASE_URL: str = Field(
            default_factory=lambda: os.getenv(
                "IMMICH_BASE_URL",
                "http://immich-immich-server.immich.svc.cluster.local:2283",
            ),
            description="Immich server base URL, in-cluster Service DNS "
            "(no trailing slash, no /api suffix). app-template names "
            "Services '<release>-<service-key>', hence the doubled name.",
        )
        API_KEY: str = Field(
            default_factory=lambda: os.getenv("IMMICH_API_KEY", ""),
            description="Immich API key (x-api-key header) -- auto-filled "
            "from the cluster secret; override only to rotate/test.",
        )
        REQUEST_TIMEOUT_SECONDS: int = Field(default=30)
        DEFAULT_RESULT_SIZE: int = Field(
            default=25,
            description="Default number of search hits. Immich itself "
            "allows up to 1000, which is far more than a chat turn can "
            "usefully hold.",
        )
        MAX_RESULT_SIZE: int = Field(default=200)
        MAX_INLINE_DOWNLOAD_BYTES: int = Field(
            default=3_000_000,
            description="Cap on how large an asset download_asset will "
            "inline as base64. Larger assets return an error with the "
            "size instead.",
        )

    def __init__(self):
        self.valves = self.Valves()

    # ------------------------------------------------------------------
    # internal helpers (not exposed to the model)
    # ------------------------------------------------------------------
    def _auth_header(self) -> dict[str, str]:
        if not self.valves.API_KEY:
            raise RuntimeError(
                "No Immich API key configured. This should be auto-filled "
                "from the cluster secret -- if missing, check "
                "IMMICH_API_KEY on the Open WebUI pod, or set it manually "
                "in this tool's Valves (gear icon). Immich API keys are "
                "minted by hand in Account Settings > API Keys."
            )
        return {"x-api-key": self.valves.API_KEY}

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.valves.IMMICH_BASE_URL.rstrip("/"),
            headers={**self._auth_header(), "Accept": "application/json"},
            timeout=self.valves.REQUEST_TIMEOUT_SECONDS,
        )

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
    ) -> Any:
        try:
            with self._client() as client:
                resp = client.request(method, path, params=params, json=json_body)
                if resp.status_code == 204:
                    return {"status": "ok"}
                resp.raise_for_status()
                if not resp.content:
                    return {}
                ctype = resp.headers.get("content-type", "")
                if "application/json" not in ctype:
                    return {
                        "content_type": ctype,
                        "content_length": len(resp.content),
                        "note": "Non-JSON response; use download_asset() or "
                        "get_asset_thumbnail() for image/video bytes.",
                    }
                return resp.json()
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            try:
                detail = str(e.response.json())
            except Exception:  # noqa: BLE001
                detail = e.response.text[:500]
            if status == 401:
                return {
                    "error": "Immich rejected the API key (401). Check the "
                    "key configured in this tool's Valves."
                }
            if status == 403:
                return {
                    "error": "Immich denied access (403). The API key's "
                    "permission list likely doesn't cover this endpoint, "
                    "or it needs an admin user. Detail: " + detail
                }
            if status == 404:
                return {"error": "Not found (404). Check the id/path."}
            if status in (400, 422):
                return {"error": f"Validation error ({status}): {detail}"}
            return {"error": f"Immich API error: HTTP {status}. {detail}"}
        except httpx.RequestError as e:
            return {"error": f"Could not reach Immich: {e}"}

    def _slim_asset(self, asset: dict[str, Any]) -> dict[str, Any]:
        """Project one asset down to what a chat turn actually needs. See
        the module docstring's token-budget note for why this exists."""
        exif = asset.get("exifInfo") or {}
        place = ", ".join(
            p for p in (exif.get("city"), exif.get("state"), exif.get("country")) if p
        )
        slim = {
            "id": asset.get("id"),
            "file": asset.get("originalFileName"),
            "type": asset.get("type"),
            "taken": asset.get("localDateTime") or asset.get("fileCreatedAt"),
        }
        if place:
            slim["place"] = place
        if asset.get("isFavorite"):
            slim["favorite"] = True
        people = [p.get("name") for p in (asset.get("people") or []) if p.get("name")]
        if people:
            slim["people"] = people
        if exif.get("description") or asset.get("description"):
            slim["description"] = exif.get("description") or asset.get("description")
        return slim

    def _slim_search(self, result: Any, verbose: bool) -> Any:
        if verbose or not isinstance(result, dict):
            return result
        assets = result.get("assets")
        if not isinstance(assets, dict):
            return result
        items = assets.get("items")
        if not isinstance(items, list):
            return result
        return {
            "total": assets.get("total"),
            "count": assets.get("count"),
            "next_page": assets.get("nextPage"),
            "assets": [self._slim_asset(a) for a in items if isinstance(a, dict)],
            "note": "Slimmed. Pass verbose=True for the full asset records.",
        }

    def _search_body(self, size: Optional[int], page: Optional[int], **fields: Any) -> dict[str, Any]:
        body: dict[str, Any] = {
            "size": min(size or self.valves.DEFAULT_RESULT_SIZE, self.valves.MAX_RESULT_SIZE)
        }
        if page is not None:
            body["page"] = page
        for key, value in fields.items():
            if value is not None:
                body[key] = value
        return body

    def _fetch_bytes(self, path: str, params: Optional[dict[str, Any]] = None) -> str:
        try:
            with self._client() as client:
                resp = client.get(path, params=params)
                resp.raise_for_status()
                size = len(resp.content)
                if size > self.valves.MAX_INLINE_DOWNLOAD_BYTES:
                    return str(
                        {
                            "error": f"Asset is {size} bytes, over the "
                            f"{self.valves.MAX_INLINE_DOWNLOAD_BYTES}-byte "
                            "inline cap (MAX_INLINE_DOWNLOAD_BYTES valve). "
                            "Use get_asset_thumbnail() instead, or open it "
                            "in the Immich web UI."
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
            return str({"error": f"Could not reach Immich: {e}"})

    # ==================================================================
    # Discovery -- the progressive-disclosure half of the token budget
    # ==================================================================
    def capabilities(self, topic: str = "") -> str:
        """Reference for driving search and raw_request(): the full search filter field list, endpoint paths, and what this tool deliberately refuses. Call this before reaching for raw_request().

        :param topic: One of "search", "endpoints", "danger". Omit for all three.
        """
        key = topic.strip().lower()
        if not key:
            return "\n\n".join(f"## {k}\n{v}" for k, v in _CAPABILITIES.items())
        if key not in _CAPABILITIES:
            return f"Unknown topic '{topic}'. Available: {', '.join(_CAPABILITIES)}."
        return _CAPABILITIES[key]

    # ==================================================================
    # Search
    # ==================================================================
    def search_photos(
        self,
        query: str,
        person_ids: Optional[list[str]] = None,
        album_ids: Optional[list[str]] = None,
        taken_after: Optional[str] = None,
        taken_before: Optional[str] = None,
        city: Optional[str] = None,
        country: Optional[str] = None,
        asset_type: Optional[str] = None,
        is_favorite: Optional[bool] = None,
        size: Optional[int] = None,
        page: Optional[int] = None,
        verbose: bool = False,
    ) -> str:
        """Search photos by natural-language description using Immich's CLIP model, e.g. "kids on a beach at sunset". This is the right method for "find the photo where ...".

        :param query: What the photo shows, in plain language. Describe content, not filenames.
        :param person_ids: Restrict to photos containing ALL of these people -- get ids from list_people.
        :param taken_after: ISO datetime; when the photo was taken, not when it was uploaded.
        :param taken_before: ISO datetime.
        :param asset_type: IMAGE, VIDEO, AUDIO or OTHER.
        :param size: Number of hits, default 25, capped by the MAX_RESULT_SIZE valve.
        :param verbose: Return full asset records instead of the slimmed projection. Expensive.
        """
        body = self._search_body(
            size,
            page,
            query=query,
            personIds=person_ids,
            albumIds=album_ids,
            takenAfter=taken_after,
            takenBefore=taken_before,
            city=city,
            country=country,
            type=asset_type,
            isFavorite=is_favorite,
            withExif=True,
            withPeople=True,
        )
        return str(self._slim_search(self._request("POST", "/api/search/smart", json_body=body), verbose))

    def search_by_metadata(
        self,
        original_file_name: Optional[str] = None,
        description: Optional[str] = None,
        person_ids: Optional[list[str]] = None,
        album_ids: Optional[list[str]] = None,
        tag_ids: Optional[list[str]] = None,
        taken_after: Optional[str] = None,
        taken_before: Optional[str] = None,
        city: Optional[str] = None,
        country: Optional[str] = None,
        make: Optional[str] = None,
        model: Optional[str] = None,
        asset_type: Optional[str] = None,
        is_favorite: Optional[bool] = None,
        is_not_in_album: Optional[bool] = None,
        size: Optional[int] = None,
        page: Optional[int] = None,
        extra_filters: Optional[dict[str, Any]] = None,
        verbose: bool = False,
    ) -> str:
        """Search photos by structured metadata rather than content -- filename, date, place, camera, people, album membership. Use search_photos when the question is about what the picture shows.

        :param taken_after: ISO datetime; when the photo was taken.
        :param make: Camera manufacturer, exact string, e.g. "Apple".
        :param is_not_in_album: True finds photos not filed into any album yet.
        :param extra_filters: Any other flat search field -- see capabilities("search").
        :param verbose: Return full asset records instead of the slimmed projection. Expensive.
        """
        body = self._search_body(
            size,
            page,
            originalFileName=original_file_name,
            description=description,
            personIds=person_ids,
            albumIds=album_ids,
            tagIds=tag_ids,
            takenAfter=taken_after,
            takenBefore=taken_before,
            city=city,
            country=country,
            make=make,
            model=model,
            type=asset_type,
            isFavorite=is_favorite,
            isNotInAlbum=is_not_in_album,
            withExif=True,
            withPeople=True,
        )
        if extra_filters:
            body.update(extra_filters)
        return str(
            self._slim_search(self._request("POST", "/api/search/metadata", json_body=body), verbose)
        )

    # ==================================================================
    # Assets
    # ==================================================================
    def get_asset(self, asset_id: str) -> str:
        """Get one asset's full record: EXIF, people, tags, album membership, checksums."""
        return str(self._request("GET", f"/api/assets/{asset_id}"))

    def get_asset_thumbnail(self, asset_id: str, size: str = "preview") -> str:
        """Get an asset's thumbnail base64-encoded. Use this rather than download_asset whenever the point is to look at the picture.

        :param size: "preview" (larger, default) or "thumbnail" (small).
        """
        if size not in ("preview", "thumbnail"):
            return str({"error": 'size must be "preview" or "thumbnail"'})
        return self._fetch_bytes(f"/api/assets/{asset_id}/thumbnail", params={"size": size})

    def download_asset(self, asset_id: str) -> str:
        """Download an asset's full-resolution original base64-encoded, capped by the MAX_INLINE_DOWNLOAD_BYTES valve. Videos will usually exceed the cap."""
        return self._fetch_bytes(f"/api/assets/{asset_id}/original")

    def update_asset(
        self,
        asset_id: str,
        is_favorite: Optional[bool] = None,
        description: Optional[str] = None,
        rating: Optional[int] = None,
    ) -> str:
        """Update an asset's favorite flag, description or rating. Only pass what should change.

        :param rating: -1 to 5.
        """
        body: dict[str, Any] = {}
        if is_favorite is not None:
            body["isFavorite"] = is_favorite
        if description is not None:
            body["description"] = description
        if rating is not None:
            body["rating"] = rating
        if not body:
            return str({"error": "Nothing to update -- pass at least one field."})
        return str(self._request("PUT", f"/api/assets/{asset_id}", json_body=body))

    # ==================================================================
    # Albums
    # ==================================================================
    def list_albums(self, shared: Optional[bool] = None) -> str:
        """List albums with their names, asset counts and owners.

        :param shared: True lists only shared albums, False only own ones. Omit for all.
        """
        params = {"shared": str(shared).lower()} if shared is not None else None
        return str(self._request("GET", "/api/albums", params=params))

    def get_album(self, album_id: str, verbose: bool = False) -> str:
        """Get one album with its assets.

        :param verbose: Return full asset records instead of the slimmed projection. Expensive on a large album.
        """
        result = self._request("GET", f"/api/albums/{album_id}")
        if verbose or not isinstance(result, dict):
            return str(result)
        assets = result.get("assets")
        if isinstance(assets, list):
            result = dict(result)
            result["assets"] = [self._slim_asset(a) for a in assets if isinstance(a, dict)]
            result["note"] = "Assets slimmed. Pass verbose=True for the full records."
        return str(result)

    def create_album(
        self,
        album_name: str,
        description: str = "",
        asset_ids: Optional[list[str]] = None,
    ) -> str:
        """Create an album, optionally with an initial set of assets."""
        body: dict[str, Any] = {"albumName": album_name}
        if description:
            body["description"] = description
        if asset_ids:
            body["assetIds"] = asset_ids
        return str(self._request("POST", "/api/albums", json_body=body))

    def add_assets_to_album(self, album_id: str, asset_ids: list[str]) -> str:
        """Add assets to an existing album. Assets already in it are reported as duplicates rather than failing the call."""
        return str(
            self._request("PUT", f"/api/albums/{album_id}/assets", json_body={"ids": asset_ids})
        )

    # ==================================================================
    # People & server
    # ==================================================================
    def list_people(
        self, name: Optional[str] = None, page: int = 1, size: int = 50, with_hidden: bool = False
    ) -> str:
        """List the people Immich's face recognition has found, with their ids and photo counts. Use this to resolve a name to the person_ids the search methods take.

        :param name: Search by name instead of listing everyone.
        """
        if name:
            return str(self._request("GET", "/api/search/person", params={"name": name}))
        return str(
            self._request(
                "GET",
                "/api/people",
                params={"page": page, "size": size, "withHidden": str(with_hidden).lower()},
            )
        )

    def get_server_statistics(self) -> str:
        """Library-wide counts and storage usage. Needs an admin-scoped API key; a 403 means the key is too narrow."""
        return str(self._request("GET", "/api/server/statistics"))

    def raw_request(
        self,
        method: str,
        path: str,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
    ) -> str:
        """Call any Immich API endpoint not covered above -- tags, stacks, memories, shared links, timeline buckets, OCR, server info. Call capabilities("endpoints") first for the paths.

        :param method: GET, POST, PUT or PATCH. DELETE is refused by this tool -- see capabilities("danger").
        :param path: API path starting with "/api/", e.g. "/api/memories".
        :param json_body: JSON request body for POST/PUT/PATCH.
        """
        verb = method.upper()
        if not path.startswith("/api/"):
            return str({"error": 'path must start with "/api/"'})
        if verb == "DELETE":
            return str(
                {
                    "error": "This tool refuses DELETE on Immich. Removing "
                    "photos, albums or album members is a deliberate act "
                    "in the Immich UI, not a chat turn. See "
                    'capabilities("danger").'
                }
            )
        if verb not in ("GET", "POST", "PUT", "PATCH"):
            return str({"error": f"Unsupported method '{method}'. Use GET, POST, PUT or PATCH."})
        return str(self._request(verb, path, params=params, json_body=json_body))
