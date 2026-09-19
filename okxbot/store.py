"""SQLite journal.

Every plan, every submitted order and every realised P&L line is written here
before or immediately after it hits the exchange. On restart the bot rebuilds
its view of the world from this file *and* from OKX, then compares the two --
so the journal exists to be contradicted, not trusted blindly.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    plan_id      TEXT PRIMARY KEY,
    inst_id      TEXT NOT NULL,
    side         TEXT NOT NULL,
    status       TEXT NOT NULL,
    payload      TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    cl_ord_id    TEXT PRIMARY KEY,
    plan_id      TEXT NOT NULL,
    inst_id      TEXT NOT NULL,
    role         TEXT NOT NULL,
    ord_id       TEXT,
    algo_id      TEXT,
    side         TEXT NOT NULL,
    ord_type     TEXT NOT NULL,
    sz           TEXT NOT NULL,
    px           TEXT,
    status       TEXT NOT NULL,
    raw          TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    kind         TEXT NOT NULL,
    plan_id      TEXT,
    detail       TEXT
);

CREATE TABLE IF NOT EXISTS realized (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    plan_id      TEXT,
    inst_id      TEXT NOT NULL,
    pnl_quote    REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orders_plan ON orders(plan_id);
CREATE INDEX IF NOT EXISTS idx_realized_ts ON realized(ts);
CREATE INDEX IF NOT EXISTS idx_plans_status ON plans(status);
"""

# A plan is "live" while it can still cost money.
OPEN_STATUSES = ("submitted", "entry_filled", "protected")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: str = "okxbot.sqlite3"):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def tx(self):
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # ------------------------------------------------------------------ plans

    def save_plan(self, plan, status: str = "draft") -> None:
        with self.tx() as conn:
            conn.execute(
                """INSERT INTO plans (plan_id, inst_id, side, status, payload, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(plan_id) DO UPDATE SET
                       status=excluded.status,
                       payload=excluded.payload,
                       updated_at=excluded.updated_at""",
                (
                    plan.plan_id,
                    plan.inst_id,
                    plan.side,
                    status,
                    plan.to_json(indent=0),
                    plan.created_at.isoformat(),
                    _now(),
                ),
            )

    def set_plan_status(self, plan_id: str, status: str) -> None:
        with self.tx() as conn:
            conn.execute(
                "UPDATE plans SET status=?, updated_at=? WHERE plan_id=?", (status, _now(), plan_id)
            )

    def get_plan_row(self, plan_id: str) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()

    def open_plans(self) -> list[sqlite3.Row]:
        marks = ",".join("?" * len(OPEN_STATUSES))
        return self._conn.execute(
            f"SELECT * FROM plans WHERE status IN ({marks}) ORDER BY created_at", OPEN_STATUSES
        ).fetchall()

    def open_plan_count(self) -> int:
        marks = ",".join("?" * len(OPEN_STATUSES))
        row = self._conn.execute(
            f"SELECT COUNT(*) AS n FROM plans WHERE status IN ({marks})", OPEN_STATUSES
        ).fetchone()
        return int(row["n"])

    # ----------------------------------------------------------------- orders

    def order_exists(self, cl_ord_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM orders WHERE cl_ord_id=? LIMIT 1", (cl_ord_id,)
        ).fetchone()
        return row is not None

    def record_order(
        self,
        cl_ord_id: str,
        plan_id: str,
        inst_id: str,
        role: str,
        side: str,
        ord_type: str,
        sz: str,
        px: str | None = None,
        ord_id: str | None = None,
        algo_id: str | None = None,
        status: str = "submitted",
        raw: dict | None = None,
    ) -> None:
        with self.tx() as conn:
            conn.execute(
                """INSERT INTO orders
                   (cl_ord_id, plan_id, inst_id, role, ord_id, algo_id, side, ord_type,
                    sz, px, status, raw, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(cl_ord_id) DO UPDATE SET
                       ord_id=COALESCE(excluded.ord_id, orders.ord_id),
                       algo_id=COALESCE(excluded.algo_id, orders.algo_id),
                       status=excluded.status,
                       raw=excluded.raw""",
                (
                    cl_ord_id, plan_id, inst_id, role, ord_id, algo_id, side, ord_type,
                    sz, px, status, json.dumps(raw or {}), _now(),
                ),
            )

    def orders_for_plan(self, plan_id: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM orders WHERE plan_id=? ORDER BY created_at", (plan_id,)
        ).fetchall()

    # ------------------------------------------------------- events & accounting

    def log_event(self, kind: str, detail: str = "", plan_id: str | None = None) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO events (ts, kind, plan_id, detail) VALUES (?,?,?,?)",
                (_now(), kind, plan_id, detail),
            )

    def last_event_at(self, kind: str, plan_id: str) -> datetime | None:
        """When this plan last produced an event of this kind, if ever.

        Used to prove a preview happened before an approval was acted on.
        """
        row = self._conn.execute(
            "SELECT ts FROM events WHERE kind=? AND plan_id=? ORDER BY id DESC LIMIT 1",
            (kind, plan_id),
        ).fetchone()
        return datetime.fromisoformat(row["ts"]) if row else None

    def recent_events(self, limit: int = 30) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    def record_realized(self, inst_id: str, pnl_quote: float, plan_id: str | None = None) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO realized (ts, plan_id, inst_id, pnl_quote) VALUES (?,?,?,?)",
                (_now(), plan_id, inst_id, float(pnl_quote)),
            )

    def realized_since(self, since: datetime) -> float:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(pnl_quote), 0.0) AS total FROM realized WHERE ts >= ?",
            (since.isoformat(),),
        ).fetchone()
        return float(row["total"])

    def realized_today(self, now: datetime | None = None) -> float:
        """Rolling 24h, not calendar day.

        A calendar reset hands a losing strategy a fresh budget at midnight; a
        rolling window does not.
        """
        now = now or datetime.now(timezone.utc)
        return self.realized_since(now - timedelta(hours=24))
