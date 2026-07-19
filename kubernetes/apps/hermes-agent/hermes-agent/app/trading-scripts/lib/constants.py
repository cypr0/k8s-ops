"""SAFETY-CRITICAL: the only place position-sizing and loss limits are
defined. Never move these into config.yaml, an env var, or any path the
agent's own tools can edit -- deterministic code only, per explicit user
requirement (hard loss/position limits must never be prompt- or
LLM-editable). Changing any of these values requires a git commit + review
+ redeploy, not a chat message.
"""
from decimal import Decimal

ALLOWED_SYMBOLS = ("BTC", "ETH")

# Kraken public Ticker API: the pair name you *query* with does not match
# the key in the *response* (legacy X=crypto/Z=fiat asset-class prefixes).
KRAKEN_TICKER_URL = "https://api.kraken.com/0/public/Ticker"
KRAKEN_PAIR = {"BTC": "XBTEUR", "ETH": "ETHEUR"}
KRAKEN_RESULT_KEY = {"BTC": "XXBTZEUR", "ETH": "XETHZEUR"}

# Alpha Vantage: used only for the periodic cross-check, never the fast path.
ALPHAVANTAGE_URL = "https://www.alphavantage.co/query"
ALPHAVANTAGE_SYMBOL = {"BTC": "BTC", "ETH": "ETH"}

STARTING_CAPITAL_EUR = Decimal("300.00")
MAX_POSITION_PCT = Decimal("0.10")
STOP_LOSS_PCT = Decimal("-0.05")
PROFIT_TARGET_PCT = Decimal("0.15")
DAILY_CIRCUIT_BREAKER_PCT = Decimal("-0.03")
PROPOSAL_EXPIRY_MINUTES = 30
TIMEZONE = "Europe/Berlin"

# Cross-check (Kraken vs. Alpha Vantage): divergence beyond this flags data
# quality as bad and blocks new BUY proposals until a later check agrees again.
DATA_QUALITY_DIVERGENCE_PCT = Decimal("0.02")
