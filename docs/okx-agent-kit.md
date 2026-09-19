# Using OKX's official agent-trade-kit alongside this project

OKX ships [`okx/agent-trade-kit`](https://github.com/okx/agent-trade-kit): an MCP
server, a CLI (`okx`) and a set of agent Skills, in TypeScript, covering spot,
swap, futures, options, grid bots and 70+ indicators across 167 tools.

It is genuinely good, and it overlaps this repository. This document records
what to take from it, what not to, and why.

## What the kit replaces

Use the kit for the **analysis half** of the loop. Installed as an MCP server in
read-only mode, it lets an assistant pull tickers, candles, order books and
indicators directly in conversation, with no snapshot file to shuttle around:

```bash
npm install -g @okx_ai/okx-trade-mcp @okx_ai/okx-trade-cli
okx config init
okx-trade-mcp setup --client claude-desktop   # then add --read-only, see below
```

That is a real ergonomic win over `okxbot scan`, and it carries no execution
risk as long as the server stays read-only.

## What the kit does not replace

The kit's README documents a `--read-only` flag and a rate limiter. It does not
document position sizing, a portfolio kill switch, or maximum-loss limits.
Those are the whole point of `okxbot/risk.py`, and they have to live in the
same process that submits the order — a limit the caller can decline to consult
is a suggestion.

Two further gaps matter for the execution path, both to be re-checked against
`okx spot place --help` because the published CLI reference is a summary:

- **No documented `--clOrdId`.** This project's idempotency rests on a client
  order id derived from the plan id, so a retry after a lost response is
  rejected by OKX as a duplicate rather than opening a second position. Without
  it, a network timeout during submission is genuinely ambiguous.
- **No documented instrument-metadata command.** `tickSz` / `lotSz` / `minSz`
  come from `/api/v5/public/instruments`, which needs no authentication and is
  what `OkxRest.instrument()` calls. Sizes still have to be snapped to the grid
  by somebody.

## The recommended split

```
analysis   OKX MCP server, --read-only        <- conversation, ad-hoc questions
           okxbot scan                        <- reproducible, journalled

decision   TradePlan JSON                     <- the contract, human-approved

execution  okxbot submit / sync               <- risk gate, idempotency, journal
```

## One security note about the MCP server

An MCP server with trading permissions attached to an assistant's context makes
every document that assistant reads a potential order. A web page, a pasted
snapshot, a code comment — anything that reaches the context can attempt to
phrase itself as an instruction, and the model has no reliable way to tell a
user's request from text that merely looks like one.

`--read-only` closes that hole completely, at the cost of nothing this
architecture wanted in the first place: the model was never supposed to place
orders. Keep the write path in the CLI, behind the risk gate, behind a human
typing a plan id.
