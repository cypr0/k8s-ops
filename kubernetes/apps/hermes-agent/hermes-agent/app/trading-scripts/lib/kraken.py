"""Primary, fast price source. Public endpoint, no API key needed."""
from decimal import Decimal

import requests

from . import constants as C


def get_price(symbol: str) -> Decimal:
    if symbol not in C.ALLOWED_SYMBOLS:
        raise ValueError(f"symbol not in allow-list: {symbol!r}")
    pair = C.KRAKEN_PAIR[symbol]
    resp = requests.get(C.KRAKEN_TICKER_URL, params={"pair": pair}, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if data.get("error"):
        raise RuntimeError(f"Kraken API error: {data['error']}")
    result_key = C.KRAKEN_RESULT_KEY[symbol]
    last_trade_price = data["result"][result_key]["c"][0]
    return Decimal(last_trade_price)


def get_closes(symbol: str, interval_minutes: int = 60, count: int | None = None) -> list:
    """Historical closing prices, oldest first. Candle format from Kraken:
    [time, open, high, low, close, vwap, volume, count] -- close is index 4.
    """
    if symbol not in C.ALLOWED_SYMBOLS:
        raise ValueError(f"symbol not in allow-list: {symbol!r}")
    pair = C.KRAKEN_PAIR[symbol]
    resp = requests.get(
        C.KRAKEN_OHLC_URL,
        params={"pair": pair, "interval": interval_minutes},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("error"):
        raise RuntimeError(f"Kraken API error: {data['error']}")
    result_key = C.KRAKEN_RESULT_KEY[symbol]
    candles = data["result"][result_key]
    closes = [Decimal(c[4]) for c in candles]
    if count is not None:
        closes = closes[-count:]
    return closes
