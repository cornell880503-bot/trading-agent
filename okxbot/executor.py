"""Order submission and position protection.

Two invariants govern everything here:

**Protection lives at the exchange.** Once an entry fills, the take-profit and
stop-loss are resting OKX algo orders. This process can then crash, lose its
network, or be killed by the OOM reaper, and the position is still guarded.
Nothing in this file implements "watch the price and sell when it drops" --
that pattern dies with the process that runs it.

**Every order carries a deterministic client id** derived from the plan id, so
submitting the same plan twice is rejected by OKX as a duplicate instead of
doubling the position. The local journal is checked first, but it is the
belt; ``clOrdId`` is the braces.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal

from .errors import OkxApiError, OkxBotError
from .okx.precision import InstrumentSpec, fmt, floor_to_step
from .plan import TradePlan
from .risk import RiskDecision

log = logging.getLogger(__name__)

# OKX rejects a second order carrying a clOrdId it has already seen.
DUPLICATE_CLORDID_CODES = {"51006", "51603", "1015"}


class DuplicateSubmission(OkxBotError):
    """This plan has already been submitted; refusing to submit it again."""


@dataclass
class Leg:
    """One protective order: a take-profit paired with the shared stop, or,
    for the residual runner, the stop alone."""

    role: str
    size: str
    take_profit: str | None
    stop: str
    client_id: str

    @property
    def is_runner(self) -> bool:
        return self.take_profit is None


@dataclass
class ExecutionIntent:
    """Exactly what will be sent, resolved to exchange-grid strings.

    Built before anything is transmitted so the confirmation prompt shows the
    real numbers -- not the plan's pre-rounding intent.
    """

    plan: TradePlan
    spec: InstrumentSpec
    entry_size: str
    entry_price: str | None
    entry_client_id: str
    exit_side: str
    legs: list[Leg] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def render(self) -> str:
        plan = self.plan
        px = f"limit @ {self.entry_price}" if self.entry_price else "market"
        lines = [
            f"execution intent for plan {plan.plan_id} on {plan.inst_id}",
            f"  entry  {plan.side.upper():<4} {self.entry_size} {self.spec.base_ccy} {px}",
            f"         clOrdId={self.entry_client_id}",
            "  protection (placed once the entry fills):",
        ]
        for leg in self.legs:
            if leg.is_runner:
                lines.append(f"    runner  {leg.size:>14}   stop {leg.stop}  (no take-profit)")
            else:
                lines.append(
                    f"    {leg.role:<7} {leg.size:>14}   TP {leg.take_profit}  /  SL {leg.stop}"
                )
        for note in self.notes:
            lines.append(f"  ! {note}")
        return "\n".join(lines)


class Executor:
    def __init__(self, rest, store, quote_ccy: str = "USDT"):
        self.rest = rest
        self.store = store
        self.quote_ccy = quote_ccy

    # ------------------------------------------------------------------ build

    def build_intent(self, plan: TradePlan, decision: RiskDecision, spec: InstrumentSpec) -> ExecutionIntent:
        if not decision.approved:
            raise OkxBotError("refusing to build an intent from a rejected risk decision")

        entry_size = spec.size(decision.base_size)
        entry_price = spec.price(plan.entry_price) if plan.entry_type == "limit" else None
        exit_side = "sell" if plan.is_long else "buy"
        stop_px = spec.price(plan.stop_price)

        legs, notes = self._allocate_legs(plan, spec, Decimal(entry_size), exit_side, stop_px)

        return ExecutionIntent(
            plan=plan,
            spec=spec,
            entry_size=entry_size,
            entry_price=entry_price,
            entry_client_id=plan.client_id("e"),
            exit_side=exit_side,
            legs=legs,
            notes=notes,
        )

    def _allocate_legs(
        self, plan: TradePlan, spec: InstrumentSpec, total: Decimal, exit_side: str, stop_px: str
    ) -> tuple[list[Leg], list[str]]:
        """Split the filled quantity across take-profit legs.

        Lot rounding means the fractions never divide cleanly. Legs that round
        below ``minSz`` are dropped and their share flows to the remainder,
        which is protected by a stop-only order rather than left naked.
        """
        legs: list[Leg] = []
        notes: list[str] = []
        assigned = Decimal(0)

        for index, target in enumerate(plan.targets, start=1):
            raw = total * Decimal(str(target.fraction))
            size = spec.size_or_none(raw)
            if size is None:
                notes.append(
                    f"target{index} ({target.fraction:.0%}) rounds below minSz "
                    f"{fmt(spec.min_sz)} and was folded into the runner"
                )
                continue
            assigned += Decimal(size)
            legs.append(
                Leg(
                    role=f"tp{index}",
                    size=size,
                    take_profit=spec.price(target.price),
                    stop=stop_px,
                    client_id=plan.client_id(f"t{index}"),
                )
            )

        residual = total - assigned
        if residual > 0:
            runner = spec.size_or_none(residual)
            if runner is not None:
                legs.append(
                    Leg(
                        role="runner",
                        size=runner,
                        take_profit=None,
                        stop=stop_px,
                        client_id=plan.client_id("r"),
                    )
                )
                notes.append(
                    f"{runner} {spec.base_ccy} has no take-profit and rides on the stop alone"
                )
            elif legs:
                # Dust below minSz cannot get its own order. Fold it into the
                # last leg so the whole position stays covered by a stop.
                last = legs[-1]
                merged = floor_to_step(Decimal(last.size) + residual, spec.lot_sz)
                notes.append(
                    f"{fmt(residual)} {spec.base_ccy} of dust folded into {last.role}"
                )
                legs[-1] = Leg(
                    role=last.role,
                    size=fmt(merged),
                    take_profit=last.take_profit,
                    stop=last.stop,
                    client_id=last.client_id,
                )

        if not legs:
            notes.append("WARNING: no protective order could be sized; entry will be UNPROTECTED")
        return legs, notes

    # ----------------------------------------------------------------- submit

    def submit_entry(self, intent: ExecutionIntent, dry_run: bool = True) -> dict | None:
        plan = intent.plan
        if self.store.order_exists(intent.entry_client_id):
            raise DuplicateSubmission(
                f"plan {plan.plan_id} already has an entry order "
                f"({intent.entry_client_id}); use 'sync' to check its state"
            )

        if dry_run:
            log.info("[dry-run] would submit entry %s", intent.entry_client_id)
            self.store.log_event("dry_run_entry", intent.render(), plan.plan_id)
            return None

        ord_type = "limit" if intent.entry_price else "market"

        # Write the intent down *before* transmitting. If the response is lost
        # in flight, the journal still knows this clOrdId is in play and 'sync'
        # can go ask the exchange what happened to it.
        self.store.record_order(
            cl_ord_id=intent.entry_client_id,
            plan_id=plan.plan_id,
            inst_id=plan.inst_id,
            role="entry",
            side=plan.side,
            ord_type=ord_type,
            sz=intent.entry_size,
            px=intent.entry_price,
            status="sending",
        )

        try:
            result = self.rest.place_order(
                inst_id=plan.inst_id,
                side=plan.side,
                ord_type=ord_type,
                sz=intent.entry_size,
                px=intent.entry_price,
                cl_ord_id=intent.entry_client_id,
                # Always explicit: OKX reads sz on a SPOT market buy as quote
                # currency unless told otherwise, and our sizing is in base.
                tgt_ccy="base_ccy" if ord_type == "market" else None,
            )
        except OkxApiError as exc:
            if exc.code in DUPLICATE_CLORDID_CODES:
                self.store.record_order(
                    cl_ord_id=intent.entry_client_id,
                    plan_id=plan.plan_id,
                    inst_id=plan.inst_id,
                    role="entry",
                    side=plan.side,
                    ord_type=ord_type,
                    sz=intent.entry_size,
                    px=intent.entry_price,
                    status="duplicate",
                )
                raise DuplicateSubmission(
                    f"OKX already holds clOrdId {intent.entry_client_id}: {exc.msg}"
                ) from exc
            self.store.record_order(
                cl_ord_id=intent.entry_client_id,
                plan_id=plan.plan_id,
                inst_id=plan.inst_id,
                role="entry",
                side=plan.side,
                ord_type=ord_type,
                sz=intent.entry_size,
                px=intent.entry_price,
                status="rejected",
                raw={"code": exc.code, "msg": exc.msg},
            )
            raise

        self.store.record_order(
            cl_ord_id=intent.entry_client_id,
            plan_id=plan.plan_id,
            inst_id=plan.inst_id,
            role="entry",
            side=plan.side,
            ord_type=ord_type,
            sz=intent.entry_size,
            px=intent.entry_price,
            ord_id=result.get("ordId"),
            status="submitted",
            raw=result,
        )
        self.store.save_plan(plan, status="submitted")
        self.store.log_event("entry_submitted", f"ordId={result.get('ordId')}", plan.plan_id)
        return result

    def place_protection(self, intent: ExecutionIntent, filled_size: str, dry_run: bool = True) -> list[dict]:
        """Attach exchange-side exits to a filled entry.

        Re-derives the legs from the quantity actually filled -- a partial fill
        must not be protected as though it were whole.
        """
        plan = intent.plan
        spec = intent.spec
        legs, notes = self._allocate_legs(
            plan, spec, Decimal(filled_size), intent.exit_side, spec.price(plan.stop_price)
        )
        for note in notes:
            log.warning("%s: %s", plan.plan_id, note)

        placed: list[dict] = []
        for leg in legs:
            if self.store.order_exists(leg.client_id):
                log.info("%s: %s already placed, skipping", plan.plan_id, leg.role)
                continue

            if dry_run:
                log.info("[dry-run] would place %s %s", leg.role, leg.size)
                continue

            try:
                if leg.is_runner:
                    result = self.rest.place_stop(
                        inst_id=plan.inst_id,
                        side=intent.exit_side,
                        sz=leg.size,
                        sl_trigger_px=leg.stop,
                        algo_cl_ord_id=leg.client_id,
                    )
                else:
                    result = self.rest.place_oco(
                        inst_id=plan.inst_id,
                        side=intent.exit_side,
                        sz=leg.size,
                        tp_trigger_px=leg.take_profit,
                        sl_trigger_px=leg.stop,
                        algo_cl_ord_id=leg.client_id,
                    )
            except OkxApiError as exc:
                # One failed leg must not abort the others: partial protection
                # beats none. The failure is journalled and surfaced by 'status'.
                log.error("%s: %s failed (%s) %s", plan.plan_id, leg.role, exc.code, exc.msg)
                self.store.log_event(
                    "protection_failed", f"{leg.role}: {exc.code} {exc.msg}", plan.plan_id
                )
                continue

            self.store.record_order(
                cl_ord_id=leg.client_id,
                plan_id=plan.plan_id,
                inst_id=plan.inst_id,
                role=leg.role,
                side=intent.exit_side,
                ord_type="conditional" if leg.is_runner else "oco",
                sz=leg.size,
                px=leg.take_profit,
                algo_id=result.get("algoId"),
                status="live",
                raw=result,
            )
            placed.append(result)

        if placed and not dry_run:
            self.store.set_plan_status(plan.plan_id, "protected")
            self.store.log_event("protected", f"{len(placed)} leg(s) live", plan.plan_id)
        return placed
