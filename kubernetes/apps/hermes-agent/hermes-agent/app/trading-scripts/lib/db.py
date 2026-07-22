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
    symbol              TEXT NOT NULL,
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
    symbol                 TEXT NOT NULL,
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
    symbol        TEXT NOT NULL,
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

-- Phase 4: without this, a *persistent* failure (e.g. the Alpha Vantage
-- quota exhaustion incident) only ever alerted once, on the ok->not-ok
-- transition -- every following check also failed but stayed silent
-- (alerts only fired on a state *change*), so the block on new BUY
-- proposals could sit unnoticed for days. See check_data_quality.py.
ALTER TABLE data_quality_status ADD COLUMN IF NOT EXISTS last_alerted_at TIMESTAMPTZ;

-- Phase 2: tracks whether a BUY proposal was manually requested or
-- bot-suggested by the opportunity scanner -- reporting/feedback only,
-- not a safety gate. ADD COLUMN IF NOT EXISTS is safe to rerun against
-- the already-live table.
ALTER TABLE proposals ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'user';

-- Phase 3: historical portfolio value, recorded on every check_positions.py
-- tick -- total value is otherwise always computed live (never stored) to
-- avoid drift, but a dashboard time-series panel needs real history.
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id                  BIGSERIAL PRIMARY KEY,
    snapshot_time       TIMESTAMPTZ NOT NULL DEFAULT now(),
    cash_eur            NUMERIC(14,2) NOT NULL,
    total_value_eur     NUMERIC(14,2) NOT NULL,
    num_open_positions  SMALLINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_portfolio_snapshots_time ON portfolio_snapshots (snapshot_time);
"""

# Phase 4 (stocks): the original CREATE TABLEs above inlined
# CHECK (symbol IN ('BTC','ETH')) directly on the column -- too narrow now
# that STOCK_SYMBOLS exists (constants.py). Postgres auto-names a single-
# column inline CHECK "<table>_symbol_check", so DROP CONSTRAINT IF EXISTS
# + a freshly named ADD CONSTRAINT is rerun-safe without needing a
# PL/pgSQL DO-block (which Flux's postBuild.substituteFrom would mangle,
# see the GRANT_READONLY_SQL comment above). Re-executes on every
# migrate() call, which is fine -- widening to the exact same list is a
# no-op.
_SYMBOL_LIST_SQL = ", ".join(f"'{s}'" for s in C.ALL_SYMBOLS)
WIDEN_SYMBOL_CHECKS_SQL = f"""
ALTER TABLE positions DROP CONSTRAINT IF EXISTS positions_symbol_check;
ALTER TABLE positions ADD CONSTRAINT positions_symbol_check CHECK (symbol IN ({_SYMBOL_LIST_SQL}));
ALTER TABLE proposals DROP CONSTRAINT IF EXISTS proposals_symbol_check;
ALTER TABLE proposals ADD CONSTRAINT proposals_symbol_check CHECK (symbol IN ({_SYMBOL_LIST_SQL}));
ALTER TABLE trades DROP CONSTRAINT IF EXISTS trades_symbol_check;
ALTER TABLE trades ADD CONSTRAINT trades_symbol_check CHECK (symbol IN ({_SYMBOL_LIST_SQL}));
"""

# Phase 3: read-only access for the Grafana datasource and the OpenSearch
# stats exporter. CNPG's managed roles only support attributes, never
# grants, so this is the only way to give either of them SELECT-only
# access -- granted by tradingusr, the schema owner. The existence check
# is a plain Python conditional rather than a PL/pgSQL DO-block, because
# a PL/pgSQL block needs dollar-quoting and Flux's postBuild.substituteFrom
# mangles repeated dollar-sign sequences when rendering this ConfigMap
# (the same class of issue already hit with the WhatsApp number env var
# in deployment.yaml) -- avoiding dollar-quoting sidesteps it entirely.
# A no-op until the tradingreadonly role exists; takes effect on the
# next migration run after it does.
GRANT_READONLY_SQL = """
GRANT CONNECT ON DATABASE trading TO tradingreadonly;
GRANT USAGE ON SCHEMA public TO tradingreadonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO tradingreadonly;
ALTER DEFAULT PRIVILEGES FOR ROLE tradingusr IN SCHEMA public
  GRANT SELECT ON TABLES TO tradingreadonly;
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
            cur.execute(WIDEN_SYMBOL_CHECKS_SQL)
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'tradingreadonly'")
            if cur.fetchone():
                cur.execute(GRANT_READONLY_SQL)
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


def record_portfolio_snapshot(cur, cash_eur, total_value_eur, num_open_positions: int) -> None:
    cur.execute(
        """
        INSERT INTO portfolio_snapshots (cash_eur, total_value_eur, num_open_positions)
        VALUES (%s, %s, %s)
        """,
        (cash_eur, total_value_eur, num_open_positions),
    )


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


def mark_data_quality_alerted(cur) -> None:
    cur.execute("UPDATE data_quality_status SET last_alerted_at = now() WHERE id = 1")


def data_quality_alert_due(status: dict, still_ok: bool) -> bool:
    """Whether check_data_quality.py should actually send a WhatsApp alert
    this run, vs. staying silent. Always alerts on a state transition
    (ok<->not-ok); if it's been continuously flagged, re-alerts once every
    24h as a reminder rather than either spamming every run or going silent
    forever (see the ALTER TABLE ... last_alerted_at comment above).
    """
    was_ok = status["ok"]
    if was_ok != still_ok:
        return True
    if still_ok:
        return False  # was ok, still ok -- nothing to say
    last_alerted_at = status["last_alerted_at"]
    if last_alerted_at is None:
        return True
    return (datetime.now(ZoneInfo("UTC")) - last_alerted_at) >= timedelta(hours=24)


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


def get_pending_buy_proposal(cur, symbol: str):
    """Any outstanding BUY proposal for this symbol, regardless of who
    requested it -- used by check_opportunities.py to avoid piling up
    duplicate scan-suggested proposals while one is already pending.
    """
    cur.execute(
        """
        SELECT * FROM proposals
        WHERE symbol = %s AND proposal_type = 'BUY' AND status = 'PENDING'
        """,
        (symbol,),
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


def create_buy_proposal(
    cur, symbol: str, amount_eur, current_price, source: str = "user"
) -> uuid.UUID:
    """source: 'user' for a manually requested buy, 'scan' for one
    generated by the opportunity scanner (check_opportunities.py) --
    reporting/feedback only, does not affect validation.
    """
    expires_at = datetime.now(ZoneInfo("UTC")) + timedelta(minutes=C.PROPOSAL_EXPIRY_MINUTES)
    cur.execute(
        """
        INSERT INTO proposals (proposal_type, symbol, amount_eur, price_at_proposal_eur, expires_at, source)
        VALUES ('BUY', %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (symbol, amount_eur, current_price, expires_at, source),
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
