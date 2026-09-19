"""Command-line surface.

The workflow this implements is deliberately two-stage:

    okxbot scan BTC-USDT --json snap.json    # numbers out, for analysis
    <analysis happens elsewhere, producing plan.json>
    okxbot submit plan.json --live           # numbers in, after a human says yes

Nothing here calls a language model. The analysis step is a human pasting a
snapshot into a conversation and pasting a plan back out. That keeps the model
out of the execution path, which is the whole architecture in one sentence.

Safety is layered:

* ``--live`` is required to transmit anything. Without it every command is a
  dry run that prints exactly what would have been sent.
* ``OKX_LIVE_TRADING=i-understand-the-risk`` is required to leave the demo
  endpoint. Without it, ``--live`` trades against OKX's paper environment.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import termios
from pathlib import Path

from .config import load_config
from .errors import OkxBotError, PlanError
from .executor import Executor
from .notify import notify
from .okx.rest import OkxRest
from .plan import SCHEMA_HINT, TradePlan
from .risk import AccountState, RiskGate
from .snapshot import build_snapshot, render_text, to_json
from .store import Store

log = logging.getLogger("okxbot")


def _build(args, require_credentials: bool = True):
    config = load_config(args.config, require_credentials=require_credentials)
    if args.db:
        config.db_path = args.db
    rest = OkxRest(creds=config.credentials, base_url=config.base_url,
                   read_only=config.read_only)
    store = Store(config.db_path)
    return config, rest, store


def _account_state(rest, config, plan) -> AccountState:
    balances = rest.balances()
    quote = balances.get(config.risk.quote_ccy, {})
    base_ccy = plan.inst_id.split("-")[0]
    base = balances.get(base_ccy, {})

    # Equity is the quote balance plus anything already sitting in whitelisted
    # base assets, so percentage-based sizing does not shrink just because the
    # account happens to be holding coin rather than stablecoin.
    equity = float(quote.get("eq", 0.0))
    for ccy, detail in balances.items():
        if ccy != config.risk.quote_ccy:
            equity += float(detail.get("eq_usd", 0.0))

    return AccountState(
        equity_quote=equity,
        available_quote=float(quote.get("avail", 0.0)),
        available_base=float(base.get("avail", 0.0)),
    )


def _refuse_write_if_read_only(config, action: str) -> bool:
    if not config.read_only:
        return False
    print(
        f"refusing to {action}: OKX_READ_ONLY is set.\n"
        "  Read-only is for verifying credentials and inspecting an account "
        "without any risk of a write.\n"
        "  Unset it to place orders.",
        file=sys.stderr,
    )
    return True


def _drain_stdin() -> None:
    """Discard input typed or pasted before the prompt appeared.

    Without this, pasting a block of commands means the line *after*
    ``submit --live`` is waiting in the terminal buffer when the confirmation
    prompt opens, and gets consumed as the answer. Usually that aborts the
    submission harmlessly. It does not have to: a pasted block whose next line
    happened to be the plan id would confirm a real-money order that nobody
    agreed to. The answer must be typed after the question is asked.
    """
    try:
        if sys.stdin.isatty():
            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except (termios.error, ValueError, OSError):
        pass  # not a terminal, or no tty to flush; the prompt still works


def _confirm(prompt: str, expected: str | None = None) -> bool:
    """Ask before spending money.

    For real-money submissions the user must type the plan id, not 'y'. Muscle
    memory can produce a 'y'; it cannot produce a twelve-character hex string
    by accident.
    """
    _drain_stdin()
    try:
        answer = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer == expected if expected else answer.lower() in ("y", "yes")


# --------------------------------------------------------------------- commands


def cmd_schema(args) -> int:
    print(SCHEMA_HINT)
    return 0


def cmd_scan(args) -> int:
    config, rest, store = _build(args, require_credentials=False)
    timeframes = tuple(args.timeframes.split(",")) if args.timeframes else config.timeframes

    snapshot = build_snapshot(rest, args.inst_id, timeframes=timeframes, limit=config.candle_limit)

    if args.json:
        Path(args.json).write_text(to_json(snapshot), encoding="utf-8")
        print(f"snapshot written to {args.json}")
    if args.raw:
        print(to_json(snapshot))
    else:
        print(render_text(snapshot))
    store.log_event("scan", args.inst_id)
    return 0


def cmd_validate(args) -> int:
    plan = TradePlan.from_json(Path(args.plan).read_text(encoding="utf-8"))
    print(plan.summary())
    print("\nplan is structurally valid (risk limits are checked at submit time)")
    return 0


def cmd_submit(args) -> int:
    config, rest, store = _build(args)
    plan = TradePlan.from_json(Path(args.plan).read_text(encoding="utf-8"))
    store.save_plan(plan, status="draft")

    spec = rest.instrument(plan.inst_id)
    ticker = rest.ticker(plan.inst_id)
    last_price = float(ticker.get("last") or 0)
    account = _account_state(rest, config, plan)

    gate = RiskGate(config.risk, store)
    decision = gate.evaluate(plan, account, last_price)

    print(plan.summary())
    print()
    print(
        f"account: equity {account.equity_quote:.2f} {config.risk.quote_ccy}, "
        f"available {account.available_quote:.2f}, last price {last_price:g}"
    )
    print(decision.render())

    if not decision.approved:
        store.set_plan_status(plan.plan_id, "rejected")
        store.log_event("risk_rejected", "; ".join(decision.reasons), plan.plan_id)
        return 2

    executor = Executor(rest, store, quote_ccy=config.risk.quote_ccy)
    intent = executor.build_intent(plan, decision, spec)
    print()
    print(intent.render())

    live = args.live
    if live and _refuse_write_if_read_only(config, "submit an order"):
        return 3
    real_money = live and not config.simulated
    print()
    if not live:
        print("DRY RUN -- nothing was sent. Re-run with --live to submit.")
        executor.submit_entry(intent, dry_run=True)
        return 0

    env_label = "REAL MONEY" if real_money else "demo (paper) trading"
    print(f"target environment: {env_label}")
    if real_money:
        ok = _confirm(f"Type the plan id ({plan.plan_id}) to submit for real: ", expected=plan.plan_id)
    else:
        ok = args.yes or _confirm("Submit to the demo environment? [y/N] ")

    if not ok:
        print("aborted; nothing was sent")
        store.log_event("submit_aborted", "operator declined", plan.plan_id)
        return 1

    result = executor.submit_entry(intent, dry_run=False)
    notify(
        f"entry submitted: {plan.inst_id} {plan.side}",
        f"plan {plan.plan_id}  size {intent.entry_size}  ordId {result.get('ordId')}\n"
        f"run 'okxbot sync {plan.plan_id} --live' once it fills to attach TP/SL",
    )
    return 0


def cmd_sync(args) -> int:
    """Check entries for fills and attach exchange-side protection.

    This is the command to put on a short cron once an entry is resting.
    """
    config, rest, store = _build(args)
    if args.live and _refuse_write_if_read_only(config, "place protective orders"):
        return 3
    rows = store.open_plans() if not args.plan_id else [store.get_plan_row(args.plan_id)]
    rows = [r for r in rows if r is not None]
    if not rows:
        print("no open plans")
        return 0

    executor = Executor(rest, store, quote_ccy=config.risk.quote_ccy)
    exit_code = 0

    for row in rows:
        plan = TradePlan.from_json(row["payload"])
        entry_id = plan.client_id("e")
        try:
            order = rest.order(plan.inst_id, cl_ord_id=entry_id)
        except OkxBotError as exc:
            print(f"{plan.plan_id}: could not read entry order -- {exc}")
            exit_code = 1
            continue

        state = order.get("state", "unknown")
        filled = order.get("accFillSz") or "0"
        print(f"{plan.plan_id} {plan.inst_id}: entry is {state}, filled {filled}")

        if state == "canceled":
            store.set_plan_status(plan.plan_id, "canceled")
            continue
        if state == "live":
            continue
        if state == "partially_filled" and not args.partial:
            print("  partially filled; re-run with --partial to protect the filled portion")
            continue
        if state not in ("filled", "partially_filled"):
            continue
        if float(filled) <= 0:
            continue

        spec = rest.instrument(plan.inst_id)
        base_ccy = plan.inst_id.split("-")[0]

        # Protect what the account actually holds, not what was ordered. Fees
        # on a spot buy come out of the base currency, and the balance is the
        # final word on what can be sold.
        protectable = executor.protectable_size(order, base_ccy)
        try:
            available = float(rest.balances(base_ccy).get(base_ccy, {}).get("avail", 0.0))
            if available < protectable:
                print(f"  capping protection at the {available:.8f} {base_ccy} actually available")
                protectable = available
        except OkxBotError as exc:
            print(f"  could not read the {base_ccy} balance ({exc}); using the fee-adjusted fill")

        if protectable < float(filled):
            print(f"  protecting {protectable:.8f} of {filled} filled (fees are paid in {base_ccy})")

        from .risk import RiskDecision  # local import: only needed on this path

        intent = executor.build_intent(
            plan, RiskDecision(approved=True, base_size=protectable), spec
        )
        placed = executor.place_protection(intent, spec.size(protectable), dry_run=not args.live)
        if args.live and placed:
            notify(
                f"protection live: {plan.inst_id}",
                f"plan {plan.plan_id}: {len(placed)} exit order(s) resting at the exchange",
            )
        elif not args.live:
            print("  DRY RUN -- re-run with --live to place protection")
    return exit_code


def cmd_status(args) -> int:
    config, rest, store = _build(args)
    print(f"environment: {config.environment_label}")
    print(f"database:    {config.db_path}")

    realized = store.realized_today()
    limit = config.risk.max_daily_loss_quote
    print(
        f"realised (rolling 24h): {realized:+.2f} {config.risk.quote_ccy}"
        f"   kill switch at {-abs(limit):.2f}"
        + ("   [TRIPPED]" if realized <= -abs(limit) else "")
    )

    try:
        balances = rest.balances()
        interesting = {k: v for k, v in balances.items() if v["eq"] > 0}
        print("balances: " + (", ".join(f"{k} {v['avail']:g}" for k, v in interesting.items()) or "none"))
    except OkxBotError as exc:
        print(f"balances unavailable: {exc}")

    rows = store.open_plans()
    print(f"\nopen plans ({len(rows)}/{config.risk.max_open_plans}):")
    for row in rows:
        plan = TradePlan.from_json(row["payload"])
        print(f"  {row['status']:<13} {plan.plan_id} {plan.inst_id} {plan.side} "
              f"entry {plan.entry_price:g} stop {plan.stop_price:g}")
        for order in store.orders_for_plan(plan.plan_id):
            print(f"      {order['role']:<7} {order['status']:<10} sz={order['sz']} "
                  f"{'algo=' + order['algo_id'] if order['algo_id'] else 'ord=' + (order['ord_id'] or '-')}")

    if args.events:
        print("\nrecent events:")
        for event in store.recent_events(args.events):
            detail = (event["detail"] or "").splitlines()[:1]
            print(f"  {event['ts'][:19]} {event['kind']:<20} {detail[0] if detail else ''}")
    return 0


def cmd_cancel(args) -> int:
    config, rest, store = _build(args)
    if args.live and _refuse_write_if_read_only(config, "cancel orders"):
        return 3
    row = store.get_plan_row(args.plan_id)
    if row is None:
        print(f"unknown plan {args.plan_id}")
        return 1
    plan = TradePlan.from_json(row["payload"])

    if not args.live:
        print(f"DRY RUN -- would cancel the entry and every algo order for {plan.plan_id}")
        return 0

    try:
        rest.cancel_order(plan.inst_id, cl_ord_id=plan.client_id("e"))
        print("entry cancelled")
    except OkxBotError as exc:
        print(f"entry not cancelled: {exc}")

    algos = [
        {"instId": plan.inst_id, "algoId": o["algo_id"]}
        for o in store.orders_for_plan(plan.plan_id)
        if o["algo_id"]
    ]
    if algos:
        try:
            rest.cancel_algos(algos)
            print(f"cancelled {len(algos)} algo order(s)")
        except OkxBotError as exc:
            print(f"algo cancellation failed: {exc}")

    store.set_plan_status(plan.plan_id, "canceled")
    store.log_event("canceled", "operator cancelled", plan.plan_id)
    return 0


def cmd_close(args) -> int:
    """Settle a finished plan and book its realised P&L.

    P&L is reconstructed from OKX fills matched on this plan's client-order-id
    prefix. Fees denominated in the base currency (which is how OKX charges a
    spot buy) are *not* netted here -- they show up as a slightly smaller base
    quantity on the sell side instead, so the number is close but not exact.
    """
    config, rest, store = _build(args)
    row = store.get_plan_row(args.plan_id)
    if row is None:
        print(f"unknown plan {args.plan_id}")
        return 1
    plan = TradePlan.from_json(row["payload"])
    prefix = f"p{plan.plan_id}"

    fills = [f for f in rest.fills(plan.inst_id, limit=100) if (f.get("clOrdId") or "").startswith(prefix)]
    if not fills:
        print(f"no fills found for {plan.plan_id}; nothing to book")
        return 1

    realized = 0.0
    for fill in fills:
        notional = float(fill["fillSz"]) * float(fill["fillPx"])
        realized += notional if fill["side"] == "sell" else -notional
        if fill.get("feeCcy") == config.risk.quote_ccy:
            realized += float(fill.get("fee") or 0.0)  # OKX reports fees as negative

    store.record_realized(plan.inst_id, realized, plan.plan_id)
    store.set_plan_status(plan.plan_id, "closed")
    store.log_event("closed", f"realised {realized:+.2f}", plan.plan_id)
    print(f"{plan.plan_id} closed over {len(fills)} fill(s): {realized:+.2f} {config.risk.quote_ccy}")
    return 0


# ----------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="okxbot", description=__doc__.splitlines()[0])
    parser.add_argument("--config", help="path to config.yaml (default: ./config.yaml)")
    parser.add_argument("--db", help="override the SQLite journal path")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("schema", help="print the TradePlan JSON schema")
    p.set_defaults(func=cmd_schema)

    p = sub.add_parser("scan", help="build a market snapshot for analysis")
    p.add_argument("inst_id", help="e.g. BTC-USDT")
    p.add_argument("--timeframes", help="comma separated, e.g. 1H,4H,1D")
    p.add_argument("--json", help="also write the full snapshot to this path")
    p.add_argument("--raw", action="store_true", help="print JSON instead of the text summary")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("validate", help="check a plan file without touching the network")
    p.add_argument("plan")
    p.set_defaults(func=cmd_validate)

    # Same code path as `submit` with --live withheld. It exists as its own verb
    # so a permission policy can separate "show me what would happen" from
    # "spend money" -- a distinction a flag buried mid-command cannot express.
    p = sub.add_parser("preview", help="risk-check a plan and print the orders, sending nothing")
    p.add_argument("plan")
    p.set_defaults(func=cmd_submit, live=False, yes=False)

    p = sub.add_parser("submit", help="risk-check a plan and place its entry order")
    p.add_argument("plan")
    p.add_argument("--live", action="store_true", help="actually transmit (default is a dry run)")
    p.add_argument("--yes", action="store_true", help="skip the prompt (demo environment only)")
    p.set_defaults(func=cmd_submit)

    p = sub.add_parser("sync", help="attach TP/SL to entries that have filled")
    p.add_argument("plan_id", nargs="?")
    p.add_argument("--live", action="store_true")
    p.add_argument("--partial", action="store_true", help="protect a partially filled entry")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("status", help="open plans, orders, balances, kill-switch state")
    p.add_argument("--events", type=int, default=0, metavar="N", help="also show the last N events")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("cancel", help="cancel a plan's entry and protective orders")
    p.add_argument("plan_id")
    p.add_argument("--live", action="store_true")
    p.set_defaults(func=cmd_cancel)

    p = sub.add_parser("close", help="book realised P&L for a finished plan")
    p.add_argument("plan_id")
    p.set_defaults(func=cmd_close)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
    )
    try:
        return args.func(args)
    except PlanError as exc:
        print(f"plan rejected: {exc}", file=sys.stderr)
        return 2
    except OkxBotError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"file not found: {exc.filename}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
