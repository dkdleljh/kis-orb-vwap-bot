from __future__ import annotations

import os


async def aclose(self) -> None:
    """Close internally-owned resources (best-effort)."""
    try:
        if hasattr(self, "rest") and self.rest is not None:
            await self.rest.aclose()
    except Exception:
        pass


def on_ws_status(self, connected: bool) -> None:
    """Handle websocket connectivity status updates."""
    self.ws_connected = connected


def kill_switch_on(self) -> bool:
    """Check whether trading should stop via environment or flag file."""
    if os.environ.get("KIS_KILL_SWITCH", "0") == "1":
        return True
    return os.path.exists(os.path.join(self.base_dir, "STOP_TRADING.flag"))


def live_ordering_enabled(self) -> bool:
    """Check whether live order placement is currently allowed."""
    return bool(self.live_enabled and self.live_confirmed and (not self.kill_switch_on()))
