"""The risk gate.

Every plan passes through here before a single byte reaches the exchange. The
gate is deterministic, has no network calls of its own, and is the one module
in this package whose behaviour is fully covered by tests.

It does two jobs:

1. **Reject** plans that violate a hard limit. Rejections are never repaired --
   a plan that risks 8% of the account is not a 1.5% plan with a typo.
2. **Cap** position size down to the smallest ceiling that applies, and say so
   loudly. Capping is visible in the confirmation prompt; it never happens
   quietly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .config import RiskLimits
from .plan import TradePlan

# Taker fees plus a little room, so a buy sized to the last decimal of the
# balance does not bounce with "insufficient funds".
FEE_HEADROOM = 1.0015


@dataclass
class AccountState:
    """The account facts the gate needs, fetched once per evaluation."""

    equity_quote: float
    available_quote: float
    available_base: float = 0.0


@dataclass
class RiskDecision:
    approved: bool
    base_size: float = 0.0
    notional: float = 0.0
    risk_quote: float = 0.0
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def render(self) -> str:
        head = "APPROVED" if self.approved else "REJECTED"
        lines = [f"risk gate: {head}"]
        if self.approved:
            lines.append(
                f"  size {self.base_size:.8f} base / {self.notional:.2f} quote"
                f"   risking {self.risk_quote:.2f}"
            )
        for warning in self.warnings:
            lines.append(f"  ! {warning}")
        for reason in self.reasons:
            lines.append(f"  x {reason}")
        return "\n".join(lines)


class RiskGate:
    def __init__(self, limits: RiskLimits, store=None):
        self.limits = limits
        self.store = store

    def evaluate(
        self,
        plan: TradePlan,
        account: AccountState,
        last_price: float,
        now: datetime | None = None,
        open_plan_count: int | None = None,
        realized_24h: float | None = None,
    ) -> RiskDecision:
        now = now or datetime.now(timezone.utc)
        limits = self.limits
        reasons: list[str] = []
        warnings: list[str] = []

        # ---------------------------------------------------------- eligibility
        if plan.inst_id not in limits.symbol_whitelist:
            reasons.append(
                f"{plan.inst_id} is not whitelisted (allowed: {', '.join(limits.symbol_whitelist)})"
            )

        if plan.is_expired(now):
            reasons.append(f"plan expired at {plan.expires_at.isoformat()}")

        age = plan.age_seconds(now)
        if age > limits.max_plan_age_seconds:
            reasons.append(
                f"plan is {age / 60:.0f} min old, limit is {limits.max_plan_age_seconds / 60:.0f} min"
            )

        if plan.risk_reward < limits.min_risk_reward:
            reasons.append(
                f"R:R {plan.risk_reward:.2f} is below the {limits.min_risk_reward:.2f} minimum"
            )

        # ------------------------------------------------------- price reality
        if last_price <= 0:
            reasons.append("no usable last price")
        else:
            # The market may have already done what the plan was waiting for.
            through_stop = last_price <= plan.stop_price if plan.is_long else last_price >= plan.stop_price
            if through_stop:
                reasons.append(
                    f"last price {last_price:g} is already through the stop {plan.stop_price:g}"
                )

            past_first_target = (
                last_price >= plan.targets[0].price if plan.is_long else last_price <= plan.targets[0].price
            )
            if past_first_target:
                reasons.append(
                    f"last price {last_price:g} has already reached target1 {plan.targets[0].price:g}"
                )

            slippage = 100.0 * abs(last_price - plan.entry_price) / plan.entry_price
            if plan.entry_type == "market" and slippage > limits.max_slippage_pct:
                reasons.append(
                    f"market entry would slip {slippage:.2f}% from the planned "
                    f"{plan.entry_price:g} (limit {limits.max_slippage_pct:.2f}%)"
                )
            elif slippage > limits.max_slippage_pct:
                warnings.append(
                    f"last price {last_price:g} is {slippage:.2f}% away from the "
                    f"{plan.entry_price:g} limit; it may not fill"
                )

        # ------------------------------------------------------- portfolio state
        if open_plan_count is None and self.store is not None:
            open_plan_count = self.store.open_plan_count()
        open_plan_count = open_plan_count or 0
        if open_plan_count >= limits.max_open_plans:
            reasons.append(
                f"{open_plan_count} plans already open, limit is {limits.max_open_plans}"
            )

        if realized_24h is None and self.store is not None:
            realized_24h = self.store.realized_today(now)
        realized_24h = realized_24h or 0.0
        if realized_24h <= -abs(limits.max_daily_loss_quote):
            reasons.append(
                f"kill switch: {realized_24h:.2f} {limits.quote_ccy} realised in the last 24h "
                f"(limit {-abs(limits.max_daily_loss_quote):.2f})"
            )

        # ----------------------------------------------------------- position size
        requested = self._requested_base_size(plan, account)
        if requested <= 0:
            reasons.append("requested size resolves to zero")
            return RiskDecision(approved=False, reasons=reasons, warnings=warnings)

        base_size, caps = self._apply_caps(plan, account, requested)
        for label, capped_to in caps:
            warnings.append(
                f"size capped by {label}: {requested:.8f} -> {capped_to:.8f} base"
            )

        notional = base_size * plan.entry_price
        risk_quote = base_size * plan.risk_per_unit

        if base_size <= 0:
            reasons.append("every size cap reduced this position to zero")

        # ------------------------------------------------------------- funding
        if plan.is_long:
            needed = notional * FEE_HEADROOM
            if needed > account.available_quote:
                reasons.append(
                    f"needs {needed:.2f} {limits.quote_ccy} but only "
                    f"{account.available_quote:.2f} is available"
                )
        elif base_size > account.available_base:
            reasons.append(
                f"needs {base_size:.8f} base but only {account.available_base:.8f} is available"
            )

        return RiskDecision(
            approved=not reasons,
            base_size=base_size,
            notional=notional,
            risk_quote=risk_quote,
            reasons=reasons,
            warnings=warnings,
        )

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _requested_base_size(plan: TradePlan, account: AccountState) -> float:
        """Translate the plan's size spec into a base-currency quantity."""
        mode, value = plan.size.mode, plan.size.value
        if mode == "risk_pct":
            risk_budget = account.equity_quote * value / 100.0
            per_unit = plan.risk_per_unit
            return risk_budget / per_unit if per_unit > 0 else 0.0
        if mode == "quote":
            return value / plan.entry_price
        if mode == "base":
            return value
        return 0.0

    def _apply_caps(
        self, plan: TradePlan, account: AccountState, requested: float
    ) -> tuple[float, list[tuple[str, float]]]:
        """Return the capped size plus a record of which ceilings actually bit."""
        limits = self.limits
        candidates: list[tuple[str, float]] = [
            ("max_notional_per_trade", limits.max_notional_per_trade / plan.entry_price),
            (
                "max_account_fraction_per_trade",
                account.equity_quote * limits.max_account_fraction_per_trade / plan.entry_price,
            ),
        ]
        if plan.risk_per_unit > 0:
            candidates.append(
                (
                    "max_risk_pct_per_trade",
                    account.equity_quote * limits.max_risk_pct_per_trade / 100.0 / plan.risk_per_unit,
                )
            )

        final = requested
        applied: list[tuple[str, float]] = []
        for label, ceiling in candidates:
            if ceiling < final:
                final = ceiling
                applied.append((label, ceiling))

        # Only the binding cap is interesting; the rest were never reached.
        return final, applied[-1:]
