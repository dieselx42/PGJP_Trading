"""The abstract broker interface.

Implementations must satisfy two rules that the rest of the system depends on:

1. **Account identity is reported, never assumed.** ``get_connection_info``
   must return :attr:`~app.enums.AccountType.UNKNOWN` unless the adapter has
   positively established what kind of account it is attached to. The transmit
   gate refuses to trade an unknown account, so "unknown" is always safe and
   guessing never is.

2. **``transmit=False`` means do not send.** ``place_order`` must refuse an
   :class:`~app.broker.models.OrderRequest` whose ``transmit`` flag is false.
   This is a second, independent barrier behind the transmit gate.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from app.broker.models import (
    AccountSummary,
    BrokerFill,
    BrokerOrderSnapshot,
    BrokerPosition,
    ConnectionInfo,
    MarketDataTick,
    OrderRequest,
    PlaceOrderResult,
)
from app.contracts.models import ContractSpec, QualifiedContract
from app.enums import AccountType, ConnectionState


class Broker(ABC):
    """Broker-neutral interface used by the whole application."""

    #: Short identifier for logs and /status, e.g. ``"mock"`` or ``"ibkr"``.
    name: str = "abstract"

    # -- lifecycle --------------------------------------------------------

    @abstractmethod
    async def connect(self) -> ConnectionInfo:
        """Establish a session and determine the account identity.

        Must raise :class:`~app.broker.models.BrokerConnectionError` on
        transport failure rather than returning a disconnected
        :class:`ConnectionInfo`.
        """

    @abstractmethod
    async def disconnect(self) -> None:
        """Close the session. Must be safe to call when already disconnected."""

    @abstractmethod
    def is_connected(self) -> bool: ...

    @abstractmethod
    def get_connection_state(self) -> ConnectionState: ...

    @abstractmethod
    def get_connection_info(self) -> ConnectionInfo: ...

    @property
    def account_type(self) -> AccountType:
        """Observed account identity. ``UNKNOWN`` until positively established."""
        return self.get_connection_info().account_type

    # -- account and state ------------------------------------------------

    @abstractmethod
    async def get_account_summary(self) -> AccountSummary: ...

    @abstractmethod
    async def get_positions(self) -> Sequence[BrokerPosition]: ...

    @abstractmethod
    async def get_open_orders(self) -> Sequence[BrokerOrderSnapshot]: ...

    @abstractmethod
    async def get_fills(self, *, since_seconds: int = 86_400) -> Sequence[BrokerFill]: ...

    # -- contracts --------------------------------------------------------

    @abstractmethod
    async def get_contract_details(self, spec: ContractSpec) -> Sequence[QualifiedContract]:
        """Return every contract matching ``spec``. May be empty."""

    @abstractmethod
    async def qualify_contract(self, spec: ContractSpec) -> QualifiedContract:
        """Resolve ``spec`` to exactly one tradeable contract.

        Must raise :class:`~app.broker.models.BrokerContractError` when the spec
        matches zero contracts *or more than one*. An ambiguous match is an
        error, never a "pick the first" situation.
        """

    # -- market data ------------------------------------------------------

    @abstractmethod
    async def request_market_data(self, contract: QualifiedContract) -> MarketDataTick:
        """Return the latest tick, subscribing on first use."""

    @abstractmethod
    async def cancel_market_data(self, contract: QualifiedContract) -> None: ...

    # -- orders -----------------------------------------------------------

    @abstractmethod
    async def place_order(self, request: OrderRequest) -> PlaceOrderResult:
        """Transmit an order. Must refuse when ``request.transmit`` is false."""

    @abstractmethod
    async def modify_order(
        self, request: OrderRequest, broker_order_id: str
    ) -> PlaceOrderResult: ...

    @abstractmethod
    async def cancel_order(self, broker_order_id: str) -> bool: ...

    @abstractmethod
    async def cancel_all_orders(self) -> int:
        """Cancel every working order. Returns how many cancels were issued.

        This never closes positions. Cancelling resting orders and liquidating
        a book are separate decisions and only the former is automated.
        """

    # -- health -----------------------------------------------------------

    @abstractmethod
    def last_heartbeat_age_seconds(self) -> float | None:
        """Seconds since the last sign of life, or ``None`` if never seen."""


__all__ = ["Broker"]
