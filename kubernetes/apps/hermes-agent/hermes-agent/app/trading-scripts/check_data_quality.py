#!/opt/hermes/.venv/bin/python3
"""Periodic (every 4 hours) cross-check of Kraken vs. Finnhub prices for
BTC/ETH only (stocks have no separate cross-check -- Finnhub IS the
primary stock price source, there's nothing independent to compare it
against), run via Hermes' cron scheduler in --no-agent mode. If the two
sources diverge beyond DATA_QUALITY_DIVERGENCE_PCT, flags data quality as
bad -- propose_buy() in cli.py then refuses new BUY proposals until a later
check confirms agreement again. Stop-losses/closes are unaffected (they
trust Kraken directly, see check_positions.py).

Was Alpha Vantage instead of Finnhub until the free-tier daily quota (25
req/day) turned out to already be exhausted by the user's own unrelated use
of the same API key -- see constants.py. Finnhub has no daily cap.

Alerts on every ok<->not-ok transition, AND once every 24h while
continuously flagged (db.data_quality_alert_due) -- a *persistent* failure
used to go silent after the first alert (state-transition-only logic) and
sat unnoticed blocking new BUY proposals for days.
"""
import sys

# Scoped to this one-off process only, see cli.py for why.
sys.path.insert(0, "/opt/data/tools/pip")

from lib import constants as C
from lib import db
from lib import finnhub
from lib import kraken


def main() -> None:
    alerts = []
    conn = db.connect()
    try:
        with conn, conn.cursor() as cur:
            status = db.get_data_quality_status(cur)

            worst_divergence = None
            worst_symbol = None
            failure_reason = None

            for symbol in C.CRYPTO_SYMBOLS:
                try:
                    kraken_price = kraken.get_price(symbol)
                    finnhub_price = finnhub.get_price(symbol)
                except Exception as exc:  # noqa: BLE001 -- any fetch failure is a data-quality concern
                    failure_reason = f"{symbol}: fetch failed ({exc})"
                    continue

                divergence = abs(kraken_price - finnhub_price) / kraken_price
                if worst_divergence is None or divergence > worst_divergence:
                    worst_divergence = divergence
                    worst_symbol = symbol

            if failure_reason is not None:
                still_ok = False
                reason = failure_reason
            elif worst_divergence is not None and worst_divergence > C.DATA_QUALITY_DIVERGENCE_PCT:
                still_ok = False
                reason = (
                    f"{worst_symbol} price diverges {worst_divergence:.2%} between "
                    f"Kraken and Finnhub (threshold {C.DATA_QUALITY_DIVERGENCE_PCT:.0%})"
                )
            else:
                still_ok = True
                reason = None

            alert_due = db.data_quality_alert_due(status, still_ok)
            db.set_data_quality_status(cur, ok=still_ok, divergence_pct=worst_divergence, reason=reason)
            if alert_due:
                db.mark_data_quality_alerted(cur)
                if not still_ok:
                    alerts.append(
                        f"⚠️ Data quality flagged: {reason}. No new BUY proposals until this clears."
                    )
                elif not status["ok"]:
                    alerts.append(
                        "✅ Data quality restored: Kraken and Finnhub agree again. "
                        "New BUY proposals allowed."
                    )
    finally:
        conn.close()

    if alerts:
        print("\n\n".join(alerts))


if __name__ == "__main__":
    main()
