from dataclasses import dataclass


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


class RiskManager:
    """Enforces intraday trading risk limits.

    This manager tracks daily trading activity and determines whether additional
    entries are allowed. Trading is disabled for the day once any configured
    risk threshold is reached.

    Attributes:
        max_entries: Maximum number of entries allowed in one day.
        daily_loss_limit_pct: Daily PnL percentage threshold that disables
            further entries when breached.
        max_consecutive_stop: Maximum allowed consecutive stop-loss exits.
        state: Mutable daily risk state used by all checks.
    """

    def __init__(
        self, max_entries: int, daily_loss_limit_pct: float, max_consecutive_stop: int
    ):
        self.max_entries = max_entries
        self.daily_loss_limit_pct = daily_loss_limit_pct
        self.max_consecutive_stop = max_consecutive_stop
        self.state = RiskState()

    def can_enter(self) -> bool:
        """Determine whether a new position entry is currently allowed.

        The method checks done status, entry count, daily loss threshold, and
        consecutive stop-loss count. If any limit is reached, trading is marked
        done for the day.

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
        return True

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
