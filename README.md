# okx-spot-advisor

Semi-automated OKX **spot** trading on the 1h–1d horizon. An assistant reads the
market and proposes a trade; a human approves it; deterministic code sizes it,
checks it against hard limits, submits it, and hands the exits to the exchange.

## The one idea

**The model is never in the execution path.**

A language model is slow, non-deterministic and expensive per call. Those are
fine properties for judgement and disqualifying ones for order submission: you
cannot backtest a decision that is not reproducible, and you cannot debug a loss
whose cause was a sampling temperature.

So the system is two loops joined by one contract:

```
slow loop  (minutes to hours, judgement)
   OKX candles -> indicators (pandas, local) -> analysis -> TradePlan JSON
                                                                 |
                                                     human types the plan id
                                                                 |
fast loop  (no model, fully tested, deterministic)               v
   risk gate -> size -> entry order -> [fill] -> TP/SL resting AT THE EXCHANGE
```

The second invariant follows from the first: **protection lives at OKX, not in
this process.** Once an entry fills, the take-profit and stop-loss are resting
algo orders. This program can crash, lose its network or be killed; the position
stays guarded. There is no `while True: if price < stop: sell()` anywhere in
this repository, because that pattern dies with the process that runs it.

## Install

```bash
git clone https://github.com/cornell880503-bot/trading-agent.git
cd trading-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest                     # 110 tests, no network required
cp .env.example .env       # then fill it in
```

## Creating the OKX API key

Account → API → create a V5 key.

| Setting | Value |
|---|---|
| Permissions | **Trade only.** Leave **Withdraw** off. |
| IP whitelist | The **public IP of the machine that runs this bot** — your VPS. |
| Passphrase | Any string; it goes in `OKX_PASSPHRASE`. |

Find the IP to whitelist by running this **on that machine**:

```bash
curl -s https://checkip.amazonaws.com
```

Whitelist that. Not your laptop's IP, not an office IP, and not the IP of any
machine an assistant happens to be running on — those are ephemeral, shared, and
change between sessions. If the bot runs somewhere with a dynamic IP, either put
it on a host with a static one or leave the whitelist empty and accept that a
leaked key is then usable from anywhere (which is exactly why the key must not
have withdrawal rights).

Keys are read from the environment only, never from `config.yaml`:

```bash
set -a; source .env; set +a
```

For a server deployment -- which IP to whitelist, clock sync, the unprivileged
user, and a systemd timer for `sync` -- see [docs/vps-deploy.md](docs/vps-deploy.md).

Everything runs against OKX's **demo** environment until `OKX_LIVE_TRADING` is
set to the exact string `i-understand-the-risk`. `true`, `1` and `yes` all keep
you on paper. That is deliberate.

`OKX_READ_ONLY=1` is a separate switch that makes the transport refuse any
non-GET request, in either environment. Use it to verify credentials against a
live account without a demo key. It is not a substitute for the demo
environment: reads cannot exercise sizing, tick and lot rounding, order
submission, or the fill-to-protection handoff, which is where the money is.

Your account may not live on the global host. OKX runs separate regional
entities, and a key issued by one is reported as non-existent (`50119`) by
another. Public market data answers from any host, so `scan` working while
every authenticated call fails is the signature of a wrong `base_url` rather
than a wrong key. Set `base_url` to whatever domain your browser shows on the
API page.

## The workflow

```bash
# 1. Numbers out. Multi-timeframe indicators on confirmed candles only.
okxbot scan BTC-USDT --json /tmp/snap.json

# 2. Analysis happens elsewhere: paste the snapshot into a conversation and get
#    a plan back. `okxbot schema` prints the contract; .claude/skills/trade-plan
#    teaches an assistant to fill it in.
okxbot validate plans/btc-4h.json

# 3. Dry run. Prints the risk decision and the exact orders that would be sent.
okxbot submit plans/btc-4h.json

# 4. For real. On live, this asks you to type the plan id -- muscle memory can
#    produce a "y", it cannot produce twelve hex characters.
okxbot submit plans/btc-4h.json --live

# 5. Once the entry fills, attach the exchange-side exits.
okxbot sync --live

# 6. Where things stand.
okxbot status --events 10
```

Step 5 is the one that must be automated: until `sync` runs, a filled entry
has no stop attached. Units for a systemd timer are in `deploy/`; see
[docs/vps-deploy.md](docs/vps-deploy.md).

`sync` is idempotent — every order carries a client id derived from the plan id,
so a second run re-reads state rather than re-submitting.

## Driving it from a conversation

Copying snapshots into a chat and commands back into a terminal makes the
operator a clipboard. An assistant can run the read-only commands itself and
stop at the one that spends money, where the client's permission prompt
becomes the approval gate. `okxbot preview` exists to make that boundary
expressible: it is `submit` with no way to become live.
See [docs/claude-code-setup.md](docs/claude-code-setup.md).

## What stops a bad trade

`config.yaml` holds the limits; `okxbot/risk.py` enforces them. The analysis
layer cannot see or change them, which is the entire point of the asymmetry.

Hard **rejections** — the plan is refused, never quietly repaired:

- instrument not in `symbol_whitelist`
- plan expired, or older than `max_plan_age_seconds`
- R:R to the first target below `min_risk_reward`
- last price already through the stop, or already at the first target
- a market entry that would slip past `max_slippage_pct`
- `max_open_plans` already live
- **kill switch**: realised P&L over a rolling 24h at or below
  `-max_daily_loss_quote`
- insufficient balance

Size **caps** — applied loudly, reported at the confirmation prompt:

- `max_notional_per_trade`, `max_account_fraction_per_trade`,
  `max_risk_pct_per_trade`, whichever binds first

The rolling 24h window is deliberate: a calendar-day reset hands a losing
strategy a fresh budget at midnight.

## Position sizing

`size.mode: risk_pct` is the mode to use. It sets quantity from the stop
distance, so a tighter stop buys a bigger position for the same money at risk
and a wider stop buys a smaller one. `quote` and `base` modes are fixed-size
escape hatches that ignore this; they are still capped.

## Known limits

- **Spot only, long-biased.** No margin, no leverage, no liquidation logic. A
  `sell` plan means reducing base currency you already hold.
- **`close` P&L is approximate.** It nets fees denominated in the quote
  currency. OKX charges spot *buy* fees in the base currency, which show up as a
  slightly smaller quantity on the sell side instead.
- **Protection is sized from what the account holds, not what was ordered.**
  A spot buy pays its fee in the base currency, so slightly less arrives than
  `accFillSz` reports; the exits are sized from the fee-adjusted fill and then
  capped by the live base balance. Without both, the exit orders bounce on
  insufficient balance and leave a filled position unprotected.
- **`sync` protects a partial fill only with `--partial`,** and having done so
  will not extend protection as more of the entry fills — the leg client ids are
  already used. Cancel and re-plan instead.
- **No backtester.** Indicator functions are pure and tested, so one can be
  built on them, but the plan-generation step is a human-in-the-loop process
  and is not replayable.
- **Nothing here predicts anything.** A plan is a hypothesis with a
  pre-committed exit. The value is in the pre-commitment.

## Relationship to OKX's own agent kit

OKX publishes [`agent-trade-kit`](https://github.com/okx/agent-trade-kit) — an
MCP server, CLI and Skills covering far more of the API than this repo does. Use
it for analysis, in **read-only** mode. It does not carry position sizing, a
kill switch or loss limits, and attaching a *write-enabled* MCP server to an
assistant's context puts the model back in the execution path.
See [docs/okx-agent-kit.md](docs/okx-agent-kit.md).

## Layout

| Path | Role |
|---|---|
| `okxbot/okx/auth.py` | v5 request signing; demo-mode header |
| `okxbot/okx/rest.py` | REST client, retries, per-item `sCode` checking |
| `okxbot/okx/precision.py` | `Decimal` tick/lot quantisation |
| `okxbot/indicators.py` | EMA, RSI, ATR, MACD, Bollinger, Donchian, ADX, pivots |
| `okxbot/snapshot.py` | multi-timeframe market snapshot for analysis |
| `okxbot/plan.py` | the TradePlan contract and its validator |
| `okxbot/risk.py` | the gate: rejections and size caps |
| `okxbot/executor.py` | idempotent submission, exchange-side protection |
| `okxbot/store.py` | SQLite journal of plans, orders, events, P&L |
| `okxbot/cli.py` | `scan` `validate` `submit` `sync` `status` `cancel` `close` |
