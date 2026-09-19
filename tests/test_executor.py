from datetime import datetime, timedelta, timezone

import pytest

from decimal import Decimal

from okxbot.errors import OkxApiError
from okxbot.executor import DuplicateSubmission, Executor
from okxbot.okx.precision import InstrumentSpec
from okxbot.plan import TradePlan
from okxbot.risk import RiskDecision
from okxbot.store import Store

NOW = datetime.now(timezone.utc)
BTC = InstrumentSpec("BTC-USDT", "0.1", "0.00000001", "0.001", "BTC", "USDT")
CHUNKY = InstrumentSpec("CHUNK-USDT", "0.1", "1", "1", "CHUNK", "USDT")
# The real BTC-USDT grid, for cases that turn on the true minimum.
LIVE_BTC = InstrumentSpec("BTC-USDT", "0.1", "0.00000001", "0.00001", "BTC", "USDT")


def plan(**overrides) -> TradePlan:
    payload = {
        "inst_id": "BTC-USDT",
        "side": "buy",
        "entry": {"type": "limit", "price": 60_000.0},
        "stop": {"price": 58_000.0},
        "targets": [{"price": 64_000.0, "fraction": 0.5}, {"price": 68_000.0, "fraction": 0.5}],
        "size": {"mode": "base", "value": 1.0},
        "plan_id": "abcdef123456",
        "expires_at": (NOW + timedelta(days=1)).isoformat(),
    }
    payload.update(overrides)
    return TradePlan.from_dict(payload)


class FakeRest:
    """Records calls instead of making them, and can be told to fail."""

    def __init__(self, fail_with: OkxApiError | None = None, fail_on: str | None = None):
        self.orders, self.ocos, self.stops = [], [], []
        self.fail_with, self.fail_on = fail_with, fail_on

    def place_order(self, **kwargs):
        if self.fail_with and self.fail_on in (None, "order"):
            raise self.fail_with
        self.orders.append(kwargs)
        return {"ordId": f"ord{len(self.orders)}", "clOrdId": kwargs.get("cl_ord_id"), "sCode": "0"}

    def place_oco(self, **kwargs):
        if self.fail_with and self.fail_on == "oco" and len(self.ocos) == 0:
            self.ocos.append(kwargs)  # record the attempt, then fail it
            raise self.fail_with
        self.ocos.append(kwargs)
        return {"algoId": f"algo{len(self.ocos)}", "sCode": "0"}

    def place_stop(self, **kwargs):
        self.stops.append(kwargs)
        return {"algoId": f"stop{len(self.stops)}", "sCode": "0"}


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


def approved(size: float) -> RiskDecision:
    return RiskDecision(approved=True, base_size=size)


# ------------------------------------------------------------ leg allocation


def test_two_equal_targets_become_two_oco_legs_sharing_one_stop(store):
    intent = Executor(FakeRest(), store).build_intent(plan(), approved(1.0), BTC)
    assert [leg.role for leg in intent.legs] == ["tp1", "tp2"]
    assert [leg.size for leg in intent.legs] == ["0.5", "0.5"]
    assert {leg.stop for leg in intent.legs} == {"58000"}
    assert [leg.take_profit for leg in intent.legs] == ["64000", "68000"]


def test_partial_targets_leave_a_runner_protected_by_the_stop_alone(store):
    p = plan(targets=[{"price": 64_000.0, "fraction": 0.6}])
    intent = Executor(FakeRest(), store).build_intent(p, approved(1.0), BTC)
    roles = [leg.role for leg in intent.legs]
    assert roles == ["tp1", "runner"]
    runner = intent.legs[-1]
    assert runner.is_runner and runner.take_profit is None
    assert runner.size == "0.4"
    assert any("no take-profit" in note for note in intent.notes)


def test_the_whole_filled_quantity_is_always_covered(store):
    p = plan(targets=[{"price": 64_000.0, "fraction": 0.33},
                      {"price": 66_000.0, "fraction": 0.33}])
    intent = Executor(FakeRest(), store).build_intent(p, approved(1.0), BTC)
    assert sum(float(leg.size) for leg in intent.legs) == pytest.approx(1.0)


def test_a_leg_that_rounds_below_min_size_is_folded_into_the_runner(store):
    # 1% of 100 CHUNK is 1 unit; 0.4% rounds to 0 and must not vanish silently.
    p = plan(inst_id="CHUNK-USDT",
             entry={"type": "limit", "price": 60_000.0},
             targets=[{"price": 64_000.0, "fraction": 0.004},
                      {"price": 68_000.0, "fraction": 0.9}])
    intent = Executor(FakeRest(), store).build_intent(p, approved(100.0), CHUNKY)
    assert sum(float(leg.size) for leg in intent.legs) == pytest.approx(100.0)
    assert any("folded" in note for note in intent.notes)


def test_dust_below_min_size_is_merged_rather_than_left_naked(store):
    p = plan(inst_id="CHUNK-USDT", targets=[{"price": 64_000.0, "fraction": 0.996}])
    intent = Executor(FakeRest(), store).build_intent(p, approved(100.0), CHUNKY)
    assert sum(float(leg.size) for leg in intent.legs) == pytest.approx(100.0)


def test_building_an_intent_from_a_rejected_decision_is_refused(store):
    with pytest.raises(Exception, match="rejected risk decision"):
        Executor(FakeRest(), store).build_intent(plan(), RiskDecision(approved=False), BTC)


# ----------------------------------------------------------------- submission


def test_dry_run_transmits_nothing(store):
    rest = FakeRest()
    executor = Executor(rest, store)
    intent = executor.build_intent(plan(), approved(1.0), BTC)
    assert executor.submit_entry(intent, dry_run=True) is None
    assert rest.orders == []
    assert not store.order_exists(intent.entry_client_id)


def test_a_live_entry_is_journalled_before_and_after_transmission(store):
    rest = FakeRest()
    executor = Executor(rest, store)
    p = plan()
    store.save_plan(p)
    intent = executor.build_intent(p, approved(1.0), BTC)
    result = executor.submit_entry(intent, dry_run=False)

    assert result["ordId"] == "ord1"
    assert rest.orders[0]["cl_ord_id"] == intent.entry_client_id
    assert rest.orders[0]["px"] == "60000"
    row = store.orders_for_plan(p.plan_id)[0]
    assert row["status"] == "submitted" and row["ord_id"] == "ord1"
    assert store.get_plan_row(p.plan_id)["status"] == "submitted"


def test_a_market_entry_states_its_size_currency_explicitly(store):
    rest = FakeRest()
    executor = Executor(rest, store)
    p = plan(entry={"type": "market", "price": 60_000.0})
    intent = executor.build_intent(p, approved(1.0), BTC)
    executor.submit_entry(intent, dry_run=False)
    # Without this OKX reads sz on a spot market buy as quote currency.
    assert rest.orders[0]["tgt_ccy"] == "base_ccy"
    assert rest.orders[0]["ord_type"] == "market"


def test_resubmitting_the_same_plan_is_blocked_by_the_journal(store):
    rest = FakeRest()
    executor = Executor(rest, store)
    p = plan()
    intent = executor.build_intent(p, approved(1.0), BTC)
    executor.submit_entry(intent, dry_run=False)
    with pytest.raises(DuplicateSubmission, match="already has an entry order"):
        executor.submit_entry(intent, dry_run=False)
    assert len(rest.orders) == 1


def test_a_duplicate_rejected_by_okx_surfaces_as_a_duplicate_not_a_crash(store):
    rest = FakeRest(fail_with=OkxApiError("51006", "clOrdId already exists"), fail_on="order")
    executor = Executor(rest, store)
    intent = executor.build_intent(plan(), approved(1.0), BTC)
    with pytest.raises(DuplicateSubmission):
        executor.submit_entry(intent, dry_run=False)


def test_a_rejected_entry_is_journalled_with_its_reason(store):
    rest = FakeRest(fail_with=OkxApiError("51008", "insufficient balance"), fail_on="order")
    executor = Executor(rest, store)
    p = plan()
    intent = executor.build_intent(p, approved(1.0), BTC)
    with pytest.raises(OkxApiError):
        executor.submit_entry(intent, dry_run=False)
    assert store.orders_for_plan(p.plan_id)[0]["status"] == "rejected"


# ----------------------------------------------------------------- protection


def test_protection_is_sized_from_the_fill_not_from_the_plan(store):
    rest = FakeRest()
    executor = Executor(rest, store)
    p = plan()
    intent = executor.build_intent(p, approved(1.0), BTC)
    executor.place_protection(intent, filled_size="0.4", dry_run=False)
    assert [o["sz"] for o in rest.ocos] == ["0.2", "0.2"]


def test_protection_marks_the_plan_protected(store):
    rest = FakeRest()
    executor = Executor(rest, store)
    p = plan()
    store.save_plan(p, status="submitted")
    intent = executor.build_intent(p, approved(1.0), BTC)
    executor.place_protection(intent, filled_size="1.0", dry_run=False)
    assert store.get_plan_row(p.plan_id)["status"] == "protected"


def test_one_failed_leg_does_not_abort_the_others(store):
    rest = FakeRest(fail_with=OkxApiError("51000", "rate limited"), fail_on="oco")
    executor = Executor(rest, store)
    p = plan()
    intent = executor.build_intent(p, approved(1.0), BTC)
    placed = executor.place_protection(intent, filled_size="1.0", dry_run=False)
    assert len(placed) == 1, "the second leg must still go out"
    kinds = [e["kind"] for e in store.recent_events(10)]
    assert "protection_failed" in kinds


def test_protection_is_not_placed_twice(store):
    rest = FakeRest()
    executor = Executor(rest, store)
    intent = executor.build_intent(plan(), approved(1.0), BTC)
    executor.place_protection(intent, filled_size="1.0", dry_run=False)
    executor.place_protection(intent, filled_size="1.0", dry_run=False)
    assert len(rest.ocos) == 2


def test_a_runner_leg_uses_a_stop_only_order(store):
    rest = FakeRest()
    executor = Executor(rest, store)
    p = plan(targets=[{"price": 64_000.0, "fraction": 0.5}])
    intent = executor.build_intent(p, approved(1.0), BTC)
    executor.place_protection(intent, filled_size="1.0", dry_run=False)
    assert len(rest.ocos) == 1 and len(rest.stops) == 1
    assert rest.stops[0]["sl_trigger_px"] == "58000"


def test_exits_are_the_opposite_side_of_the_entry(store):
    rest = FakeRest()
    executor = Executor(rest, store)
    intent = executor.build_intent(plan(), approved(1.0), BTC)
    assert intent.exit_side == "sell"
    executor.place_protection(intent, filled_size="1.0", dry_run=False)
    assert all(o["side"] == "sell" for o in rest.ocos)


# ------------------------------------------------- what can actually be sold


def test_a_spot_buy_fee_reduces_what_there_is_to_protect():
    """The fee on a spot buy is taken in BTC, so less BTC arrives than was ordered."""
    order = {"accFillSz": "0.00013333", "fee": "-0.00000013", "feeCcy": "BTC"}
    assert Executor.protectable_size(order, "BTC") == Decimal("0.00013320")


def test_a_quote_denominated_fee_leaves_the_base_quantity_alone():
    # Selling costs USDT; the BTC that changed hands is unaffected.
    order = {"accFillSz": "0.5", "fee": "-40.5", "feeCcy": "USDT"}
    assert Executor.protectable_size(order, "BTC") == Decimal("0.5")


def test_a_rebate_never_inflates_the_protected_quantity():
    order = {"accFillSz": "1.0", "fee": "0.001", "feeCcy": "BTC"}
    assert Executor.protectable_size(order, "BTC") == Decimal("1.0")


def test_an_unfilled_order_has_nothing_to_protect():
    assert Executor.protectable_size({"accFillSz": "0", "fee": "0"}, "BTC") == 0
    assert Executor.protectable_size({}, "BTC") == 0


def test_a_fee_larger_than_the_fill_cannot_go_negative():
    order = {"accFillSz": "0.001", "fee": "-0.002", "feeCcy": "BTC"}
    assert Executor.protectable_size(order, "BTC") == 0


def test_protection_never_exceeds_the_quantity_received(store):
    """The whole point: legs must sum to at most what is actually held."""
    rest = FakeRest()
    executor = Executor(rest, store)
    order = {"accFillSz": "0.00013333", "fee": "-0.00000013", "feeCcy": "BTC"}
    held = Executor.protectable_size(order, "BTC")

    p = plan(size={"mode": "quote", "value": 10.0})
    intent = executor.build_intent(p, approved(held), LIVE_BTC)
    executor.place_protection(intent, LIVE_BTC.size(held), dry_run=False)

    sold = sum(Decimal(o["sz"]) for o in rest.ocos) + sum(Decimal(o["sz"]) for o in rest.stops)
    assert sold <= held, "never offer to sell more than the account received"
    assert held - sold < LIVE_BTC.min_sz, "the shortfall must be unprotectable dust"


def test_the_fee_subtraction_is_exact_rather_than_float_drift():
    """Observed live: float arithmetic lost a satoshi to the lot-grid floor.

    0.00012296 - 0.00000024 is 0.00012272 in decimal, but just under it in
    binary, so flooring produced 0.00012271 and one unit went unprotected.
    """
    order = {"accFillSz": "0.00012296", "fee": "-0.00000024", "feeCcy": "BTC"}
    assert Executor.protectable_size(order, "BTC") == Decimal("0.00012272")
    assert LIVE_BTC.size(Executor.protectable_size(order, "BTC")) == "0.00012272"


def test_an_exact_halving_produces_no_dust(store):
    """0.00012272 splits evenly, so neither leg should need a dust merge."""
    rest = FakeRest()
    executor = Executor(rest, store)
    held = Executor.protectable_size(
        {"accFillSz": "0.00012296", "fee": "-0.00000024", "feeCcy": "BTC"}, "BTC"
    )
    intent = executor.build_intent(plan(), approved(held), LIVE_BTC)
    assert [leg.size for leg in intent.legs] == ["0.00006136", "0.00006136"]
    assert not any("dust" in note for note in intent.notes)
