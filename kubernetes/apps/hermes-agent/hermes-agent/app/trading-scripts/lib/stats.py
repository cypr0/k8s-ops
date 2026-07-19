"""Feedback loop + Kelly Criterion position sizing.

Both are deterministic math over our own trade history -- no LLM
involvement, same safety boundary as the rest of lib/. Kelly's result is
ALWAYS clamped to [0, MAX_POSITION_PCT] by the caller (cli.py) -- it can
only shrink the position size the hard cap already allows, never grow
beyond it.
"""
from decimal import Decimal

from . import constants as C


def get_win_rate(cur, symbol: str | None = None) -> dict:
    """Bayesian-smoothed win rate from closed positions.

    (wins + PRIOR_WINS) / (total + PRIOR_WINS + PRIOR_LOSSES) -- a weak
    neutral prior centered on 50% so early estimates (few or zero closed
    trades) aren't wildly noisy, converging toward the real win rate as
    more trades close.
    """
    if symbol:
        cur.execute(
            """
            SELECT
                count(*) FILTER (WHERE close_reason = 'PROFIT_TARGET') AS wins,
                count(*) FILTER (WHERE close_reason = 'STOP_LOSS') AS losses
            FROM positions
            WHERE status = 'CLOSED' AND symbol = %s
              AND close_reason IN ('PROFIT_TARGET', 'STOP_LOSS')
            """,
            (symbol,),
        )
    else:
        cur.execute(
            """
            SELECT
                count(*) FILTER (WHERE close_reason = 'PROFIT_TARGET') AS wins,
                count(*) FILTER (WHERE close_reason = 'STOP_LOSS') AS losses
            FROM positions
            WHERE status = 'CLOSED'
              AND close_reason IN ('PROFIT_TARGET', 'STOP_LOSS')
            """
        )
    row = cur.fetchone()
    wins = Decimal(row["wins"])
    losses = Decimal(row["losses"])
    total = wins + losses
    smoothed = (wins + C.FEEDBACK_PRIOR_WINS) / (
        total + C.FEEDBACK_PRIOR_WINS + C.FEEDBACK_PRIOR_LOSSES
    )
    return {
        "wins": int(wins),
        "losses": int(losses),
        "total_closed": int(total),
        "win_rate": smoothed,
    }


def kelly_fraction(win_rate: Decimal) -> Decimal:
    """Fractional-Kelly position size as a fraction of portfolio value.

    b = reward/risk ratio, fixed by our own existing profit-target/
    stop-loss percentages (currently ~3.0). f* = (b*p - (1-p)) / b.
    Negative f* (edge looks unfavorable) clamps to 0 -- Kelly says "don't
    buy", never "short". Caller must still clamp the result to
    MAX_POSITION_PCT -- this function does not know that cap.
    """
    b = C.PROFIT_TARGET_PCT / abs(C.STOP_LOSS_PCT)
    p = win_rate
    q = 1 - p
    f_star = (b * p - q) / b
    f_used = f_star * C.KELLY_FRACTION_MULTIPLIER
    return max(Decimal("0"), f_used)


def capped_kelly_position_pct(cur, symbol: str | None = None) -> Decimal:
    """Convenience: win rate -> Kelly fraction -> clamped to MAX_POSITION_PCT."""
    stats = get_win_rate(cur, symbol)
    fraction = kelly_fraction(stats["win_rate"])
    return min(fraction, C.MAX_POSITION_PCT)
