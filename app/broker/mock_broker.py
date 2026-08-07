"""In-process simulated broker.

Everything here is deterministic. Given the same seed, the same sequence of
calls produces the same prices, contract ids, and fills, which is what makes it
usable as a test fixture rather than just a demo.

The mock reports :attr:`~app.enums.AccountType.SIMULATED`. It can never be
mistaken for a paper or live account, so mock mode cannot accidentally satisfy
the paper/live account-identity interlock.

Failure injection knobs (``fail_connect``, ``account_type_override``,
``permission_denied``, ...) exist so the safety tests can drive the system into
states that are impossible to reach on demand with a real broker.
"""

from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from app.broker.base import Broker
from app.broker.models import (
    AccountSummary,
    BrokerConnectionError,
    BrokerContractError,
    BrokerFill,
    BrokerOrderRejectedError,
    BrokerOrderSnapshot,
    BrokerPermissionError,
    BrokerPosition,
    ConnectionInfo,
    MarketDataTick,
    OrderRequest,
    PlaceOrderResult,
)
from app.contracts.models import ContractSpec, QualifiedContract
from app.contracts.solana import get_product
from app.enums import AccountType, ConnectionState, OrderSide, OrderStatus, OrderType, SecurityType
from app.logging_config import get_logger
from app.utilities.timeutils import utc_now

_LOG = get_logger("broker.mock")

MOCK_ACCOUNT_ID = "DUMOCK0001"
MOCK_SOURCE = "mock"


class MockBroker(Broker):
    """Deterministic simulated broker for DISABLED/MOCK modes and tests."""

    name = "mock"

    def __init__(
        self,
        *,
        seed: int = 20240101,
        account_id: str = MOCK_ACCOUNT_ID,
        account_type_override: AccountType | None = None,
        permission_denied: bool = False,
        fail_connect: bool = False,
        initial_positions: Sequence[BrokerPosition] | None = None,
        fill_market_orders: bool = True,
    ) -> None:
        self._seed = seed
        self._rng = random.Random(seed)  # noqa: S311 -- simulation, not cryptography
        self._account_id = account_id
        self._account_type_override = account_type_override
        self._permission_denied = permission_denied
        self._fail_connect = fail_connect
        self._fill_market_orders = fill_market_orders

        self._connection_state = ConnectionState.DISCONNECTED
        self._connected_at: datetime | None = None
        self._last_heartbeat: datetime | None = None

        self._positions: dict[int, BrokerPosition] = {
            position.con_id: position for position in (initial_positions or ())
        }
        self._open_orders: dict[str, BrokerOrderSnapshot] = {}
        self._fills: list[BrokerFill] = []
        self._order_counter = 0
        self._exec_counter = 0
        self._tick_counter: dict[str, int] = {}
        self._subscribed: set[str] = set()

    # ------------------------------------------------------------------
    # Test controls
    # ------------------------------------------------------------------

    def set_account_type(self, account_type: AccountType) -> None:
        """Force the reported account identity (used by the mismatch tests)."""
        self._account_type_override = account_type

    def set_permission_denied(self, denied: bool) -> None:
        self._permission_denied = denied

    def force_disconnect(self, *, state: ConnectionState = ConnectionState.DISCONNECTED) -> None:
        """Simulate the broker dropping the session underneath us."""
        self._connection_state = state
        self._subscribed.clear()

    def seed_position(self, position: BrokerPosition) -> None:
        self._positions[position.con_id] = position

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> ConnectionInfo:
        if self._fail_connect:
            self._connection_state = ConnectionState.FAILED
            raise BrokerConnectionError("mock broker configured to fail connect")
        self._connection_state = ConnectionState.CONNECTING
        await asyncio.sleep(0)
        self._connected_at = utc_now()
        self._last_heartbeat = self._connected_at
        self._connection_state = ConnectionState.CONNECTED
        info = self.get_connection_info()
        _LOG.info(
            "mock broker connected",
            extra={"event": "broker.connected", "broker": self.name, **info.describe()},
        )
        return info

    async def disconnect(self) -> None:
        self._connection_state = ConnectionState.DISCONNECTED
        self._subscribed.clear()
        _LOG.info("mock broker disconnected", extra={"event": "broker.disconnected"})

    def is_connected(self) -> bool:
        return self._connection_state is ConnectionState.CONNECTED

    def get_connection_state(self) -> ConnectionState:
        return self._connection_state

    def get_connection_info(self) -> ConnectionInfo:
        account_type = (
            self._account_type_override
            if self._account_type_override is not None
            else AccountType.SIMULATED
        )
        if not self.is_connected():
            # Not connected means we know nothing about the account.
            account_type = AccountType.UNKNOWN
        return ConnectionInfo(
            connected=self.is_connected(),
            account_type=account_type,
            account_id=self._account_id if self.is_connected() else None,
            server_version="mock-1",
            connection_time=self._connected_at,
            host="in-process",
            port=None,
            client_id=0,
            detail="deterministic simulated broker; never contacts IBKR",
        )

    def last_heartbeat_age_seconds(self) -> float | None:
        if self._last_heartbeat is None:
            return None
        return (utc_now() - self._last_heartbeat).total_seconds()

    # ------------------------------------------------------------------
    # Account and state
    # ------------------------------------------------------------------

    def _require_connected(self) -> None:
        if not self.is_connected():
            raise BrokerConnectionError("mock broker is not connected")

    async def get_account_summary(self) -> AccountSummary:
        self._require_connected()
        self._last_heartbeat = utc_now()
        return AccountSummary(
            account_id=self._account_id,
            account_type=self.get_connection_info().account_type,
            currency="USD",
            net_liquidation=Decimal("100000.00"),
            available_funds=Decimal("100000.00"),
            buying_power=Decimal("100000.00"),
            excess_liquidity=Decimal("100000.00"),
            realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            futures_permission=not self._permission_denied,
            raw={"simulated": "true"},
        )

    async def get_positions(self) -> Sequence[BrokerPosition]:
        self._require_connected()
        return tuple(self._positions.values())

    async def get_open_orders(self) -> Sequence[BrokerOrderSnapshot]:
        self._require_connected()
        return tuple(self._open_orders.values())

    async def get_fills(self, *, since_seconds: int = 86_400) -> Sequence[BrokerFill]:
        self._require_connected()
        cutoff = utc_now().timestamp() - since_seconds
        return tuple(fill for fill in self._fills if fill.executed_at.timestamp() >= cutoff)

    # ------------------------------------------------------------------
    # Contracts
    # ------------------------------------------------------------------

    async def get_contract_details(self, spec: ContractSpec) -> Sequence[QualifiedContract]:
        self._require_connected()
        if self._permission_denied:
            raise BrokerPermissionError(
                f"simulated permission denial for {spec.symbol}: account not enabled for product"
            )
        if spec.sec_type is SecurityType.CONTINUOUS_FUTURE:
            raise BrokerContractError(
                "continuous futures are analytics-only and are never qualified for trading"
            )
        try:
            product = get_product(spec.symbol)
        except KeyError as exc:
            raise BrokerContractError(str(exc)) from exc
        if not spec.last_trade_date_or_contract_month:
            raise BrokerContractError(
                f"{spec.symbol}: an explicit contract month is required to qualify a future"
            )

        expiry = spec.last_trade_date_or_contract_month
        con_id = _deterministic_con_id(spec.symbol, expiry)
        contract = QualifiedContract(
            con_id=con_id,
            symbol=product.symbol,
            local_symbol=f"{product.symbol}{_month_code(expiry)}",
            sec_type=SecurityType.FUTURE,
            exchange=product.exchange,
            currency=product.currency,
            expiration=expiry,
            last_trade_date=expiry if len(expiry) == 8 else f"{expiry}15",
            multiplier=product.reference_multiplier,
            min_tick=product.reference_min_tick,
            trading_class=product.trading_class,
            primary_exchange=product.exchange,
            trading_hours="SIMULATED:0000-2359",
            liquid_hours="SIMULATED:1330-2000",
            time_zone_id="UTC",
            long_name=product.description,
            qualified_at=utc_now().isoformat(),
            raw={"source": MOCK_SOURCE, "note": "simulated contract; not from IBKR"},
        )
        return (contract,)

    async def qualify_contract(self, spec: ContractSpec) -> QualifiedContract:
        matches = await self.get_contract_details(spec)
        if not matches:
            raise BrokerContractError(f"{spec.symbol}: no matching contract")
        if len(matches) > 1:
            raise BrokerContractError(
                f"{spec.symbol}: ambiguous contract specification matched {len(matches)} contracts"
            )
        return matches[0]

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    async def request_market_data(self, contract: QualifiedContract) -> MarketDataTick:
        self._require_connected()
        if self._permission_denied:
            raise BrokerPermissionError(
                f"simulated market data permission denial for {contract.symbol}"
            )
        key = contract.key()
        self._subscribed.add(key)
        self._last_heartbeat = utc_now()
        return self._next_tick(contract)

    async def cancel_market_data(self, contract: QualifiedContract) -> None:
        self._subscribed.discard(contract.key())

    def _next_tick(self, contract: QualifiedContract) -> MarketDataTick:
        """Deterministic random walk around the product's reference price."""
        key = contract.key()
        step = self._tick_counter.get(key, 0)
        self._tick_counter[key] = step + 1

        product = get_product(contract.symbol)
        base = product.mock_reference_price
        # Seeded per (contract, step) so the sequence is reproducible and
        # independent of call interleaving between contracts.
        rng = random.Random(f"{self._seed}:{key}:{step}")  # noqa: S311 -- simulation
        drift = Decimal(str(round(rng.uniform(-2.0, 2.0), 2)))
        mid = base + drift
        half_spread = contract.min_tick
        return MarketDataTick(
            contract_key=key,
            received_at=utc_now(),
            source=MOCK_SOURCE,
            bid=mid - half_spread,
            ask=mid + half_spread,
            last=mid,
            bid_size=10,
            ask_size=10,
        )

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    async def place_order(self, request: OrderRequest) -> PlaceOrderResult:
        """Simulate placement.

        Refuses ``transmit=False`` unconditionally. This is the adapter-level
        barrier that sits behind the transmit gate; both must agree before
        anything is "sent".
        """
        if not request.transmit:
            raise BrokerOrderRejectedError(
                "refusing to place an order with transmit=False "
                "(this is the broker adapter's independent safety barrier)"
            )
        self._require_connected()
        if self._permission_denied:
            raise BrokerPermissionError(
                "simulated trading permission denial: account not enabled for product"
            )
        if request.order_type not in (OrderType.MARKET, OrderType.LIMIT):
            raise BrokerOrderRejectedError(
                f"mock broker does not simulate {request.order_type.value} orders in this phase"
            )

        self._order_counter += 1
        broker_order_id = f"mock-{self._order_counter:06d}"
        snapshot = BrokerOrderSnapshot(
            broker_order_id=broker_order_id,
            account_id=self._account_id,
            con_id=request.contract.con_id,
            symbol=request.contract.symbol,
            side=request.side,
            quantity=request.quantity,
            order_type=request.order_type,
            status=OrderStatus.SUBMITTED,
            remaining_quantity=request.quantity,
            limit_price=request.limit_price,
            stop_price=request.stop_price,
            time_in_force=request.time_in_force,
        )
        self._open_orders[broker_order_id] = snapshot

        if request.order_type is OrderType.MARKET and self._fill_market_orders:
            self._simulate_fill(snapshot, request.contract)
            return PlaceOrderResult(
                accepted=True, broker_order_id=broker_order_id, status=OrderStatus.FILLED
            )

        return PlaceOrderResult(
            accepted=True, broker_order_id=broker_order_id, status=OrderStatus.SUBMITTED
        )

    def _simulate_fill(self, snapshot: BrokerOrderSnapshot, contract: QualifiedContract) -> None:
        tick = self._next_tick(contract)
        price = tick.mid if tick.mid is not None else contract.min_tick
        self._exec_counter += 1
        fill = BrokerFill(
            execution_id=f"mockexec-{self._exec_counter:06d}",
            broker_order_id=snapshot.broker_order_id,
            account_id=self._account_id,
            con_id=snapshot.con_id,
            symbol=snapshot.symbol,
            side=snapshot.side,
            quantity=snapshot.quantity,
            price=price,
            executed_at=utc_now(),
            commission=Decimal("0.35") * snapshot.quantity,
            commission_currency="USD",
        )
        self._fills.append(fill)
        self._open_orders.pop(snapshot.broker_order_id, None)
        self._apply_fill_to_positions(fill, contract)

    def _apply_fill_to_positions(self, fill: BrokerFill, contract: QualifiedContract) -> None:
        delta = fill.quantity * (1 if fill.side is OrderSide.BUY else -1)
        existing = self._positions.get(fill.con_id)
        new_quantity = (existing.quantity if existing else 0) + delta
        if new_quantity == 0:
            self._positions.pop(fill.con_id, None)
            return
        self._positions[fill.con_id] = BrokerPosition(
            account_id=self._account_id,
            con_id=fill.con_id,
            symbol=fill.symbol,
            local_symbol=contract.local_symbol,
            sec_type=contract.sec_type.value,
            exchange=contract.exchange,
            currency=contract.currency,
            quantity=new_quantity,
            average_cost=fill.price * contract.multiplier_decimal,
        )

    async def modify_order(self, request: OrderRequest, broker_order_id: str) -> PlaceOrderResult:
        if not request.transmit:
            raise BrokerOrderRejectedError("refusing to modify an order with transmit=False")
        self._require_connected()
        existing = self._open_orders.get(broker_order_id)
        if existing is None:
            return PlaceOrderResult(
                accepted=False,
                broker_order_id=broker_order_id,
                status=OrderStatus.REJECTED,
                error_code="UNKNOWN_ORDER",
                error_message=f"no working order {broker_order_id}",
            )
        self._open_orders[broker_order_id] = BrokerOrderSnapshot(
            broker_order_id=broker_order_id,
            account_id=existing.account_id,
            con_id=existing.con_id,
            symbol=existing.symbol,
            side=request.side,
            quantity=request.quantity,
            order_type=request.order_type,
            status=OrderStatus.SUBMITTED,
            remaining_quantity=request.quantity,
            limit_price=request.limit_price,
            stop_price=request.stop_price,
            time_in_force=request.time_in_force,
        )
        return PlaceOrderResult(
            accepted=True, broker_order_id=broker_order_id, status=OrderStatus.SUBMITTED
        )

    async def cancel_order(self, broker_order_id: str) -> bool:
        self._require_connected()
        return self._open_orders.pop(broker_order_id, None) is not None

    async def cancel_all_orders(self) -> int:
        self._require_connected()
        count = len(self._open_orders)
        self._open_orders.clear()
        _LOG.warning(
            "cancelled all working orders",
            extra={"event": "broker.cancel_all", "cancelled": count},
        )
        return count


def _deterministic_con_id(symbol: str, expiry: str) -> int:
    """Stable synthetic conId so mock runs and tests agree across processes."""
    digest = hashlib.sha256(f"{symbol}:{expiry}".encode()).digest()
    # Keep it comfortably inside a signed 32-bit range and clearly synthetic.
    return 900_000_000 + int.from_bytes(digest[:4], "big") % 90_000_000


def _month_code(expiry: str) -> str:
    """Approximate CME-style local symbol suffix, e.g. ``202603`` -> ``H6``."""
    codes = "FGHJKMNQUVXZ"
    month = int(expiry[4:6])
    year_digit = expiry[3]
    return f"{codes[month - 1]}{year_digit}"


__all__ = ["MOCK_ACCOUNT_ID", "MockBroker"]
