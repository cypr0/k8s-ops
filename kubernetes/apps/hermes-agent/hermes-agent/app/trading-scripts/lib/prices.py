"""Asset-class dispatcher: every caller that used to import lib.kraken
directly (cli.py, check_positions.py, check_opportunities.py, signals.py)
now imports this instead, so the same code path handles crypto (Kraken,
direct EUR) and stocks (Finnhub, USD converted to EUR via lib.fx) uniformly.
Mirrors db.compute_total_portfolio_value()'s existing price_source
duck-typing (get_price(symbol)) -- get_closes follows the same shape.
"""
from decimal import Decimal

from . import constants as C
from . import finnhub
from . import kraken


def get_price(symbol: str) -> Decimal:
    asset_class = C.ASSET_CLASS.get(symbol)
    if asset_class == "crypto":
        return kraken.get_price(symbol)
    if asset_class == "stock":
        return finnhub.get_price(symbol)
    raise ValueError(f"symbol not in allow-list: {symbol!r}")


def get_closes(symbol: str, interval_minutes: int = 60, count: int | None = None) -> list:
    asset_class = C.ASSET_CLASS.get(symbol)
    if asset_class == "crypto":
        return kraken.get_closes(symbol, interval_minutes=interval_minutes, count=count)
    if asset_class == "stock":
        return finnhub.get_closes(symbol, interval_minutes=interval_minutes, count=count)
    raise ValueError(f"symbol not in allow-list: {symbol!r}")
