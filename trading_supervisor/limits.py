"""
Hard-limit checker and enforcer.

check_all() is pure Python — no LLM in the loop.  The enforcer acts on
violations before the AI analysis step, so limits are always satisfied
regardless of what Claude recommends.
"""
import logging
from typing import Optional

from .config import HardLimits
from .models import AccountSnapshot, LimitViolation, ViolationType

logger = logging.getLogger(__name__)


class LimitChecker:
    def __init__(self, limits: HardLimits, session_open_equity: float, peak_equity: float):
        self.limits = limits
        self.session_open_equity = session_open_equity
        self._peak_equity = peak_equity  # updated via update_peak()

    # ------------------------------------------------------------------
    # State updates
    # ------------------------------------------------------------------

    def update_peak(self, equity: float) -> None:
        if equity > self._peak_equity:
            self._peak_equity = equity

    @property
    def peak_equity(self) -> float:
        return self._peak_equity

    # ------------------------------------------------------------------
    # Public check API
    # ------------------------------------------------------------------

    def check_all(self, snapshot: AccountSnapshot) -> list[LimitViolation]:
        """Return all active limit violations for the given snapshot."""
        violations: list[LimitViolation] = []

        v = self._check_daily_loss(snapshot.equity)
        if v:
            violations.append(v)

        v = self._check_drawdown(snapshot.equity)
        if v:
            violations.append(v)

        violations.extend(self._check_position_sizes(snapshot))

        v = self._check_position_count(snapshot)
        if v:
            violations.append(v)

        v = self._check_cash_reserve(snapshot)
        if v:
            violations.append(v)

        return violations

    def check_order_size(self, usd_value: float) -> Optional[LimitViolation]:
        """Called before placing any order to block oversized trades."""
        if usd_value > self.limits.max_single_order_usd:
            return LimitViolation(
                type=ViolationType.ORDER_SIZE_EXCEEDED,
                message=(
                    f"Order value ${usd_value:,.2f} exceeds hard limit "
                    f"${self.limits.max_single_order_usd:,.2f}."
                ),
                current_value=usd_value,
                limit_value=self.limits.max_single_order_usd,
            )
        return None

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    def _check_daily_loss(self, equity: float) -> Optional[LimitViolation]:
        if self.session_open_equity <= 0:
            return None
        loss_pct = ((self.session_open_equity - equity) / self.session_open_equity) * 100
        if loss_pct >= self.limits.max_daily_loss_pct:
            return LimitViolation(
                type=ViolationType.DAILY_LOSS_EXCEEDED,
                message=(
                    f"Daily loss {loss_pct:.2f}% has reached hard limit "
                    f"{self.limits.max_daily_loss_pct}%.  "
                    f"Session open equity: ${self.session_open_equity:,.2f}, "
                    f"current: ${equity:,.2f}."
                ),
                current_value=loss_pct,
                limit_value=self.limits.max_daily_loss_pct,
            )
        return None

    def _check_drawdown(self, equity: float) -> Optional[LimitViolation]:
        if self._peak_equity <= 0:
            return None
        drawdown_pct = ((self._peak_equity - equity) / self._peak_equity) * 100
        if drawdown_pct >= self.limits.max_drawdown_pct:
            return LimitViolation(
                type=ViolationType.DRAWDOWN_EXCEEDED,
                message=(
                    f"Drawdown {drawdown_pct:.2f}% from peak ${self._peak_equity:,.2f} "
                    f"has reached hard limit {self.limits.max_drawdown_pct}%."
                ),
                current_value=drawdown_pct,
                limit_value=self.limits.max_drawdown_pct,
            )
        return None

    def _check_position_sizes(self, snapshot: AccountSnapshot) -> list[LimitViolation]:
        if snapshot.equity <= 0:
            return []
        violations = []
        for pos in snapshot.positions:
            size_pct = (pos.market_value / snapshot.equity) * 100
            if size_pct > self.limits.max_position_size_pct:
                max_value = snapshot.equity * (self.limits.max_position_size_pct / 100)
                excess_value = pos.market_value - max_value
                excess_shares = excess_value / pos.current_price if pos.current_price > 0 else 0
                violations.append(
                    LimitViolation(
                        type=ViolationType.POSITION_SIZE_EXCEEDED,
                        message=(
                            f"{pos.symbol} position is {size_pct:.1f}% of equity "
                            f"(limit: {self.limits.max_position_size_pct}%).  "
                            f"Need to trim {excess_shares:.4f} shares "
                            f"(≈${excess_value:,.2f})."
                        ),
                        symbol=pos.symbol,
                        current_value=size_pct,
                        limit_value=self.limits.max_position_size_pct,
                        excess_shares=excess_shares,
                    )
                )
        return violations

    def _check_position_count(self, snapshot: AccountSnapshot) -> Optional[LimitViolation]:
        count = len(snapshot.positions)
        if count > self.limits.max_open_positions:
            return LimitViolation(
                type=ViolationType.POSITION_COUNT_EXCEEDED,
                message=(
                    f"{count} open positions exceeds hard limit of "
                    f"{self.limits.max_open_positions}."
                ),
                current_value=float(count),
                limit_value=float(self.limits.max_open_positions),
            )
        return None

    def _check_cash_reserve(self, snapshot: AccountSnapshot) -> Optional[LimitViolation]:
        if snapshot.equity <= 0:
            return None
        cash_pct = (snapshot.cash / snapshot.equity) * 100
        if cash_pct < self.limits.min_cash_reserve_pct:
            return LimitViolation(
                type=ViolationType.CASH_RESERVE_VIOLATED,
                message=(
                    f"Cash is {cash_pct:.1f}% of equity (minimum: "
                    f"{self.limits.min_cash_reserve_pct}%).  "
                    f"Buy orders will be blocked until cash is restored."
                ),
                current_value=cash_pct,
                limit_value=self.limits.min_cash_reserve_pct,
            )
        return None
