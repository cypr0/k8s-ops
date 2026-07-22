"""USD -> EUR conversion for stock prices (Finnhub quotes US equities in
USD, but the whole portfolio/ledger is EUR-denominated, see lib/db.py).
Frankfurter serves ECB reference rates, no API key, no daily/monthly quota
(only abuse-prevention rate limiting) -- unlike Alpha Vantage, safe to call
on every stock price fetch without a shared-key contention risk.
"""
from decimal import Decimal

import requests

FRANKFURTER_URL = "https://api.frankfurter.dev/v1/latest"


def get_usd_to_eur_rate() -> Decimal:
    resp = requests.get(FRANKFURTER_URL, params={"from": "USD", "to": "EUR"}, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    rate = data.get("rates", {}).get("EUR")
    if rate is None:
        raise RuntimeError(f"Unexpected Frankfurter response shape: {data!r}")
    return Decimal(str(rate))
