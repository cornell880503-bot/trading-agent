"""The TradePlan contract.

This is the only interface between the analysis layer (slow, LLM-driven,
non-deterministic) and the execution layer (fast, pure code, unit-tested).
A plan is data. It is validated on the way in, and nothing downstream trusts
a field it has not checked.

Keeping the boundary this narrow is what makes the system auditable: every
trade traces back to exactly one JSON document, stored verbatim.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .errors import PlanError

SCHEMA_VERSION = 1
VALID_SIDES = ("buy", "sell")
VALID_ENTRY_TYPES = ("limit", "market")
VALID_SIZE_MODES = ("risk_pct", "quote", "base")

# Handed to the analysis layer verbatim so the JSON it emits parses first time.
SCHEMA_HINT = """\
{
  "version": 1,
  "inst_id": "BTC-USDT",
  "timeframe": "4H",
  "side": "buy",
  "entry": {"type": "limit", "price": 61500.0},
  "stop":  {"price": 59800.0},
  "targets": [
    {"price": 65000.0, "fraction": 0.5},
    {"price": 68000.0, "fraction": 0.5}
  ],
  "size": {"mode": "risk_pct", "value": 1.0},
  "invalidation": "4H close below 59800, or ADX falls under 18",
  "expires_at": "2026-09-21T00:00:00Z",
  "confidence": 0.62,
  "rationale": "Reclaim of the 50EMA with rising ADX; stop under the 12 Sep swing low."
}

size.mode:
  risk_pct - value is the percent of quote equity lost if the stop is hit.
             This is the only mode that keeps position size proportional to
             stop distance; prefer it.
  quote    - value is a fixed notional in the quote currency (e.g. 500 USDT).
  base     - value is a fixed base quantity (e.g. 0.01 BTC).

Rules the validator enforces (a plan breaking any of these is rejected, not repaired):
  - buy  => stop.price < entry.price < every target.price
  - sell => stop.price > entry.price > every target.price
  - target fractions are each in (0, 1] and sum to <= 1.0
  - expires_at is in the future and in UTC
  - entry.price is required even for market orders: it is the expected fill,
    and execution aborts if the live price has slipped past max_slippage_pct.
"""


def _utc(value, field_name: str) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:
            raise PlanError(f"{field_name}: {value!r} is not an ISO-8601 timestamp") from exc
    else:
        raise PlanError(f"{field_name}: expected a timestamp, got {type(value).__name__}")
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _positive_float(value, field_name: str) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise PlanError(f"{field_name}: {value!r} is not a number") from exc
    if not (out > 0) or out != out or out in (float("inf"), float("-inf")):
        raise PlanError(f"{field_name}: must be a finite positive number, got {value!r}")
    return out


@dataclass(frozen=True)
class Target:
    price: float
    fraction: float


@dataclass(frozen=True)
class SizeSpec:
    mode: str
    value: float


@dataclass
class TradePlan:
    inst_id: str
    side: str
    entry_type: str
    entry_price: float
    stop_price: float
    targets: list[Target]
    size: SizeSpec
    expires_at: datetime
    timeframe: str = "4H"
    invalidation: str = ""
    rationale: str = ""
    confidence: float = 0.5
    plan_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    version: int = SCHEMA_VERSION

    # ------------------------------------------------------------ derived math

    @property
    def is_long(self) -> bool:
        return self.side == "buy"

    @property
    def risk_per_unit(self) -> float:
        """Distance from entry to stop, in quote currency per unit of base."""
        return abs(self.entry_price - self.stop_price)

    @property
    def risk_pct_of_entry(self) -> float:
        return 100.0 * self.risk_per_unit / self.entry_price

    def reward_per_unit(self, target: Target | None = None) -> float:
        tgt = target or self.targets[0]
        return abs(tgt.price - self.entry_price)

    @property
    def risk_reward(self) -> float:
        """R:R against the *first* target -- the one most likely to be reached."""
        risk = self.risk_per_unit
        return self.reward_per_unit() / risk if risk else 0.0

    @property
    def expected_r(self) -> float:
        """Fraction-weighted R multiple assuming every target fills.

        Optimistic by construction (it ignores the stop), so it is a comparison
        aid between plans, never a forecast.
        """
        risk = self.risk_per_unit
        if not risk:
            return 0.0
        return sum(self.reward_per_unit(t) * t.fraction for t in self.targets) / risk

    def is_expired(self, now: datetime | None = None) -> bool:
        return (now or datetime.now(timezone.utc)) >= self.expires_at

    def age_seconds(self, now: datetime | None = None) -> float:
        return ((now or datetime.now(timezone.utc)) - self.created_at).total_seconds()

    # --------------------------------------------------------- client order ids

    def client_id(self, suffix: str) -> str:
        """Deterministic ``clOrdId`` -- the idempotency key for this plan.

        Derived from the plan id, so re-submitting the same plan is rejected by
        OKX as a duplicate instead of opening a second position. OKX allows
        1-32 alphanumeric characters.
        """
        raw = f"p{self.plan_id}{suffix}"
        cleaned = "".join(ch for ch in raw if ch.isalnum())[:32]
        if not cleaned:
            raise PlanError(f"cannot derive a client id from plan {self.plan_id!r}")
        return cleaned

    # ------------------------------------------------------------- (de)serialise

    def to_dict(self) -> dict:
        """Emit the same nested shape :meth:`from_dict` accepts.

        Serialisation must round-trip: the journal stores this JSON and later
        commands (``sync``, ``status``, ``cancel``) rebuild the plan from it.
        A flat dump would parse back as a plan with no entry price.
        """
        return {
            "version": self.version,
            "plan_id": self.plan_id,
            "inst_id": self.inst_id,
            "timeframe": self.timeframe,
            "side": self.side,
            "entry": {"type": self.entry_type, "price": self.entry_price},
            "stop": {"price": self.stop_price},
            "targets": [{"price": t.price, "fraction": t.fraction} for t in self.targets],
            "size": {"mode": self.size.mode, "value": self.size.value},
            "invalidation": self.invalidation,
            "rationale": self.rationale,
            "confidence": self.confidence,
            "created_at": self.created_at.isoformat().replace("+00:00", "Z"),
            "expires_at": self.expires_at.isoformat().replace("+00:00", "Z"),
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "TradePlan":
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise PlanError(f"plan is not valid JSON: {exc}") from exc
        return cls.from_dict(payload)

    @classmethod
    def from_dict(cls, payload: dict) -> "TradePlan":
        if not isinstance(payload, dict):
            raise PlanError(f"plan must be a JSON object, got {type(payload).__name__}")

        version = int(payload.get("version", SCHEMA_VERSION))
        if version != SCHEMA_VERSION:
            raise PlanError(f"unsupported plan version {version}; this build speaks v{SCHEMA_VERSION}")

        inst_id = str(payload.get("inst_id", "")).strip().upper()
        if inst_id.count("-") != 1 or not all(inst_id.split("-")):
            raise PlanError(f"inst_id: expected SPOT pair like 'BTC-USDT', got {inst_id!r}")

        side = str(payload.get("side", "")).strip().lower()
        if side not in VALID_SIDES:
            raise PlanError(f"side: must be one of {VALID_SIDES}, got {side!r}")

        entry = payload.get("entry") or {}
        if not isinstance(entry, dict):
            raise PlanError("entry: must be an object with 'type' and 'price'")
        entry_type = str(entry.get("type", "limit")).strip().lower()
        if entry_type not in VALID_ENTRY_TYPES:
            raise PlanError(f"entry.type: must be one of {VALID_ENTRY_TYPES}, got {entry_type!r}")
        entry_price = _positive_float(entry.get("price"), "entry.price")

        stop = payload.get("stop") or {}
        if not isinstance(stop, dict):
            raise PlanError("stop: must be an object with 'price'")
        stop_price = _positive_float(stop.get("price"), "stop.price")

        raw_targets = payload.get("targets") or []
        if not isinstance(raw_targets, list) or not raw_targets:
            raise PlanError("targets: at least one target is required")
        targets: list[Target] = []
        for index, raw in enumerate(raw_targets):
            if not isinstance(raw, dict):
                raise PlanError(f"targets[{index}]: must be an object")
            price = _positive_float(raw.get("price"), f"targets[{index}].price")
            fraction = float(raw.get("fraction", 1.0 / len(raw_targets)))
            if not (0.0 < fraction <= 1.0):
                raise PlanError(f"targets[{index}].fraction: must be in (0, 1], got {fraction}")
            targets.append(Target(price=price, fraction=fraction))

        total = sum(t.fraction for t in targets)
        if total > 1.0 + 1e-9:
            raise PlanError(f"targets: fractions sum to {total:.4f}, which exceeds 1.0")

        raw_size = payload.get("size") or {}
        if not isinstance(raw_size, dict):
            raise PlanError("size: must be an object with 'mode' and 'value'")
        mode = str(raw_size.get("mode", "")).strip().lower()
        if mode not in VALID_SIZE_MODES:
            raise PlanError(f"size.mode: must be one of {VALID_SIZE_MODES}, got {mode!r}")
        value = _positive_float(raw_size.get("value"), "size.value")
        if mode == "risk_pct" and value > 100.0:
            raise PlanError(f"size.value: {value} percent risk is not a position, it is a donation")

        # Directional sanity. A plan that gets this wrong is not a plan with a
        # typo; it is a plan whose author was confused about which way it bets.
        if side == "buy":
            if stop_price >= entry_price:
                raise PlanError(
                    f"buy: stop {stop_price} must sit below entry {entry_price}"
                )
            bad = [t.price for t in targets if t.price <= entry_price]
            if bad:
                raise PlanError(f"buy: targets {bad} must sit above entry {entry_price}")
        else:
            if stop_price <= entry_price:
                raise PlanError(
                    f"sell: stop {stop_price} must sit above entry {entry_price}"
                )
            bad = [t.price for t in targets if t.price >= entry_price]
            if bad:
                raise PlanError(f"sell: targets {bad} must sit below entry {entry_price}")

        created_at = _utc(payload["created_at"], "created_at") if payload.get("created_at") else datetime.now(timezone.utc)
        expires_at = _utc(payload.get("expires_at"), "expires_at")
        if expires_at <= created_at:
            raise PlanError(f"expires_at {expires_at.isoformat()} is not after created_at")

        confidence = float(payload.get("confidence", 0.5))
        if not (0.0 <= confidence <= 1.0):
            raise PlanError(f"confidence: must be in [0, 1], got {confidence}")

        plan_id = str(payload.get("plan_id") or uuid.uuid4().hex[:12])
        if not plan_id.isalnum():
            raise PlanError(f"plan_id: must be alphanumeric, got {plan_id!r}")

        # Nearest target first, so partial exits ladder outwards in order.
        targets.sort(key=lambda t: abs(t.price - entry_price))

        return cls(
            inst_id=inst_id,
            side=side,
            entry_type=entry_type,
            entry_price=entry_price,
            stop_price=stop_price,
            targets=targets,
            size=SizeSpec(mode=mode, value=value),
            expires_at=expires_at,
            timeframe=str(payload.get("timeframe", "4H")),
            invalidation=str(payload.get("invalidation", "")),
            rationale=str(payload.get("rationale", "")),
            confidence=confidence,
            plan_id=plan_id,
            created_at=created_at,
            version=version,
        )

    def summary(self) -> str:
        """Human-readable block shown at the confirmation prompt."""
        arrow = "LONG" if self.is_long else "SHORT/EXIT"
        lines = [
            f"plan {self.plan_id}  {arrow} {self.inst_id}  [{self.timeframe}]",
            f"  entry   {self.entry_type:<6} @ {self.entry_price:g}",
            f"  stop            @ {self.stop_price:g}   ({self.risk_pct_of_entry:.2f}% from entry)",
        ]
        for i, t in enumerate(self.targets, 1):
            rr = self.reward_per_unit(t) / self.risk_per_unit if self.risk_per_unit else 0.0
            lines.append(f"  target{i}         @ {t.price:g}   {t.fraction:.0%} of size, {rr:.2f}R")
        lines += [
            f"  size    {self.size.mode} = {self.size.value:g}",
            f"  R:R     {self.risk_reward:.2f} (first target), expected {self.expected_r:.2f}R",
            f"  expires {self.expires_at.isoformat().replace('+00:00', 'Z')}",
            f"  confidence {self.confidence:.0%}",
        ]
        if self.invalidation:
            lines.append(f"  invalidation: {self.invalidation}")
        if self.rationale:
            lines.append(f"  rationale: {self.rationale}")
        return "\n".join(lines)
