---
name: trade-plan
description: Turn an okxbot market snapshot into a validated TradePlan JSON file. Use when the user asks for a trade idea, entry/exit levels, or analysis of an OKX spot instrument, or when they paste a snapshot produced by `okxbot scan`.
---

# Producing a TradePlan

You are the slow loop. You read numbers and emit one JSON document. You do not
place orders, and nothing you write reaches the exchange without a human typing
the plan id first.

## Steps

1. **Get a snapshot.** Either the user pastes one, or run:
   `okxbot scan BTC-USDT --json /tmp/snap.json --raw`
2. **Read, do not recompute.** Indicator values in the snapshot are computed by
   `okxbot/indicators.py` on confirmed candles. Do not derive RSI, ATR or EMAs
   yourself from `recent_bars`; you will get different numbers than the risk
   gate and the charts.
3. **Form a view across timeframes.** The higher timeframe sets direction; the
   lower one sets the trigger. If they disagree, say so and prefer no trade.
4. **Write the plan** to a file and run `okxbot validate <file>`.
5. **Hand it over.** Show the summary and stop. The human runs `submit`.

## Placing the stop

The stop is the plan. Derive it from market structure, not from a round number
or a fixed percentage:

- Below the most recent confirmed swing low (`levels.support`) for a long.
- Give it room: at least 1×ATR(14) beyond the level, or noise takes you out.
- If that stop implies a position the risk limits will shrink to dust, the
  trade is wrong for this account. Say so rather than tightening the stop to
  fit — a stop chosen to make the size work is not a stop.

## Targets

- The first target should be reachable: prior swing high, Donchian upper band,
  or a measured move. R:R to it must be at least 1.5 or the gate refuses.
- Fractions may sum to less than 1.0. The remainder becomes a "runner" that
  the executor protects with the stop alone. That is a deliberate choice, not
  an oversight — state it in the rationale when you do it.

## Expiry

Set `expires_at` to the point at which the setup stops being the setup, usually
1–3 bars of the plan's timeframe. Plans older than `max_plan_age_seconds`
(default one hour) are refused at submission regardless.

## Honesty rules

- `confidence` is your own estimate. A snapshot showing chop deserves a low
  number or no plan at all. "No trade" is a valid, frequently correct output.
- Put the reason the trade could be wrong in `invalidation`, in terms a program
  or a human can check later.
- Never adjust a stop, a target or the size to get past the risk gate. If the
  gate rejects a plan, report its reasons verbatim and let the human decide.

## Schema

Run `okxbot schema` for the authoritative version, or see
`examples/plan.example.json`. The validator rejects rather than repairs, so
malformed output costs a round trip — get the direction rules right:

- buy  → `stop.price` < `entry.price` < every `targets[].price`
- sell → `stop.price` > `entry.price` > every `targets[].price`
