"""Secondary, low-frequency price source -- used only by check_data_quality.py
for periodic cross-checking, never by the fast 5-minute stop-loss path.
Respect the free-tier rate limit: call this rarely.
"""
import os
from decimal import Decimal

import requests

from . import constants as C


class AlphaVantageError(RuntimeError):
    pass


def get_price(symbol: str) -> Decimal:
    if symbol not in C.ALLOWED_SYMBOLS:
        raise ValueError(f"symbol not in allow-list: {symbol!r}")
    api_key = os.environ.get("ALPHAVANTAGE_API_KEY")
    if not api_key:
        raise AlphaVantageError("ALPHAVANTAGE_API_KEY not set in environment")

    from_currency = C.ALPHAVANTAGE_SYMBOL[symbol]
    resp = requests.get(
        C.ALPHAVANTAGE_URL,
        params={
            "function": "CURRENCY_EXCHANGE_RATE",
            "from_currency": from_currency,
            "to_currency": "EUR",
            "apikey": api_key,
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    # Alpha Vantage returns HTTP 200 with an "Error Message"/"Note"/
    # "Information" key instead of the expected payload on failure/rate-limit.
    if "Error Message" in data:
        raise AlphaVantageError(f"Alpha Vantage error: {data['Error Message']}")
    if "Note" in data:
        raise AlphaVantageError(f"Alpha Vantage rate-limited: {data['Note']}")
    if "Information" in data:
        raise AlphaVantageError(f"Alpha Vantage quota/info: {data['Information']}")

    rate_block = data.get("Realtime Currency Exchange Rate")
    if not rate_block:
        raise AlphaVantageError(f"Unexpected Alpha Vantage response shape: {data!r}")
    return Decimal(rate_block["5. Exchange Rate"])
