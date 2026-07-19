#!/opt/hermes/.venv/bin/python3
"""Periodic (every 4 hours) cross-check of Kraken vs. Alpha Vantage prices for
BTC/ETH, run via Hermes' cron scheduler in --no-agent mode. If the two
sources diverge beyond DATA_QUALITY_DIVERGENCE_PCT, flags data quality as
bad -- propose_buy() in cli.py then refuses new BUY proposals until a later
check confirms agreement again. Stop-losses/closes are unaffected (they
trust Kraken directly, see check_positions.py).

Deliberately low-frequency: Alpha Vantage's free tier has a very small daily
call budget. Silent (no WhatsApp alert) when nothing changed.
"""
import sys

# Scoped to this one-off process only, see cli.py for why.
sys.path.insert(0, "/opt/data/tools/pip")

from lib import constants as C
from lib import alphavantage
from lib import db
from lib import kraken


def main() -> None:
    alerts = []
    conn = db.connect()
    try:
        with conn, conn.cursor() as cur:
            status = db.get_data_quality_status(cur)
            was_ok = status["ok"]

            worst_divergence = None
            worst_symbol = None
            failure_reason = None

            for symbol in C.ALLOWED_SYMBOLS:
                try:
                    kraken_price = kraken.get_price(symbol)
                    av_price = alphavantage.get_price(symbol)
                except Exception as exc:  # noqa: BLE001 -- any fetch failure is a data-quality concern
                    failure_reason = f"{symbol}: fetch failed ({exc})"
                    continue

                divergence = abs(kraken_price - av_price) / kraken_price
                if worst_divergence is None or divergence > worst_divergence:
                    worst_divergence = divergence
                    worst_symbol = symbol

            if failure_reason is not None:
                db.set_data_quality_status(cur, ok=False, divergence_pct=worst_divergence, reason=failure_reason)
                if was_ok:
                    alerts.append(
                        f"⚠️ Data quality check couldn't complete ({failure_reason}) "
                        f"-- treating as unreliable, no new BUY proposals until this clears."
                    )
            elif worst_divergence is not None and worst_divergence > C.DATA_QUALITY_DIVERGENCE_PCT:
                reason = (
                    f"{worst_symbol} price diverges {worst_divergence:.2%} between "
                    f"Kraken and Alpha Vantage (threshold {C.DATA_QUALITY_DIVERGENCE_PCT:.0%})"
                )
                db.set_data_quality_status(cur, ok=False, divergence_pct=worst_divergence, reason=reason)
                if was_ok:
                    alerts.append(
                        f"⚠️ Data quality flagged: {reason}. "
                        f"No new BUY proposals until this clears."
                    )
            else:
                db.set_data_quality_status(cur, ok=True, divergence_pct=worst_divergence, reason=None)
                if not was_ok:
                    alerts.append(
                        "✅ Data quality restored: Kraken and Alpha Vantage agree again. "
                        "New BUY proposals allowed."
                    )
    finally:
        conn.close()

    if alerts:
        print("\n\n".join(alerts))


if __name__ == "__main__":
    main()
