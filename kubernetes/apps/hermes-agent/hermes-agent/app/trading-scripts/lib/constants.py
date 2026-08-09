"""SAFETY-CRITICAL: the only place position-sizing and loss limits are
defined. Never move these into config.yaml or any path the agent's own
tools can WRITE -- deterministic code only, per explicit user requirement
(hard loss/position limits must never be prompt- or LLM-editable).

The personal capital/risk figures below are read from environment
variables (populated by a Kubernetes Secret -- see
externalsecret-postgres.yaml, 1Password item hermes-agent-trading) rather
than hardcoded literals, so they don't sit in a public git repo. This does
NOT weaken the safety guarantee: the agent's own RBAC is read-only (see
rbac.yaml), so it has no way to read or write that Secret/1Password item
either -- changing these values still requires a human editing 1Password
+ a pod restart, never a chat message. Same non-editability as a
hardcoded constant, just not publicly visible.
"""
import os
from decimal import Decimal, InvalidOperation


def _env_decimal(name: str) -> Decimal:
    raw = os.environ.get(name)
    if raw is None:
        raise RuntimeError(f"required trading risk env var {name} is not set")
    try:
        return Decimal(raw)
    except InvalidOperation:
        raise RuntimeError(f"env var {name}={raw!r} is not a valid decimal")


def _env_int(name: str) -> int:
    raw = os.environ.get(name)
    if raw is None:
        raise RuntimeError(f"required trading risk env var {name} is not set")
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(f"env var {name}={raw!r} is not a valid integer")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


CRYPTO_SYMBOLS = ("BTC", "ETH")

# Curated, human-reviewed pool the stock-scan agent job (see
# check_stock_opportunities cron job, deployment.yaml + soul_briefing.md) is
# allowed to pick from -- roughly S&P 100-sized, liquid US large caps. This
# is the actual safety boundary for stocks (same role ALLOWED_SYMBOLS used
# to play alone): the LLM decides WHICH of these look promising via
# news/fundamentals, but it can never propose a symbol outside this list --
# propose_buy (cli.py) rejects anything not in ALL_SYMBOLS just like today.
# Not auto-updated -- a human edits this list deliberately, same as any
# other code change.
STOCK_SYMBOLS = (
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "LLY", "V", "UNH",
    "XOM", "JPM", "WMT", "MA", "JNJ", "PG", "HD", "MRK", "COST", "ABBV",
    "CVX", "CRM", "BAC", "KO", "PEP", "AMD", "NFLX", "TMO", "ADBE", "MCD",
    "CSCO", "ABT", "LIN", "WFC", "DHR", "TXN", "PM", "ORCL", "DIS", "VZ",
    "INTU", "AMGN", "CAT", "IBM", "GE", "NOW", "QCOM", "UNP", "SPGI", "LOW",
    "HON", "AXP", "NKE", "BA", "PFE", "RTX", "GS", "T", "ELV", "SBUX",
    "DE", "BLK", "LMT", "MDT", "PLD", "GILD", "ADP", "MDLZ", "SYK", "ISRG",
    "CVS", "C", "TJX", "MO", "BKNG", "VRTX", "ADI", "MMC", "REGN", "CI",
    "ZTS", "SCHW", "PGR", "SO", "DUK", "APD", "CB", "PYPL", "TGT", "FI",
)

# International exposure via US-listed ADRs/dual-listings of major non-US
# companies -- NOT native local-exchange listings (Xetra, LSE, Tokyo, HKEX,
# ...). Every free-tier stock data API (Finnhub included) restricts real
# international exchange data to paid plans; these tickers sidestep that
# entirely by trading in USD on NYSE/Nasdaq like any other US stock, so the
# whole existing pipeline (Finnhub quotes, USD->EUR conversion via lib.fx,
# NYSE hours via lib.market_hours) applies unchanged -- no per-exchange
# currency or trading-calendar logic needed. Deliberately excludes ADRs
# that only trade OTC/pink-sheet (e.g. Samsung, Siemens, LVMH) -- those
# have thin liquidity and shakier quote quality on Finnhub's free tier.
INTERNATIONAL_ADR_SYMBOLS = (
    # Europe
    "SAP", "ASML", "NVO", "SHEL", "BP", "AZN", "GSK", "UL", "DEO", "BTI",
    "HSBC", "TTE", "SNY", "NVS", "UBS", "ING", "PHG", "STLA", "E",
    # Japan
    "TM", "SONY", "HMC", "MUFG", "SMFG", "MFG", "NMR", "CAJ", "TAK",
    # Greater China / Taiwan
    "TSM", "BABA", "JD", "PDD", "BIDU", "NTES", "TCOM", "LI", "NIO", "XPEV",
    # Korea
    "KB", "SHG", "PKX", "KT", "SKM",
    # India
    "INFY", "WIT", "HDB", "IBN", "TTM",
    # Australia / other
    "BHP",
)

# Combined stock pool -- both groups are equally US-listed-in-USD as far
# as the rest of the code is concerned (Finnhub quotes, lib.fx conversion,
# lib.market_hours), so callers that need "every tradeable stock" (cli.py's
# get_news/get_fundamentals choices, ASSET_CLASS below) use this rather
# than STOCK_SYMBOLS alone.
ALL_STOCK_SYMBOLS = STOCK_SYMBOLS + INTERNATIONAL_ADR_SYMBOLS

ALL_SYMBOLS = CRYPTO_SYMBOLS + ALL_STOCK_SYMBOLS

ASSET_CLASS = {symbol: "crypto" for symbol in CRYPTO_SYMBOLS}
ASSET_CLASS.update({symbol: "stock" for symbol in ALL_STOCK_SYMBOLS})

# Kraken public Ticker API: the pair name you *query* with does not match
# the key in the *response* (legacy X=crypto/Z=fiat asset-class prefixes).
KRAKEN_TICKER_URL = "https://api.kraken.com/0/public/Ticker"
KRAKEN_PAIR = {"BTC": "XBTEUR", "ETH": "ETHEUR"}
KRAKEN_RESULT_KEY = {"BTC": "XXBTZEUR", "ETH": "XETHZEUR"}

# Finnhub: primary price/candle/news/fundamentals source for stocks
# (lib/finnhub.py, routed via lib/prices.py) -- NOT used for crypto, see
# lib/finnhub.py's module docstring (its /crypto/candle needs a paid plan).
# Originally replaced Alpha Vantage as the crypto cross-check source too,
# but Alpha Vantage's free-tier daily quota (25 req/day) turned out to be
# shared with the user's other, unrelated use of the same API key and was
# permanently exhausted as a result -- see check_data_quality.py, which now
# uses lib/binance.py for that instead. Finnhub's free tier has no daily
# cap, only a 60-req/min rate limit, and this system's stock call volume is
# nowhere near that.
FINNHUB_BASE_URL = "https://finnhub.io/api/v1"

# Personal capital/risk figures -- kept out of the public repo, see module
# docstring above for why this is safe.
STARTING_CAPITAL_EUR = _env_decimal("TRADING_STARTING_CAPITAL_EUR")
MAX_POSITION_PCT = _env_decimal("TRADING_MAX_POSITION_PCT")
STOP_LOSS_PCT = _env_decimal("TRADING_STOP_LOSS_PCT")
PROFIT_TARGET_PCT = _env_decimal("TRADING_PROFIT_TARGET_PCT")
DAILY_CIRCUIT_BREAKER_PCT = _env_decimal("TRADING_DAILY_CIRCUIT_BREAKER_PCT")
PROPOSAL_EXPIRY_MINUTES = _env_int("TRADING_PROPOSAL_EXPIRY_MINUTES")

# Opt-in (default off, no 1Password change needed to keep current
# behaviour): when true, the deterministic crypto opportunity scanner
# (check_opportunities.py) auto-executes its own scan-sourced BUY proposals
# instead of waiting for a WhatsApp "yes" -- still fully bounded by every
# existing deterministic check (circuit breaker, data quality, Kelly-capped
# position size, available cash). Deliberately does NOT apply to the
# LLM-judgment stock-scan or manual propose_buy paths -- those keep
# requiring explicit human confirmation via cli.py's confirm_buy, since
# letting the LLM's own judgment auto-execute is exactly the risk the
# original human-in-the-loop design was built to avoid. To enable, add
# TRADING_AUTOPILOT_ENABLED=true to the hermes-agent-trading 1Password item.
TRADING_AUTOPILOT_ENABLED = _env_bool("TRADING_AUTOPILOT_ENABLED", default=False)

# Not personally sensitive -- generic technical config, fine to keep in code.
TIMEZONE = "Europe/Berlin"

# Cross-check (Kraken vs. Finnhub, crypto only): divergence beyond this
# flags data quality as bad and blocks new BUY proposals until a later
# check agrees again.
DATA_QUALITY_DIVERGENCE_PCT = Decimal("0.02")

# --- Phase 2: Kelly Criterion / EV scoring / feedback loop ---
# Methodology constants, not personal financial data -- plain code is fine
# (unlike the capital/risk-% figures above).

KRAKEN_OHLC_URL = "https://api.kraken.com/0/public/OHLC"

# Feedback loop: Bayesian-smoothed win rate from our own closed positions.
# A weak neutral prior (starts at 50/50) keeps early estimates from being
# wildly noisy before enough trade history exists.
FEEDBACK_PRIOR_WINS = Decimal("2")
FEEDBACK_PRIOR_LOSSES = Decimal("2")

# Kelly Criterion: fractional multiplier applied to the raw Kelly fraction
# (half-Kelly is standard practice to reduce variance from estimation
# error). The result is ALWAYS additionally clamped to
# [0, MAX_POSITION_PCT] -- Kelly can only shrink the position size below
# the existing hard cap above, never grow beyond it.
KELLY_FRACTION_MULTIPLIER = Decimal("0.5")

# Technical signal: simple mean-reversion heuristic over hourly candles.
# NOT a claim of real predictive edge -- a common, well-known indicator,
# appropriate to test cheaply in paper trading, nothing more.
SMA_PERIOD_HOURS = 20
MEAN_REVERSION_THRESHOLD_PCT = Decimal("0.03")
