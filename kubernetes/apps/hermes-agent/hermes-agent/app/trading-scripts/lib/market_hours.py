"""Trading-hours guard for stocks. Crypto (Kraken) trades 24/7 so nothing
in the original design ever needed this; stocks don't, and Finnhub simply
returns the last close price outside trading hours -- feeding that into
stop-loss/profit-target checks or the opportunity scanner would compare a
frozen price against itself (harmless no-op) or, worse, against a stale
entry-time baseline. Checked in America/New_York directly (not a fixed
Europe/Berlin offset) so US/EU daylight-saving transitions on different
dates never drift the window.

Does NOT know about US market holidays (Thanksgiving, July 4th, etc.) --
on those dates this still reports "open", and Finnhub still just returns
the prior close, so it degrades to the same harmless no-op as a weekend
would if this check didn't exist at all. Good enough for a paper-trading
system; revisit if that ever stops being true.
"""
import datetime
from zoneinfo import ZoneInfo

MARKET_TZ = ZoneInfo("America/New_York")
MARKET_OPEN = datetime.time(9, 30)
MARKET_CLOSE = datetime.time(16, 0)


def is_market_open(asset_class: str) -> bool:
    if asset_class == "crypto":
        return True
    if asset_class != "stock":
        raise ValueError(f"unknown asset class: {asset_class!r}")

    now = datetime.datetime.now(MARKET_TZ)
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE
