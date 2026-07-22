"""Free, keyless public price source for the crypto data-quality
cross-check (check_data_quality.py) only -- Finnhub's crypto candle
endpoint turned out to require a paid plan (confirmed live: 403 "You
don't have access to this resource" on an otherwise-working free-tier
key, even though its stock endpoints work fine), so this replaces it
entirely for that one purpose. Binance's spot ticker is public, needs no
API key, and its rate limit (1200 request-weight/min) is far more than a
2-symbol check every 4 hours needs.
"""
from decimal import Decimal

import requests

from . import constants as C

BINANCE_TICKER_URL = "https://api.binance.com/api/v3/ticker/price"
BINANCE_SYMBOL = {"BTC": "BTCUSDT", "ETH": "ETHUSDT"}


def get_price_usd(symbol: str) -> Decimal:
    if symbol not in C.CRYPTO_SYMBOLS:
        raise ValueError(f"symbol not in allow-list: {symbol!r}")
    resp = requests.get(BINANCE_TICKER_URL, params={"symbol": BINANCE_SYMBOL[symbol]}, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    return Decimal(data["price"])
