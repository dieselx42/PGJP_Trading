"""Persistence records for entities without a richer domain model."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from app.utilities.timeutils import ensure_utc, utc_now


@dataclass(frozen=True, slots=True)
class BotEvent:
    """An application-level event worth keeping beyond the log files."""

    run_id: str
    event: str
    level: str = "INFO"
    message: str = ""
    data: dict[str, object] = field(default_factory=dict)
    correlation_id: str | None = None
    occurred_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        ensure_utc(self.occurred_at)

    def data_json(self) -> str:
        return json.dumps(self.data, default=str, sort_keys=True)


@dataclass(frozen=True, slots=True)
class BrokerEvent:
    """A broker-reported event: connection change, API error, order feedback."""

    run_id: str
    event: str
    code: str | None = None
    message: str = ""
    data: dict[str, object] = field(default_factory=dict)
    correlation_id: str | None = None
    occurred_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        ensure_utc(self.occurred_at)

    def data_json(self) -> str:
        return json.dumps(self.data, default=str, sort_keys=True)


@dataclass(frozen=True, slots=True)
class DailyPerformance:
    """Per-UTC-day totals. Drives the daily-loss risk limit."""

    trade_date: str
    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    commission: Decimal = Decimal("0")
    order_count: int = 0
    fill_count: int = 0
    updated_at: datetime = field(default_factory=utc_now)

    @property
    def total_pnl(self) -> Decimal:
        """Realized plus unrealized, net of commission.

        The daily-loss limit is evaluated against total P&L rather than
        realized alone, so an open losing position cannot hide from it.
        """
        return self.realized_pnl + self.unrealized_pnl - self.commission

    def describe(self) -> dict[str, object]:
        return {
            "trade_date": self.trade_date,
            "realized_pnl": str(self.realized_pnl),
            "unrealized_pnl": str(self.unrealized_pnl),
            "commission": str(self.commission),
            "total_pnl": str(self.total_pnl),
            "order_count": self.order_count,
            "fill_count": self.fill_count,
        }


@dataclass(frozen=True, slots=True)
class SignalRecord:
    """A signal as persisted, including why it was rejected if it was."""

    intent_id: str
    correlation_id: str
    run_id: str
    strategy_name: str
    symbol: str
    direction: str
    requested_position: int
    created_at: datetime
    accepted: bool
    confidence: float | None = None
    contract_key: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)
    rejection_reasons: tuple[str, ...] = ()

    def metadata_json(self) -> str:
        return json.dumps(self.metadata, default=str, sort_keys=True)

    def reasons_json(self) -> str:
        return json.dumps(list(self.rejection_reasons))


__all__ = ["BotEvent", "BrokerEvent", "DailyPerformance", "SignalRecord"]
