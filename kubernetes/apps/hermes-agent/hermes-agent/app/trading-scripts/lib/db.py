"""Postgres access for the paper-trading ledger. All money math is Decimal
(psycopg2 maps NUMERIC -> Decimal natively). Total portfolio value is always
computed from cash + open positions, never stored, to avoid drift.
"""
import os
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.extras

from . import constants as C

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS portfolio (
    id          SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    cash_eur    NUMERIC(14,2) NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO portfolio (id, cash_eur) VALUES (1, %(starting_capital)s) ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS positions (
    id                  BIGSERIAL PRIMARY KEY,
    symbol              TEXT NOT NULL CHECK (symbol IN ('BTC','ETH')),
    quantity            NUMERIC(20,8) NOT NULL CHECK (quantity > 0),
    entry_price_eur     NUMERIC(14,4) NOT NULL CHECK (entry_price_eur > 0),
    entry_time          TIMESTAMPTZ NOT NULL,
    stop_loss_price     NUMERIC(14,4) NOT NULL,
    profit_target_price NUMERIC(14,4) NOT NULL,
    status              TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','CLOSED')),
    closed_price_eur    NUMERIC(14,4),
    closed_time         TIMESTAMPTZ,
    close_reason        TEXT CHECK (close_reason IN ('STOP_LOSS','PROFIT_TARGET','MANUAL')),
    buy_proposal_id     UUID
);
CREATE INDEX IF NOT EXISTS idx_positions_open ON positions (symbol) WHERE status = 'OPEN';

CREATE TABLE IF NOT EXISTS proposals (
    id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    proposal_type          TEXT NOT NULL CHECK (proposal_type IN ('BUY','SELL_PROFIT_TARGET')),
    symbol                 TEXT NOT NULL CHECK (symbol IN ('BTC','ETH')),
    quantity               NUMERIC(20,8),
    amount_eur             NUMERIC(14,2),
    position_id            BIGINT REFERENCES positions(id),
    price_at_proposal_eur  NUMERIC(14,4) NOT NULL,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at             TIMESTAMPTZ NOT NULL,
    status                 TEXT NOT NULL DEFAULT 'PENDING'
                             CHECK (status IN ('PENDING','EXECUTED','EXPIRED','REJECTED')),
    confirmed_at           TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_proposals_pending ON proposals (status) WHERE status = 'PENDING';

CREATE TABLE IF NOT EXISTS trades (
    id            BIGSERIAL PRIMARY KEY,
    position_id   BIGINT REFERENCES positions(id),
    proposal_id   UUID REFERENCES proposals(id),
    symbol        TEXT NOT NULL CHECK (symbol IN ('BTC','ETH')),
    side          TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
    quantity      NUMERIC(20,8) NOT NULL,
    price_eur     NUMERIC(14,4) NOT NULL,
    value_eur     NUMERIC(14,2) NOT NULL,
    trade_type    TEXT NOT NULL CHECK (trade_type IN
                    ('BUY_CONFIRMED','STOP_LOSS_AUTO','PROFIT_TARGET_CONFIRMED')),
    executed_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS daily_snapshot (
    snapshot_date           DATE PRIMARY KEY,
    day_start_value_eur     NUMERIC(14,2) NOT NULL,
    circuit_breaker_tripped BOOLEAN NOT NULL DEFAULT false,
    tripped_at              TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS data_quality_status (
    id                   SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    ok                   BOOLEAN NOT NULL DEFAULT true,
    last_checked_at      TIMESTAMPTZ,
    last_divergence_pct  NUMERIC(6,4),
    flagged_reason       TEXT
);
INSERT INTO data_quality_status (id) VALUES (1) ON CONFLICT (id) DO NOTHING;
"""


def connect():
    return psycopg2.connect(
        host=os.environ["POSTGRES_HOST"],
        dbname=os.environ["POSTGRES_DB"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        cursor_factory=psycopg2.extras.RealDictCursor,
        connect_timeout=10,
    )


def migrate() -> None:
    conn = connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(SCHEMA_SQL, {"starting_capital": C.STARTING_CAPITAL_EUR})
    finally:
        conn.close()


def _today_berlin():
    return datetime.now(ZoneInfo(C.TIMEZONE)).date()


def get_portfolio_cash(cur) -> "Decimal":
    cur.execute("SELECT cash_eur FROM portfolio WHERE id = 1")
    return cur.fetchone()["cash_eur"]


def get_open_positions(cur, symbol: str | None = None) -> list:
    if symbol:
        cur.execute(
            "SELECT * FROM positions WHERE status = 'OPEN' AND symbol = %s ORDER BY id",
            (symbol,),
        )
    else:
        cur.execute("SELECT * FROM positions WHERE status = 'OPEN' ORDER BY id")
    return cur.fetchall()


def compute_total_portfolio_value(cur, price_source) -> "Decimal":
    cash = get_portfolio_cash(cur)
    total = cash
    for pos in get_open_positions(cur):
        price = price_source.get_price(pos["symbol"])
        total += pos["quantity"] * price
    return total


def get_or_create_daily_snapshot(cur, total_value_now) -> dict:
    today = _today_berlin()
    cur.execute("SELECT * FROM daily_snapshot WHERE snapshot_date = %s", (today,))
    row = cur.fetchone()
    if row is not None:
        return row
    cur.execute(
        """
        INSERT INTO daily_snapshot (snapshot_date, day_start_value_eur)
        VALUES (%s, %s)
        ON CONFLICT (snapshot_date) DO UPDATE SET snapshot_date = EXCLUDED.snapshot_date
        RETURNING *
        """,
        (today, total_value_now),
    )
    return cur.fetchone()


def trip_circuit_breaker(cur, snapshot_date) -> None:
    cur.execute(
        """
        UPDATE daily_snapshot
        SET circuit_breaker_tripped = true, tripped_at = now()
        WHERE snapshot_date = %s
        """,
        (snapshot_date,),
    )


def is_circuit_breaker_tripped_today(cur) -> bool:
    today = _today_berlin()
    cur.execute(
        "SELECT circuit_breaker_tripped FROM daily_snapshot WHERE snapshot_date = %s",
        (today,),
    )
    row = cur.fetchone()
    return bool(row and row["circuit_breaker_tripped"])


def get_data_quality_status(cur) -> dict:
    cur.execute("SELECT * FROM data_quality_status WHERE id = 1")
    return cur.fetchone()


def set_data_quality_status(cur, ok: bool, divergence_pct, reason: str | None) -> None:
    cur.execute(
        """
        UPDATE data_quality_status
        SET ok = %s, last_checked_at = now(), last_divergence_pct = %s, flagged_reason = %s
        WHERE id = 1
        """,
        (ok, divergence_pct, reason),
    )


def execute_stop_loss(cur, position: dict, current_price) -> None:
    value = position["quantity"] * current_price
    cur.execute(
        """
        UPDATE positions
        SET status = 'CLOSED', closed_price_eur = %s, closed_time = now(),
            close_reason = 'STOP_LOSS'
        WHERE id = %s
        """,
        (current_price, position["id"]),
    )
    cur.execute(
        """
        INSERT INTO trades (position_id, symbol, side, quantity, price_eur, value_eur, trade_type)
        VALUES (%s, %s, 'SELL', %s, %s, %s, 'STOP_LOSS_AUTO')
        """,
        (position["id"], position["symbol"], position["quantity"], current_price, value),
    )
    cur.execute(
        "UPDATE portfolio SET cash_eur = cash_eur + %s, updated_at = now() WHERE id = 1",
        (value,),
    )


def get_pending_sell_proposal(cur, position_id: int):
    cur.execute(
        """
        SELECT * FROM proposals
        WHERE position_id = %s AND proposal_type = 'SELL_PROFIT_TARGET' AND status = 'PENDING'
        """,
        (position_id,),
    )
    return cur.fetchone()


def create_sell_target_proposal(cur, position: dict, current_price) -> uuid.UUID:
    expires_at = datetime.now(ZoneInfo("UTC")) + timedelta(minutes=C.PROPOSAL_EXPIRY_MINUTES)
    cur.execute(
        """
        INSERT INTO proposals
            (proposal_type, symbol, quantity, position_id, price_at_proposal_eur, expires_at)
        VALUES ('SELL_PROFIT_TARGET', %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (position["symbol"], position["quantity"], position["id"], current_price, expires_at),
    )
    return cur.fetchone()["id"]


def create_buy_proposal(cur, symbol: str, amount_eur, current_price) -> uuid.UUID:
    expires_at = datetime.now(ZoneInfo("UTC")) + timedelta(minutes=C.PROPOSAL_EXPIRY_MINUTES)
    cur.execute(
        """
        INSERT INTO proposals (proposal_type, symbol, amount_eur, price_at_proposal_eur, expires_at)
        VALUES ('BUY', %s, %s, %s, %s)
        RETURNING id
        """,
        (symbol, amount_eur, current_price, expires_at),
    )
    return cur.fetchone()["id"]


def get_pending_proposal(cur, proposal_id):
    cur.execute(
        "SELECT * FROM proposals WHERE id = %s AND status = 'PENDING' AND expires_at > now()",
        (proposal_id,),
    )
    return cur.fetchone()


def reject_proposal(cur, proposal_id, reason: str) -> None:
    # `reason` isn't persisted on the row today (no column for it) -- the
    # caller is expected to print/relay it; status is what matters for
    # re-confirmation attempts.
    cur.execute("UPDATE proposals SET status = 'REJECTED' WHERE id = %s", (proposal_id,))


def execute_buy(cur, proposal: dict, execution_price) -> int:
    quantity = proposal["amount_eur"] / execution_price
    stop_loss_price = execution_price * (1 + C.STOP_LOSS_PCT)
    profit_target_price = execution_price * (1 + C.PROFIT_TARGET_PCT)
    cur.execute(
        """
        INSERT INTO positions
            (symbol, quantity, entry_price_eur, entry_time, stop_loss_price,
             profit_target_price, buy_proposal_id)
        VALUES (%s, %s, %s, now(), %s, %s, %s)
        RETURNING id
        """,
        (
            proposal["symbol"], quantity, execution_price, stop_loss_price,
            profit_target_price, proposal["id"],
        ),
    )
    position_id = cur.fetchone()["id"]
    cur.execute(
        """
        INSERT INTO trades (position_id, proposal_id, symbol, side, quantity, price_eur, value_eur, trade_type)
        VALUES (%s, %s, %s, 'BUY', %s, %s, %s, 'BUY_CONFIRMED')
        """,
        (position_id, proposal["id"], proposal["symbol"], quantity, execution_price, proposal["amount_eur"]),
    )
    cur.execute(
        "UPDATE portfolio SET cash_eur = cash_eur - %s, updated_at = now() WHERE id = 1",
        (proposal["amount_eur"],),
    )
    cur.execute(
        "UPDATE proposals SET status = 'EXECUTED', confirmed_at = now() WHERE id = %s",
        (proposal["id"],),
    )
    return position_id


def execute_sell_target(cur, proposal: dict, position: dict, execution_price) -> None:
    value = position["quantity"] * execution_price
    cur.execute(
        """
        UPDATE positions
        SET status = 'CLOSED', closed_price_eur = %s, closed_time = now(),
            close_reason = 'PROFIT_TARGET'
        WHERE id = %s
        """,
        (execution_price, position["id"]),
    )
    cur.execute(
        """
        INSERT INTO trades (position_id, proposal_id, symbol, side, quantity, price_eur, value_eur, trade_type)
        VALUES (%s, %s, %s, 'SELL', %s, %s, %s, 'PROFIT_TARGET_CONFIRMED')
        """,
        (position["id"], proposal["id"], position["symbol"], position["quantity"], execution_price, value),
    )
    cur.execute(
        "UPDATE portfolio SET cash_eur = cash_eur + %s, updated_at = now() WHERE id = 1",
        (value,),
    )
    cur.execute(
        "UPDATE proposals SET status = 'EXECUTED', confirmed_at = now() WHERE id = %s",
        (proposal["id"],),
    )


def get_position(cur, position_id: int):
    cur.execute("SELECT * FROM positions WHERE id = %s", (position_id,))
    return cur.fetchone()


def expire_stale_proposals(conn) -> None:
    with conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE proposals SET status = 'EXPIRED' WHERE status = 'PENDING' AND expires_at <= now()"
        )
