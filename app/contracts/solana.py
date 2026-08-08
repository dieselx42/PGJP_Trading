"""CME Solana futures definitions.

IMPORTANT -- what is and is not authoritative here.

The values below are **reference defaults used only to build a lookup request
and to drive mock mode**. They are explicitly *not* treated as truth. Every
number that affects an order (multiplier, minimum tick, expiration, trading
class, trading hours) is overwritten by whatever IBKR returns from
``contractDetails`` during qualification, and an order can only be built from a
:class:`~app.contracts.models.QualifiedContract`, which by construction comes
from the broker.

MSL was checked against a live IBKR paper session on 2026-08-08 and the
constants below were correct: multiplier 25, minimum tick 0.05, trading class
MSL, exchange CME. IBKR listed ten contracts, monthly through Jan 2027 and
quarterly thereafter, reporting expirations as full last-trade dates
(``20260828``) rather than months.

That agreement does not promote them to authoritative. Contract specifications
change, SOL has not been checked at all, and the whole point of overwriting them
from ``contractDetails`` is that an order must never depend on a constant
somebody typed. Being right once is not the same as being a source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from app.contracts.models import ContractSpec
from app.enums import SecurityType


@dataclass(frozen=True, slots=True)
class FuturesProduct:
    """Static description of a tradeable futures product."""

    symbol: str
    description: str
    exchange: str
    currency: str
    trading_class: str

    #: Reference only. Replaced by the broker-reported multiplier at
    #: qualification time. See the module docstring.
    reference_multiplier: str

    #: Reference only. Replaced by the broker-reported minimum tick.
    reference_min_tick: Decimal

    #: Used by the deterministic mock data source so simulated prices land in a
    #: plausible range. Has no effect on paper or live behaviour.
    mock_reference_price: Decimal

    def spec(self, contract_month: str | None = None) -> ContractSpec:
        return ContractSpec(
            symbol=self.symbol,
            sec_type=SecurityType.FUTURE,
            exchange=self.exchange,
            currency=self.currency,
            last_trade_date_or_contract_month=contract_month,
            trading_class=self.trading_class,
        )

    def continuous_spec(self) -> ContractSpec:
        """Continuous future for charting/analytics only. Never orderable."""
        return ContractSpec(
            symbol=self.symbol,
            sec_type=SecurityType.CONTINUOUS_FUTURE,
            exchange=self.exchange,
            currency=self.currency,
            trading_class=self.trading_class,
        )


MICRO_SOLANA: Final = FuturesProduct(
    symbol="MSL",
    description="Micro Solana Futures",
    exchange="CME",
    currency="USD",
    trading_class="MSL",
    reference_multiplier="25",
    reference_min_tick=Decimal("0.05"),
    mock_reference_price=Decimal("150.00"),
)

SOLANA: Final = FuturesProduct(
    symbol="SOL",
    description="Solana Futures",
    exchange="CME",
    currency="USD",
    trading_class="SOL",
    reference_multiplier="500",
    reference_min_tick=Decimal("0.05"),
    mock_reference_price=Decimal("150.00"),
)

PRODUCTS: Final[dict[str, FuturesProduct]] = {
    MICRO_SOLANA.symbol: MICRO_SOLANA,
    SOLANA.symbol: SOLANA,
}

#: The smaller contract is the development default, per the project brief.
DEFAULT_PRODUCT: Final = MICRO_SOLANA


def get_product(symbol: str) -> FuturesProduct:
    """Look up a supported product, refusing anything unknown.

    The application never substitutes a different symbol when one is not found.
    Silently trading a neighbouring instrument is worse than not trading.
    """
    key = symbol.strip().upper()
    product = PRODUCTS.get(key)
    if product is None:
        raise KeyError(
            f"unsupported futures symbol {symbol!r}; supported symbols: {sorted(PRODUCTS)}"
        )
    return product


__all__ = [
    "DEFAULT_PRODUCT",
    "MICRO_SOLANA",
    "PRODUCTS",
    "SOLANA",
    "FuturesProduct",
    "get_product",
]
