#!/opt/hermes/.venv/bin/python3
"""Proactive opportunity scanner, run every 30 minutes via Hermes' cron
scheduler in --no-agent mode (no LLM involved -- the EV/Kelly math is
deterministic). For each symbol with no existing open position, not
circuit-breaker-tripped, with market data quality ok: scores the entry via
lib.signals.expected_value_score() (a technical mean-reversion signal
combined with our own historical win rate). If favorable, creates a
Kelly-sized BUY proposal and alerts WhatsApp with the reasoning.

IMPORTANT: this only ever creates a *proposal*. Nothing executes without
the user's explicit confirmation via `cli.py confirm_buy <id>` -- identical
safety boundary to a manually requested buy. The technical signal is a
simple, well-known heuristic; it is not a claim of real predictive edge.
"""
import sys

# Scoped to this one-off process only, see cli.py for why.
sys.path.insert(0, "/opt/data/tools/pip")

from lib import constants as C
from lib import db
from lib import kraken
from lib import signals


def main() -> None:
    alerts = []
    conn = db.connect()
    try:
        with conn, conn.cursor() as cur:
            if db.is_circuit_breaker_tripped_today(cur):
                return  # no alert needed, check_positions.py already announced this
            quality = db.get_data_quality_status(cur)
            if not quality["ok"]:
                return  # ditto, check_data_quality.py already announced this

            for symbol in C.ALLOWED_SYMBOLS:
                if db.get_open_positions(cur, symbol):
                    continue  # already holding this symbol
                if db.get_pending_buy_proposal(cur, symbol):
                    continue  # a proposal (manual or scan) is already outstanding

                score = signals.expected_value_score(cur, symbol)
                if not score["favorable"]:
                    continue

                total_value = db.compute_total_portfolio_value(cur, kraken)
                cash = db.get_portfolio_cash(cur)
                amount_eur = min(total_value * score["position_pct"], cash)
                if amount_eur <= 0:
                    continue

                price = score["signal"]["current_price"]
                proposal_id = db.create_buy_proposal(
                    cur, symbol, amount_eur, price, source="scan"
                )
                alerts.append(
                    f"\U0001F50D Opportunity spotted: {symbol} {score['signal']['reason']}. "
                    f"Historical win rate {score['win_rate']:.0%} "
                    f"({score['total_closed_trades']} closed trades so far). "
                    f"Suggesting EUR {amount_eur:.2f} "
                    f"(Kelly-sized, {score['position_pct']:.1%} of portfolio).\n"
                    f"Reply 'yes' to buy, then I'll run:\n"
                    f"  confirm_buy {proposal_id}"
                )
        db.expire_stale_proposals(conn)
    finally:
        conn.close()

    if alerts:
        print("\n\n".join(alerts))


if __name__ == "__main__":
    main()
