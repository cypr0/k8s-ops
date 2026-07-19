"""Technical signal + EV scoring for the proactive opportunity scanner
(check_opportunities.py). Deterministic, no LLM involvement.

The mean-reversion signal is a simple, well-known heuristic -- NOT a claim
of real predictive edge over crypto prices. It's cheap and honest to test
in paper trading; nothing here should be read as investment advice or a
validated trading strategy.
"""
from decimal import Decimal

from . import constants as C
from . import kraken
from . import stats


def mean_reversion_signal(symbol: str) -> dict:
    """How far the current price sits below its N-period SMA.

    A price more than MEAN_REVERSION_THRESHOLD_PCT below the SMA is
    treated as a candidate buy signal (assumes prices tend to revert
    toward a recent average -- true only sometimes, for some assets, for
    some periods; not guaranteed).
    """
    closes = kraken.get_closes(symbol, interval_minutes=60, count=C.SMA_PERIOD_HOURS)
    if len(closes) < C.SMA_PERIOD_HOURS:
        return {"fires": False, "reason": "not enough price history yet"}

    sma = sum(closes) / Decimal(len(closes))
    current_price = closes[-1]
    distance_pct = (current_price - sma) / sma

    fires = distance_pct <= -C.MEAN_REVERSION_THRESHOLD_PCT
    return {
        "fires": fires,
        "current_price": current_price,
        "sma": sma,
        "distance_pct": distance_pct,
        "reason": (
            f"price is {distance_pct:.1%} vs. {C.SMA_PERIOD_HOURS}h SMA "
            f"(EUR {sma:.2f})"
        ),
    }


def expected_value_score(cur, symbol: str) -> dict:
    """Combines the technical signal with the feedback-loop win rate into a
    single go/no-go decision. Favorable only if the technical signal fires
    AND our own historical edge (Kelly fraction) is positive -- a purely
    reactive signal with no favorable historical edge is not enough on its
    own, and vice versa.
    """
    signal = mean_reversion_signal(symbol)
    win_stats = stats.get_win_rate(cur, symbol)
    kelly = stats.kelly_fraction(win_stats["win_rate"])

    favorable = bool(signal.get("fires")) and kelly > 0
    return {
        "favorable": favorable,
        "signal": signal,
        "win_rate": win_stats["win_rate"],
        "total_closed_trades": win_stats["total_closed"],
        "kelly_fraction": kelly,
        "position_pct": min(kelly, C.MAX_POSITION_PCT),
    }
