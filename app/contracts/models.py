"""Contract models.

A :class:`ContractSpec` is a *request*: what we are asking the broker to look
up. A :class:`QualifiedContract` is an *answer*: what the broker actually
confirmed exists, complete with the conId that makes it unambiguous.

Only a :class:`QualifiedContract` may carry an order, and only one whose
security type is ``FUT``. This distinction is enforced by construction --
``QualifiedContract`` cannot be built without a positive conId and a concrete
expiration.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from app.enums import SecurityType


class ContractError(ValueError):
    """Raised when a contract is malformed or not tradeable."""


@dataclass(frozen=True, slots=True)
class ContractSpec:
    """An unresolved contract request.

    ``last_trade_date_or_contract_month`` is required for tradeable specs. It is
    never defaulted to "the front month" here: picking an expiration implicitly
    is exactly the class of mistake that puts an order on the wrong contract.
    """

    symbol: str
    sec_type: SecurityType = SecurityType.FUTURE
    exchange: str = "CME"
    currency: str = "USD"
    last_trade_date_or_contract_month: str | None = None
    trading_class: str | None = None
    local_symbol: str | None = None
    con_id: int | None = None
    multiplier: str | None = None

    def __post_init__(self) -> None:
        if not self.symbol or not self.symbol.strip():
            raise ContractError("contract symbol is required")
        if not self.exchange:
            raise ContractError("contract exchange is required")
        if not self.currency:
            raise ContractError("contract currency is required")

    @property
    def is_continuous(self) -> bool:
        return self.sec_type is SecurityType.CONTINUOUS_FUTURE

    @property
    def is_dated(self) -> bool:
        return bool(self.last_trade_date_or_contract_month)

    def require_orderable(self) -> None:
        """Raise unless this spec could legitimately become a tradeable contract."""
        if self.is_continuous:
            raise ContractError(
                f"{self.symbol}: continuous futures (CONTFUT) are analytics-only "
                "and can never carry an order"
            )
        if not self.is_dated:
            raise ContractError(
                f"{self.symbol}: an explicit expiration is required; "
                "front-month selection is never implicit"
            )

    def key(self) -> str:
        """Stable identity for market-data bookkeeping before qualification."""
        expiry = self.last_trade_date_or_contract_month or "UNDATED"
        return f"{self.symbol}:{self.sec_type.value}:{self.exchange}:{expiry}"

    def describe(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "sec_type": self.sec_type.value,
            "exchange": self.exchange,
            "currency": self.currency,
            "expiry": self.last_trade_date_or_contract_month,
            "trading_class": self.trading_class,
            "local_symbol": self.local_symbol,
            "con_id": self.con_id,
        }


@dataclass(frozen=True, slots=True)
class QualifiedContract:
    """A contract the broker has confirmed, with everything needed to trade it."""

    con_id: int
    symbol: str
    local_symbol: str
    sec_type: SecurityType
    exchange: str
    currency: str
    expiration: str
    """Contract month or last trade date as reported by the broker (YYYYMM/YYYYMMDD)."""

    last_trade_date: str
    multiplier: str
    min_tick: Decimal
    trading_class: str
    primary_exchange: str | None = None
    trading_hours: str | None = None
    liquid_hours: str | None = None
    time_zone_id: str | None = None
    long_name: str | None = None
    qualified_at: str | None = None
    raw: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.con_id <= 0:
            raise ContractError(
                f"{self.symbol}: qualified contract requires a positive conId, got {self.con_id}"
            )
        if self.sec_type is not SecurityType.FUTURE:
            raise ContractError(
                f"{self.symbol}: only FUT contracts are orderable, got {self.sec_type.value}"
            )
        if not self.expiration:
            raise ContractError(f"{self.symbol}: qualified contract requires an expiration")
        if self.min_tick <= 0:
            raise ContractError(f"{self.symbol}: min tick must be positive, got {self.min_tick}")

    @property
    def is_continuous(self) -> bool:
        return False

    def expiry_date(self) -> date | None:
        """Last trade date, or ``None`` when it cannot be determined.

        ``None`` is a refusal, not a shrug: a caller that cannot establish
        when a contract stops trading must not trade it. IBKR reports
        ``lastTradeDateOrContractMonth`` on every qualified future, so this
        returning ``None`` means the field was malformed and something is
        wrong that guessing would hide.

        A bare ``YYYYMM`` is deliberately NOT accepted here. Assuming the last
        day of the month would keep a dead contract tradeable for weeks, and
        assuming the first would refuse a live one; neither is a fact, and this
        function only reports facts.
        """
        for raw in (self.last_trade_date, self.expiration):
            if raw and len(raw) == 8 and raw.isdigit():
                try:
                    return date(int(raw[0:4]), int(raw[4:6]), int(raw[6:8]))
                except ValueError:
                    continue
        return None

    def is_expired(self, on: date) -> bool:
        """Whether this contract has stopped trading by ``on``.

        Fails closed twice over: an undeterminable expiry counts as expired,
        and the comparison is strictly *after* the last trade date, so the
        final session stays tradeable. Refusing on the last day would strand
        any position held into it -- the operator could not flatten, which is
        the one thing that must always be possible.
        """
        expiry = self.expiry_date()
        return expiry is None or on > expiry

    def days_until_expiry(self, on: date) -> int | None:
        """Sessions remaining, negative once past. ``None`` if undeterminable."""
        expiry = self.expiry_date()
        return None if expiry is None else (expiry - on).days

    @property
    def multiplier_decimal(self) -> Decimal:
        try:
            return Decimal(self.multiplier)
        except Exception as exc:
            raise ContractError(
                f"{self.symbol}: multiplier {self.multiplier!r} is not numeric"
            ) from exc

    def notional(self, quantity: int, price: Decimal) -> Decimal:
        """Notional exposure of ``quantity`` contracts at ``price``."""
        return abs(Decimal(quantity)) * self.multiplier_decimal * price

    def to_spec(self) -> ContractSpec:
        return ContractSpec(
            symbol=self.symbol,
            sec_type=self.sec_type,
            exchange=self.exchange,
            currency=self.currency,
            last_trade_date_or_contract_month=self.expiration,
            trading_class=self.trading_class,
            local_symbol=self.local_symbol,
            con_id=self.con_id,
            multiplier=self.multiplier,
        )

    def key(self) -> str:
        return f"conid:{self.con_id}"

    def describe(self) -> dict[str, object]:
        """Full metadata for /status, logs, and the contract-info command."""
        return {
            "con_id": self.con_id,
            "symbol": self.symbol,
            "local_symbol": self.local_symbol,
            "sec_type": self.sec_type.value,
            "exchange": self.exchange,
            "primary_exchange": self.primary_exchange,
            "currency": self.currency,
            "expiration": self.expiration,
            "last_trade_date": self.last_trade_date,
            "multiplier": self.multiplier,
            "min_tick": str(self.min_tick),
            "trading_class": self.trading_class,
            "trading_hours": self.trading_hours,
            "liquid_hours": self.liquid_hours,
            "time_zone_id": self.time_zone_id,
            "long_name": self.long_name,
            "qualified_at": self.qualified_at,
        }

    def raw_json(self) -> str:
        return json.dumps(self.raw, default=str, sort_keys=True)


__all__ = ["ContractError", "ContractSpec", "QualifiedContract"]
