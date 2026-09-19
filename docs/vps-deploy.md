# Deploying to a VPS

## The order matters

You cannot create the OKX API key first. The key is bound to an IP, and the IP
does not exist until the server does.

```
1. create the VPS
2. read its outbound IP        <- from the VPS itself, not the dashboard
3. create the OKX key, whitelisting that IP
4. deploy and run on demo
5. only then consider live
```

## 1. Choosing the VPS

| | |
|---|---|
| Region | **Not the United States.** OKX does not serve US users and blocks US IPs. Singapore, Tokyo and Frankfurt are all fine. |
| Latency | Irrelevant here. This bot trades 1h–1d bars; a 200ms round trip changes nothing. Choose for reliability and legality, not proximity. |
| Spec | 1 vCPU / 1 GB RAM is plenty. Do not go below 1 GB — pandas and numpy together need roughly 300 MB resident, and a 512 MB box will OOM during `pip install`. |
| IP | A static IPv4. Nearly every provider gives one by default. |

## 2. Reading the outbound IP

Run this **on the VPS**, over SSH. The provider's dashboard usually shows the
right address, but what OKX checks is the address your traffic actually leaves
from, which is not always the same thing (NAT, floating IPs, proxies).

```bash
curl -4 -s https://checkip.amazonaws.com
```

Then check whether the machine also has working IPv6:

```bash
curl -6 -s https://checkip.amazonaws.com
```

**If the IPv6 call succeeds, fix it before going further.** A dual-stack Linux
box prefers IPv6 for outbound connections. You would whitelist the IPv4 address,
and OKX would see the IPv6 one and reject every authenticated request with a
confusing "invalid IP" error. Force IPv4 preference system-wide:

```bash
echo 'precedence ::ffff:0:0/96  100' | sudo tee -a /etc/gai.conf
curl -s https://checkip.amazonaws.com    # must now match the -4 answer
```

Whitelist the address that last command prints.

## 3. Creating the OKX key

Account → API → create V5 API key.

- Permissions: **Trade only.** Leave **Withdraw** off. If the key leaks, the
  worst case is unwanted trades, not an empty account.
- IP whitelist: the address from step 2.
- Passphrase: any string — this becomes `OKX_PASSPHRASE`, and it is not your
  login password.

Create a **demo** key too (OKX Demo Trading → API). It is a separate key; the
live one will not work against the paper endpoint.

## 4. Clock synchronisation

Request signatures carry a timestamp. OKX rejects anything more than ~30
seconds off, and a drifting VM clock produces intermittent `50102 Timestamp
request expired` errors that look like network flakiness.

```bash
sudo timedatectl set-ntp true
timedatectl status | grep -E 'synchronized|NTP service'
```

Both should read `yes`/`active`.

## 5. Deploying

```bash
sudo apt update && sudo apt install -y python3-venv python3-pip git

# A dedicated unprivileged user. The bot never needs root.
sudo adduser --system --group --home /opt/trading-agent okxbot
sudo -u okxbot git clone https://github.com/cornell880503-bot/trading-agent.git /opt/trading-agent
cd /opt/trading-agent

sudo -u okxbot python3 -m venv .venv
sudo -u okxbot .venv/bin/pip install -e ".[dev]"
sudo -u okxbot .venv/bin/pytest          # 122 tests, no network needed
```

If `pytest` fails here, stop. It runs entirely offline, so a failure means the
environment is wrong, not the exchange.

```bash
sudo -u okxbot cp .env.example .env
sudo chmod 600 .env && sudo chown okxbot:okxbot .env
sudoedit .env                            # paste the DEMO key
```

## 6. First run, on paper

```bash
sudo -u okxbot bash -c 'cd /opt/trading-agent && set -a && . ./.env && set +a && \
  .venv/bin/okxbot scan BTC-USDT'
```

Compare the printed RSI / ATR / EMA values against the same instrument and
timeframe on OKX's own chart. They should agree to the last decimal. **This is
the only check that proves the indicator implementations are correct** — the
unit tests prove they are self-consistent, not that they match what you see on
a chart.

Then:

```bash
... .venv/bin/okxbot status              # should report "demo (paper)"
```

`status` printing `LIVE` at this stage means `OKX_LIVE_TRADING` is set when it
should not be.

## 7. Scheduling `sync`

`sync` is the one command worth automating: it notices a filled entry and
attaches the exchange-side TP/SL. Until it runs, a filled position is
unprotected, so do not leave it to manual invocation.

Copy the units from `deploy/` and enable the timer:

```bash
sudo cp deploy/okxbot-sync.service deploy/okxbot-sync.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now okxbot-sync.timer

systemctl list-timers okxbot-sync        # confirm it is scheduled
journalctl -u okxbot-sync -f             # watch it run
```

`sync` is idempotent — client order ids are derived from the plan id, so a
second run re-reads state instead of re-submitting.

## 8. Firewall

The bot makes only outbound connections. Nothing needs to reach it except your
own SSH session.

Linode now requires every new instance to be attached to a Cloud Firewall
before it can be created, so configure it at the create step:

| Direction | Policy | Rules |
|---|---|---|
| Inbound | **Drop** | one rule: TCP 22 from `<your workstation's public IP>/32` |
| Outbound | **Accept** | none needed |

Do not open 80 or 443 inbound. Nothing here serves HTTP.

To find the address for that rule, run this **on your own machine**, not on the
VPS:

```bash
curl -4 -s https://checkip.amazonaws.com
```

This is the same command as step 2, run somewhere else for a different purpose,
and the two are easy to confuse:

- run on the **VPS** -> the address to whitelist on the **OKX API key**
- run on your **workstation** -> the address to allow through the **firewall**

Restricting SSH to a single source cannot lock you out permanently: Linode's
LISH console reaches the machine out of band, bypassing the Cloud Firewall
entirely, so a changed home IP is an inconvenience rather than a disaster.

A host firewall (`ufw`) on top of this is redundant -- the Cloud Firewall drops
traffic before it reaches the machine -- and having two means checking two
places the next time something cannot connect. Prefer the Cloud Firewall alone.

Do still disable SSH password authentication, on the **VPS**:

```bash
sudo sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sudo systemctl restart ssh
```

(That is a GNU `sed` invocation and a systemd unit. Run it on the Ubuntu VPS;
on a macOS workstation it fails, which is fortunate, because succeeding would
mean editing your laptop's own SSH configuration.)

## 8b. When authenticated calls fail

`scan` works but `status` cannot read balances: public market data is served
from any OKX host, while account data is bound to the entity your account was
opened on. That split is the diagnostic, and it points at `base_url`.

Run this on the VPS to find the host that recognises your key. It only reads a
balance.

```bash
cat > /tmp/hosttest.py <<'SCRIPT'
import os, requests
from okxbot.okx.auth import Credentials, rest_headers

path  = "/api/v5/account/balance"
hosts = ["https://www.okx.com", "https://my.okx.com",
         "https://app.okx.com", "https://eea.okx.com"]

for host in hosts:
    for mode, sim in (("demo", True), ("live", False)):
        creds = Credentials(os.environ["OKX_API_KEY"], os.environ["OKX_API_SECRET"],
                            os.environ["OKX_PASSPHRASE"], simulated=sim)
        try:
            r = requests.get(host + path, headers=rest_headers(creds, "GET", path), timeout=15)
            b = r.json()
            print(f"{host:24} {mode}  HTTP {r.status_code}  code={b.get('code')}  {b.get('msg')}")
        except Exception as exc:
            print(f"{host:24} {mode}  ERROR {type(exc).__name__}")
SCRIPT

sudo -u okxbot bash -c 'cd /opt/trading-agent && set -a && . ./.env && set +a && \
  .venv/bin/python /tmp/hosttest.py'
```

Reading the result:

| Code | Meaning | Fix |
|---|---|---|
| `0` | this host and environment accept the key | put that host in `base_url` |
| `50119` | the key is unknown to this host entirely | wrong regional entity |
| `50101` | right host, key belongs to the other environment | a live key cannot serve the demo endpoint; issue a demo key |
| `50110` | source IP not whitelisted | compare against `curl -4 -s https://checkip.amazonaws.com` **on the VPS** |
| `50113` | signature mismatch | trailing space, or a CRLF from a DOS-format save |
| `50102` | timestamp outside tolerance | clock drift; see step 4 |

A key that fails with `50119` on every host was probably issued from OKX's
Web3/DEX developer portal rather than the main trading account's API page.
Those keys carry an `OK-ACCESS-PROJECT` header and cannot reach `/api/v5/account/`.

## 9. Going live

Only after the demo loop has run end to end — scan, plan, submit, fill, sync,
protection visible in the OKX web UI — for at least two weeks.

Swap the demo key for the live key in `.env`, then add:

```
OKX_LIVE_TRADING=i-understand-the-risk
```

Restart nothing; each invocation reads the file fresh. Confirm with
`okxbot status` that it now reports `LIVE`, and re-check that `config.yaml`
limits are sized for real money rather than for paper.
