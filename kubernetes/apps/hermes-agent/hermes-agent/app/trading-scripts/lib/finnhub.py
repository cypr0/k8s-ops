"""Finnhub client: primary price/candle/news/fundamentals source for
stocks (lib/prices.py routes stock symbols here, see constants.ASSET_CLASS).
Free tier: 60 calls/min, no daily cap (unlike Alpha Vantage) -- see
constants.py for the API key env var.

NOT used for crypto (unlike an earlier version of this module) --
/crypto/candle turned out to need a paid plan (confirmed live: 403 "You
don't have access to this resource" on an otherwise-working free-tier
key, even though /stock/quote and /stock/candle both work fine on it).
check_data_quality.py's crypto cross-check uses lib.binance instead.
"""
import os
from decimal import Decimal

import requests

from . import constants as C
from . import fx


class FinnhubError(RuntimeError):
    pass


def _api_key() -> str:
    api_key = os.environ.get("FINNHUB_API_KEY")
    if not api_key:
        raise FinnhubError("FINNHUB_API_KEY not set in environment")
    return api_key


def _get(path: str, params: dict) -> dict:
    params = {**params, "token": _api_key()}
    resp = requests.get(f"{C.FINNHUB_BASE_URL}{path}", params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_price(symbol: str) -> Decimal:
    """Current price in EUR. Stocks only, see module docstring."""
    if C.ASSET_CLASS.get(symbol) != "stock":
        raise ValueError(f"finnhub.get_price is stock-only, got {symbol!r}")
    data = _get("/quote", {"symbol": symbol})
    price_usd = data.get("c")
    if not price_usd or price_usd <= 0:
        raise FinnhubError(f"Finnhub returned no usable quote for {symbol}: {data!r}")
    rate = fx.get_usd_to_eur_rate()
    return Decimal(str(price_usd)) * rate


def get_closes(symbol: str, interval_minutes: int = 60, count: int | None = None) -> list:
    """Historical closing prices in EUR, oldest first. Stocks only."""
    if C.ASSET_CLASS.get(symbol) != "stock":
        raise ValueError(f"get_closes is stock-only, got {symbol!r}")
    resp_key = _finnhub_resolution(interval_minutes)
    data = _get(
        "/stock/candle",
        {"symbol": symbol, "resolution": resp_key, **_lookback_range(interval_minutes, count)},
    )
    if data.get("s") != "ok":
        raise FinnhubError(f"Finnhub candle error for {symbol}: {data!r}")
    rate = fx.get_usd_to_eur_rate()
    closes = [Decimal(str(c)) * rate for c in data["c"]]
    if count is not None:
        closes = closes[-count:]
    return closes


def _finnhub_resolution(interval_minutes: int) -> str:
    # Finnhub resolutions are one of 1,5,15,30,60,D,W,M -- only the
    # granularity lib/signals.py actually uses is mapped here.
    if interval_minutes == 60:
        return "60"
    raise ValueError(f"unsupported interval_minutes: {interval_minutes}")


def _lookback_range(interval_minutes: int, count: int | None) -> dict:
    import time

    now = int(time.time())
    span_seconds = interval_minutes * 60 * ((count or 1) + 5)  # small buffer for weekends/gaps
    return {"from": now - span_seconds, "to": now}


def get_company_news(symbol: str, days: int = 3) -> list:
    """Recent headlines for the LLM-driven fundamental scan (see
    check_stock_opportunities agent-mode cron job, registered in
    deployment.yaml) -- deliberately just headlines/summaries, not full
    article text, to keep this a cheap, curated data source rather than
    letting the agent browse the open web for financial news.
    """
    import datetime

    to_date = datetime.date.today()
    from_date = to_date - datetime.timedelta(days=days)
    data = _get(
        "/company-news",
        {"symbol": symbol, "from": from_date.isoformat(), "to": to_date.isoformat()},
    )
    if not isinstance(data, list):
        raise FinnhubError(f"Finnhub company-news error for {symbol}: {data!r}")
    return [
        {"datetime": a.get("datetime"), "headline": a.get("headline"), "summary": a.get("summary")}
        for a in data
    ]


def get_basic_financials(symbol: str) -> dict:
    """Key valuation/growth metrics for the LLM's fundamental scan."""
    data = _get("/stock/metric", {"symbol": symbol, "metric": "all"})
    metric = data.get("metric")
    if metric is None:
        raise FinnhubError(f"Finnhub basic-financials error for {symbol}: {data!r}")
    return metric
