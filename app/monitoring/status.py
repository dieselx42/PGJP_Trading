"""Status payload assembly.

Everything here is safe to display. The application holds no credentials, and
account identifiers are masked before they leave this module.
"""

from __future__ import annotations

from typing import Protocol

from app import __version__
from app.config import Config


class StatusSource(Protocol):
    """Implemented by the running application; consumed by the HTTP server."""

    def status_payload(self) -> dict[str, object]: ...

    def health_payload(self) -> tuple[int, dict[str, object]]: ...


def build_info(config: Config) -> dict[str, object]:
    """Version, commit, and build timestamp.

    Commit and timestamp are injected at image build time; they read
    ``"unknown"`` for a local run from a working tree, which is honest rather
    than misleading.
    """
    return {
        "version": __version__,
        "git_commit": config.git_commit,
        "build_timestamp": config.build_timestamp,
    }


def safety_summary(config: Config, *, kill_switch_engaged: bool) -> dict[str, object]:
    """The four interlocks plus the question everyone actually wants answered."""
    return {
        "trading_mode": config.trading_mode.value,
        "allow_order_transmit": config.allow_order_transmit,
        "live_trading_enabled": config.live_trading_enabled,
        "kill_switch": kill_switch_engaged,
        "sol_futures_permission_ready": config.sol_futures_permission_ready,
        "can_transmit_live_orders": (
            config.trading_mode.value == "live"
            and config.live_trading_enabled
            and config.allow_order_transmit
            and not kill_switch_engaged
        ),
    }


__all__ = ["StatusSource", "build_info", "safety_summary"]
