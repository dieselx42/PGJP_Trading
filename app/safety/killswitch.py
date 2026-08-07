"""Kill switch.

Two independent sources engage it, and they OR together:

* ``KILL_SWITCH=true`` in the environment -- survives restarts, is the
  configuration of record, and is what the deployed system uses.
* A durable latch in the database -- lets an operator engage the switch on a
  *running* process without a redeploy.

The latch is deliberately one-way in this phase. There is no
``kill-switch-off`` command and no API that clears it. Resuming trading is a
configuration change made by a human, which is exactly the friction we want.

Engaging the kill switch stops NEW orders and stops strategies producing
actionable output. It does **not** liquidate positions. Cancelling working
orders and flattening a book are different decisions with different risk, and
only the first is automated here.
"""

from __future__ import annotations

from typing import Protocol

KILL_SWITCH_STATE_KEY = "kill_switch_latched"
KILL_SWITCH_REASON_KEY = "kill_switch_reason"
KILL_SWITCH_ENGAGED_AT_KEY = "kill_switch_engaged_at"


class StateStore(Protocol):
    """Minimal durable key/value interface (implemented by the state repository)."""

    def get_state(self, key: str) -> str | None: ...

    def set_state(self, key: str, value: str) -> None: ...


class KillSwitch:
    """Combines the configured switch with the durable latch."""

    def __init__(self, *, config_engaged: bool, store: StateStore | None = None) -> None:
        self._config_engaged = config_engaged
        self._store = store

    @property
    def config_engaged(self) -> bool:
        return self._config_engaged

    def latched(self) -> bool:
        if self._store is None:
            return False
        return self._store.get_state(KILL_SWITCH_STATE_KEY) == "true"

    def engaged(self) -> bool:
        """True if either source is engaged. Never false because of an error.

        If the durable store cannot be read we treat the switch as engaged:
        being unable to determine the safety state is itself an unsafe state.
        """
        if self._config_engaged:
            return True
        try:
            return self.latched()
        except Exception:  # noqa: BLE001 -- deliberate fail-closed catch-all
            return True

    def engage(self, reason: str, *, engaged_at: str) -> None:
        """Latch the switch durably. There is intentionally no ``disengage``."""
        if self._store is None:
            raise RuntimeError("kill switch has no durable store; cannot latch")
        self._store.set_state(KILL_SWITCH_STATE_KEY, "true")
        self._store.set_state(KILL_SWITCH_REASON_KEY, reason)
        self._store.set_state(KILL_SWITCH_ENGAGED_AT_KEY, engaged_at)

    def describe(self) -> dict[str, object]:
        try:
            latched = self.latched()
        except Exception:  # noqa: BLE001
            latched = True
        reason = None
        engaged_at = None
        if self._store is not None:
            try:
                reason = self._store.get_state(KILL_SWITCH_REASON_KEY)
                engaged_at = self._store.get_state(KILL_SWITCH_ENGAGED_AT_KEY)
            except Exception:  # noqa: BLE001
                reason = "state_store_unreadable"
        return {
            "engaged": self.engaged(),
            "config_engaged": self._config_engaged,
            "latched": latched,
            "reason": reason,
            "engaged_at": engaged_at,
        }


__all__ = [
    "KILL_SWITCH_ENGAGED_AT_KEY",
    "KILL_SWITCH_REASON_KEY",
    "KILL_SWITCH_STATE_KEY",
    "KillSwitch",
    "StateStore",
]
