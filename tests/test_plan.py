from datetime import datetime, timedelta, timezone

import pytest

from okxbot.errors import PlanError
from okxbot.plan import TradePlan

FUTURE = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat().replace("+00:00", "Z")


def base_plan(**overrides) -> dict:
    payload = {
        "version": 1,
        "inst_id": "BTC-USDT",
        "timeframe": "4H",
        "side": "buy",
        "entry": {"type": "limit", "price": 60000.0},
        "stop": {"price": 58000.0},
        "targets": [{"price": 64000.0, "fraction": 0.5}, {"price": 68000.0, "fraction": 0.5}],
        "size": {"mode": "risk_pct", "value": 1.0},
        "expires_at": FUTURE,
        "confidence": 0.6,
    }
    payload.update(overrides)
    return payload


def test_a_well_formed_plan_round_trips_through_json():
    plan = TradePlan.from_dict(base_plan())
    again = TradePlan.from_json(plan.to_json())
    assert again.plan_id == plan.plan_id
    assert again.entry_price == plan.entry_price
    assert [t.price for t in again.targets] == [t.price for t in plan.targets]


def test_risk_and_reward_are_measured_from_entry():
    plan = TradePlan.from_dict(base_plan())
    assert plan.risk_per_unit == 2000.0
    assert plan.risk_reward == 2.0  # first target is 4000 away
    assert plan.expected_r == pytest.approx((4000 * 0.5 + 8000 * 0.5) / 2000)


def test_targets_are_reordered_nearest_first():
    plan = TradePlan.from_dict(
        base_plan(targets=[{"price": 70000.0, "fraction": 0.5}, {"price": 62000.0, "fraction": 0.5}])
    )
    assert [t.price for t in plan.targets] == [62000.0, 70000.0]


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"side": "hodl"}, "side"),
        ({"inst_id": "BTCUSDT"}, "inst_id"),
        ({"stop": {"price": 61000.0}}, "stop"),
        ({"targets": [{"price": 59000.0, "fraction": 1.0}]}, "targets"),
        ({"targets": []}, "at least one target"),
        ({"size": {"mode": "yolo", "value": 1}}, "size.mode"),
        ({"size": {"mode": "risk_pct", "value": 150}}, "donation"),
        ({"confidence": 1.4}, "confidence"),
        ({"entry": {"type": "limit", "price": -1}}, "entry.price"),
        ({"entry": {"type": "telepathy", "price": 60000}}, "entry.type"),
        ({"version": 99}, "unsupported plan version"),
    ],
)
def test_malformed_plans_are_rejected_with_a_pointed_message(overrides, message):
    with pytest.raises(PlanError, match=message):
        TradePlan.from_dict(base_plan(**overrides))


def test_a_short_plan_inverts_every_directional_rule():
    plan = TradePlan.from_dict(
        base_plan(
            side="sell",
            entry={"type": "limit", "price": 60000.0},
            stop={"price": 62000.0},
            targets=[{"price": 56000.0, "fraction": 1.0}],
        )
    )
    assert not plan.is_long
    assert plan.risk_reward == 2.0


def test_fractions_may_not_exceed_the_whole_position():
    with pytest.raises(PlanError, match="exceeds 1.0"):
        TradePlan.from_dict(
            base_plan(targets=[{"price": 64000.0, "fraction": 0.8},
                               {"price": 68000.0, "fraction": 0.5}])
        )


def test_fractions_below_one_are_allowed_because_a_runner_is_legitimate():
    plan = TradePlan.from_dict(base_plan(targets=[{"price": 64000.0, "fraction": 0.6}]))
    assert plan.targets[0].fraction == 0.6


def test_expiry_must_be_after_creation():
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    with pytest.raises(PlanError, match="expires_at"):
        TradePlan.from_dict(base_plan(expires_at=past))


def test_naive_timestamps_are_read_as_utc():
    plan = TradePlan.from_dict(base_plan(expires_at="2099-01-01T00:00:00"))
    assert plan.expires_at.tzinfo is timezone.utc


def test_client_ids_are_deterministic_alphanumeric_and_distinct():
    plan = TradePlan.from_dict(base_plan(plan_id="abc123def456"))
    again = TradePlan.from_dict(base_plan(plan_id="abc123def456"))
    assert plan.client_id("e") == again.client_id("e")
    ids = {plan.client_id(s) for s in ("e", "t1", "t2", "r")}
    assert len(ids) == 4
    for cid in ids:
        assert cid.isalnum() and 1 <= len(cid) <= 32


def test_is_expired_uses_the_clock_it_is_given():
    plan = TradePlan.from_dict(base_plan())
    assert not plan.is_expired(datetime.now(timezone.utc))
    assert plan.is_expired(datetime.now(timezone.utc) + timedelta(days=5))


def test_summary_mentions_the_numbers_a_human_needs_to_approve():
    text = TradePlan.from_dict(base_plan(plan_id="deadbeef0001")).summary()
    for fragment in ("deadbeef0001", "BTC-USDT", "60000", "58000", "R:R"):
        assert fragment in text
