#!/opt/hermes/.venv/bin/python3
"""Deterministic watcher, run every 5 minutes via Hermes' cron scheduler in
--no-agent mode (no LLM involved -- safe to run unattended):
  - auto-executes stop-losses (no confirmation, protective/time-sensitive)
  - proposes profit-target closes (requires human confirmation separately)
  - updates/checks the daily circuit breaker

Covers both crypto (Kraken, 24/7) and stock (Finnhub) positions via
lib.prices' asset-class dispatch. Stock positions are skipped in the
per-position stop-loss/profit-target loop outside NYSE trading hours (see
lib.market_hours) -- Finnhub just returns the last close price when the
market's shut, so checking it 24/7 like crypto would compare a frozen price
against itself (harmless) or fire on a stale price the moment the market
reopens with a materially different one (not harmless). Portfolio
valuation (compute_total_portfolio_value) still includes stock positions
at their last-known price even when the market's closed -- a stale
valuation is fine, only the trading *decisions* need the guard.

Stdout is delivered verbatim to WhatsApp by the cron scheduler; empty
stdout = no alert this tick.
"""
import sys

# Scoped to this one-off process only, see cli.py for why.
sys.path.insert(0, "/opt/data/tools/pip")

from lib import constants as C
from lib import db
from lib import market_hours
from lib import prices


def main() -> None:
    alerts = []
    conn = db.connect()
    try:
        with conn:
            with conn.cursor() as cur:
                total_value = db.compute_total_portfolio_value(cur, prices)
                snapshot = db.get_or_create_daily_snapshot(cur, total_value)

                if not snapshot["circuit_breaker_tripped"]:
                    day_start = snapshot["day_start_value_eur"]
                    drop_pct = (total_value - day_start) / day_start
                    if drop_pct <= C.DAILY_CIRCUIT_BREAKER_PCT:
                        db.trip_circuit_breaker(cur, snapshot["snapshot_date"])
                        alerts.append(
                            f"\U0001F534 Circuit breaker tripped: portfolio down "
                            f"{drop_pct:.1%} today (EUR {day_start} -> {total_value}). "
                            f"No new BUY proposals allowed for the rest of today. "
                            f"Stop-losses/closes still active."
                        )

                positions = db.get_open_positions(cur)
                closed_this_tick = 0
                for pos in positions:
                    asset_class = C.ASSET_CLASS[pos["symbol"]]
                    if not market_hours.is_market_open(asset_class):
                        continue

                    price = prices.get_price(pos["symbol"])
                    change_pct = (price - pos["entry_price_eur"]) / pos["entry_price_eur"]

                    if price <= pos["stop_loss_price"]:
                        db.execute_stop_loss(cur, pos, price)
                        closed_this_tick += 1
                        alerts.append(
                            f"\U0001F6D1 STOP-LOSS executed on {pos['symbol']} "
                            f"position #{pos['id']}: sold at EUR {price} "
                            f"({change_pct:.1%}). Automatic, no confirmation needed."
                        )
                    elif price >= pos["profit_target_price"]:
                        existing = db.get_pending_sell_proposal(cur, pos["id"])
                        if existing is None:
                            proposal_id = db.create_sell_target_proposal(cur, pos, price)
                            alerts.append(
                                f"\U0001F7E2 {pos['symbol']} position #{pos['id']} hit "
                                f"{change_pct:+.1%} (target {C.PROFIT_TARGET_PCT:+.0%}), "
                                f"now at EUR {price}. Reply 'yes' to close, then I'll run:\n"
                                f"  confirm_sell_target {proposal_id}"
                            )

                # Phase 3: historical record for the Grafana/OpenSearch
                # dashboards -- total value above already reflects any
                # stop-losses just executed in this same tick.
                db.record_portfolio_snapshot(
                    cur, db.get_portfolio_cash(cur), total_value,
                    len(positions) - closed_this_tick,
                )
        db.expire_stale_proposals(conn)
    finally:
        conn.close()

    if alerts:
        print("\n\n".join(alerts))


if __name__ == "__main__":
    main()
