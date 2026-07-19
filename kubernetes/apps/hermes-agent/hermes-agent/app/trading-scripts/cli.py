#!/opt/hermes/.venv/bin/python3
"""Agent-invoked trading CLI -- called via Hermes' local "terminal" tool
mid-conversation. Every subcommand prints a short, WhatsApp-ready summary to
stdout that the agent is expected to relay to the user near-verbatim.

HARD RULE (see /opt/data/SOUL.md briefing): confirm_buy and
confirm_sell_target must only ever be run after the user has just replied
with an explicit affirmative in this same conversation. propose_buy,
get_price, and get_portfolio may be run freely.

All limits are re-validated fresh inside confirm_buy/confirm_sell_target --
a proposal is a suggestion, not a pre-authorization. Exit code is always 0
on a *handled* outcome (including rejections) so the agent sees clean
stdout instead of a stack trace; unexpected errors exit non-zero.
"""
import argparse
import sys
from decimal import Decimal, InvalidOperation

# Scoped to this one-off process only -- deliberately not a global
# PYTHONPATH on the main container, which would also apply to the
# long-running `hermes gateway run` process and risk shadowing Hermes' own
# internal dependency versions.
sys.path.insert(0, "/opt/data/tools/pip")

from lib import constants as C
from lib import db
from lib import kraken
from lib import stats


def cmd_get_price(args: argparse.Namespace) -> None:
    price = kraken.get_price(args.symbol)
    print(f"{args.symbol}: EUR {price}")


def cmd_get_portfolio(args: argparse.Namespace) -> None:
    conn = db.connect()
    try:
        with conn, conn.cursor() as cur:
            cash = db.get_portfolio_cash(cur)
            positions = db.get_open_positions(cur)
            quality = db.get_data_quality_status(cur)
            breaker_tripped = db.is_circuit_breaker_tripped_today(cur)

            total_value = cash
            lines = [f"Cash: EUR {cash}"]
            if positions:
                lines.append("Open positions:")
                for pos in positions:
                    price = kraken.get_price(pos["symbol"])
                    value = pos["quantity"] * price
                    total_value += value
                    change_pct = (price - pos["entry_price_eur"]) / pos["entry_price_eur"]
                    lines.append(
                        f"  #{pos['id']} {pos['symbol']}: {pos['quantity']} @ entry "
                        f"EUR {pos['entry_price_eur']}, now EUR {price} ({change_pct:+.1%}), "
                        f"value EUR {value:.2f} "
                        f"[stop-loss EUR {pos['stop_loss_price']}, target EUR {pos['profit_target_price']}]"
                    )
            else:
                lines.append("No open positions.")
            lines.append(f"Total portfolio value: EUR {total_value:.2f}")
            if breaker_tripped:
                lines.append("\U0001F534 Daily circuit breaker is TRIPPED -- no new buys until tomorrow.")
            if not quality["ok"]:
                lines.append(f"⚠️ Data quality flagged: {quality['flagged_reason']}")
            print("\n".join(lines))
    finally:
        conn.close()


def cmd_get_stats(args: argparse.Namespace) -> None:
    conn = db.connect()
    try:
        with conn, conn.cursor() as cur:
            lines = []
            for symbol in C.ALLOWED_SYMBOLS:
                s = stats.get_win_rate(cur, symbol)
                kelly = stats.kelly_fraction(s["win_rate"])
                capped = min(kelly, C.MAX_POSITION_PCT)
                lines.append(
                    f"{symbol}: {s['wins']}W/{s['losses']}L closed "
                    f"({s['total_closed']} total), smoothed win rate "
                    f"{s['win_rate']:.1%}, Kelly fraction {kelly:.1%} "
                    f"(capped at {capped:.1%} of portfolio, hard ceiling "
                    f"{C.MAX_POSITION_PCT:.0%})"
                )
            print("\n".join(lines))
    finally:
        conn.close()


def cmd_propose_buy(args: argparse.Namespace) -> None:
    if args.symbol not in C.ALLOWED_SYMBOLS:
        print(f"Rejected: {args.symbol} is not tradeable. Allowed: {', '.join(C.ALLOWED_SYMBOLS)}")
        return
    try:
        amount = Decimal(args.amount_eur)
    except InvalidOperation:
        print(f"Rejected: {args.amount_eur!r} is not a valid EUR amount.")
        return
    if amount <= 0:
        print("Rejected: amount must be positive.")
        return

    conn = db.connect()
    try:
        with conn, conn.cursor() as cur:
            # Max position size is a per-*symbol* cap -- a second manual buy
            # while one's already open would silently stack past it
            # (the automated scanner already avoided this, this closes the
            # same gap for manually requested buys).
            if db.get_open_positions(cur, args.symbol):
                print(
                    f"Rejected: you already have an open {args.symbol} position. "
                    f"Only one open position per symbol at a time -- close it "
                    f"first (stop-loss/profit-target) before opening another."
                )
                return
            if db.is_circuit_breaker_tripped_today(cur):
                print(
                    "Rejected: daily circuit breaker is tripped (portfolio down "
                    f"{C.DAILY_CIRCUIT_BREAKER_PCT:.0%}+ today) -- no new buys allowed "
                    "until tomorrow."
                )
                return
            quality = db.get_data_quality_status(cur)
            if not quality["ok"]:
                print(
                    f"Rejected: market data quality is currently flagged "
                    f"({quality['flagged_reason']}) -- new buys are paused until it clears."
                )
                return

            price = kraken.get_price(args.symbol)
            total_value = db.compute_total_portfolio_value(cur, kraken)
            # Kelly-derived cap: can only be <= MAX_POSITION_PCT, never
            # more -- see lib/stats.py. When there's no trade history yet,
            # this starts near the flat MAX_POSITION_PCT (neutral prior)
            # and tightens or loosens as our own win rate becomes clearer.
            position_pct = stats.capped_kelly_position_pct(cur, args.symbol)
            max_position = total_value * position_pct
            if amount > max_position:
                print(
                    f"Rejected: EUR {amount} exceeds the current max position size "
                    f"({position_pct:.1%} of EUR {total_value:.2f} = "
                    f"EUR {max_position:.2f}, Kelly-adjusted from our own trade history)."
                )
                return
            cash = db.get_portfolio_cash(cur)
            if amount > cash:
                print(f"Rejected: EUR {amount} exceeds available cash (EUR {cash}).")
                return

            proposal_id = db.create_buy_proposal(cur, args.symbol, amount, price, source="user")
            print(
                f"Proposal: BUY EUR {amount} of {args.symbol} at ~EUR {price} "
                f"(expires in {C.PROPOSAL_EXPIRY_MINUTES} min).\n"
                f"Waiting for your explicit confirmation. If you confirm, I'll run:\n"
                f"  confirm_buy {proposal_id}"
            )
    finally:
        conn.close()


def cmd_confirm_buy(args: argparse.Namespace) -> None:
    conn = db.connect()
    try:
        with conn, conn.cursor() as cur:
            proposal = db.get_pending_proposal(cur, args.proposal_id)
            if proposal is None:
                print("Cannot confirm: proposal not found, already handled, or expired. Propose again if you still want in.")
                return
            if proposal["proposal_type"] != "BUY":
                print("Cannot confirm: this proposal is not a BUY proposal.")
                return

            # Re-validate everything fresh -- price/portfolio may have moved
            # since the proposal was created.
            if db.get_open_positions(cur, proposal["symbol"]):
                db.reject_proposal(cur, proposal["id"], "a position in this symbol was opened since proposal")
                print(
                    f"Cancelled: you already opened a {proposal['symbol']} position "
                    f"since this proposal was made (e.g. from confirming another "
                    f"pending buy first) -- only one open position per symbol at a time."
                )
                return
            if db.is_circuit_breaker_tripped_today(cur):
                db.reject_proposal(cur, proposal["id"], "circuit breaker tripped since proposal")
                print("Cancelled: the daily circuit breaker tripped since you asked -- buy cancelled.")
                return
            quality = db.get_data_quality_status(cur)
            if not quality["ok"]:
                db.reject_proposal(cur, proposal["id"], "data quality flagged since proposal")
                print("Cancelled: market data quality became flagged since you asked -- buy cancelled.")
                return

            price = kraken.get_price(proposal["symbol"])
            total_value = db.compute_total_portfolio_value(cur, kraken)
            # Re-check the Kelly-derived cap fresh -- our own win rate may
            # have shifted (more trades closed) since the proposal was made.
            position_pct = stats.capped_kelly_position_pct(cur, proposal["symbol"])
            max_position = total_value * position_pct
            if proposal["amount_eur"] > max_position:
                db.reject_proposal(cur, proposal["id"], "exceeds max position size at confirm-time")
                print(
                    f"Cancelled: EUR {proposal['amount_eur']} now exceeds the max position "
                    f"size (EUR {max_position:.2f}, {position_pct:.1%} of portfolio) -- "
                    f"portfolio value or our own trade history moved. Propose a smaller amount."
                )
                return
            cash = db.get_portfolio_cash(cur)
            if proposal["amount_eur"] > cash:
                db.reject_proposal(cur, proposal["id"], "insufficient cash at confirm-time")
                print(f"Cancelled: insufficient cash (EUR {cash}) to cover EUR {proposal['amount_eur']}.")
                return

            position_id = db.execute_buy(cur, proposal, price)
            print(
                f"✅ Bought EUR {proposal['amount_eur']} of {proposal['symbol']} at "
                f"EUR {price}. Position #{position_id} opened "
                f"(stop-loss and profit-target are now being watched automatically)."
            )
    finally:
        conn.close()


def cmd_confirm_sell_target(args: argparse.Namespace) -> None:
    conn = db.connect()
    try:
        with conn, conn.cursor() as cur:
            proposal = db.get_pending_proposal(cur, args.proposal_id)
            if proposal is None:
                print("Cannot confirm: proposal not found, already handled, or expired.")
                return
            if proposal["proposal_type"] != "SELL_PROFIT_TARGET":
                print("Cannot confirm: this proposal is not a profit-target sell proposal.")
                return

            position = db.get_position(cur, proposal["position_id"])
            if position is None or position["status"] != "OPEN":
                db.reject_proposal(cur, proposal["id"], "position already closed")
                print("Cancelled: that position is no longer open (already closed, e.g. by a stop-loss).")
                return

            price = kraken.get_price(proposal["symbol"])
            if price < position["profit_target_price"]:
                db.reject_proposal(cur, proposal["id"], "price fell back below target at confirm-time")
                print(
                    f"Cancelled: {proposal['symbol']} price (EUR {price}) fell back below "
                    f"the profit target (EUR {position['profit_target_price']}) -- position left open."
                )
                return

            db.execute_sell_target(cur, proposal, position, price)
            print(
                f"✅ Closed position #{position['id']} ({proposal['symbol']}) at EUR {price} "
                f"-- profit target reached."
            )
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Crypto paper-trading CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("get_price")
    p.add_argument("symbol", choices=C.ALLOWED_SYMBOLS)
    p.set_defaults(func=cmd_get_price)

    p = sub.add_parser("get_portfolio")
    p.set_defaults(func=cmd_get_portfolio)

    p = sub.add_parser("get_stats")
    p.set_defaults(func=cmd_get_stats)

    p = sub.add_parser("propose_buy")
    p.add_argument("symbol", choices=C.ALLOWED_SYMBOLS)
    p.add_argument("amount_eur")
    p.set_defaults(func=cmd_propose_buy)

    p = sub.add_parser("confirm_buy")
    p.add_argument("proposal_id")
    p.set_defaults(func=cmd_confirm_buy)

    p = sub.add_parser("confirm_sell_target")
    p.add_argument("proposal_id")
    p.set_defaults(func=cmd_confirm_sell_target)

    args = parser.parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
