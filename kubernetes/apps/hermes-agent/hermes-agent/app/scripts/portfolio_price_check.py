#!/usr/bin/env python3
"""Daily portfolio price-check for hermes-agent's `portfolio-price-check` cron job.

Fetches current prices for every active `holdings` row from Yahoo
Finance's unofficial endpoints (no API key -- see the k8s-ops memory
entry on why this was chosen over Alpha Vantage/Finnhub/Stooq for this
specific mixed-currency, ISIN-heavy portfolio), converts everything to
EUR, records it in `price_history`, and prints a plain-text summary to
stdout.

`holdings` covers two kinds of rows via `status`: 'owned' (real
positions, quantity/entry_price_eur set) and 'watching' (candidate new
investments the agent researched and inserted itself via its terminal
tool -- quantity/entry_price_eur left NULL). Both get the exact same
real price history; only the summary line format differs, since a
watching row has no quantity or entry price to compare against.

Also computes real risk stats per holding from `price_history` itself
(annualized volatility, max drawdown, geometric-vs-arithmetic
volatility drag, Sharpe/Sortino with an assumed 0% risk-free rate --
no external rate feed exists here, this is a deliberate simplification).
All of it is gated behind MIN_DATA_POINTS: below that, every stat
prints as "n/a" rather than a number computed from too little history
to mean anything. This is intentional -- the full set of metrics is
already wired in now so nothing needs restructuring later, but with
~40 holdings and a same-day rollout none of them have enough history
yet to safely influence a recommendation. Portfolio-level metrics that
need a return-covariance matrix across holdings (Markowitz variance,
Kelly position sizing) are deliberately NOT included: Kelly assumes a
repeated bet with a known edge (p/b), which doesn't exist for a
buy-and-hold stock/ETF portfolio, and a real covariance-matrix
computation needs months of overlapping history plus numpy -- neither
is worth scaffolding today with no way to validate it yet.

Deliberately does NOT compose any buy/sell recommendation itself --
this script is deterministic data plumbing, run via `hermes cron
create --script` (agent mode, not --no-agent), so its stdout becomes
part of the LLM's prompt and the LLM writes the actual commentary. Any
single ticker's fetch failure is caught, logged to stdout, and
skipped -- one bad/delisted ticker must never abort the whole run.

Connects to Postgres via the standard PG* environment variables
(psycopg2.connect() with no arguments reads these automatically).
"""

import math
import statistics
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

# ~3 Kalenderwochen taeglicher Kurspunkte -- darunter ist eine Std.-Abw.-
# /Sharpe-Schaetzung statistisch nicht belastbar, die Metrik bleibt "n/a"
# statt eine Zahl mit Schein-Praezision zu liefern. Ein Kurspunkt pro
# Kalendertag (nicht pro Handelstag): am Wochenende liefert Yahoo den
# unveraenderten letzten Schlusskurs, was die geschaetzte Volatilitaet
# leicht nach unten drueckt -- fuer diese grobe Heuristik tolerierbar.
MIN_DATA_POINTS = 20
TRADING_PERIODS_PER_YEAR = 365


def compute_risk_stats(price_points: list) -> dict | None:
    """price_points: chronologically ascending EUR closes for one ISIN."""
    n = len(price_points)
    if n < MIN_DATA_POINTS:
        return None

    returns = [
        price_points[i] / price_points[i - 1] - 1
        for i in range(1, n)
        if price_points[i - 1]
    ]
    mean_r = statistics.mean(returns)
    std_r = statistics.stdev(returns)

    total_return = price_points[-1] / price_points[0] - 1
    geo_annual = (1 + total_return) ** (TRADING_PERIODS_PER_YEAR / len(returns)) - 1
    arith_annual = (1 + mean_r) ** TRADING_PERIODS_PER_YEAR - 1

    peak = price_points[0]
    max_dd = 0.0
    for p in price_points:
        peak = max(peak, p)
        if peak:
            max_dd = min(max_dd, (p - peak) / peak)

    downside = [r for r in returns if r < 0]
    downside_std = statistics.stdev(downside) if len(downside) > 1 else None

    return {
        "vol_annual": std_r * math.sqrt(TRADING_PERIODS_PER_YEAR),
        "max_drawdown": max_dd,
        "vol_drag": arith_annual - geo_annual,
        "sharpe": (mean_r / std_r) * math.sqrt(TRADING_PERIODS_PER_YEAR) if std_r > 0 else None,
        "sortino": (
            (mean_r / downside_std) * math.sqrt(TRADING_PERIODS_PER_YEAR)
            if downside_std
            else None
        ),
    }


def format_risk_stats(stats: dict | None) -> str:
    if stats is None:
        return f"Statistik: n/a (< {MIN_DATA_POINTS} Kurspunkte)"
    parts = [
        f"Vol(ann.): {stats['vol_annual'] * 100:.1f}%",
        f"MaxDD: {stats['max_drawdown'] * 100:.1f}%",
        f"Drag: {stats['vol_drag'] * 100:+.1f}%",
    ]
    if stats["sharpe"] is not None:
        parts.append(f"Sharpe: {stats['sharpe']:.2f}")
    if stats["sortino"] is not None:
        parts.append(f"Sortino: {stats['sortino']:.2f}")
    return " | ".join(parts)


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
        "SELECT isin, name, asset_class, ticker, quantity, entry_price_eur, status, note "
        "FROM holdings WHERE active = true AND ticker IS NOT NULL ORDER BY status, name"
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

        pct_7d = (
            round((price_eur / float(row_7d["price_eur"]) - 1) * 100, 1) if row_7d else None
        )
        pct_30d = (
            round((price_eur / float(row_30d["price_eur"]) - 1) * 100, 1) if row_30d else None
        )
        trend = (
            f"7T: {f'{pct_7d:+.1f}%' if pct_7d is not None else 'n/a'} | "
            f"30T: {f'{pct_30d:+.1f}%' if pct_30d is not None else 'n/a'}"
        )

        cur.execute(
            "SELECT price_eur FROM price_history WHERE isin = %s ORDER BY fetched_at ASC",
            (h["isin"],),
        )
        price_points = [float(r["price_eur"]) for r in cur.fetchall()]
        stats_str = format_risk_stats(compute_risk_stats(price_points))

        if h["status"] == "watching":
            note = f" -- {h['note']}" if h["note"] else ""
            lines.append(
                f"[BEOBACHTUNG] {h['name']} [{h['asset_class']}] {h['ticker']}: "
                f"{price_eur:.2f} EUR | {trend} | {stats_str}{note}"
            )
        else:
            entry = h["entry_price_eur"]
            pct_vs_entry = round((price_eur / float(entry) - 1) * 100, 1) if entry else None
            lines.append(
                f"{h['name']} [{h['asset_class']}] {h['ticker']}: {price_eur:.2f} EUR "
                f"(x{h['quantity']}) | vs. Einstieg: {pct_vs_entry:+.1f}% | {trend} | {stats_str}"
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
