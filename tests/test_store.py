from datetime import datetime, timedelta, timezone

import pytest

from okxbot.plan import TradePlan
from okxbot.store import Store

NOW = datetime.now(timezone.utc)


def plan(plan_id="abcdef123456", **overrides) -> TradePlan:
    payload = {
        "inst_id": "BTC-USDT", "side": "buy",
        "entry": {"type": "limit", "price": 60_000.0}, "stop": {"price": 58_000.0},
        "targets": [{"price": 64_000.0, "fraction": 1.0}],
        "size": {"mode": "risk_pct", "value": 1.0},
        "plan_id": plan_id, "expires_at": (NOW + timedelta(days=1)).isoformat(),
    }
    payload.update(overrides)
    return TradePlan.from_dict(payload)


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


def test_a_saved_plan_survives_a_json_round_trip(store):
    p = plan()
    store.save_plan(p, status="submitted")
    restored = TradePlan.from_json(store.get_plan_row(p.plan_id)["payload"])
    assert restored.entry_price == p.entry_price
    assert restored.stop_price == p.stop_price
    assert restored.client_id("e") == p.client_id("e")


def test_saving_the_same_plan_twice_updates_rather_than_duplicates(store):
    p = plan()
    store.save_plan(p, status="draft")
    store.save_plan(p, status="submitted")
    assert store.get_plan_row(p.plan_id)["status"] == "submitted"
    assert store.open_plan_count() == 1


def test_only_live_statuses_count_towards_the_open_limit(store):
    store.save_plan(plan("aaa111"), status="submitted")
    store.save_plan(plan("bbb222"), status="protected")
    store.save_plan(plan("ccc333"), status="closed")
    store.save_plan(plan("ddd444"), status="rejected")
    assert store.open_plan_count() == 2


def test_order_ids_are_unique_and_recording_twice_merges(store):
    store.record_order("cid1", "p1", "BTC-USDT", "entry", "buy", "limit", "1", status="sending")
    store.record_order("cid1", "p1", "BTC-USDT", "entry", "buy", "limit", "1",
                       ord_id="ord9", status="submitted")
    rows = store.orders_for_plan("p1")
    assert len(rows) == 1
    assert rows[0]["ord_id"] == "ord9" and rows[0]["status"] == "submitted"


def test_a_later_write_never_erases_an_exchange_id(store):
    store.record_order("cid1", "p1", "BTC-USDT", "entry", "buy", "limit", "1", ord_id="ord9")
    store.record_order("cid1", "p1", "BTC-USDT", "entry", "buy", "limit", "1", status="filled")
    assert store.orders_for_plan("p1")[0]["ord_id"] == "ord9"


def test_order_exists_is_the_idempotency_check(store):
    assert not store.order_exists("cid1")
    store.record_order("cid1", "p1", "BTC-USDT", "entry", "buy", "limit", "1")
    assert store.order_exists("cid1")


def test_realised_pnl_uses_a_rolling_24h_window(store):
    store.record_realized("BTC-USDT", -50.0)
    store.record_realized("BTC-USDT", 20.0)
    assert store.realized_today() == pytest.approx(-30.0)
    # A window that starts in the future sees nothing.
    assert store.realized_since(NOW + timedelta(hours=1)) == 0.0


def test_events_come_back_newest_first(store):
    store.log_event("first", "a")
    store.log_event("second", "b")
    assert [e["kind"] for e in store.recent_events(2)] == ["second", "first"]
