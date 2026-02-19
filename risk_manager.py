from dataclasses import dataclass
from typing import Optional


@dataclass
class RiskState:
    """Risk management state tracked for a single trading day.

    Attributes:
        entries_today: Number of entries already taken today.
        daily_pnl_pct: Cumulative realized day PnL as a percentage.
        consecutive_stop: Number of consecutive stop-loss exits.
        done_today: Whether new entries are blocked for the rest of the day.
    """

    entries_today: int = 0
    daily_pnl_pct: float = 0.0
    consecutive_stop: int = 0
    done_today: bool = False
    unrealized_pnl: float = 0.0
    max_drawdown_pct: float = 0.0
    peak_equity: float = 0.0


class RiskManager:
    """Enforces intraday trading risk limits.

    This manager tracks daily trading activity and determines whether additional
    entries are allowed. Trading is disabled for the day once any configured
    risk threshold is reached.

    RISK-01 Extension:
    - Ledger-based PnL (realized + unrealized)
    - Drawdown tracking
    - Portfolio exposure limits

    Attributes:
        max_entries: Maximum number of entries allowed in one day.
        daily_loss_limit_pct: Daily PnL percentage threshold that disables
            further entries when breached.
        max_consecutive_stop: Maximum allowed consecutive stop-loss exits.
        max_drawdown_pct: Maximum allowed drawdown from peak equity.
        state: Mutable daily risk state used by all checks.
    """

    def __init__(
        self,
        max_entries: int,
        daily_loss_limit_pct: float,
        max_consecutive_stop: int,
        max_drawdown_pct: float = 0.10,
        max_position_pct: float = 0.12,
        max_total_exposure_pct: float = 0.35,
    ):
        self.max_entries = max_entries
        self.daily_loss_limit_pct = daily_loss_limit_pct
        self.max_consecutive_stop = max_consecutive_stop
        self.max_drawdown_pct = max_drawdown_pct
        self.max_position_pct = max_position_pct
        self.max_total_exposure_pct = max_total_exposure_pct
        self.state = RiskState()

    def update_from_ledger(
        self,
        ledger_cash: float,
        ledger_equity: float,
        market_prices: dict = None,
    ) -> None:
        """Update risk state from ledger data.

        RISK-01: Use ledger as source of truth for PnL.

        Args:
            ledger_cash: Current cash from ledger
            ledger_equity: Total equity from ledger
            market_prices: Optional dict of symbol -> price for unrealized calc
        """
        if self.state.peak_equity == 0:
            self.state.peak_equity = ledger_equity

        if ledger_equity < self.state.peak_equity:
            drawdown = (self.state.peak_equity - ledger_equity) / self.state.peak_equity
            self.state.max_drawdown_pct = max(self.state.max_drawdown_pct, drawdown)

        if ledger_equity > self.state.peak_equity:
            self.state.peak_equity = ledger_equity

    def can_enter(
        self,
        position_value: float = 0,
        total_equity: float = 0,
    ) -> bool:
        """Determine whether a new position entry is currently allowed.

        RISK-01: Added portfolio exposure checks.

        The method checks done status, entry count, daily loss threshold,
        consecutive stop-loss count, drawdown, and position limits.

        Args:
            position_value: Value of new position to add
            total_equity: Current total equity

        Returns:
            bool: ``True`` if a new entry is allowed, otherwise ``False``.
        """

        if self.state.done_today:
            return False
        if self.state.entries_today >= self.max_entries:
            self.state.done_today = True
            return False
        if self.state.daily_pnl_pct <= self.daily_loss_limit_pct:
            self.state.done_today = True
            return False
        if self.state.consecutive_stop >= self.max_consecutive_stop:
            self.state.done_today = True
            return False
        if self.state.max_drawdown_pct >= self.max_drawdown_pct:
            self.state.done_today = True
            return False

        if total_equity > 0 and position_value > 0:
            new_exposure = position_value / total_equity
            if new_exposure > self.max_position_pct:
                return False

        return True

    def check_exposure_limit(
        self,
        current_exposure: float,
    ) -> bool:
        """Check if adding new position would exceed total exposure limit.

        Args:
            current_exposure: Current total exposure as percentage of equity

        Returns:
            bool: True if within limits
        """
        return current_exposure < self.max_total_exposure_pct

    def record_entry(self) -> None:
        """Record a successful entry attempt for the current day.

        Increments the daily entry counter by one.
        """

        self.state.entries_today += 1

    def record_exit(self, pnl_pct: float, is_stop: bool) -> None:
        """Record exit outcome and update cumulative risk state.

        Args:
            pnl_pct: Realized PnL contribution from the exit as a percentage.
            is_stop: Whether the exit occurred due to stop-loss.

        Updates:
            - Adds ``pnl_pct`` to cumulative daily PnL.
            - Increments consecutive stop count when ``is_stop`` is ``True``.
            - Resets consecutive stop count to zero for non-stop exits.
        """

        self.state.daily_pnl_pct += pnl_pct
        if is_stop:
            self.state.consecutive_stop += 1
        else:
            self.state.consecutive_stop = 0

    def force_done(self) -> None:
        """Disable new entries for the remainder of the trading day."""

        self.state.done_today = True

    def reset_daily(self) -> None:
        """Reset daily counters for new trading day."""
        self.state.entries_today = 0
        self.state.daily_pnl_pct = 0.0
        self.state.consecutive_stop = 0
        self.state.done_today = False
        self.state.unrealized_pnl = 0.0

    def get_risk_status(self) -> dict:
        """Get current risk status for reporting.

        Returns:
            Dict with risk metrics
        """
        return {
            "entries_today": self.state.entries_today,
            "daily_pnl_pct": self.state.daily_pnl_pct,
            "consecutive_stop": self.state.consecutive_stop,
            "done_today": self.state.done_today,
            "max_drawdown_pct": self.state.max_drawdown_pct,
            "peak_equity": self.state.peak_equity,
        }
