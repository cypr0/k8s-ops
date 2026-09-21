"""
title: Mailu (Mail, Calendar & Contacts)
author: cypr0
version: 2.0.0
license: MIT
requirements: httpx, icalendar
description: >
  Access to the in-cluster Mailu instance's mailbox and to the CalDAV
  calendar plus CardDAV addressbook its bundled Radicale component
  (`mailu-webdav`, kubernetes/apps/mail/mailu/app/helmrelease.yaml's
  `webdav` section) serves -- for BOTH the owner's and Ann's personal
  accounts. Replaces the former sogo-mcp, which pointed at the same kind of
  account on Netcup's now-retired SOGo hosting (see git history).

  Auth model: HTTP Basic Auth (CalDAV/CardDAV, via Mailu's `front`
  component, which validates it and forwards to Radicale) and plain IMAP
  LOGIN (against Mailu's `dovecot` component via `front`), both using the
  SAME real personal Mailu account per mailbox (PHILIPP_USERNAME/
  PHILIPP_PASSWORD or ANN_USERNAME/ANN_PASSWORD env vars -- see
  externalsecret.yaml, sourced from the "mailu-philipp"/"mailu-anna"
  1Password items). These are the owner's and Ann's own real mailbox/
  calendar accounts, not dedicated narrower-scope service accounts --
  same tradeoff the former sogo-mcp made, since Mailu (like most mail
  servers) has no separate read-only or calendar-only credential
  mechanism, only per-mailbox master passwords.

  ── Scope: mail reads, calendar and contacts write ──────────────────────
  IMAP is READ-ONLY by design (list folders, search, read a message) -- no
  send/delete/flag-mutate methods. This exists so the tool can look
  something up in a mailbox on request, not act as a mail client, and an
  LLM that can send mail from the owner's real address is a different risk
  class from one that can read it. v2.0.0 deliberately did NOT add SMTP.

  CalDAV and CardDAV are read/write: create, update and delete for both
  events and contacts. v1.0.0 had create-only calendar access on the
  argument that fixing a wrongly-created event is a two-second job in any
  calendar client -- that held while hermes-agent's invoice/contract
  reminders were the only caller, but the Open WebUI side of this tool is
  interactive ("verschieb den Termin auf Donnerstag"), where a create-only
  API means the model's only way to "move" an event is to create a second
  one. Recurrence is still out of scope in both directions: RRULE is
  neither generated nor expanded, so a recurring event is reported as the
  series it is, not as occurrences -- see list_events' note.

  Radicale has NO trash. delete_event/delete_contact are immediate and
  irreversible, unlike Nextcloud's or Paperless's deletes.

  Mailu-specific: reached via mail.${SECRET_DOMAIN} (NOT the raw
  mailu-front.mail.svc.cluster.local Service name -- that name isn't
  covered by Mailu's TLS cert SANs, which only list mail.${SECRET_DOMAIN}
  and webmail.${SECRET_DOMAIN}; CoreDNS resolves the former straight to
  mailu-front's ClusterIP for in-cluster clients, same trick
  paperless-ngx's own IMAP integration relies on). CalDAV/CardDAV are
  proxied by `front` at a fixed /webdav/ path (confirmed live via front's
  nginx conf: `/.well-known/caldav` 301-redirects to `/webdav/`, and
  `/webdav` itself does `auth_request /internal/auth/basic` then forwards
  to Radicale with the validated username in an X-Remote-User header --
  Radicale's own auth is `type = http_x_remote_user`, it never sees the
  password directly). Each mailbox's calendar-and-addressbook home is at
  `/webdav/<full-email-address>/`; collections are NOT distinguished by a
  friendly name (Radicale assigns each one a UUID, e.g. `1eec3a2f-.../`)
  and calendars and addressbooks sit side by side in that one home, so
  list_calendars()/list_addressbooks() and the auto-detect defaults
  discover them by CalDAV/CardDAV resourcetype rather than any hardcoded
  id or a generic "is it a collection" check.

  MIRROR NOTICE: as of v2.0.0 this file is duplicated byte-for-byte into
  kubernetes/apps/open-webui/open-webui/app/tools/mailu_full.py, where it
  is an Open WebUI Tool, the same way paperless_full.py/nextcloud_full.py
  are mirrored the other way round. kustomize's configMapGenerator refuses
  file paths that escape its own kustomization directory, so a single
  shared copy isn't possible -- keep both in sync. The Open WebUI side is
  the reason `mailbox` is a Literal rather than free text: it renders as a
  two-value dropdown in the tool-call UI instead of an open field pointing
  at someone's real mailbox.
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
_CARDDAV_NS = "urn:ietf:params:xml:ns:carddav"

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

    def update_event(
        self,
        mailbox: Mailbox,
        uid: str,
        summary: Optional[str] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        all_day: Optional[bool] = None,
        description: Optional[str] = None,
        calendar: Optional[str] = None,
    ) -> str:
        """Update an existing event. Only pass the fields to change; the rest are read back from the stored event and preserved.

        :param mailbox: Whose calendar -- "philipp" or "ann".
        :param uid: The event's uid, from list_events.
        :param start: ISO date (with all_day=True) or datetime with timezone. Pass start to change the time; end defaults the same way create_event does.
        :param all_day: Switch the event between date-only and timed. Requires start in the matching format.
        """
        err = self._require_config(mailbox)
        if err:
            return str({"error": err})
        cal_id, cal_err = self._resolve_calendar(mailbox, calendar)
        if cal_err:
            return str(cal_err)
        path = f"{self._calendar_home(mailbox)}{cal_id}/{uid}.ics"
        try:
            with self._dav_client(mailbox) as client:
                existing = client.get(path)
                if existing.status_code != 200:
                    return str(self._handle_dav_error(existing))
                try:
                    cal = Calendar.from_ical(existing.text)
                except ValueError as e:
                    return str({"error": f"Could not parse the stored event: {e}"})
                vevents = list(cal.walk("VEVENT"))
                if not vevents:
                    return str({"error": f"No VEVENT in the stored resource for uid '{uid}'."})
                vevent = vevents[0]
                if summary is not None:
                    vevent["SUMMARY"] = summary
                if description is not None:
                    vevent["DESCRIPTION"] = description
                if start is not None:
                    # all_day is only decidable together with start: a
                    # date-only DTSTART and a timestamped one are different
                    # value types, so changing one without the other would
                    # write an event the server may reject or a client may
                    # render an hour wide.
                    is_all_day = (
                        all_day
                        if all_day is not None
                        else not hasattr(vevent.get("DTSTART").dt, "hour")
                    )
                    try:
                        dtstart, dtend = self._parse_event_range(start, end, is_all_day)
                    except ValueError as e:
                        return str({"error": f"Invalid start/end: {e}"})
                    del vevent["DTSTART"]
                    del vevent["DTEND"]
                    vevent.add("dtstart", dtstart)
                    vevent.add("dtend", dtend)
                if "DTSTAMP" in vevent:
                    del vevent["DTSTAMP"]
                vevent.add("dtstamp", datetime.now(timezone.utc))
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
        return str({"status": "updated", "uid": uid, "mailbox": mailbox, "calendar": cal_id})

    def delete_event(self, mailbox: Mailbox, uid: str, calendar: Optional[str] = None) -> str:
        """PERMANENTLY delete a calendar event. Radicale has no trash -- this is irreversible.

        :param mailbox: Whose calendar -- "philipp" or "ann".
        :param uid: The event's uid, from list_events.
        """
        err = self._require_config(mailbox)
        if err:
            return str({"error": err})
        cal_id, cal_err = self._resolve_calendar(mailbox, calendar)
        if cal_err:
            return str(cal_err)
        path = f"{self._calendar_home(mailbox)}{cal_id}/{uid}.ics"
        try:
            with self._dav_client(mailbox) as client:
                resp = client.request("DELETE", path)
        except httpx.RequestError as e:
            return str({"error": f"Could not reach Mailu: {e}"})
        if resp.status_code == 404:
            return str({"error": f"No event with uid '{uid}' in calendar '{cal_id}'."})
        if resp.status_code not in (200, 204):
            return str(self._handle_dav_error(resp))
        return str({"status": "deleted", "uid": uid, "mailbox": mailbox, "calendar": cal_id})

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
    # CardDAV (contacts) -- same Radicale collection home as the calendar,
    # distinguished by resourcetype (see list_calendars' comment on why a
    # generic "is it a collection" check is wrong on this server).
    #
    # vCards are hand-rolled rather than pulled in from a library: the only
    # extra dependency that would help (vobject) is unmaintained, and the
    # subset actually needed here -- FN/N/EMAIL/TEL/ORG/BDAY/NOTE with RFC
    # 6350 escaping and RFC 5322-style line folding -- is small enough to
    # own. icalendar, already a dependency, does iCalendar only.
    # ==================================================================
    def _addressbook_home(self, mailbox: Mailbox) -> str:
        # Identical to the calendar home: Radicale puts calendars and
        # addressbooks side by side under one per-user collection.
        return self._calendar_home(mailbox)

    def _discover_collections(self, mailbox: Mailbox, ns: str, tag: str):
        """PROPFIND the mailbox's collection home and return the ids of every
        child whose resourcetype contains {ns}tag. Returns (ids, error)."""
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<D:propfind xmlns:D="DAV:">'
            "<D:prop><D:displayname/><D:resourcetype/></D:prop>"
            "</D:propfind>"
        ).encode()
        try:
            with self._dav_client(mailbox) as client:
                resp = client.request(
                    "PROPFIND",
                    self._addressbook_home(mailbox),
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
        home_path = self._addressbook_home(mailbox)
        found = []
        for response_el in root.findall(f"{{{_DAV_NS}}}response"):
            href_el = response_el.find(f"{{{_DAV_NS}}}href")
            href = href_el.text if href_el is not None else ""
            if not href or href.rstrip("/") == home_path.rstrip("/"):
                continue
            rt_el = response_el.find(
                f"{{{_DAV_NS}}}propstat/{{{_DAV_NS}}}prop/{{{_DAV_NS}}}resourcetype"
            )
            if rt_el is None or rt_el.find(f"{{{ns}}}{tag}") is None:
                continue
            name_el = response_el.find(
                f"{{{_DAV_NS}}}propstat/{{{_DAV_NS}}}prop/{{{_DAV_NS}}}displayname"
            )
            coll_id = href.rstrip("/").rsplit("/", 1)[-1]
            found.append(
                {"id": coll_id, "display_name": name_el.text if name_el is not None else coll_id}
            )
        return found, None

    def _resolve_addressbook(self, mailbox: Mailbox, addressbook: Optional[str]):
        """Return (addressbook_id, None) or (None, error_dict). Auto-detects
        the mailbox's single addressbook when not given -- same contract as
        _resolve_calendar."""
        if addressbook:
            return addressbook, None
        found, err = self._discover_collections(mailbox, _CARDDAV_NS, "addressbook")
        if err:
            return None, err
        ids = [c["id"] for c in found]
        if len(ids) == 1:
            return ids[0], None
        if not ids:
            return None, {"error": f"No addressbook found for mailbox '{mailbox}'."}
        return None, {
            "error": f"Multiple addressbooks for mailbox '{mailbox}', pass one explicitly: {ids}"
        }

    @staticmethod
    def _vcard_escape(value: str) -> str:
        return (
            value.replace("\\", "\\\\")
            .replace(";", "\\;")
            .replace(",", "\\,")
            .replace("\r\n", "\\n")
            .replace("\n", "\\n")
        )

    @staticmethod
    def _vcard_unescape(value: str) -> str:
        out, i = [], 0
        while i < len(value):
            ch = value[i]
            if ch == "\\" and i + 1 < len(value):
                nxt = value[i + 1]
                out.append("\n" if nxt in ("n", "N") else nxt)
                i += 2
                continue
            out.append(ch)
            i += 1
        return "".join(out)

    @staticmethod
    def _vcard_fold(line: str) -> str:
        """RFC 5322/6350 folding: continuation lines start with one space."""
        if len(line) <= 73:
            return line
        chunks = [line[:73]]
        rest = line[73:]
        while rest:
            chunks.append(" " + rest[:72])
            rest = rest[72:]
        return "\r\n".join(chunks)

    def _parse_vcard(self, text: str) -> dict[str, Any]:
        # Unfold first: a line beginning with space or tab continues the
        # previous one, and splitting before unfolding silently truncates
        # every long value (notably NOTE and multi-part ADR).
        unfolded: list[str] = []
        for raw in text.replace("\r\n", "\n").split("\n"):
            if raw[:1] in (" ", "\t") and unfolded:
                unfolded[-1] += raw[1:]
            else:
                unfolded.append(raw)
        card: dict[str, Any] = {"emails": [], "phones": []}
        for line in unfolded:
            if not line or ":" not in line:
                continue
            head, _, value = line.partition(":")
            name = head.split(";", 1)[0].upper()
            value = self._vcard_unescape(value).strip()
            if not value:
                continue
            if name == "UID":
                card["uid"] = value
            elif name == "FN":
                card["full_name"] = value
            elif name == "N":
                parts = value.split(";")
                card["last_name"] = parts[0] if len(parts) > 0 else ""
                card["first_name"] = parts[1] if len(parts) > 1 else ""
            elif name == "EMAIL":
                card["emails"].append(value)
            elif name == "TEL":
                card["phones"].append(value)
            elif name == "ORG":
                card["organization"] = value.split(";")[0]
            elif name == "TITLE":
                card["title"] = value
            elif name == "BDAY":
                card["birthday"] = value
            elif name == "NOTE":
                card["note"] = value
        return card

    def _build_vcard(
        self,
        uid: str,
        full_name: str,
        first_name: str = "",
        last_name: str = "",
        emails: Optional[list[str]] = None,
        phones: Optional[list[str]] = None,
        organization: str = "",
        title: str = "",
        birthday: str = "",
        note: str = "",
    ) -> bytes:
        e = self._vcard_escape
        # vCard 3.0, not 4.0: Radicale stores either, but 3.0 is what every
        # mainstream client (iOS/macOS Contacts, Thunderbird, DAVx5) reads
        # without quirks, and nothing here needs a 4.0-only property.
        lines = ["BEGIN:VCARD", "VERSION:3.0", f"UID:{e(uid)}", f"FN:{e(full_name)}"]
        lines.append(f"N:{e(last_name)};{e(first_name)};;;")
        for addr in emails or []:
            lines.append(f"EMAIL;TYPE=INTERNET:{e(addr)}")
        for phone in phones or []:
            lines.append(f"TEL;TYPE=CELL:{e(phone)}")
        if organization:
            lines.append(f"ORG:{e(organization)}")
        if title:
            lines.append(f"TITLE:{e(title)}")
        if birthday:
            lines.append(f"BDAY:{e(birthday)}")
        if note:
            lines.append(f"NOTE:{e(note)}")
        lines.append("END:VCARD")
        return ("\r\n".join(self._vcard_fold(line) for line in lines) + "\r\n").encode("utf-8")

    def _fetch_vcards(self, mailbox: Mailbox, book_id: str):
        """REPORT addressbook-query with an empty filter = every card in the
        collection. RFC 6352 requires the <filter> element to be present
        even when it selects everything, so it is sent empty rather than
        omitted."""
        path = f"{self._addressbook_home(mailbox)}{book_id}/"
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<C:addressbook-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:carddav">'
            "<D:prop><D:getetag/><C:address-data/></D:prop>"
            "<C:filter/>"
            "</C:addressbook-query>"
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
            return None, {"error": f"Could not reach Mailu: {e}"}
        if resp.status_code != 207:
            return None, self._handle_dav_error(resp)
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError as e:
            return None, {"error": f"Could not parse Mailu response: {e}"}
        cards = []
        for response_el in root.findall(f"{{{_DAV_NS}}}response"):
            href_el = response_el.find(f"{{{_DAV_NS}}}href")
            data_el = response_el.find(
                f"{{{_DAV_NS}}}propstat/{{{_DAV_NS}}}prop/{{{_CARDDAV_NS}}}address-data"
            )
            if data_el is None or not data_el.text:
                continue
            card = self._parse_vcard(data_el.text)
            if href_el is not None and href_el.text:
                card["href"] = href_el.text
            cards.append(card)
        return cards, None

    def list_addressbooks(self, mailbox: Mailbox) -> str:
        """List a mailbox's CardDAV addressbooks (id + display name). Excludes calendars, which share the same collection home.

        :param mailbox: Whose contacts -- "philipp" or "ann".
        """
        err = self._require_config(mailbox)
        if err:
            return str({"error": err})
        found, coll_err = self._discover_collections(mailbox, _CARDDAV_NS, "addressbook")
        if coll_err:
            return str(coll_err)
        return str({"mailbox": mailbox, "addressbooks": found})

    def list_contacts(
        self, mailbox: Mailbox, search: Optional[str] = None, addressbook: Optional[str] = None
    ) -> str:
        """List or search contacts in a mailbox's addressbook.

        :param mailbox: Whose contacts -- "philipp" or "ann".
        :param search: Case-insensitive substring matched against name, email, phone and organization. Omit to list everything.
        :param addressbook: Addressbook id from list_addressbooks. Auto-detected if omitted.
        """
        err = self._require_config(mailbox)
        if err:
            return str({"error": err})
        book_id, book_err = self._resolve_addressbook(mailbox, addressbook)
        if book_err:
            return str(book_err)
        cards, fetch_err = self._fetch_vcards(mailbox, book_id)
        if fetch_err:
            return str(fetch_err)
        if search:
            needle = search.lower()

            def matches(card: dict[str, Any]) -> bool:
                haystack = " ".join(
                    [
                        str(card.get("full_name", "")),
                        str(card.get("first_name", "")),
                        str(card.get("last_name", "")),
                        str(card.get("organization", "")),
                        " ".join(card.get("emails", [])),
                        " ".join(card.get("phones", [])),
                    ]
                ).lower()
                return needle in haystack

            cards = [c for c in cards if matches(c)]
        return str({"mailbox": mailbox, "addressbook": book_id, "contacts": cards})

    def create_contact(
        self,
        mailbox: Mailbox,
        full_name: str,
        first_name: str = "",
        last_name: str = "",
        emails: Optional[list[str]] = None,
        phones: Optional[list[str]] = None,
        organization: str = "",
        title: str = "",
        birthday: str = "",
        note: str = "",
        addressbook: Optional[str] = None,
    ) -> str:
        """Create a contact in a mailbox's addressbook. Check list_contacts first -- a duplicate is harder to notice than a missing entry.

        :param mailbox: Whose contacts -- "philipp" or "ann".
        :param full_name: Display name, e.g. "Erika Mustermann".
        :param birthday: ISO date, "YYYY-MM-DD".
        :param addressbook: Addressbook id from list_addressbooks. Auto-detected if omitted.
        """
        err = self._require_config(mailbox)
        if err:
            return str({"error": err})
        book_id, book_err = self._resolve_addressbook(mailbox, addressbook)
        if book_err:
            return str(book_err)
        uid = str(uuid.uuid4())
        vcard = self._build_vcard(
            uid=uid,
            full_name=full_name,
            first_name=first_name,
            last_name=last_name,
            emails=emails,
            phones=phones,
            organization=organization,
            title=title,
            birthday=birthday,
            note=note,
        )
        path = f"{self._addressbook_home(mailbox)}{book_id}/{uid}.vcf"
        try:
            with self._dav_client(mailbox) as client:
                resp = client.request(
                    "PUT",
                    path,
                    headers={
                        "Content-Type": "text/vcard; charset=utf-8",
                        # Fail instead of silently overwriting if that
                        # resource name is somehow already taken.
                        "If-None-Match": "*",
                    },
                    content=vcard,
                )
        except httpx.RequestError as e:
            return str({"error": f"Could not reach Mailu: {e}"})
        if resp.status_code not in (200, 201, 204):
            return str(self._handle_dav_error(resp))
        return str({"status": "created", "uid": uid, "mailbox": mailbox, "addressbook": book_id})

    def update_contact(
        self,
        mailbox: Mailbox,
        uid: str,
        full_name: Optional[str] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        emails: Optional[list[str]] = None,
        phones: Optional[list[str]] = None,
        organization: Optional[str] = None,
        title: Optional[str] = None,
        birthday: Optional[str] = None,
        note: Optional[str] = None,
        addressbook: Optional[str] = None,
    ) -> str:
        """Update a contact. Only pass the fields to change; the rest are read back from the stored card and preserved.

        :param mailbox: Whose contacts -- "philipp" or "ann".
        :param uid: The contact's uid, from list_contacts.
        :param emails: FULL replacement of the email list, not additive.
        :param phones: FULL replacement of the phone list, not additive.
        """
        err = self._require_config(mailbox)
        if err:
            return str({"error": err})
        book_id, book_err = self._resolve_addressbook(mailbox, addressbook)
        if book_err:
            return str(book_err)
        path = f"{self._addressbook_home(mailbox)}{book_id}/{uid}.vcf"
        try:
            with self._dav_client(mailbox) as client:
                existing = client.get(path)
                if existing.status_code != 200:
                    return str(self._handle_dav_error(existing))
                current = self._parse_vcard(existing.text)
                merged = self._build_vcard(
                    uid=uid,
                    full_name=full_name if full_name is not None else current.get("full_name", ""),
                    first_name=(
                        first_name if first_name is not None else current.get("first_name", "")
                    ),
                    last_name=last_name if last_name is not None else current.get("last_name", ""),
                    emails=emails if emails is not None else current.get("emails", []),
                    phones=phones if phones is not None else current.get("phones", []),
                    organization=(
                        organization
                        if organization is not None
                        else current.get("organization", "")
                    ),
                    title=title if title is not None else current.get("title", ""),
                    birthday=birthday if birthday is not None else current.get("birthday", ""),
                    note=note if note is not None else current.get("note", ""),
                )
                resp = client.request(
                    "PUT",
                    path,
                    headers={"Content-Type": "text/vcard; charset=utf-8"},
                    content=merged,
                )
        except httpx.RequestError as e:
            return str({"error": f"Could not reach Mailu: {e}"})
        if resp.status_code not in (200, 201, 204):
            return str(self._handle_dav_error(resp))
        return str({"status": "updated", "uid": uid, "mailbox": mailbox, "addressbook": book_id})

    def delete_contact(
        self, mailbox: Mailbox, uid: str, addressbook: Optional[str] = None
    ) -> str:
        """PERMANENTLY delete a contact. Radicale has no trash -- this is irreversible.

        :param mailbox: Whose contacts -- "philipp" or "ann".
        :param uid: The contact's uid, from list_contacts.
        """
        err = self._require_config(mailbox)
        if err:
            return str({"error": err})
        book_id, book_err = self._resolve_addressbook(mailbox, addressbook)
        if book_err:
            return str(book_err)
        path = f"{self._addressbook_home(mailbox)}{book_id}/{uid}.vcf"
        try:
            with self._dav_client(mailbox) as client:
                resp = client.request("DELETE", path)
        except httpx.RequestError as e:
            return str({"error": f"Could not reach Mailu: {e}"})
        if resp.status_code not in (200, 204, 404):
            return str(self._handle_dav_error(resp))
        if resp.status_code == 404:
            return str({"error": f"No contact with uid '{uid}' in addressbook '{book_id}'."})
        return str({"status": "deleted", "uid": uid, "mailbox": mailbox, "addressbook": book_id})

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
