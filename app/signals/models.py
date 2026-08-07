"""Trade intents.

A :class:`TradeIntent` is what a strategy *wants*, expressed in portfolio terms
("be long 2 contracts of MSL"), not in broker terms. Turning that into an order
is the order manager's job, and it only happens after risk approval and the
transmit gate.

``requested_position`` is a target, not a delta. Expressing intent as an
absolute target makes a replayed or duplicated signal idempotent by
construction: asking twice for "be long 2" is not the same as asking twice for
"buy 2".
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

from app.enums import Direction
from app.utilities.ids import idempotency_key, new_intent_id
from app.utilities.timeutils import ensure_utc


class SignalError(ValueError):
    """Raised when an intent is structurally invalid."""


@dataclass(frozen=True, slots=True)
class TradeIntent:
    """A strategy's proposed target position."""

    strategy_name: str
    symbol: str
    direction: Direction
    requested_position: int
    """Target position in contracts, signed. ``0`` means flat."""

    created_at: datetime
    intent_id: str = field(default_factory=new_intent_id)
    correlation_id: str | None = None
    confidence: float | None = None
    contract_key: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.strategy_name.strip():
            raise SignalError("strategy_name is required")
        if not self.symbol.strip():
            raise SignalError("symbol is required")
        ensure_utc(self.created_at)
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise SignalError(f"confidence must be within [0, 1], got {self.confidence}")
        self._check_direction_consistency()

    def _check_direction_consistency(self) -> None:
        """Direction and signed target must agree.

        A LONG intent with a negative target is a strategy bug. Catching it here
        stops it becoming a trade in the wrong direction.
        """
        if self.direction is Direction.LONG and self.requested_position <= 0:
            raise SignalError(
                f"LONG intent requires a positive requested_position, got {self.requested_position}"
            )
        if self.direction is Direction.SHORT and self.requested_position >= 0:
            raise SignalError(
                f"SHORT intent requires a negative requested_position, got "
                f"{self.requested_position}"
            )
        if self.direction is Direction.FLAT and self.requested_position != 0:
            raise SignalError(
                f"FLAT intent requires requested_position == 0, got {self.requested_position}"
            )

    @property
    def is_actionable(self) -> bool:
        """FLAT-to-flat intents carry no work."""
        return self.direction is not Direction.FLAT or self.requested_position != 0

    def idempotency_key(self, *, con_id: int) -> str:
        """Deterministic key for the order this intent would produce.

        Derived from economic content only. Two runs of the same strategy that
        produce the same target for the same contract on the same intent
        produce the same key, so a restart cannot duplicate the order.
        """
        return idempotency_key(
            {
                "intent_id": self.intent_id,
                "strategy": self.strategy_name,
                "con_id": con_id,
                "target": self.requested_position,
            }
        )

    def metadata_json(self) -> str:
        return json.dumps(self.metadata, default=str, sort_keys=True)

    def describe(self) -> dict[str, object]:
        return {
            "intent_id": self.intent_id,
            "strategy_name": self.strategy_name,
            "symbol": self.symbol,
            "direction": self.direction.value,
            "requested_position": self.requested_position,
            "confidence": self.confidence,
            "created_at": self.created_at.isoformat(),
            "correlation_id": self.correlation_id,
            "contract_key": self.contract_key,
        }


@dataclass(frozen=True, slots=True)
class SignalValidation:
    """Outcome of validating an intent."""

    accepted: bool
    reasons: tuple[str, ...] = ()

    @property
    def reason(self) -> str:
        return self.reasons[0] if self.reasons else ("ACCEPTED" if self.accepted else "REJECTED")


__all__ = ["SignalError", "SignalValidation", "TradeIntent"]
