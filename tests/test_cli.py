"""End-to-end CLI tests against a fake exchange.

These exercise the wiring -- config, plan loading, risk gate, intent building,
journalling -- which unit tests cannot reach. No network is touched.
"""

import json
from pathlib import Path

import pytest

from okxbot import cli
from okxbot.config import Config, RiskLimits
from okxbot.okx.auth import Credentials
from okxbot.okx.precision import InstrumentSpec

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "plan.example.json"

BTC = InstrumentSpec("BTC-USDT", "0.1", "0.00000001", "0.00001", "BTC", "USDT")


def candle_rows(n=260):
    rows = []
    for i in range(n):
        price = 55_000 + i * 25
        rows.append([str(1_700_000_000_000 + i * 3_600_000), str(price), str(price + 200),
                     str(price - 200), str(price + 50), "12", "12", "700000", "1"])
    return list(reversed(rows))


class FakeRest:
    def __init__(self, last=61_400.0, equity=10_000.0):
        self.last, self.equity = last, equity
        self.placed = []

    def ticker(self, inst_id):
        return {"last": str(self.last), "open24h": "60000", "high24h": "62000",
                "low24h": "59000", "vol24h": "1234", "sodUtc0": "61000",
                "sodUtc8": "60800"}

    def candles(self, inst_id, bar="1H", limit=300, **kwargs):
        return candle_rows()

    def instrument(self, inst_id, refresh=False):
        return BTC

    def balances(self, ccy=None):
        return {"USDT": {"avail": self.equity, "frozen": 0.0, "eq": self.equity,
                         "eq_usd": self.equity}}

    def place_order(self, **kwargs):
        self.placed.append(kwargs)
        return {"ordId": "ord1", "sCode": "0"}


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Patch the CLI's exchange and config so commands run offline."""
    rest = FakeRest()
    config = Config(
        credentials=Credentials("k", "s", "p", simulated=True),
        risk=RiskLimits(
            symbol_whitelist=("BTC-USDT",), max_notional_per_trade=500.0,
            max_account_fraction_per_trade=0.20, max_risk_pct_per_trade=1.0,
            max_open_plans=2, max_daily_loss_quote=100.0, min_risk_reward=1.5,
            max_plan_age_seconds=3600, max_slippage_pct=0.5,
        ),
        db_path=str(tmp_path / "test.sqlite3"),
    )
    monkeypatch.setattr(cli, "OkxRest", lambda **kwargs: rest)
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: config)
    return rest, config


def run(argv):
    return cli.main(argv)


def test_schema_prints_the_contract(capsys):
    assert run(["schema"]) == 0
    assert "size.mode" in capsys.readouterr().out


def test_the_shipped_example_plan_validates(capsys):
    assert run(["validate", str(EXAMPLE)]) == 0
    assert "structurally valid" in capsys.readouterr().out


def test_scan_renders_every_requested_timeframe(wired, capsys):
    assert run(["scan", "BTC-USDT", "--timeframes", "1H,4H"]) == 0
    out = capsys.readouterr().out
    assert "BTC-USDT" in out and "[1H]" in out and "[4H]" in out
    assert "RSI" in out and "ATR" in out


def test_scan_reports_both_reference_prices(wired, capsys):
    """OKX's chart header uses the 00:00 UTC open; our headline uses rolling 24h.

    Showing only one of them makes the snapshot look wrong beside the
    exchange's own screen, so both are printed and labelled.
    """
    assert run(["scan", "BTC-USDT", "--timeframes", "4H"]) == 0
    out = capsys.readouterr().out
    assert "rolling 24h" in out
    assert "since 00:00 UTC" in out
    # last 61400 against open24h 60000 and sodUtc0 61000
    assert "2.333%" in out and "0.656%" in out


def test_scan_names_the_bar_each_reading_came_from(wired, capsys):
    """Without this, the output looks wrong next to an exchange chart.

    The chart's rightmost candle is still forming; every number here is from
    the last closed bar. Naming the bar is what makes the two comparable.
    """
    assert run(["scan", "BTC-USDT", "--timeframes", "4H"]) == 0
    out = capsys.readouterr().out
    assert "(closed)" in out
    assert "bar 20" in out, "the bar open timestamp must be printed"


def test_scan_states_the_conventions_a_chart_comparison_needs(wired, capsys):
    assert run(["scan", "BTC-USDT", "--timeframes", "4H"]) == 0
    out = capsys.readouterr().out
    # OKX's RSI panel defaults to 6/12/24 and its MACD histogram is doubled;
    # both differences look like bugs until the output says otherwise.
    assert "6/12/24" in out
    assert "DIF - DEA" in out
    assert "RSI(14)" in out


def test_scan_writes_a_json_snapshot_that_parses(wired, tmp_path, capsys):
    target = tmp_path / "snap.json"
    assert run(["scan", "BTC-USDT", "--timeframes", "4H", "--json", str(target)]) == 0
    snapshot = json.loads(target.read_text())
    assert snapshot["inst_id"] == "BTC-USDT"
    assert snapshot["timeframes"]["4H"]["rsi14"] is not None
    assert len(snapshot["timeframes"]["4H"]["recent_bars"]) == 20


def test_submit_dry_run_shows_the_intent_and_sends_nothing(wired, capsys):
    rest, _ = wired
    assert run(["submit", str(EXAMPLE)]) == 0
    out = capsys.readouterr().out
    assert "APPROVED" in out
    assert "execution intent" in out
    assert "DRY RUN" in out
    assert rest.placed == []


def test_the_notional_cap_is_visible_at_the_prompt(wired, capsys):
    # 1% of 10,000 over a 1,700 stop wants ~3,600 USDT; the cap is 500.
    assert run(["submit", str(EXAMPLE)]) == 0
    out = capsys.readouterr().out
    assert "capped by max_notional_per_trade" in out


def test_the_runner_leg_is_called_out_as_unprotected_by_a_target(wired, capsys):
    # The example takes profit on 80%; the rest rides the stop.
    assert run(["submit", str(EXAMPLE)]) == 0
    out = capsys.readouterr().out
    assert "runner" in out and "no take-profit" in out


def test_a_plan_outside_the_whitelist_exits_nonzero(wired, tmp_path, capsys):
    payload = json.loads(EXAMPLE.read_text())
    payload["inst_id"] = "SOL-USDT"
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(payload))
    assert run(["submit", str(path)]) == 2
    assert "not whitelisted" in capsys.readouterr().out


def test_a_malformed_plan_exits_two_with_the_validator_message(wired, tmp_path, capsys):
    path = tmp_path / "broken.json"
    path.write_text(json.dumps({"version": 1, "inst_id": "BTC-USDT", "side": "buy",
                                "entry": {"type": "limit", "price": 100},
                                "stop": {"price": 200},
                                "targets": [{"price": 300, "fraction": 1.0}],
                                "size": {"mode": "base", "value": 1},
                                "expires_at": "2099-01-01T00:00:00Z"}))
    assert run(["submit", str(path)]) == 2
    assert "must sit below entry" in capsys.readouterr().err


def test_the_kill_switch_blocks_submission_through_the_cli(wired, capsys):
    _, config = wired
    from okxbot.store import Store
    store = Store(config.db_path)
    store.record_realized("BTC-USDT", -150.0)
    store.close()
    assert run(["submit", str(EXAMPLE)]) == 2
    assert "kill switch" in capsys.readouterr().out


def test_read_only_blocks_a_live_submit_before_anything_is_sent(wired, monkeypatch, capsys):
    rest, config = wired
    config.read_only = True
    assert run(["submit", str(EXAMPLE), "--live", "--yes"]) == 3
    assert "OKX_READ_ONLY" in capsys.readouterr().err
    assert rest.placed == [], "nothing may reach the exchange"


def test_read_only_still_allows_the_dry_run(wired, capsys):
    _, config = wired
    config.read_only = True
    assert run(["submit", str(EXAMPLE)]) == 0
    assert "DRY RUN" in capsys.readouterr().out


def test_status_names_read_only_in_the_environment_line(wired, capsys):
    _, config = wired
    config.read_only = True
    assert run(["status"]) == 0
    assert "read-only" in capsys.readouterr().out


def test_status_reports_the_environment_and_the_switch(wired, capsys):
    assert run(["status"]) == 0
    out = capsys.readouterr().out
    assert "demo (paper)" in out and "kill switch at" in out


def test_a_missing_plan_file_exits_two(wired, capsys):
    assert run(["submit", "/nope/missing.json"]) == 2
    assert "file not found" in capsys.readouterr().err


def test_a_confirmation_never_reads_input_buffered_before_the_prompt(monkeypatch):
    """A pasted command block must not be able to answer a real-money prompt."""
    from okxbot import cli

    drained = []
    monkeypatch.setattr(cli, "_drain_stdin", lambda: drained.append(True))
    monkeypatch.setattr("builtins.input", lambda prompt="": "plan123")

    assert cli._confirm("type it: ", expected="plan123") is True
    assert drained == [True], "the buffer must be flushed before asking"


def test_a_wrong_answer_declines(monkeypatch):
    from okxbot import cli

    monkeypatch.setattr(cli, "_drain_stdin", lambda: None)
    monkeypatch.setattr("builtins.input", lambda prompt="": "E sync --live")
    assert cli._confirm("type it: ", expected="plumbingtest02") is False


def test_end_of_input_declines_rather_than_proceeding(monkeypatch):
    from okxbot import cli

    monkeypatch.setattr(cli, "_drain_stdin", lambda: None)

    def raise_eof(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", raise_eof)
    assert cli._confirm("type it: ", expected="x") is False
