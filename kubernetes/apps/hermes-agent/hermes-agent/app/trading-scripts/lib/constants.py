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


ALLOWED_SYMBOLS = ("BTC", "ETH")

# Kraken public Ticker API: the pair name you *query* with does not match
# the key in the *response* (legacy X=crypto/Z=fiat asset-class prefixes).
KRAKEN_TICKER_URL = "https://api.kraken.com/0/public/Ticker"
KRAKEN_PAIR = {"BTC": "XBTEUR", "ETH": "ETHEUR"}
KRAKEN_RESULT_KEY = {"BTC": "XXBTZEUR", "ETH": "XETHZEUR"}

# Alpha Vantage: used only for the periodic cross-check, never the fast path.
ALPHAVANTAGE_URL = "https://www.alphavantage.co/query"
ALPHAVANTAGE_SYMBOL = {"BTC": "BTC", "ETH": "ETH"}

# Personal capital/risk figures -- kept out of the public repo, see module
# docstring above for why this is safe.
STARTING_CAPITAL_EUR = _env_decimal("TRADING_STARTING_CAPITAL_EUR")
MAX_POSITION_PCT = _env_decimal("TRADING_MAX_POSITION_PCT")
STOP_LOSS_PCT = _env_decimal("TRADING_STOP_LOSS_PCT")
PROFIT_TARGET_PCT = _env_decimal("TRADING_PROFIT_TARGET_PCT")
DAILY_CIRCUIT_BREAKER_PCT = _env_decimal("TRADING_DAILY_CIRCUIT_BREAKER_PCT")
PROPOSAL_EXPIRY_MINUTES = _env_int("TRADING_PROPOSAL_EXPIRY_MINUTES")

# Not personally sensitive -- generic technical config, fine to keep in code.
TIMEZONE = "Europe/Berlin"

# Cross-check (Kraken vs. Alpha Vantage): divergence beyond this flags data
# quality as bad and blocks new BUY proposals until a later check agrees again.
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
