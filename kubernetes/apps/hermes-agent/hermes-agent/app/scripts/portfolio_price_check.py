#!/usr/bin/env python3
"""Daily portfolio price-check for hermes-agent's `portfolio-price-check` cron job.

Fetches current prices for every active `holdings` row from Yahoo
Finance's unofficial endpoints (no API key -- see the k8s-ops memory
entry on why this was chosen over Alpha Vantage/Finnhub/Stooq for this
specific mixed-currency, ISIN-heavy portfolio), converts everything to
EUR, records it in `price_history`, and prints a plain-text summary to
stdout.

Deliberately does NOT compose any buy/sell recommendation itself --
this script is deterministic data plumbing, run via `hermes cron
create --script` (agent mode, not --no-agent), so its stdout becomes
part of the LLM's prompt and the LLM writes the actual commentary. Any
single ticker's fetch failure is caught, logged to stdout, and
skipped -- one bad/delisted ticker must never abort the whole run.

Connects to Postgres via the standard PG* environment variables
(psycopg2.connect() with no arguments reads these automatically).
"""

import sys
import time
import urllib.error
import urllib.request
import json

import psycopg2
import psycopg2.extras

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
REQUEST_TIMEOUT = 10

# GBp ("pence") needs /100 before FX conversion -- Yahoo reports UK
# equities in pence, not pounds, confirmed live against BA.L/SHEL.L.
FX_TICKERS = {
    "GBP": "GBPEUR=X",
    "USD": "USDEUR=X",
    "HKD": "HKDEUR=X",
}


def fetch_yahoo(ticker: str) -> dict:
    req = urllib.request.Request(
        YAHOO_CHART_URL.format(ticker), headers={"User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        data = json.load(resp)
    result = data.get("chart", {}).get("result")
    if not result:
        err = data.get("chart", {}).get("error", {})
        raise ValueError(err.get("description") or "no chart result")
    meta = result[0]["meta"]
    price = meta.get("regularMarketPrice")
    currency = meta.get("currency")
    if price is None or currency is None:
        raise ValueError("missing price/currency in response")
    return {"price": float(price), "currency": currency}


def fetch_fx_rates() -> dict:
    """EUR value of 1 unit of each foreign currency. EUR itself is always 1.0."""
    rates = {"EUR": 1.0}
    for ccy, ticker in FX_TICKERS.items():
        try:
            rates[ccy] = fetch_yahoo(ticker)["price"]
        except Exception as e:  # noqa: BLE001
            print(f"WARN: could not fetch FX rate {ticker}: {e}")
    return rates


def to_eur(price: float, currency: str, fx_rates: dict) -> float | None:
    if currency == "EUR":
        return price
    if currency == "GBp":
        rate = fx_rates.get("GBP")
        return round(price / 100 * rate, 4) if rate else None
    rate = fx_rates.get(currency)
    return round(price * rate, 4) if rate else None


def main() -> int:
    conn = psycopg2.connect()
    conn.autocommit = True
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute(
        "SELECT isin, name, asset_class, ticker, quantity, entry_price_eur "
        "FROM holdings WHERE active = true AND ticker IS NOT NULL ORDER BY name"
    )
    holdings = cur.fetchall()
    if not holdings:
        print("No active holdings with a resolved ticker -- nothing to check.")
        return 0

    fx_rates = fetch_fx_rates()

    lines = []
    skipped = []
    for h in holdings:
        try:
            quote = fetch_yahoo(h["ticker"])
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError, TimeoutError) as e:
            skipped.append(f"{h['name']} ({h['ticker']}): {e}")
            continue

        price_native = quote["price"]
        currency_native = quote["currency"]
        price_eur = to_eur(price_native, currency_native, fx_rates)
        if price_eur is None:
            skipped.append(f"{h['name']} ({h['ticker']}): no FX rate for {currency_native}")
            continue

        cur.execute(
            "INSERT INTO price_history (isin, price_native, currency_native, price_eur) "
            "VALUES (%s, %s, %s, %s)",
            (h["isin"], price_native, currency_native, price_eur),
        )
        cur.execute(
            "UPDATE holdings SET last_price_native = %s, last_price_currency = %s, "
            "last_price_eur = %s, last_price_at = now(), updated_at = now() "
            "WHERE isin = %s",
            (price_native, currency_native, price_eur, h["isin"]),
        )

        # 7d/30d trend from price_history, if enough history has accumulated yet.
        cur.execute(
            "SELECT price_eur FROM price_history WHERE isin = %s AND "
            "fetched_at <= now() - interval '7 days' ORDER BY fetched_at DESC LIMIT 1",
            (h["isin"],),
        )
        row_7d = cur.fetchone()
        cur.execute(
            "SELECT price_eur FROM price_history WHERE isin = %s AND "
            "fetched_at <= now() - interval '30 days' ORDER BY fetched_at DESC LIMIT 1",
            (h["isin"],),
        )
        row_30d = cur.fetchone()

        entry = h["entry_price_eur"]
        pct_vs_entry = round((price_eur / float(entry) - 1) * 100, 1) if entry else None
        pct_7d = (
            round((price_eur / float(row_7d["price_eur"]) - 1) * 100, 1) if row_7d else None
        )
        pct_30d = (
            round((price_eur / float(row_30d["price_eur"]) - 1) * 100, 1) if row_30d else None
        )

        lines.append(
            f"{h['name']} [{h['asset_class']}] {h['ticker']}: {price_eur:.2f} EUR "
            f"(x{h['quantity']}) | vs. Einstieg: {pct_vs_entry:+.1f}% | "
            f"7T: {f'{pct_7d:+.1f}%' if pct_7d is not None else 'n/a'} | "
            f"30T: {f'{pct_30d:+.1f}%' if pct_30d is not None else 'n/a'}"
        )
        time.sleep(0.2)  # be a polite, unhurried caller -- this is a once-daily job

    cur.close()
    conn.close()

    print(f"Portfolio-Kurscheck ({len(lines)}/{len(holdings)} Positionen aktualisiert):")
    print()
    for line in lines:
        print(line)
    if skipped:
        print()
        print(f"Übersprungen ({len(skipped)}):")
        for s in skipped:
            print(f"  - {s}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
