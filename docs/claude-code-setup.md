# Driving this from a conversation

The workflow this repository was first operated with — read a snapshot, paste
it into a chat, paste a plan back, paste commands into a terminal — turns the
operator into a clipboard. This sets up the version where an assistant runs the
commands and the operator only decides.

The split stays exactly as it was. The assistant reads, forms a view and writes
a plan; `okxbot` sizes, risk-checks, submits and protects; the human approves
the one step that spends money. What changes is who does the typing.

## Why not point an assistant at OKX directly

OKX publishes an MCP server that can place orders. Wiring it to an assistant
would skip every control in this repository: the risk gate, the plan contract,
the journal, fee adjustment, lot quantisation, and the idempotency key that
stops a retry from doubling a position. Those are not ceremony — two of them
caught real bugs during the first live session.

Install it read-only if ad-hoc market questions are useful. Orders go through
`okxbot`.

## Choose where it runs

The OKX API key is bound to one IP. Everything below keeps requests leaving
from that address.

**A — on the VPS.** Fewest moving parts. You SSH in and talk to the assistant
there.

**B — on your workstation, reaching the VPS over SSH.** A real desktop UI, and
requests still originate from the whitelisted VPS, so a changing home IP never
breaks authentication. Recommended.

## Install the command wrapper

Both setups expose the same command name, `okxbot`, which is what lets one
permission policy cover either.

**On the VPS**, for both setups:

```bash
sudo install -m 0755 /opt/trading-agent/deploy/okxbot /usr/local/bin/okxbot
okxbot status          # should print the environment line
```

It runs the real binary as the `okxbot` account and reads `.env` there, so the
file stays `0600` and the secrets never enter the caller's environment.

**On your workstation**, for setup B only:

```bash
# ~/.ssh/config
Host okx-vps
    HostName 139.162.42.221
    User root
    IdentityFile ~/.ssh/id_ed25519

mkdir -p ~/bin
curl -o ~/bin/okxbot https://raw.githubusercontent.com/cornell880503-bot/trading-agent/main/deploy/okxbot-remote
chmod +x ~/bin/okxbot
export PATH="$HOME/bin:$PATH"     # add to ~/.zshrc
okxbot status
```

Set `OKXBOT_SSH_HOST` if your SSH alias is not `okx-vps`.

## The approval gate

With the assistant running commands, the confirmation prompt inside
`submit --live` is no longer the human gate — the assistant would be the one
typing. `CLAUDE.md` forbids that, but a rule is not a control.

The control is the client's own permission prompt, and `--approved` is what
lets it *be* the control: it tells `submit` that the approval already happened
outside the process, so the interactive prompt does not also have to be
answered. Without it the two gates deadlock — the assistant runs the command,
the prompt opens, and the assistant is forbidden from answering it.

`--approved` is not a way around approval. It refuses unless a `preview` of the
same plan ran within the last 15 minutes, because a permission dialog shows a
command line, not a position size. Something has to guarantee the numbers were
on screen before the click, and that check is it. A plan file with no declared
`plan_id` is refused for the same reason: every load would invent a new id, so
no preview could be tied to the submission.

`.claude/settings.json` splits the commands:

| Allowed silently | Asks every time |
|---|---|
| `scan` `status` `validate` `preview` `schema` | `submit` `sync` `cancel` `close` |

So the assistant investigates freely and the operator sees a prompt only when
money or the journal is about to change. `preview` exists for exactly this
reason: it is `submit` with no way to become live, so "show me what would
happen" can be allowed without allowing "do it".

**Do not add `okxbot submit` to the allow list**, and do not run the session in
a mode that skips permission prompts. That is the entire gate.

## What a session looks like

> **Operator:** BTC held 80,900 overnight, worth a long?
>
> **Assistant:** runs `okxbot scan BTC-USDT`, reads it, gives a view. Writes
> `plans/btc-4h.json` if the view supports one, runs `okxbot validate` and
> `okxbot preview`, and reports the risk gate's decision and the exact orders.
>
> **Operator:** send it.
>
> **Assistant:** runs `okxbot submit plans/btc-4h.json --live --approved` → the client
> asks the operator to approve the command → the operator approves → the
> assistant reports the fill, runs `okxbot sync --live` (approved again) and
> confirms the exits are resting at the exchange.

Two approvals, no copying.

## Keep the scheduled sync

The systemd timer from `docs/vps-deploy.md` stays. It is what protects a
position that fills while nobody is talking to the assistant.
