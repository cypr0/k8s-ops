# MIRROR NOTICE: this file has no Open WebUI counterpart (unlike
# paperless_full.py/nextcloud_full.py) -- it was written directly for
# hermes-agent's MCP use via owui_tool_mcp_bridge.py, which only requires
# a plain `Tools` class with public methods, no Open-WebUI-specific
# runtime behavior. Kept in the same shape as the other two anyway
# (Valves, docstring-as-tool-description, `str(...)`-returning methods)
# so the pattern stays consistent across all MCP servers in this
# namespace.
"""
title: Mailu (Calendar + Mail)
author: cypr0
version: 1.0.0
license: MIT
requirements: httpx, icalendar
description: >
  Read/write access to the in-cluster Mailu instance's CalDAV calendar
  (via its bundled Radicale component), and read-only access to its IMAP
  mailbox -- for BOTH the owner's and Ann's personal mailboxes. Replaces
  the former sogo-mcp, which pointed at the same kind of account on
  Netcup's now-retired SOGo hosting (see git history) -- this is a
  straight swap of backend, same scope/capabilities/tradeoffs. Built for
  hermes-agent's document-pipeline / contract-monitoring use cases:
  creating calendar reminders for invoice due dates or contract
  cancellation deadlines, and letting the agent search/read either
  mailbox directly when asked.

  Auth model: HTTP Basic Auth (CalDAV, via Mailu's `front` component,
  which validates it and forwards to Radicale) and plain IMAP LOGIN
  (against Mailu's `dovecot` component via `front`), both using the SAME
  real personal Mailu account per mailbox (PHILIPP_USERNAME/
  PHILIPP_PASSWORD or ANN_USERNAME/ANN_PASSWORD env vars -- see
  externalsecret.yaml, sourced from the "mailu-philipp"/"mailu-ann"
  1Password items). These are the owner's and Ann's own real mailbox/
  calendar accounts, not dedicated narrower-scope service accounts --
  same tradeoff the former sogo-mcp made, since Mailu (like most mail
  servers) has no separate read-only or calendar-only credential
  mechanism, only per-mailbox master passwords.

  Scope decisions: CalDAV (RFC 4791) covers list/create of events only --
  no recurrence rules, no attendee invites, no delete/update. The
  intended use is one-shot reminder events; editing or removing a
  wrongly-created one is a two-second job in any real calendar client,
  and a delete_event tool against someone's actual personal calendar
  isn't worth the risk for what this integration is for. IMAP is READ-
  ONLY by design (list folders, search, read a message) -- no send/
  delete/flag-mutate methods: this exists so the agent can look
  something up in a mailbox on request, not act as a mail client.

  Mailu-specific: reached via mail.${SECRET_DOMAIN} (NOT the raw
  mailu-front.mail.svc.cluster.local Service name -- that name isn't
  covered by Mailu's TLS cert SANs, which only list mail.${SECRET_DOMAIN}
  and webmail.${SECRET_DOMAIN}; CoreDNS resolves the former straight to
  mailu-front's ClusterIP for in-cluster clients, same trick
  paperless-ngx's own IMAP integration relies on). CalDAV is proxied by
  `front` at a fixed /webdav/ path (confirmed live via front's nginx
  conf: `/.well-known/caldav` 301-redirects to `/webdav/`, and `/webdav`
  itself does `auth_request /internal/auth/basic` then forwards to
  Radicale with the validated username in an X-Remote-User header --
  Radicale's own auth is `type = http_x_remote_user`, it never sees the
  password directly). Each mailbox's calendar-and-addressbook home is at
  `/webdav/<full-email-address>/`; calendars are NOT distinguished by a
  friendly name (Radicale assigns each collection a UUID, e.g.
  `1eec3a2f-.../`), so list_calendars()/the auto-detect default below
  discover them by CalDAV resourcetype rather than any hardcoded id.
"""

from __future__ import annotations

import imaplib
import logging
import os
import re
import uuid
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email import message_from_bytes
from email.header import decode_header
from email.utils import parsedate_to_datetime
from typing import Any, Literal, Optional

import httpx
from icalendar import Calendar, Event
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_DAV_NS = "DAV:"
_CALDAV_NS = "urn:ietf:params:xml:ns:caldav"

Mailbox = Literal["philipp", "ann"]

# RFC 3501 mailbox-list untagged response: (flags) delimiter name -- name
# and delimiter are each EITHER a quoted string or a bare atom (same
# mixed-form issue the former sogo-mcp hit against Netcup's SOGo server;
# kept defensively here even though Dovecot hasn't been observed to mix
# them), so a naive quote-count split breaks on the bare-atom entries.
_IMAP_LIST_RE = re.compile(r'^\([^)]*\)\s+(?:"[^"]*"|\S+)\s+(?P<name>".*"|\S+)$')


class Tools:
    class Valves(BaseModel):
        """Admin-configured. Per-mailbox username/password are required --
        there is no safe guessable default for someone else's mail/
        calendar account. MAILU_HOST/MAILU_IMAP_PORT have real defaults
        since they're not secret (see module docstring)."""

        MAILU_HOST: str = Field(
            default_factory=lambda: os.getenv("MAILU_HOST", ""),
            description="e.g. mail.example.com -- the public hostname Mailu's TLS cert covers.",
        )
        MAILU_IMAP_PORT: int = Field(
            default_factory=lambda: int(os.getenv("MAILU_IMAP_PORT", "993") or "993")
        )
        PHILIPP_USERNAME: str = Field(default_factory=lambda: os.getenv("PHILIPP_USERNAME", ""))
        PHILIPP_PASSWORD: str = Field(default_factory=lambda: os.getenv("PHILIPP_PASSWORD", ""))
        ANN_USERNAME: str = Field(default_factory=lambda: os.getenv("ANN_USERNAME", ""))
        ANN_PASSWORD: str = Field(default_factory=lambda: os.getenv("ANN_PASSWORD", ""))
        REQUEST_TIMEOUT_SECONDS: int = Field(default=30)

    def __init__(self):
        self.valves = self.Valves()

    # ------------------------------------------------------------------
    # internal helpers (not exposed to the model)
    # ------------------------------------------------------------------
    def _creds(self, mailbox: Mailbox) -> tuple[str, str]:
        if mailbox == "philipp":
            return self.valves.PHILIPP_USERNAME, self.valves.PHILIPP_PASSWORD
        return self.valves.ANN_USERNAME, self.valves.ANN_PASSWORD

    def _require_config(self, mailbox: Mailbox, *, need_imap: bool = False) -> Optional[str]:
        missing = []
        if not self.valves.MAILU_HOST:
            missing.append("MAILU_HOST")
        username, password = self._creds(mailbox)
        if not username:
            missing.append(f"{mailbox.upper()}_USERNAME")
        if not password:
            missing.append(f"{mailbox.upper()}_PASSWORD")
        if need_imap and not self.valves.MAILU_IMAP_PORT:
            missing.append("MAILU_IMAP_PORT")
        if missing:
            return f"Missing required config: {', '.join(missing)} (set as env vars / Valves)."
        return None

    def _dav_client(self, mailbox: Mailbox) -> httpx.Client:
        username, password = self._creds(mailbox)
        return httpx.Client(
            base_url=f"https://{self.valves.MAILU_HOST}",
            auth=(username, password),
            timeout=self.valves.REQUEST_TIMEOUT_SECONDS,
        )

    def _calendar_home(self, mailbox: Mailbox) -> str:
        username, _ = self._creds(mailbox)
        return f"/webdav/{username}/"

    def _handle_dav_error(self, resp: httpx.Response) -> dict[str, Any]:
        if resp.status_code == 401:
            return {"error": "Mailu rejected the credentials (401)."}
        if resp.status_code == 403:
            return {"error": "Mailu denied access (403)."}
        if resp.status_code == 404:
            return {"error": "Not found (404) -- check the calendar id/path."}
        return {"error": f"Mailu CalDAV error HTTP {resp.status_code}: {resp.text[:400]}"}

    # -- CalDAV: calendars ----------------------------------------------
    def list_calendars(self, mailbox: Mailbox) -> str:
        """List a mailbox's CalDAV calendar collections (id + display name). Excludes addressbooks, which share the same home collection on this server.

        :param mailbox: Whose calendar -- "philipp" or "ann".
        """
        err = self._require_config(mailbox)
        if err:
            return str({"error": err})
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<D:propfind xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
            "<D:prop><D:displayname/><D:resourcetype/></D:prop>"
            "</D:propfind>"
        ).encode()
        try:
            with self._dav_client(mailbox) as client:
                resp = client.request(
                    "PROPFIND",
                    self._calendar_home(mailbox),
                    headers={"Depth": "1", "Content-Type": "application/xml"},
                    content=body,
                )
        except httpx.RequestError as e:
            return str({"error": f"Could not reach Mailu: {e}"})
        if resp.status_code != 207:
            return str(self._handle_dav_error(resp))
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError as e:
            return str({"error": f"Could not parse Mailu response: {e}"})
        calendars = []
        home_path = self._calendar_home(mailbox)
        for response_el in root.findall(f"{{{_DAV_NS}}}response"):
            href_el = response_el.find(f"{{{_DAV_NS}}}href")
            href = href_el.text if href_el is not None else ""
            if not href or href.rstrip("/") == home_path.rstrip("/"):
                continue  # skip the home collection itself
            resourcetype_el = response_el.find(
                f"{{{_DAV_NS}}}propstat/{{{_DAV_NS}}}prop/{{{_DAV_NS}}}resourcetype"
            )
            if resourcetype_el is None:
                continue
            # Only actual calendars -- the same home collection also lists
            # an addressbook (VADDRESSBOOK) side by side with the calendar
            # (VCALENDAR) on this server; a generic "is it a collection"
            # check (which the former sogo-mcp used against SOGo, whose
            # home was calendar-only) would wrongly include the
            # addressbook here too.
            is_calendar = resourcetype_el.find(f"{{{_CALDAV_NS}}}calendar") is not None
            if not is_calendar:
                continue
            name_el = response_el.find(
                f"{{{_DAV_NS}}}propstat/{{{_DAV_NS}}}prop/{{{_DAV_NS}}}displayname"
            )
            calendar_id = href.rstrip("/").rsplit("/", 1)[-1]
            calendars.append(
                {"id": calendar_id, "display_name": name_el.text if name_el is not None else calendar_id}
            )
        return str({"mailbox": mailbox, "calendars": calendars})

    def _resolve_calendar(self, mailbox: Mailbox, calendar: Optional[str]) -> tuple[Optional[str], Optional[dict]]:
        """Return (calendar_id, None) or (None, error_dict). Auto-detects the
        mailbox's single calendar when `calendar` isn't given -- these
        accounts have exactly one -- erroring with the discovered list if
        there's more than one (or none), rather than guessing."""
        if calendar:
            return calendar, None
        return self._auto_detect_calendar(mailbox)

    def _auto_detect_calendar(self, mailbox: Mailbox) -> tuple[Optional[str], Optional[dict]]:
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<D:propfind xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
            "<D:prop><D:resourcetype/></D:prop>"
            "</D:propfind>"
        ).encode()
        try:
            with self._dav_client(mailbox) as client:
                resp = client.request(
                    "PROPFIND",
                    self._calendar_home(mailbox),
                    headers={"Depth": "1", "Content-Type": "application/xml"},
                    content=body,
                )
        except httpx.RequestError as e:
            return None, {"error": f"Could not reach Mailu: {e}"}
        if resp.status_code != 207:
            return None, self._handle_dav_error(resp)
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError as e:
            return None, {"error": f"Could not parse Mailu response: {e}"}
        home_path = self._calendar_home(mailbox)
        ids = []
        for response_el in root.findall(f"{{{_DAV_NS}}}response"):
            href_el = response_el.find(f"{{{_DAV_NS}}}href")
            href = href_el.text if href_el is not None else ""
            if not href or href.rstrip("/") == home_path.rstrip("/"):
                continue
            resourcetype_el = response_el.find(
                f"{{{_DAV_NS}}}propstat/{{{_DAV_NS}}}prop/{{{_DAV_NS}}}resourcetype"
            )
            if resourcetype_el is None or resourcetype_el.find(f"{{{_CALDAV_NS}}}calendar") is None:
                continue
            ids.append(href.rstrip("/").rsplit("/", 1)[-1])
        if len(ids) == 1:
            return ids[0], None
        if not ids:
            return None, {"error": f"No calendar found for mailbox '{mailbox}'."}
        return None, {
            "error": f"Multiple calendars for mailbox '{mailbox}', pass one explicitly: {ids}"
        }

    # -- CalDAV: events ---------------------------------------------------
    def list_events(
        self,
        mailbox: Mailbox,
        start_date: str,
        end_date: str,
        calendar: Optional[str] = None,
    ) -> str:
        """List events in a date range from one mailbox's calendar.

        :param mailbox: Whose calendar -- "philipp" or "ann".
        :param start_date: Range start, ISO date or datetime (e.g. "2026-08-01" or "2026-08-01T00:00:00Z").
        :param end_date: Range end, exclusive, same format as start_date.
        :param calendar: Calendar id (see list_calendars). Auto-detected if omitted (these accounts have exactly one calendar).

        NOTE on recurring events: the former sogo-mcp integration (same
        CalDAV approach, different server) observed a YEARLY-recurring
        event can be returned even when the queried range doesn't contain
        one of its actual occurrences -- some CalDAV servers match loosely
        on recurring components rather than expanding RRULE for the
        filter; not independently reconfirmed against Mailu's Radicale,
        but RRULE isn't parsed/expanded here regardless (see module
        docstring's scope decisions) -- treat a recurring hit as "this
        series might be relevant", not as proof an occurrence falls in
        the queried window.
        """
        err = self._require_config(mailbox)
        if err:
            return str({"error": err})
        cal_id, cal_err = self._resolve_calendar(mailbox, calendar)
        if cal_err:
            return str(cal_err)
        try:
            start_ical = self._to_ical_utc_stamp(start_date)
            end_ical = self._to_ical_utc_stamp(end_date)
        except ValueError as e:
            return str({"error": f"Invalid date: {e}"})
        path = f"{self._calendar_home(mailbox)}{cal_id}/"
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
            "<D:prop><D:getetag/><C:calendar-data/></D:prop>"
            "<C:filter><C:comp-filter name=\"VCALENDAR\"><C:comp-filter name=\"VEVENT\">"
            f'<C:time-range start="{start_ical}" end="{end_ical}"/>'
            "</C:comp-filter></C:comp-filter></C:filter>"
            "</C:calendar-query>"
        ).encode()
        try:
            with self._dav_client(mailbox) as client:
                resp = client.request(
                    "REPORT",
                    path,
                    headers={"Depth": "1", "Content-Type": "application/xml"},
                    content=body,
                )
        except httpx.RequestError as e:
            return str({"error": f"Could not reach Mailu: {e}"})
        if resp.status_code != 207:
            return str(self._handle_dav_error(resp))
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError as e:
            return str({"error": f"Could not parse Mailu response: {e}"})
        events = []
        for response_el in root.findall(f"{{{_DAV_NS}}}response"):
            data_el = response_el.find(
                f"{{{_DAV_NS}}}propstat/{{{_DAV_NS}}}prop/{{{_CALDAV_NS}}}calendar-data"
            )
            if data_el is None or not data_el.text:
                continue
            try:
                ical = Calendar.from_ical(data_el.text)
            except ValueError:
                continue
            for component in ical.walk("VEVENT"):
                events.append(
                    {
                        "uid": str(component.get("UID", "")),
                        "summary": str(component.get("SUMMARY", "")),
                        "description": str(component.get("DESCRIPTION", "")),
                        "start": self._ical_dt_to_str(component.get("DTSTART")),
                        "end": self._ical_dt_to_str(component.get("DTEND")),
                    }
                )
        return str({"mailbox": mailbox, "calendar": cal_id, "events": events})

    def create_event(
        self,
        mailbox: Mailbox,
        summary: str,
        start: str,
        end: Optional[str] = None,
        all_day: bool = False,
        description: str = "",
        calendar: Optional[str] = None,
    ) -> str:
        """Create a single (non-recurring) calendar event in a mailbox's calendar.

        :param mailbox: Whose calendar -- "philipp" or "ann".
        :param summary: Event title.
        :param start: ISO date ("2026-09-01", requires all_day=True) or datetime with timezone ("2026-09-01T14:00:00+02:00").
        :param end: Same format as start. Defaults to start+1 day for all-day events, or start+1 hour for timed events.
        :param all_day: True for a date-only reminder with no specific time.
        :param calendar: Calendar id (see list_calendars). Auto-detected if omitted (these accounts have exactly one calendar).
        """
        err = self._require_config(mailbox)
        if err:
            return str({"error": err})
        cal_id, cal_err = self._resolve_calendar(mailbox, calendar)
        if cal_err:
            return str(cal_err)
        try:
            dtstart, dtend = self._parse_event_range(start, end, all_day)
        except ValueError as e:
            return str({"error": f"Invalid start/end: {e}"})
        uid = str(uuid.uuid4())
        cal = Calendar()
        cal.add("prodid", "-//hermes-agent//mailu-mcp//DE")
        cal.add("version", "2.0")
        vevent = Event()
        vevent.add("uid", uid)
        vevent.add("summary", summary)
        if description:
            vevent.add("description", description)
        vevent.add("dtstart", dtstart)
        vevent.add("dtend", dtend)
        vevent.add("dtstamp", datetime.now(timezone.utc))
        cal.add_component(vevent)
        path = f"{self._calendar_home(mailbox)}{cal_id}/{uid}.ics"
        try:
            with self._dav_client(mailbox) as client:
                resp = client.request(
                    "PUT",
                    path,
                    headers={"Content-Type": "text/calendar; charset=utf-8"},
                    content=cal.to_ical(),
                )
        except httpx.RequestError as e:
            return str({"error": f"Could not reach Mailu: {e}"})
        if resp.status_code not in (200, 201, 204):
            return str(self._handle_dav_error(resp))
        return str({"status": "created", "uid": uid, "mailbox": mailbox, "calendar": cal_id})

    # -- date helpers ------------------------------------------------------
    def _to_ical_utc_stamp(self, value: str) -> str:
        """Parse an ISO date/datetime string into an iCalendar UTC time-range stamp (YYYYMMDDTHHMMSSZ)."""
        if len(value) == 10:  # date only
            dt = datetime.combine(date.fromisoformat(value), datetime.min.time())
        else:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt.strftime("%Y%m%dT%H%M%SZ")

    def _parse_event_range(self, start: str, end: Optional[str], all_day: bool):
        if all_day:
            start_d = date.fromisoformat(start[:10])
            end_d = date.fromisoformat(end[:10]) if end else start_d + timedelta(days=1)
            return start_d, end_d
        start_dt = datetime.fromisoformat(start)
        end_dt = datetime.fromisoformat(end) if end else start_dt + timedelta(hours=1)
        return start_dt, end_dt

    def _ical_dt_to_str(self, prop) -> str:
        if prop is None:
            return ""
        try:
            return prop.dt.isoformat()
        except AttributeError:
            return str(prop)

    # ==================================================================
    # IMAP (read-only)
    # ==================================================================
    def _imap_connect(self, mailbox: Mailbox):
        username, password = self._creds(mailbox)
        # Mailu/Dovecot's front-facing IMAP port is 993 (implicit TLS) --
        # plain/STARTTLS IMAP is deliberately not exposed at all (see
        # mailu's own helmrelease.yaml comment), unlike the former
        # sogo-mcp's Netcup account which needed a STARTTLS branch.
        conn = imaplib.IMAP4_SSL(self.valves.MAILU_HOST, self.valves.MAILU_IMAP_PORT)
        conn.login(username, password)
        return conn

    def _decode_mime_words(self, raw: Optional[str]) -> str:
        if not raw:
            return ""
        parts = decode_header(raw)
        return "".join(
            (chunk.decode(enc or "utf-8", errors="replace") if isinstance(chunk, bytes) else chunk)
            for chunk, enc in parts
        )

    def list_folders(self, mailbox: Mailbox) -> str:
        """List a mailbox's IMAP folders.

        :param mailbox: Whose mailbox -- "philipp" or "ann".
        """
        err = self._require_config(mailbox, need_imap=True)
        if err:
            return str({"error": err})
        try:
            conn = self._imap_connect(mailbox)
        except (imaplib.IMAP4.error, OSError) as e:
            return str({"error": f"Could not connect/login to IMAP: {e}"})
        try:
            status, folders = conn.list()
            if status != "OK":
                return str({"error": f"IMAP LIST failed: {folders}"})
            names = []
            for raw in folders:
                decoded = raw.decode(errors="replace") if isinstance(raw, bytes) else raw
                m = _IMAP_LIST_RE.match(decoded)
                if not m:
                    continue
                name = m.group("name")
                if name.startswith('"') and name.endswith('"'):
                    name = name[1:-1]
                names.append(name)
            return str({"mailbox": mailbox, "folders": names})
        finally:
            conn.logout()

    def search_messages(
        self,
        mailbox: Mailbox,
        query: str = "",
        folder: str = "INBOX",
        since: Optional[str] = None,
        limit: int = 20,
    ) -> str:
        """Search (or, with an empty query, just list) a mailbox folder. Returns UID + envelope (subject/from/date) for each match, newest first.

        :param mailbox: Whose mailbox -- "philipp" or "ann".
        :param query: Free text, matched against subject and body (IMAP TEXT search). Leave empty to list all messages in the folder (optionally narrowed by `since`).
        :param folder: Mailbox folder name (see list_folders).
        :param since: Only messages received on/after this date (YYYY-MM-DD).
        :param limit: Max results (most recent first).
        """
        err = self._require_config(mailbox, need_imap=True)
        if err:
            return str({"error": err})
        try:
            conn = self._imap_connect(mailbox)
        except (imaplib.IMAP4.error, OSError) as e:
            return str({"error": f"Could not connect/login to IMAP: {e}"})
        try:
            status, _ = conn.select(folder, readonly=True)
            if status != "OK":
                return str({"error": f"Could not select folder '{folder}'."})
            criteria = []
            if query:
                criteria += ["TEXT", f'"{query}"']
            if since:
                try:
                    since_dt = date.fromisoformat(since)
                except ValueError:
                    return str({"error": f"Invalid 'since' date: {since}"})
                criteria += ["SINCE", since_dt.strftime("%d-%b-%Y")]
            status, data = conn.uid("search", None, *(criteria or ["ALL"]))
            if status != "OK":
                return str({"error": f"IMAP SEARCH failed: {data}"})
            uids = data[0].split()
            uids = uids[-limit:][::-1]  # newest first
            results = []
            for uid in uids:
                status, msg_data = conn.uid(
                    "fetch", uid, "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE)])"
                )
                if status != "OK" or not msg_data or msg_data[0] is None:
                    continue
                raw_headers = msg_data[0][1] if isinstance(msg_data[0], tuple) else b""
                msg = message_from_bytes(raw_headers)
                results.append(
                    {
                        "uid": uid.decode(),
                        "subject": self._decode_mime_words(msg.get("Subject")),
                        "from": self._decode_mime_words(msg.get("From")),
                        "date": msg.get("Date", ""),
                    }
                )
            return str({"mailbox": mailbox, "folder": folder, "results": results})
        finally:
            conn.logout()

    def get_message(self, mailbox: Mailbox, uid: str, folder: str = "INBOX") -> str:
        """Get a single message's headers + text body by IMAP UID (see search_messages).

        :param mailbox: Whose mailbox -- "philipp" or "ann".
        """
        err = self._require_config(mailbox, need_imap=True)
        if err:
            return str({"error": err})
        try:
            conn = self._imap_connect(mailbox)
        except (imaplib.IMAP4.error, OSError) as e:
            return str({"error": f"Could not connect/login to IMAP: {e}"})
        try:
            status, _ = conn.select(folder, readonly=True)
            if status != "OK":
                return str({"error": f"Could not select folder '{folder}'."})
            status, msg_data = conn.uid("fetch", uid, "(RFC822)")
            if status != "OK" or not msg_data or msg_data[0] is None:
                return str({"error": f"Message UID {uid} not found in '{folder}'."})
            raw = msg_data[0][1] if isinstance(msg_data[0], tuple) else b""
            msg = message_from_bytes(raw)
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain" and not part.get_filename():
                        charset = part.get_content_charset() or "utf-8"
                        body = part.get_payload(decode=True).decode(charset, errors="replace")
                        break
            else:
                charset = msg.get_content_charset() or "utf-8"
                payload = msg.get_payload(decode=True)
                body = payload.decode(charset, errors="replace") if payload else ""
            try:
                date_str = parsedate_to_datetime(msg.get("Date", "")).isoformat()
            except (TypeError, ValueError):
                date_str = msg.get("Date", "")
            return str(
                {
                    "mailbox": mailbox,
                    "uid": uid,
                    "subject": self._decode_mime_words(msg.get("Subject")),
                    "from": self._decode_mime_words(msg.get("From")),
                    "to": self._decode_mime_words(msg.get("To")),
                    "date": date_str,
                    "body": body[:20000],
                }
            )
        finally:
            conn.logout()
