from datetime import datetime, timedelta, timezone

import pytest

from okxbot.config import RiskLimits
from okxbot.plan import TradePlan
from okxbot.risk import AccountState, RiskGate

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)

LIMITS = RiskLimits(
    symbol_whitelist=("BTC-USDT",),
    quote_ccy="USDT",
    # Deliberately loose, so the sizing tests below exercise the sizing math
    # rather than tripping a ceiling. Cap tests tighten these per-case.
    max_notional_per_trade=200_000.0,
    max_account_fraction_per_trade=0.90,
    max_risk_pct_per_trade=2.0,
    max_open_plans=3,
    max_daily_loss_quote=200.0,
    min_risk_reward=1.5,
    max_plan_age_seconds=3600,
    max_slippage_pct=0.5,
)


def plan(**overrides) -> TradePlan:
    payload = {
        "inst_id": "BTC-USDT",
        "side": "buy",
        "entry": {"type": "limit", "price": 60_000.0},
        "stop": {"price": 58_000.0},
        "targets": [{"price": 64_000.0, "fraction": 1.0}],
        "size": {"mode": "risk_pct", "value": 1.0},
        "created_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(days=1)).isoformat(),
    }
    payload.update(overrides)
    return TradePlan.from_dict(payload)


def gate(**limit_overrides) -> RiskGate:
    return RiskGate(RiskLimits(**{**LIMITS.__dict__, **limit_overrides}))


RICH = AccountState(equity_quote=100_000.0, available_quote=100_000.0)


def evaluate(p, account=RICH, last=60_000.0, **kwargs):
    kwargs.setdefault("open_plan_count", 0)
    kwargs.setdefault("realized_24h", 0.0)
    return gate().evaluate(p, account, last, now=NOW, **kwargs)


# ------------------------------------------------------------------- sizing


def test_risk_pct_sizes_the_position_from_the_stop_distance():
    # 1% of 100k = 1000 USDT at risk, over a 2000 USDT stop = 0.5 BTC.
    decision = evaluate(plan(), account=AccountState(100_000.0, 100_000.0))
    assert decision.approved
    assert decision.base_size == pytest.approx(0.5)
    assert decision.risk_quote == pytest.approx(1000.0)


def test_a_tighter_stop_buys_a_larger_position_for_the_same_risk():
    wide = evaluate(plan(stop={"price": 58_000.0}))
    tight = evaluate(plan(stop={"price": 59_000.0}))
    assert tight.base_size == pytest.approx(2 * wide.base_size)
    assert tight.risk_quote == pytest.approx(wide.risk_quote)


def test_quote_and_base_modes_bypass_stop_distance():
    by_quote = evaluate(plan(size={"mode": "quote", "value": 6_000.0}))
    by_base = evaluate(plan(size={"mode": "base", "value": 0.1}))
    assert by_quote.base_size == pytest.approx(0.1)
    assert by_base.notional == pytest.approx(6_000.0)


# --------------------------------------------------------------------- caps


def test_notional_ceiling_caps_the_size_and_says_so():
    decision = gate(max_notional_per_trade=6_000.0).evaluate(
        plan(), RICH, 60_000.0, now=NOW, open_plan_count=0, realized_24h=0.0
    )
    assert decision.approved
    assert decision.notional == pytest.approx(6_000.0)
    assert any("max_notional_per_trade" in w for w in decision.warnings)


def test_per_trade_risk_ceiling_overrides_an_oversized_plan():
    # The plan asks for 5% risk; the limit is 2%.
    decision = gate().evaluate(
        plan(size={"mode": "risk_pct", "value": 5.0}),
        RICH, 60_000.0, now=NOW, open_plan_count=0, realized_24h=0.0,
    )
    assert decision.approved
    assert decision.risk_quote == pytest.approx(2_000.0)
    assert any("max_risk_pct_per_trade" in w for w in decision.warnings)


def test_capping_is_reported_as_a_warning_never_applied_silently():
    decision = gate(max_notional_per_trade=100.0).evaluate(
        plan(), RICH, 60_000.0, now=NOW, open_plan_count=0, realized_24h=0.0
    )
    assert decision.warnings and "capped" in decision.warnings[0]


# --------------------------------------------------------------- rejections


def test_an_unwhitelisted_symbol_is_refused():
    decision = evaluate(plan(inst_id="DOGE-USDT",
                             entry={"type": "limit", "price": 0.4},
                             stop={"price": 0.36},
                             targets=[{"price": 0.5, "fraction": 1.0}]), last=0.4)
    assert not decision.approved
    assert any("whitelisted" in r for r in decision.reasons)


def test_a_stale_plan_is_refused():
    old = plan(created_at=(NOW - timedelta(hours=3)).isoformat())
    decision = evaluate(old)
    assert not decision.approved
    assert any("old" in r for r in decision.reasons)


def test_an_expired_plan_is_refused():
    decision = evaluate(plan(expires_at=(NOW + timedelta(minutes=1)).isoformat()),
                        last=60_000.0)
    # Not yet expired at NOW...
    assert decision.approved
    later = gate().evaluate(plan(expires_at=(NOW + timedelta(minutes=1)).isoformat()),
                            RICH, 60_000.0, now=NOW + timedelta(minutes=2),
                            open_plan_count=0, realized_24h=0.0)
    assert not later.approved
    assert any("expired" in r for r in later.reasons)


def test_a_thin_risk_reward_is_refused():
    decision = evaluate(plan(targets=[{"price": 61_000.0, "fraction": 1.0}]))
    assert not decision.approved
    assert any("R:R" in r for r in decision.reasons)


def test_price_already_through_the_stop_is_refused():
    decision = evaluate(plan(), last=57_000.0)
    assert not decision.approved
    assert any("through the stop" in r for r in decision.reasons)


def test_price_already_at_the_first_target_is_refused():
    decision = evaluate(plan(), last=65_000.0)
    assert not decision.approved
    assert any("target1" in r for r in decision.reasons)


def test_a_market_entry_that_would_slip_too_far_is_refused():
    decision = evaluate(plan(entry={"type": "market", "price": 60_000.0}), last=60_600.0)
    assert not decision.approved
    assert any("slip" in r for r in decision.reasons)


def test_the_same_gap_only_warns_for_a_limit_entry():
    decision = evaluate(plan(), last=60_600.0)
    assert decision.approved
    assert any("may not fill" in w for w in decision.warnings)


def test_the_kill_switch_blocks_everything_once_tripped():
    decision = evaluate(plan(), realized_24h=-250.0)
    assert not decision.approved
    assert any("kill switch" in r for r in decision.reasons)


def test_the_kill_switch_is_not_tripped_just_short_of_the_limit():
    assert evaluate(plan(), realized_24h=-199.0).approved


def test_too_many_open_plans_blocks_a_new_one():
    decision = evaluate(plan(), open_plan_count=3)
    assert not decision.approved
    assert any("already open" in r for r in decision.reasons)


def test_insufficient_quote_balance_is_refused():
    decision = evaluate(plan(size={"mode": "quote", "value": 9_000.0}),
                        account=AccountState(equity_quote=100_000.0, available_quote=500.0))
    assert not decision.approved
    assert any("available" in r for r in decision.reasons)


def test_a_sell_plan_checks_the_base_balance_instead():
    sell = plan(side="sell", stop={"price": 62_000.0},
                targets=[{"price": 56_000.0, "fraction": 1.0}],
                size={"mode": "base", "value": 1.0})
    broke = evaluate(sell, account=AccountState(100_000.0, 100_000.0, available_base=0.1))
    assert not broke.approved
    holding = evaluate(sell, account=AccountState(100_000.0, 100_000.0, available_base=5.0))
    assert holding.approved


def test_every_failing_reason_is_collected_not_just_the_first():
    decision = evaluate(plan(inst_id="DOGE-USDT",
                             entry={"type": "limit", "price": 0.4},
                             stop={"price": 0.36},
                             targets=[{"price": 0.41, "fraction": 1.0}]),
                        last=0.30, realized_24h=-500.0)
    assert len(decision.reasons) >= 3


def test_render_shows_rejections_and_approvals_differently():
    assert "REJECTED" in evaluate(plan(), last=1.0).render()
    assert "APPROVED" in evaluate(plan()).render()
