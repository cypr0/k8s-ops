"""
title: Portfolio (Depot watchlist)
author: cypr0
version: 1.0.0
license: MIT
requirements: psycopg
description: >
  The "portfolio" database (CNPG cluster postgres, db portfolio) for
  hermes-agent's "depot" profile: read holdings and price history, add a
  watch candidate, retire a watch candidate.

  Replaces the terminal + psycopg2 access the old portfolio cron prompt
  used. The depot profile has web search (to research candidates) and
  therefore deliberately NO terminal: web content is attacker-controlled,
  and a shell in the shared hermes pod could read every other profile's
  .env. This server is the only write path, and it is narrow by
  construction:
    * add_watch_candidate() can only INSERT a row with status 'watching'
      and NULL quantity / entry_price_eur -- never a real position (the
      column defaults to 'owned', so status is always set explicitly);
    * retire_watch_candidate() can only flip active=false on a row with
      status 'watching', never on a real position;
    * no DELETE, no free SQL.
  Real positions (quantity/entry price) are maintained by hand.
"""

from __future__ import annotations

import os
import re
from typing import Any, Optional

import psycopg
from pydantic import BaseModel, Field

_ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")


class Tools:
    class Valves(BaseModel):
        PGHOST: str = Field(default_factory=lambda: os.getenv("PGHOST", "postgres-rw.database.svc.cluster.local"))
        PGDATABASE: str = Field(default_factory=lambda: os.getenv("PGDATABASE", "portfolio"))
        PGUSER: str = Field(default_factory=lambda: os.getenv("PGUSER", "portfoliousr"))
        PGPASSWORD: str = Field(default_factory=lambda: os.getenv("PGPASSWORD", ""))

    def __init__(self):
        self.valves = self.Valves()

    def _conn(self):
        v = self.valves
        return psycopg.connect(host=v.PGHOST, dbname=v.PGDATABASE, user=v.PGUSER, password=v.PGPASSWORD, connect_timeout=10)

    @staticmethod
    def _rows(cur) -> list[dict]:
        cols = [c.name for c in cur.description]
        out = []
        for r in cur.fetchall():
            out.append({k: (float(x) if hasattr(x, "is_finite") else (x.isoformat() if hasattr(x, "isoformat") else x)) for k, x in zip(cols, r)})
        return out

    def list_holdings(self, include_inactive: bool = False) -> dict:
        """All tracked titles: real positions (status owned) and watch candidates (status watching), with last price in EUR and gain vs. entry."""
        sql = """SELECT isin, wkn, name, asset_class, ticker, quantity, entry_price_eur,
                        last_price_eur, last_price_at, active, status, note,
                        CASE WHEN entry_price_eur > 0 AND last_price_eur IS NOT NULL
                             THEN round((last_price_eur / entry_price_eur - 1) * 100, 1) END AS change_pct
                 FROM holdings {where} ORDER BY status, name"""
        with self._conn() as c, c.cursor() as cur:
            cur.execute(sql.format(where="" if include_inactive else "WHERE active"))
            rows = self._rows(cur)
        return {"count": len(rows), "holdings": rows}

    def price_history(self, isin: str, days: int = 30) -> dict:
        """Daily EUR price history of one title for the last `days` days (max 365)."""
        if not _ISIN.match(isin or ""):
            return {"error": "invalid ISIN"}
        days = max(1, min(int(days), 365))
        with self._conn() as c, c.cursor() as cur:
            cur.execute(
                "SELECT fetched_at::date AS day, max(price_eur) AS price_eur FROM price_history "
                "WHERE isin = %s AND fetched_at > now() - make_interval(days => %s) GROUP BY 1 ORDER BY 1",
                (isin, days),
            )
            return {"isin": isin, "history": self._rows(cur)}

    def add_watch_candidate(self, isin: str, name: str, ticker: str, asset_class: str = "", wkn: str = "", reason: str = "") -> dict:
        """Add ONE new watch candidate (no quantity, no entry price). isin: 12-char ISIN. ticker: Yahoo Finance ticker on the listing whose currency you want. reason: one sentence why. Refuses if the ISIN already exists."""
        isin = (isin or "").strip().upper()
        if not _ISIN.match(isin):
            return {"error": "invalid ISIN"}
        if not name or not ticker:
            return {"error": "name and ticker are required"}
        with self._conn() as c, c.cursor() as cur:
            cur.execute("SELECT 1 FROM holdings WHERE isin = %s", (isin,))
            if cur.fetchone():
                return {"error": f"{isin} is already tracked"}
            cur.execute(
                "INSERT INTO holdings (isin, wkn, name, asset_class, ticker, status, note, active) "
                "VALUES (%s, %s, %s, %s, %s, 'watching', %s, true)",
                (isin, wkn or None, name.strip()[:200], (asset_class or None), ticker.strip()[:40], (reason or "").strip()[:300] or None),
            )
            if reason:
                cur.execute(
                    "INSERT INTO recommendations_log (isin, action, rationale) VALUES (%s, 'watch', %s)",
                    (isin, reason.strip()[:500]),
                )
        return {"ok": True, "added": isin}

    def retire_watch_candidate(self, isin: str, reason: str = "") -> dict:
        """Stop tracking a watch candidate (sets active=false). Only works for watch candidates, never for real positions."""
        isin = (isin or "").strip().upper()
        if not _ISIN.match(isin):
            return {"error": "invalid ISIN"}
        with self._conn() as c, c.cursor() as cur:
            cur.execute("UPDATE holdings SET active = false, updated_at = now() WHERE isin = %s AND status = 'watching' AND active", (isin,))
            if cur.rowcount != 1:
                return {"error": f"{isin} is not an active watch candidate (real positions cannot be retired here)"}
            if reason:
                cur.execute(
                    "INSERT INTO recommendations_log (isin, action, rationale) VALUES (%s, 'unwatch', %s)",
                    (isin, reason.strip()[:500]),
                )
        return {"ok": True, "retired": isin}

    def log_recommendation(self, isin: str, action: str, rationale: str) -> dict:
        """Record a buy/sell/hold assessment for the history (no effect on the depot). action: buy, sell, hold or watch."""
        isin = (isin or "").strip().upper()
        if not _ISIN.match(isin) or action not in ("buy", "sell", "hold", "watch"):
            return {"error": "invalid ISIN or action"}
        with self._conn() as c, c.cursor() as cur:
            cur.execute(
                "INSERT INTO recommendations_log (isin, action, rationale) VALUES (%s, %s, %s)",
                (isin, action, (rationale or "").strip()[:500]),
            )
        return {"ok": True}
