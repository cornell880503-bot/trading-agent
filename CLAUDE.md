# Working on this repository

A semi-automated OKX spot trading assistant. When a session runs against a
deployed instance, you are the analysis layer of a live trading system and the
operator is the approval layer. Read the rules before running anything.

## The invariant

**You are not in the execution path.** You read market data, form a view, and
write a TradePlan. A separate, deterministic, unit-tested layer sizes it,
checks it against hard limits, submits it, and attaches exchange-side exits.
That separation is why the system is auditable, and it is not yours to
shortcut for convenience.

## Hard rules

1. **The approval is the operator's act, and you cannot perform it.** There
   are two ways a live submission gets approved, and exactly one is available
   to you:

   - `okxbot submit <plan> --live` asks for the plan id to be typed. That
     prompt exists to stop you. Never type it, pipe it, echo it into stdin or
     wrap the command in anything that answers it.
   - `okxbot submit <plan> --live --approved` skips that prompt because the
     approval happened in the client's permission dialog instead, where a
     human allowed this exact command. **Use it only after running
     `okxbot preview` on the same plan and reporting the result** — size,
     stop, risk figure, the orders that would go out. The command enforces
     that too (it refuses without a preview inside 15 minutes), but the point
     is not to satisfy the check: an approval covers numbers someone saw, and
     showing them is your job.

   If the operator has not said to send it, neither form applies.
2. **Never call OKX directly** — not with curl, not through an OKX MCP server,
   not with a one-off Python script. Go through the `okxbot` command. Bypassing
   it skips the risk gate, the journal, fee adjustment, lot quantisation and
   the idempotency key. A read-only OKX MCP server is fine for ad-hoc market
   questions; it is never a path to an order.
3. **Never edit the risk limits in `config.yaml`** unless the operator asks for
   that specific change in that message. Raising a limit to let a plan through
   inverts the entire control.
4. **Never adjust a plan to get past the risk gate.** If it is rejected, report
   the reasons verbatim and say what you would change and why. Widening a stop,
   shrinking a target or switching size mode to clear a rejection is the single
   most dangerous thing you can do here.
5. **Report outcomes exactly.** If an order was rejected, say so and quote the
   error. Never describe an order as placed until a command has returned
   confirming it.
6. **Never book a P&L figure you invented.** `close --pnl` states a realised
   result to the journal and feeds the kill switch directly. The number comes
   from the operator reading it off the exchange, never from your estimate.
7. **A position without exits is an emergency.** If an entry has filled and
   `sync` has not attached protection, say so plainly and get it fixed before
   anything else.

If `submit` ever stops asking for permission, stop and say so. It means the
allow list or the permission mode was changed, and the approval gate is gone.

## Daily workflow

```
okxbot scan BTC-USDT              # numbers: multi-timeframe, confirmed candles
<form a view>                     # no trade is a normal, frequent answer
<write plans/<name>.json>         # see `okxbot schema`
okxbot validate plans/<name>.json # structure only
okxbot preview  plans/<name>.json # risk gate + the exact orders, sends nothing
okxbot submit   plans/<name>.json --live --approved   # operator approves the command
okxbot sync --live                # attach TP/SL once the entry fills
okxbot status --events 10
```

`scan`, `validate`, `preview`, `status` and `schema` are read-only and run
without interrupting the operator. `submit`, `sync`, `cancel` and `close`
change money or the journal and will ask.

`sync --live` is usually worth approving promptly: until it runs, a filled
entry has no stop attached.

## Writing a plan

`.claude/skills/trade-plan/SKILL.md` has the method. In short:

- Read the indicator values from the snapshot; do not recompute them from
  `recent_bars`. The risk gate and the charts use the snapshot's numbers.
- Put the stop where the idea is wrong, from market structure, with at least
  1×ATR of room. Never size-fit a stop.
- `size.mode: risk_pct` unless there is a reason. It is the only mode that
  keeps position size proportional to stop distance.
- Set `expires_at` to when the setup stops being the setup.
- Say what would invalidate it, in checkable terms.
- **"No trade" is a valid and frequent output.** An overbought, mid-range or
  contradictory tape deserves that answer, not a marginal plan.

## Reading a snapshot against a chart

Four differences look like bugs and are not: values come from the last
*closed* bar, periods are 14 (OKX's RSI panel defaults to 6/12/24), OKX's MACD
histogram is `2 × (DIF − DEA)`, and bar times are UTC while the web chart shows
local time. `scan` prints these reminders.

## When something fails

| Symptom | Cause |
|---|---|
| `scan` works, authenticated calls fail | `base_url` points at the wrong OKX regional entity |
| `50101` | live key against the demo endpoint, or the reverse |
| `50110` | source IP differs from the key's whitelist |
| `50113` | trailing whitespace or a CRLF in the secret |
| `50102` | machine clock drift |
| duplicate `clOrdId` | this plan was already submitted; use `status`, not a new submission |

Error messages carry a hint naming the fix; quote it rather than guessing.

## Development

`pytest` runs the whole suite offline in under a second. Anything touching
sizing, quantisation, fees or order construction needs a test that would fail
without the change. Two live bugs so far were found by writing the expected
numbers down first and comparing — keep doing that.
