# MIRROR NOTICE: this file has no Open WebUI counterpart (unlike
# paperless_full.py/nextcloud_full.py) -- it was written directly for
# hermes-agent's MCP use via owui_tool_mcp_bridge.py, which only requires
# a plain `Tools` class with public methods, no Open-WebUI-specific
# runtime behavior. Kept in the same shape as the other two anyway
# (Valves, docstring-as-tool-description, `str(...)`-returning methods)
# so the pattern stays consistent across all three MCP servers in this
# namespace.
"""
title: SOGo (Calendar + Mail)
author: cypr0
version: 1.0.0
license: MIT
requirements: httpx, icalendar
description: >
  Read/write access to a Netcup-hosted SOGo groupware instance's CalDAV
  calendar, and read-only access to its IMAP mailbox. Built for hermes-
  agent's document-pipeline / contract-monitoring use cases: creating
  calendar reminders for invoice due dates or contract cancellation
  deadlines, and letting the agent search/read the owner's mailbox
  directly when asked.

  Auth model: HTTP Basic Auth (CalDAV) and plain IMAP LOGIN, both using
  the SAME real personal SOGo/mailbox account (SOGO_USERNAME/
  SOGO_PASSWORD env vars -- see externalsecret.yaml, sourced from the
  "sogo" 1Password item). This is the owner's own real mailbox and
  calendar account, not a dedicated narrower-scope service account --
  Netcup's SOGo hosting has no mechanism for a read-only or calendar-
  only credential, so this tool wields full access to both.

  Scope decisions: CalDAV (RFC 4791) covers list/create of events only --
  no recurrence rules, no attendee invites, no delete/update. The
  intended use is one-shot reminder events; editing or removing a
  wrongly-created one is a two-second job in any real calendar client,
  and a delete_event tool against someone's actual personal calendar
  isn't worth the risk for what this integration is for. IMAP is READ-
  ONLY by design (list folders, search, read a message) -- no send/
  delete/flag-mutate methods: this exists so the agent can look
  something up in the mailbox on request, not act as a mail client.

  Netcup-specific: the CalDAV/CardDAV base path is fixed at "/SOGo/dav"
  under the account's own *.netcup-mail.de subdomain (see
  https://www.netcup.com/de/helpcenter/dokumentation/sogo/
  sogo-groupware-thunderbird); IMAP host/port are account-specific and
  not hardcoded here -- both SOGO_BASE_URL and SOGO_IMAP_HOST/PORT are
  required config, not defaulted to a guessed Netcup hostname.
"""

from __future__ import annotations

import imaplib
import logging
import os
import uuid
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email import message_from_bytes
from email.header import decode_header
from email.utils import parsedate_to_datetime
from typing import Any, Optional

import httpx
from icalendar import Calendar, Event
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_DAV_NS = "DAV:"
_CALDAV_NS = "urn:ietf:params:xml:ns:caldav"


class Tools:
    class Valves(BaseModel):
        """Admin-configured. All fields are required -- there is no safe
        guessable default for someone else's mail/calendar account."""

        SOGO_BASE_URL: str = Field(
            default_factory=lambda: os.getenv("SOGO_BASE_URL", ""),
            description="e.g. https://<subdomain>.netcup-mail.de (no trailing slash, no /SOGo suffix).",
        )
        SOGO_USERNAME: str = Field(default_factory=lambda: os.getenv("SOGO_USERNAME", ""))
        SOGO_PASSWORD: str = Field(default_factory=lambda: os.getenv("SOGO_PASSWORD", ""))
        SOGO_IMAP_HOST: str = Field(default_factory=lambda: os.getenv("SOGO_IMAP_HOST", ""))
        SOGO_IMAP_PORT: int = Field(
            default_factory=lambda: int(os.getenv("SOGO_IMAP_PORT", "993") or "993")
        )
        REQUEST_TIMEOUT_SECONDS: int = Field(default=30)
        DEFAULT_CALENDAR: str = Field(
            default="personal",
            description="SOGo's default personal-calendar collection name.",
        )

    def __init__(self):
        self.valves = self.Valves()

    # ------------------------------------------------------------------
    # internal helpers (not exposed to the model)
    # ------------------------------------------------------------------
    def _require_config(self, *names: str) -> Optional[str]:
        missing = [n for n in names if not getattr(self.valves, n, "")]
        if missing:
            return f"Missing required config: {', '.join(missing)} (set as env vars / Valves)."
        return None

    def _base_url(self) -> str:
        # Netcup's own docs show the URL WITH the "/SOGo" suffix already
        # (https://.../SOGo/dav) -- accept that form too rather than
        # relying on the config having exactly one specific shape, since
        # _calendar_home() always appends "/SOGo/dav/..." itself.
        url = self.valves.SOGO_BASE_URL.rstrip("/")
        if url.lower().endswith("/sogo"):
            url = url[: -len("/sogo")]
        return url

    def _dav_client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self._base_url(),
            auth=(self.valves.SOGO_USERNAME, self.valves.SOGO_PASSWORD),
            timeout=self.valves.REQUEST_TIMEOUT_SECONDS,
        )

    def _calendar_home(self) -> str:
        return f"/SOGo/dav/{self.valves.SOGO_USERNAME}/Calendar/"

    def _handle_dav_error(self, resp: httpx.Response) -> dict[str, Any]:
        if resp.status_code == 401:
            return {"error": "SOGo rejected the credentials (401)."}
        if resp.status_code == 403:
            return {"error": "SOGo denied access (403)."}
        if resp.status_code == 404:
            return {"error": "Not found (404) -- check the calendar name/path."}
        return {"error": f"SOGo CalDAV error HTTP {resp.status_code}: {resp.text[:400]}"}

    # -- CalDAV: calendars ----------------------------------------------
    def list_calendars(self) -> str:
        """List the account's CalDAV calendar collections (id + display name)."""
        err = self._require_config("SOGO_BASE_URL", "SOGO_USERNAME", "SOGO_PASSWORD")
        if err:
            return str({"error": err})
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<D:propfind xmlns:D="DAV:">'
            "<D:prop><D:displayname/><D:resourcetype/></D:prop>"
            "</D:propfind>"
        ).encode()
        try:
            with self._dav_client() as client:
                resp = client.request(
                    "PROPFIND",
                    self._calendar_home(),
                    headers={"Depth": "1", "Content-Type": "application/xml"},
                    content=body,
                )
        except httpx.RequestError as e:
            return str({"error": f"Could not reach SOGo: {e}"})
        if resp.status_code != 207:
            return str(self._handle_dav_error(resp))
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError as e:
            return str({"error": f"Could not parse SOGo response: {e}"})
        calendars = []
        home_path = self._calendar_home()
        for response_el in root.findall(f"{{{_DAV_NS}}}response"):
            href_el = response_el.find(f"{{{_DAV_NS}}}href")
            href = href_el.text if href_el is not None else ""
            if not href or href.rstrip("/") == home_path.rstrip("/"):
                continue  # skip the home collection itself
            is_collection = response_el.find(
                f"{{{_DAV_NS}}}propstat/{{{_DAV_NS}}}prop/{{{_DAV_NS}}}resourcetype/{{{_DAV_NS}}}collection"
            )
            if is_collection is None:
                continue
            name_el = response_el.find(
                f"{{{_DAV_NS}}}propstat/{{{_DAV_NS}}}prop/{{{_DAV_NS}}}displayname"
            )
            calendar_id = href.rstrip("/").rsplit("/", 1)[-1]
            calendars.append(
                {"id": calendar_id, "display_name": name_el.text if name_el is not None else calendar_id}
            )
        return str({"calendars": calendars})

    # -- CalDAV: events ---------------------------------------------------
    def list_events(
        self,
        start_date: str,
        end_date: str,
        calendar: Optional[str] = None,
    ) -> str:
        """List events in a date range from one calendar.

        :param start_date: Range start, ISO date or datetime (e.g. "2026-08-01" or "2026-08-01T00:00:00Z").
        :param end_date: Range end, exclusive, same format as start_date.
        :param calendar: Calendar id (see list_calendars). Defaults to the personal calendar (Valves.DEFAULT_CALENDAR).

        NOTE on recurring events: confirmed live against this SOGo server --
        a YEARLY-recurring event can be returned even when the queried range
        doesn't contain one of its actual occurrences (the server appears to
        match loosely on recurring components rather than expanding RRULE
        for the filter). The returned start/end are always the ORIGINAL
        occurrence's dates, not a specific occurrence in range. RRULE isn't
        parsed/expanded here at all (see module docstring's scope decisions)
        -- treat a recurring hit as "this series might be relevant", not as
        proof an occurrence falls in the queried window.
        """
        err = self._require_config("SOGO_BASE_URL", "SOGO_USERNAME", "SOGO_PASSWORD")
        if err:
            return str({"error": err})
        try:
            start_ical = self._to_ical_utc_stamp(start_date)
            end_ical = self._to_ical_utc_stamp(end_date)
        except ValueError as e:
            return str({"error": f"Invalid date: {e}"})
        cal_id = calendar or self.valves.DEFAULT_CALENDAR
        path = f"{self._calendar_home()}{cal_id}/"
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
            with self._dav_client() as client:
                resp = client.request(
                    "REPORT",
                    path,
                    headers={"Depth": "1", "Content-Type": "application/xml"},
                    content=body,
                )
        except httpx.RequestError as e:
            return str({"error": f"Could not reach SOGo: {e}"})
        if resp.status_code != 207:
            return str(self._handle_dav_error(resp))
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError as e:
            return str({"error": f"Could not parse SOGo response: {e}"})
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
        return str({"calendar": cal_id, "events": events})

    def create_event(
        self,
        summary: str,
        start: str,
        end: Optional[str] = None,
        all_day: bool = False,
        description: str = "",
        calendar: Optional[str] = None,
    ) -> str:
        """Create a single (non-recurring) calendar event.

        :param summary: Event title.
        :param start: ISO date ("2026-09-01", requires all_day=True) or datetime with timezone ("2026-09-01T14:00:00+02:00").
        :param end: Same format as start. Defaults to start+1 day for all-day events, or start+1 hour for timed events.
        :param all_day: True for a date-only reminder with no specific time.
        :param calendar: Calendar id (see list_calendars). Defaults to the personal calendar (Valves.DEFAULT_CALENDAR).
        """
        err = self._require_config("SOGO_BASE_URL", "SOGO_USERNAME", "SOGO_PASSWORD")
        if err:
            return str({"error": err})
        try:
            dtstart, dtend = self._parse_event_range(start, end, all_day)
        except ValueError as e:
            return str({"error": f"Invalid start/end: {e}"})
        cal_id = calendar or self.valves.DEFAULT_CALENDAR
        uid = str(uuid.uuid4())
        cal = Calendar()
        cal.add("prodid", "-//hermes-agent//sogo-mcp//DE")
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
        path = f"{self._calendar_home()}{cal_id}/{uid}.ics"
        try:
            with self._dav_client() as client:
                resp = client.request(
                    "PUT",
                    path,
                    headers={"Content-Type": "text/calendar; charset=utf-8"},
                    content=cal.to_ical(),
                )
        except httpx.RequestError as e:
            return str({"error": f"Could not reach SOGo: {e}"})
        if resp.status_code not in (200, 201, 204):
            return str(self._handle_dav_error(resp))
        return str({"status": "created", "uid": uid, "calendar": cal_id})

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
    def _imap_connect(self):
        conn = imaplib.IMAP4_SSL(self.valves.SOGO_IMAP_HOST, self.valves.SOGO_IMAP_PORT)
        conn.login(self.valves.SOGO_USERNAME, self.valves.SOGO_PASSWORD)
        return conn

    def _decode_mime_words(self, raw: Optional[str]) -> str:
        if not raw:
            return ""
        parts = decode_header(raw)
        return "".join(
            (chunk.decode(enc or "utf-8", errors="replace") if isinstance(chunk, bytes) else chunk)
            for chunk, enc in parts
        )

    def list_folders(self) -> str:
        """List IMAP mailbox folders."""
        err = self._require_config("SOGO_IMAP_HOST", "SOGO_USERNAME", "SOGO_PASSWORD")
        if err:
            return str({"error": err})
        try:
            conn = self._imap_connect()
        except (imaplib.IMAP4.error, OSError) as e:
            return str({"error": f"Could not connect/login to IMAP: {e}"})
        try:
            status, folders = conn.list()
            if status != "OK":
                return str({"error": f"IMAP LIST failed: {folders}"})
            names = []
            for raw in folders:
                # RFC 3501 mailbox-list: (flags) "delimiter" "name"
                decoded = raw.decode(errors="replace") if isinstance(raw, bytes) else raw
                name = decoded.rsplit('"', 2)[-2] if '"' in decoded else decoded.split()[-1]
                names.append(name)
            return str({"folders": names})
        finally:
            conn.logout()

    def search_messages(
        self,
        query: str,
        folder: str = "INBOX",
        since: Optional[str] = None,
        limit: int = 20,
    ) -> str:
        """Search a mailbox folder. Returns UID + envelope (subject/from/date) for each match, newest first.

        :param query: Free text, matched against subject and body (IMAP TEXT search).
        :param folder: Mailbox folder name (see list_folders).
        :param since: Only messages received on/after this date (YYYY-MM-DD).
        :param limit: Max results (most recent first).
        """
        err = self._require_config("SOGO_IMAP_HOST", "SOGO_USERNAME", "SOGO_PASSWORD")
        if err:
            return str({"error": err})
        try:
            conn = self._imap_connect()
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
            return str({"folder": folder, "results": results})
        finally:
            conn.logout()

    def get_message(self, uid: str, folder: str = "INBOX") -> str:
        """Get a single message's headers + text body by IMAP UID (see search_messages)."""
        err = self._require_config("SOGO_IMAP_HOST", "SOGO_USERNAME", "SOGO_PASSWORD")
        if err:
            return str({"error": err})
        try:
            conn = self._imap_connect()
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
