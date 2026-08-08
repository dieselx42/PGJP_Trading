"""Interactive Brokers adapter.

This is the ONLY module in the project that knows Interactive Brokers exists.
Everything above it deals in :class:`~app.broker.base.Broker` and the
broker-neutral models.

Library choice
--------------
This adapter targets the **official Interactive Brokers TWS API** (``ibapi``:
``EClient``/``EWrapper``), not a third-party wrapper. See
``docs/IBKR_API_NOTES.md`` for the packaging caveat -- IBKR does not publish
``ibapi`` to PyPI themselves -- and for what to do about it. The dependency is
an optional extra, and this module imports it lazily so that DISABLED and MOCK
modes run with no IBKR code loaded at all.

Verification status
-------------------
**This adapter has never been executed against a real IB Gateway.** US futures
trading permission for the account is still pending, so there was no session to
test against. The code is written to the documented API and is exercised by
unit tests only through its pure helpers (error classification, account-type
determination, contract mapping). Treat every path that talks to a socket as
unverified until the read-only checkout in ``RUNBOOK.md`` has been completed.

Account identity
----------------
The account type is derived from the **broker-reported account id**, never from
configuration and never from which port we dialled. IBKR paper accounts carry a
``D`` prefix (``DU``/``DF``/``DI``); live accounts start with ``U``. Anything
unrecognised yields :attr:`~app.enums.AccountType.UNKNOWN`, which the transmit
gate refuses. Deriving identity from our own config would defeat the entire
purpose of the mode/account cross-check.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Sequence
from concurrent.futures import Future as ThreadFuture
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Final

from app.broker.base import Broker
from app.broker.models import (
    AccountSummary,
    BrokerConnectionError,
    BrokerContractError,
    BrokerError,
    BrokerFill,
    BrokerOrderRejectedError,
    BrokerOrderSnapshot,
    BrokerPermissionError,
    BrokerPosition,
    BrokerTimeoutError,
    BrokerUnsupportedOperationError,
    ConnectionInfo,
    MarketDataTick,
    OrderRequest,
    PlaceOrderResult,
)
from app.contracts.models import ContractSpec, QualifiedContract
from app.enums import AccountType, ConnectionState, OrderSide, OrderStatus, OrderType, SecurityType
from app.logging_config import get_logger, mask_account_id
from app.utilities.timeutils import utc_now

_LOG = get_logger("broker.ibkr")

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ibapi.client import EClient
    from ibapi.contract import Contract
    from ibapi.order import Order
    from ibapi.wrapper import EWrapper
else:  # pragma: no cover - exercised only when the extra is installed
    try:
        from ibapi.client import EClient
        from ibapi.contract import Contract
        from ibapi.order import Order
        from ibapi.wrapper import EWrapper

        _IBAPI_IMPORT_ERROR: str | None = None
    except ImportError as exc:  # the extra is not installed
        # Distinct placeholder bases, not `object` twice: `_IBSession` inherits
        # from both, and duplicate bases are a TypeError at class-creation time.
        # These stubs only need to make the module importable; `connect()`
        # refuses outright when `_IBAPI_IMPORT_ERROR` is set, so no stub method
        # is ever reachable.
        class EWrapper:  # type: ignore[no-redef]
            """Placeholder for ibapi.wrapper.EWrapper when the extra is absent."""

        class EClient:  # type: ignore[no-redef]
            """Placeholder for ibapi.client.EClient when the extra is absent."""

            def __init__(self, wrapper: object) -> None:
                self._wrapper = wrapper

        Contract = None
        Order = None
        _IBAPI_IMPORT_ERROR = str(exc)

if TYPE_CHECKING:  # pragma: no cover
    _IBAPI_IMPORT_ERROR: str | None = None


def ibapi_available() -> bool:
    """True when the optional ``ibkr`` extra is installed."""
    return _IBAPI_IMPORT_ERROR is None


# ---------------------------------------------------------------------------
# IBKR error classification
#
# The point of this table is to answer one question for every IBKR error:
# "should we try again?". Permission and contract problems must NOT be retried
# -- the answer will not change until a human changes an account setting -- and
# retrying them is precisely how a bot ends up hammering the broker.
# ---------------------------------------------------------------------------

#: Purely informational notices. Not failures.
INFO_CODES: Final[frozenset[int]] = frozenset(
    {2103, 2104, 2105, 2106, 2107, 2108, 2119, 2158, 2168, 2169}
)

#: Transport problems. Retryable with capped exponential backoff.
CONNECTIVITY_CODES: Final[frozenset[int]] = frozenset({502, 504, 1100, 1101, 1102, 1300, 2110})

#: Permission / entitlement problems. NEVER retried automatically.
PERMISSION_CODES: Final[frozenset[int]] = frozenset(
    {
        354,  # requested market data is not subscribed
        10089,  # requires additional market data subscription
        10090,  # part of requested market data is not subscribed
        10091,  # requires TWS market data subscription
        10197,  # no market data during competing live session
        10168,  # requested market data is not subscribed (delayed)
        10187,  # not permitted to trade this product
        10276,  # account not enabled for this product
        460,  # security not available/allowed for this account
    }
)

#: Contract resolution problems.
CONTRACT_CODES: Final[frozenset[int]] = frozenset({200, 203, 321, 322, 430})

#: Order rejections.
ORDER_REJECT_CODES: Final[frozenset[int]] = frozenset({201, 202, 103, 104, 110, 10147, 10148})


def classify_ib_error(code: int, message: str = "") -> type[BrokerError] | None:
    """Map an IBKR error code to an exception class.

    Returns ``None`` for informational notices. Unknown codes are treated as
    generic :class:`BrokerError` (not retryable), because assuming an
    unrecognised failure is transient is how retry storms start.
    """
    if code in INFO_CODES:
        return None
    if code in CONNECTIVITY_CODES:
        return BrokerConnectionError
    if code in PERMISSION_CODES:
        return BrokerPermissionError
    if code in CONTRACT_CODES:
        return BrokerContractError
    if code in ORDER_REJECT_CODES:
        return BrokerOrderRejectedError
    lowered = message.lower()
    if "not permitted" in lowered or "not enabled" in lowered or "permission" in lowered:
        return BrokerPermissionError
    return BrokerError


def account_type_from_account_id(account_id: str | None) -> AccountType:
    """Determine account identity from the broker-reported account id.

    IBKR paper accounts are prefixed ``D`` (``DU`` individual, ``DF`` advisor,
    ``DI`` institution); live accounts begin with ``U``. Anything else is
    UNKNOWN, and UNKNOWN is never tradeable.
    """
    if not account_id:
        return AccountType.UNKNOWN
    cleaned = account_id.strip().upper()
    if not cleaned:
        return AccountType.UNKNOWN
    if cleaned.startswith(("DU", "DF", "DI")):
        return AccountType.PAPER
    if cleaned.startswith("U") and len(cleaned) >= 2 and cleaned[1].isdigit():
        return AccountType.LIVE
    return AccountType.UNKNOWN


def account_type_for_accounts(account_ids: Sequence[str]) -> AccountType:
    """Identity for a whole managed-account list.

    A session that manages a mixture of paper and live accounts is not something
    this system will trade. Mixed or empty yields UNKNOWN.
    """
    types = {account_type_from_account_id(a) for a in account_ids if a and a.strip()}
    if len(types) != 1:
        return AccountType.UNKNOWN
    return types.pop()


# ---------------------------------------------------------------------------
# Request bookkeeping
# ---------------------------------------------------------------------------


@dataclass
class _PendingRequest:
    """A request in flight, resolved from the ibapi reader thread."""

    future: ThreadFuture[Any]
    rows: list[Any] = field(default_factory=list)


class _IBSession(EWrapper, EClient):  # type: ignore[misc] # ibapi is untyped
    """Low-level ibapi client.

    All callbacks arrive on ibapi's reader thread; results are handed back to
    the asyncio side through :class:`concurrent.futures.Future`, which is
    thread-safe by design.
    """

    def __init__(self) -> None:
        EClient.__init__(self, self)
        self._lock = threading.Lock()
        self._pending: dict[int, _PendingRequest] = {}
        self._next_request_id = 1000
        self._next_order_id: int | None = None
        self._next_order_id_event = threading.Event()
        self.managed_accounts: list[str] = []
        self.managed_accounts_event = threading.Event()
        self.last_message_at: datetime | None = None
        self.connection_closed = threading.Event()
        self.order_statuses: dict[str, BrokerOrderSnapshot] = {}
        self.ticks: dict[int, dict[str, Decimal]] = {}
        self.tick_request_contract: dict[int, str] = {}
        self.commissions: dict[str, tuple[Decimal, str]] = {}
        #: The most recent error IBKR reported without attributing it to a
        #: request id. See :meth:`unattributed_error_since` for why.
        self._unattributed_error: tuple[datetime, int, str] | None = None
        #: Errors attributed to a request id that had no pending future --
        #: streaming subscriptions. See :meth:`request_error`.
        self._request_errors: dict[int, BrokerError] = {}

    # -- unattributed errors ----------------------------------------------

    def record_unattributed_error(self, code: int, message: str) -> None:
        with self._lock:
            self._unattributed_error = (utc_now(), code, message)

    def unattributed_error_since(self, started_at: datetime) -> str | None:
        """The last untargeted IBKR error that arrived after ``started_at``.

        IBKR reports some rejections with ``reqId`` ``-1``, so the adapter
        cannot know which pending request they belong to and must not guess --
        failing the wrong request would be worse than failing none. But the
        request it *does* belong to then waits out its full timeout and reports
        ``BrokerTimeoutError``, which says nothing about the actual cause.

        Observed case: with IB Gateway's Read-Only API mode on, ``reqOpenOrders``
        is refused with code 321 and ``reqId=-1``. The pending future was never
        completed, and the only evidence surfaced was "did not complete within
        20.0s" -- a timeout standing in for a plain, immediate refusal.

        Attaching the error to the timeout keeps the diagnosis without ever
        mis-attributing it. The ``started_at`` bound matters: an error from
        connect time must not be reported as the cause of a later timeout.
        """
        with self._lock:
            recorded = self._unattributed_error
        if recorded is None:
            return None
        seen_at, code, message = recorded
        if seen_at < started_at:
            return None
        return f"IBKR {code}: {message}"

    # -- request plumbing -------------------------------------------------

    def allocate_request_id(self) -> int:
        with self._lock:
            self._next_request_id += 1
            return self._next_request_id

    def register(self, request_id: int) -> ThreadFuture[Any]:
        future: ThreadFuture[Any] = ThreadFuture()
        with self._lock:
            self._pending[request_id] = _PendingRequest(future=future)
        return future

    def _rows(self, request_id: int) -> list[Any] | None:
        with self._lock:
            pending = self._pending.get(request_id)
        return None if pending is None else pending.rows

    def _resolve(self, request_id: int, value: Any) -> None:
        with self._lock:
            pending = self._pending.pop(request_id, None)
        if pending is not None and not pending.future.done():
            pending.future.set_result(value)

    def _reject(self, request_id: int, error: BaseException) -> bool:
        """Fail the pending request. Returns False when there was none.

        Streaming subscriptions (market data) have no pending future to fail --
        there is no single response to complete -- so a refusal aimed at one
        would otherwise be dropped on the floor. The caller records those
        instead; see :meth:`request_error`.
        """
        with self._lock:
            pending = self._pending.pop(request_id, None)
        if pending is None:
            return False
        if not pending.future.done():
            pending.future.set_exception(error)
        return True

    def record_request_error(self, request_id: int, error: BrokerError) -> None:
        with self._lock:
            self._request_errors[request_id] = error

    def request_error(self, request_id: int) -> BrokerError | None:
        """Any error IBKR reported against a request with no pending future."""
        with self._lock:
            return self._request_errors.get(request_id)

    def fail_all(self, error: BaseException) -> None:
        with self._lock:
            pending = list(self._pending.items())
            self._pending.clear()
        for _, entry in pending:
            if not entry.future.done():
                entry.future.set_exception(error)

    # -- ibapi callbacks --------------------------------------------------

    def error(self, *args: Any, **kwargs: Any) -> None:
        """Handle ``EWrapper.error`` across ibapi signature revisions.

        ibapi 10.30 inserted an ``errorTime`` parameter ahead of ``errorCode``.
        Rather than pin a version, the arguments are identified positionally by
        shape, so the adapter keeps working across both.
        """
        req_id, code, message = _parse_error_args(args, kwargs)
        self.last_message_at = utc_now()

        error_class = classify_ib_error(code, message)
        if error_class is None:
            _LOG.info(
                "ibkr notice",
                extra={"event": "broker.ib_notice", "ib_code": code, "ib_message": message},
            )
            return

        _LOG.warning(
            "ibkr error",
            extra={
                "event": "broker.ib_error",
                "ib_code": code,
                "ib_message": message,
                "req_id": req_id,
                "classified_as": error_class.__name__,
                "retryable": error_class.retryable,
            },
        )
        if req_id is not None and req_id > 0:
            failure = error_class(f"IBKR {code}: {message}")
            if not self._reject(req_id, failure):
                # A streaming subscription: no future to fail, so keep it for
                # whoever reads that subscription's data. Without this, a
                # refused market-data request is indistinguishable from a quiet
                # market.
                self.record_request_error(req_id, failure)
        elif error_class is BrokerConnectionError:
            self.fail_all(BrokerConnectionError(f"IBKR {code}: {message}"))
        else:
            # Not attributable to a request, and not fatal to the session. The
            # request it actually refers to will time out; record this so the
            # timeout can report why instead of just how long it waited.
            self.record_unattributed_error(code, message)

    def connectionClosed(self) -> None:
        self.connection_closed.set()
        self.fail_all(BrokerConnectionError("IBKR connection closed"))
        _LOG.warning("ibkr connection closed", extra={"event": "broker.disconnected"})

    def nextValidId(self, orderId: int) -> None:
        with self._lock:
            self._next_order_id = orderId
        self._next_order_id_event.set()
        self.last_message_at = utc_now()

    def managedAccounts(self, accountsList: str) -> None:
        self.managed_accounts = [a for a in accountsList.split(",") if a.strip()]
        self.managed_accounts_event.set()
        self.last_message_at = utc_now()

    def take_order_id(self) -> int:
        with self._lock:
            if self._next_order_id is None:
                raise BrokerConnectionError("IBKR has not issued a valid order id yet")
            order_id = self._next_order_id
            self._next_order_id = order_id + 1
        return order_id

    # account summary
    def accountSummary(
        self,
        reqId: int,
        account: str,
        tag: str,
        value: str,
        currency: str,
    ) -> None:
        rows = self._rows(reqId)
        if rows is not None:
            rows.append((account, tag, value, currency))
        self.last_message_at = utc_now()

    def accountSummaryEnd(self, reqId: int) -> None:
        self._resolve(reqId, list(self._rows(reqId) or []))

    # positions
    def position(
        self,
        account: str,
        contract: Any,
        position: Any,
        avgCost: float,
    ) -> None:
        rows = self._rows(_POSITION_REQ_ID)
        if rows is not None:
            rows.append((account, contract, position, avgCost))
        self.last_message_at = utc_now()

    def positionEnd(self) -> None:
        self._resolve(_POSITION_REQ_ID, list(self._rows(_POSITION_REQ_ID) or []))

    # open orders
    def openOrder(
        self,
        orderId: int,
        contract: Any,
        order: Any,
        orderState: Any,
    ) -> None:
        rows = self._rows(_OPEN_ORDERS_REQ_ID)
        if rows is not None:
            rows.append((orderId, contract, order, orderState))
        self.last_message_at = utc_now()

    def openOrderEnd(self) -> None:
        self._resolve(_OPEN_ORDERS_REQ_ID, list(self._rows(_OPEN_ORDERS_REQ_ID) or []))

    def orderStatus(
        self,
        orderId: int,
        status: str,
        filled: Any,
        remaining: Any,
        avgFillPrice: float,
        permId: int,
        parentId: int,
        lastFillPrice: float,
        clientId: int,
        whyHeld: str,
        mktCapPrice: float = 0.0,
    ) -> None:
        self.last_message_at = utc_now()
        _LOG.info(
            "ibkr order status",
            extra={
                "event": "order.status",
                "broker_order_id": str(orderId),
                "ib_status": status,
                "filled": str(filled),
                "remaining": str(remaining),
                "avg_fill_price": avgFillPrice,
                "why_held": whyHeld,
            },
        )

    # executions
    def execDetails(self, reqId: int, contract: Any, execution: Any) -> None:
        rows = self._rows(reqId)
        if rows is not None:
            rows.append((contract, execution))
        self.last_message_at = utc_now()

    def execDetailsEnd(self, reqId: int) -> None:
        self._resolve(reqId, list(self._rows(reqId) or []))

    def commissionReport(self, commissionReport: Any) -> None:
        # A malformed commission report costs us a commission figure, not the
        # session. Everything else about the fill is already recorded.
        with contextlib.suppress(AttributeError, TypeError, InvalidOperation):
            self.commissions[str(commissionReport.execId)] = (
                _to_decimal(commissionReport.commission),
                str(commissionReport.currency),
            )

    # contract details
    def contractDetails(self, reqId: int, contractDetails: Any) -> None:
        rows = self._rows(reqId)
        if rows is not None:
            rows.append(contractDetails)
        self.last_message_at = utc_now()

    def contractDetailsEnd(self, reqId: int) -> None:
        self._resolve(reqId, list(self._rows(reqId) or []))

    # market data
    def tickPrice(self, reqId: int, tickType: int, price: float, attrib: Any) -> None:
        if price is None or price < 0:
            return
        field_name = _TICK_PRICE_FIELDS.get(tickType)
        if field_name is None:
            return
        self.ticks.setdefault(reqId, {})[field_name] = _to_decimal(price)
        self.last_message_at = utc_now()


_POSITION_REQ_ID: Final = 1
_OPEN_ORDERS_REQ_ID: Final = 2

_TICK_PRICE_FIELDS: Final[dict[int, str]] = {
    1: "bid",
    2: "ask",
    4: "last",
    66: "bid",  # delayed bid
    67: "ask",  # delayed ask
    68: "last",  # delayed last
}

#: IBKR order status strings mapped onto our vocabulary.
IB_STATUS_MAP: Final[dict[str, OrderStatus]] = {
    "PendingSubmit": OrderStatus.PENDING_SUBMIT,
    "PendingCancel": OrderStatus.CANCEL_PENDING,
    "PreSubmitted": OrderStatus.SUBMITTED,
    "Submitted": OrderStatus.ACKNOWLEDGED,
    "ApiCancelled": OrderStatus.CANCELLED,
    "Cancelled": OrderStatus.CANCELLED,
    "Filled": OrderStatus.FILLED,
    "Inactive": OrderStatus.REJECTED,
}

_ORDER_TYPE_MAP: Final[dict[OrderType, str]] = {
    OrderType.MARKET: "MKT",
    OrderType.LIMIT: "LMT",
    OrderType.STOP: "STP",
    OrderType.STOP_LIMIT: "STP LMT",
}

#: Order types this adapter will actually transmit in this phase. Models exist
#: for the rest so they can be added deliberately, with their own tests.
TRANSMITTABLE_ORDER_TYPES: Final[frozenset[OrderType]] = frozenset(
    {OrderType.MARKET, OrderType.LIMIT}
)


def _parse_error_args(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[int | None, int, str]:
    """Extract ``(reqId, code, message)`` from either ibapi error signature."""
    if kwargs:
        return (
            kwargs.get("reqId"),
            int(kwargs.get("errorCode", -1)),
            str(kwargs.get("errorString", "")),
        )
    numeric = [a for a in args if isinstance(a, int)]
    strings = [a for a in args if isinstance(a, str)]
    message = strings[0] if strings else ""
    if len(numeric) >= 3:
        # (reqId, errorTime, errorCode) -- 10.30+
        return numeric[0], numeric[2], message
    if len(numeric) == 2:
        # (reqId, errorCode) -- pre 10.30
        return numeric[0], numeric[1], message
    if len(numeric) == 1:
        return None, numeric[0], message
    return None, -1, message


def _to_decimal(value: Any) -> Decimal:
    """Convert an ibapi numeric (float, str, or ``Decimal``) safely."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class IBKRBroker(Broker):
    """:class:`~app.broker.base.Broker` implementation backed by the TWS API."""

    name = "ibkr"

    def __init__(
        self,
        *,
        host: str,
        port: int,
        client_id: int,
        connect_timeout_seconds: float = 15.0,
        request_timeout_seconds: float = 20.0,
        expected_account_type: AccountType | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._client_id = client_id
        self._connect_timeout = connect_timeout_seconds
        self._request_timeout = request_timeout_seconds
        self._expected_account_type = expected_account_type

        self._session: _IBSession | None = None
        self._reader_thread: threading.Thread | None = None
        self._state = ConnectionState.DISCONNECTED
        self._account_type = AccountType.UNKNOWN
        self._account_id: str | None = None
        self._connected_at: datetime | None = None
        self._market_data_requests: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> ConnectionInfo:
        if not ibapi_available():
            raise BrokerConnectionError(
                "the IBKR TWS API (ibapi) is not installed. It is deliberately not a "
                "runtime dependency: how it gets installed is a supply-chain decision "
                "for the component that can place orders, and docs/IBKR_API_NOTES.md "
                "sets out the options. DISABLED and MOCK modes do not require it."
            )
        if self.is_connected():
            return self.get_connection_info()

        self._state = ConnectionState.CONNECTING
        session = _IBSession()
        self._session = session

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None, lambda: session.connect(self._host, self._port, self._client_id)
            )
        except OSError as exc:
            self._state = ConnectionState.FAILED
            raise BrokerConnectionError(
                f"cannot reach IB Gateway at {self._host}:{self._port} - {exc}"
            ) from exc

        self._reader_thread = threading.Thread(target=session.run, name="ibapi-reader", daemon=True)
        self._reader_thread.start()

        # IB Gateway announces the managed accounts and the next valid order id
        # as soon as the handshake completes. Both are required before the
        # session can be considered usable.
        ready = await loop.run_in_executor(
            None, session.managed_accounts_event.wait, self._connect_timeout
        )
        if not ready:
            await self.disconnect()
            self._state = ConnectionState.FAILED
            raise BrokerTimeoutError(
                f"IB Gateway did not report managed accounts within {self._connect_timeout}s"
            )
        await loop.run_in_executor(None, session._next_order_id_event.wait, self._connect_timeout)

        self._account_type = account_type_for_accounts(session.managed_accounts)
        self._account_id = session.managed_accounts[0] if session.managed_accounts else None
        self._connected_at = utc_now()
        self._state = ConnectionState.CONNECTED

        if (
            self._expected_account_type is not None
            and self._account_type is not self._expected_account_type
        ):
            # Refuse to stay attached to the wrong kind of account at all. The
            # transmit gate would reject every order anyway, but holding the
            # session open invites a later mistake.
            observed = self._account_type.value
            expected = self._expected_account_type.value
            await self.disconnect()
            raise BrokerConnectionError(
                f"refusing session: IBKR reported a {observed} account "
                f"but this process is configured for {expected}"
            )

        info = self.get_connection_info()
        _LOG.info(
            "ibkr connected",
            extra={"event": "broker.connected", "broker": self.name, **info.describe()},
        )
        return info

    async def disconnect(self) -> None:
        session = self._session
        self._state = ConnectionState.DISCONNECTED
        self._account_type = AccountType.UNKNOWN
        self._market_data_requests.clear()
        if session is None:
            return
        try:
            session.done = True
            session.disconnect()
        except Exception as exc:  # noqa: BLE001 -- teardown must never raise
            _LOG.warning(
                "error while disconnecting from IBKR",
                extra={"event": "broker.disconnect_error", "error": str(exc)},
            )
        finally:
            self._session = None
            self._reader_thread = None

    def is_connected(self) -> bool:
        session = self._session
        if session is None or self._state is not ConnectionState.CONNECTED:
            return False
        try:
            return bool(session.isConnected())
        except Exception:  # noqa: BLE001 -- treat an unusable client as disconnected
            return False

    def get_connection_state(self) -> ConnectionState:
        if self._state is ConnectionState.CONNECTED and not self.is_connected():
            self._state = ConnectionState.DISCONNECTED
        return self._state

    def get_connection_info(self) -> ConnectionInfo:
        connected = self.is_connected()
        return ConnectionInfo(
            connected=connected,
            account_type=self._account_type if connected else AccountType.UNKNOWN,
            account_id=self._account_id if connected else None,
            server_version=str(getattr(self._session, "serverVersion_", "")) or None,
            connection_time=self._connected_at,
            host=self._host,
            port=self._port,
            client_id=self._client_id,
            detail="official IBKR TWS API (ibapi)",
        )

    def last_heartbeat_age_seconds(self) -> float | None:
        session = self._session
        if session is None or session.last_message_at is None:
            return None
        return (utc_now() - session.last_message_at).total_seconds()

    # ------------------------------------------------------------------
    # Request helper
    # ------------------------------------------------------------------

    def _require_session(self) -> _IBSession:
        session = self._session
        if session is None or not self.is_connected():
            raise BrokerConnectionError("not connected to IB Gateway")
        return session

    async def _await_request(self, future: ThreadFuture[Any], *, what: str) -> Any:
        loop = asyncio.get_running_loop()
        started_at = utc_now()
        try:
            return await asyncio.wait_for(
                asyncio.wrap_future(future, loop=loop), timeout=self._request_timeout
            )
        except TimeoutError as exc:
            detail = f"{what} did not complete within {self._request_timeout}s"
            session = self._session
            if session is not None:
                unattributed = session.unattributed_error_since(started_at)
                if unattributed:
                    detail += (
                        f"; IBKR reported an error with no request id during that "
                        f"window, which is very likely the cause: {unattributed}"
                    )
            raise BrokerTimeoutError(detail) from exc

    # ------------------------------------------------------------------
    # Account and state
    # ------------------------------------------------------------------

    async def get_account_summary(self) -> AccountSummary:
        session = self._require_session()
        request_id = session.allocate_request_id()
        future = session.register(request_id)
        tags = (
            "NetLiquidation,AvailableFunds,BuyingPower,ExcessLiquidity,"
            "MaintMarginReq,RealizedPnL,UnrealizedPnL"
        )
        session.reqAccountSummary(request_id, "All", tags)
        try:
            rows = await self._await_request(future, what="account summary")
        finally:
            with _suppress_errors():
                session.cancelAccountSummary(request_id)

        values: dict[str, str] = {}
        currency = "USD"
        account_id = self._account_id or ""
        for account, tag, value, row_currency in rows:
            values[tag] = value
            if row_currency:
                currency = row_currency
            if account:
                account_id = account

        return AccountSummary(
            account_id=account_id,
            account_type=self._account_type,
            currency=currency,
            net_liquidation=_opt_decimal(values.get("NetLiquidation")),
            available_funds=_opt_decimal(values.get("AvailableFunds")),
            buying_power=_opt_decimal(values.get("BuyingPower")),
            excess_liquidity=_opt_decimal(values.get("ExcessLiquidity")),
            maintenance_margin=_opt_decimal(values.get("MaintMarginReq")),
            realized_pnl=_opt_decimal(values.get("RealizedPnL")),
            unrealized_pnl=_opt_decimal(values.get("UnrealizedPnL")),
            # The TWS API exposes no direct "is this account permitted to trade
            # CME futures" flag. Rather than infer one, we report None
            # ("unknown"), which the transmit gate treats as not permitted. The
            # operator establishes permission out of band and records it in
            # SOL_FUTURES_PERMISSION_READY.
            futures_permission=None,
            raw=values,
        )

    async def get_positions(self) -> Sequence[BrokerPosition]:
        session = self._require_session()
        future = session.register(_POSITION_REQ_ID)
        session.reqPositions()
        try:
            rows = await self._await_request(future, what="positions")
        finally:
            with _suppress_errors():
                session.cancelPositions()

        positions: list[BrokerPosition] = []
        for account, contract, quantity, avg_cost in rows:
            size = int(_to_decimal(quantity))
            if size == 0:
                continue
            positions.append(
                BrokerPosition(
                    account_id=str(account),
                    con_id=int(contract.conId),
                    symbol=str(contract.symbol),
                    local_symbol=str(contract.localSymbol or ""),
                    sec_type=str(contract.secType),
                    exchange=str(contract.exchange or ""),
                    currency=str(contract.currency or ""),
                    quantity=size,
                    average_cost=_to_decimal(avg_cost),
                )
            )
        return tuple(positions)

    async def get_open_orders(self) -> Sequence[BrokerOrderSnapshot]:
        session = self._require_session()
        future = session.register(_OPEN_ORDERS_REQ_ID)
        session.reqOpenOrders()
        rows = await self._await_request(future, what="open orders")

        snapshots: list[BrokerOrderSnapshot] = []
        for order_id, contract, order, order_state in rows:
            snapshots.append(
                BrokerOrderSnapshot(
                    broker_order_id=str(order_id),
                    account_id=str(getattr(order, "account", "") or self._account_id or ""),
                    con_id=int(contract.conId),
                    symbol=str(contract.symbol),
                    side=OrderSide.BUY if str(order.action).upper() == "BUY" else OrderSide.SELL,
                    quantity=int(_to_decimal(order.totalQuantity)),
                    order_type=_order_type_from_ib(str(order.orderType)),
                    status=IB_STATUS_MAP.get(
                        str(getattr(order_state, "status", "")), OrderStatus.SUBMITTED
                    ),
                    limit_price=_opt_decimal(getattr(order, "lmtPrice", None)),
                    stop_price=_opt_decimal(getattr(order, "auxPrice", None)),
                    perm_id=str(getattr(order, "permId", "") or ""),
                    client_id=int(getattr(order, "clientId", 0) or 0),
                )
            )
        return tuple(snapshots)

    async def get_fills(self, *, since_seconds: int = 86_400) -> Sequence[BrokerFill]:
        session = self._require_session()
        request_id = session.allocate_request_id()
        future = session.register(request_id)
        execution_filter = _build_execution_filter()
        session.reqExecutions(request_id, execution_filter)
        rows = await self._await_request(future, what="executions")

        fills: list[BrokerFill] = []
        cutoff = utc_now().timestamp() - since_seconds
        for contract, execution in rows:
            executed_at = _parse_ib_time(str(execution.time))
            if executed_at.timestamp() < cutoff:
                continue
            exec_id = str(execution.execId)
            commission, commission_currency = session.commissions.get(exec_id, (None, None))
            fills.append(
                BrokerFill(
                    execution_id=exec_id,
                    broker_order_id=str(execution.orderId),
                    account_id=str(execution.acctNumber),
                    con_id=int(contract.conId),
                    symbol=str(contract.symbol),
                    side=(
                        OrderSide.BUY
                        if str(execution.side).upper().startswith("B")
                        else OrderSide.SELL
                    ),
                    quantity=int(_to_decimal(execution.shares)),
                    price=_to_decimal(execution.price),
                    executed_at=executed_at,
                    commission=commission,
                    commission_currency=commission_currency,
                )
            )
        return tuple(fills)

    # ------------------------------------------------------------------
    # Contracts
    # ------------------------------------------------------------------

    async def get_contract_details(self, spec: ContractSpec) -> Sequence[QualifiedContract]:
        session = self._require_session()
        if spec.sec_type is SecurityType.CONTINUOUS_FUTURE:
            raise BrokerContractError(
                "continuous futures are analytics-only and are never qualified for trading"
            )
        request_id = session.allocate_request_id()
        future = session.register(request_id)
        session.reqContractDetails(request_id, _to_ib_contract(spec))
        rows = await self._await_request(future, what=f"contract details for {spec.symbol}")
        return tuple(_from_ib_contract_details(row) for row in rows)

    async def qualify_contract(self, spec: ContractSpec) -> QualifiedContract:
        spec.require_orderable()
        matches = await self.get_contract_details(spec)
        if not matches:
            raise BrokerContractError(
                f"{spec.symbol} {spec.last_trade_date_or_contract_month}: "
                "IBKR returned no matching contract"
            )
        if len(matches) > 1:
            detail = ", ".join(f"{c.local_symbol}({c.con_id})" for c in matches[:5])
            raise BrokerContractError(
                f"{spec.symbol}: ambiguous specification matched {len(matches)} contracts "
                f"[{detail}]; refusing to guess"
            )
        return matches[0]

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    async def request_market_data(self, contract: QualifiedContract) -> MarketDataTick:
        session = self._require_session()
        key = contract.key()
        request_id = self._market_data_requests.get(key)
        if request_id is None:
            request_id = session.allocate_request_id()
            self._market_data_requests[key] = request_id
            session.reqMktData(
                request_id, _to_ib_contract(contract.to_spec()), "", False, False, []
            )
            # Streaming data has no explicit "end" callback; give the first
            # ticks a moment to arrive before reporting a snapshot.
            await asyncio.sleep(0.5)

        values = session.ticks.get(request_id, {})
        if not values:
            # No ticks. Distinguish "IBKR refused the subscription" from "the
            # market is quiet" -- they are the same empty result and completely
            # different problems. A refusal is a permission error a human has to
            # fix at IBKR; a quiet market outside liquid hours is nothing.
            refusal = session.request_error(request_id)
            if refusal is not None:
                self._market_data_requests.pop(key, None)
                raise refusal

        return MarketDataTick(
            contract_key=key,
            received_at=utc_now(),
            source="ibkr",
            bid=values.get("bid"),
            ask=values.get("ask"),
            last=values.get("last"),
        )

    async def cancel_market_data(self, contract: QualifiedContract) -> None:
        session = self._session
        request_id = self._market_data_requests.pop(contract.key(), None)
        if session is not None and request_id is not None:
            with _suppress_errors():
                session.cancelMktData(request_id)

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    async def place_order(self, request: OrderRequest) -> PlaceOrderResult:
        if not request.transmit:
            raise BrokerOrderRejectedError(
                "refusing to place an order with transmit=False "
                "(this is the broker adapter's independent safety barrier)"
            )
        if request.order_type not in TRANSMITTABLE_ORDER_TYPES:
            raise BrokerUnsupportedOperationError(
                f"{request.order_type.value} orders are modelled but not transmitted in this "
                "phase; enable them deliberately with their own tests"
            )
        if request.contract.is_continuous:
            raise BrokerOrderRejectedError("continuous futures can never carry an order")

        session = self._require_session()
        order_id = session.take_order_id()
        ib_order = _to_ib_order(request)
        session.placeOrder(order_id, _to_ib_contract(request.contract.to_spec()), ib_order)
        _LOG.warning(
            "order transmitted to IBKR",
            extra={
                "event": "order.transmitted",
                "internal_order_id": request.internal_order_id,
                "correlation_id": request.correlation_id,
                "broker_order_id": str(order_id),
                "symbol": request.contract.symbol,
                "con_id": request.contract.con_id,
                "side": request.side.value,
                "quantity": request.quantity,
                "order_type": request.order_type.value,
                "account_id": mask_account_id(self._account_id),
            },
        )
        return PlaceOrderResult(
            accepted=True,
            broker_order_id=str(order_id),
            status=OrderStatus.PENDING_SUBMIT,
        )

    async def modify_order(self, request: OrderRequest, broker_order_id: str) -> PlaceOrderResult:
        if not request.transmit:
            raise BrokerOrderRejectedError("refusing to modify an order with transmit=False")
        session = self._require_session()
        session.placeOrder(
            int(broker_order_id), _to_ib_contract(request.contract.to_spec()), _to_ib_order(request)
        )
        return PlaceOrderResult(
            accepted=True, broker_order_id=broker_order_id, status=OrderStatus.PENDING_SUBMIT
        )

    async def cancel_order(self, broker_order_id: str) -> bool:
        session = self._require_session()
        try:
            session.cancelOrder(int(broker_order_id), _empty_order_cancel())
        except TypeError:
            # Older ibapi releases take only the order id.
            session.cancelOrder(int(broker_order_id))
        return True

    async def cancel_all_orders(self) -> int:
        """Issue a global cancel. Does not touch positions."""
        session = self._require_session()
        open_orders = await self.get_open_orders()
        try:
            session.reqGlobalCancel(_empty_order_cancel())
        except TypeError:
            session.reqGlobalCancel()
        _LOG.warning(
            "global cancel requested",
            extra={"event": "broker.cancel_all", "open_orders_before": len(open_orders)},
        )
        return len(open_orders)


# ---------------------------------------------------------------------------
# ibapi <-> domain mapping
# ---------------------------------------------------------------------------


class _suppress_errors:  # noqa: N801 - context manager
    """Swallow teardown/cleanup errors that must not mask the real failure."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return exc_type is not None


def _to_ib_contract(spec: ContractSpec) -> Any:
    contract = Contract()
    contract.symbol = spec.symbol
    contract.secType = spec.sec_type.value
    contract.exchange = spec.exchange
    contract.currency = spec.currency
    if spec.last_trade_date_or_contract_month:
        contract.lastTradeDateOrContractMonth = spec.last_trade_date_or_contract_month
    if spec.trading_class:
        contract.tradingClass = spec.trading_class
    if spec.local_symbol:
        contract.localSymbol = spec.local_symbol
    if spec.con_id:
        contract.conId = spec.con_id
    if spec.multiplier:
        contract.multiplier = spec.multiplier
    return contract


def _to_ib_order(request: OrderRequest) -> Any:
    order = Order()
    order.action = request.side.value
    order.totalQuantity = Decimal(request.quantity)
    order.orderType = _ORDER_TYPE_MAP[request.order_type]
    order.tif = request.time_in_force.value
    if request.limit_price is not None:
        order.lmtPrice = float(request.limit_price)
    if request.stop_price is not None:
        order.auxPrice = float(request.stop_price)
    # Explicit, never defaulted: the adapter has already refused transmit=False.
    order.transmit = True
    order.orderRef = request.correlation_id
    return order


def _empty_order_cancel() -> Any:
    from ibapi.order_cancel import OrderCancel  # noqa: PLC0415 - optional dependency

    return OrderCancel()


def _build_execution_filter() -> Any:
    from ibapi.execution import ExecutionFilter  # noqa: PLC0415 - optional dependency

    return ExecutionFilter()


def _order_type_from_ib(value: str) -> OrderType:
    reverse = {v: k for k, v in _ORDER_TYPE_MAP.items()}
    return reverse.get(value.upper(), OrderType.MARKET)


def _from_ib_contract_details(details: Any) -> QualifiedContract:
    contract = details.contract
    return QualifiedContract(
        con_id=int(contract.conId),
        symbol=str(contract.symbol),
        local_symbol=str(contract.localSymbol or ""),
        sec_type=SecurityType.FUTURE,
        exchange=str(contract.exchange or ""),
        currency=str(contract.currency or ""),
        expiration=str(contract.lastTradeDateOrContractMonth or ""),
        last_trade_date=str(
            getattr(details, "realExpirationDate", "")
            or contract.lastTradeDateOrContractMonth
            or ""
        ),
        multiplier=str(contract.multiplier or "1"),
        min_tick=_to_decimal(getattr(details, "minTick", "0.01") or "0.01"),
        trading_class=str(getattr(details, "tradingClass", "") or contract.tradingClass or ""),
        primary_exchange=str(contract.primaryExchange or "") or None,
        trading_hours=str(getattr(details, "tradingHours", "") or "") or None,
        liquid_hours=str(getattr(details, "liquidHours", "") or "") or None,
        time_zone_id=str(getattr(details, "timeZoneId", "") or "") or None,
        long_name=str(getattr(details, "longName", "") or "") or None,
        qualified_at=utc_now().isoformat(),
        raw={
            "marketName": str(getattr(details, "marketName", "")),
            "underConId": getattr(details, "underConId", None),
            "contractMonth": str(getattr(details, "contractMonth", "")),
        },
    )


def _opt_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        parsed = Decimal(str(value))
    except InvalidOperation:
        return None
    # IBKR uses these sentinels for "no value".
    if parsed in (Decimal("-1"), Decimal("1.7976931348623157E308")):
        return None
    return parsed


def _parse_ib_time(value: str) -> datetime:
    """Parse an IBKR execution timestamp into an aware UTC datetime.

    IBKR emits ``YYYYMMDD HH:MM:SS`` (with an optional trailing timezone) or a
    Unix epoch string. A timestamp we cannot parse is an error, not something to
    guess at, because execution times drive daily P&L windows.
    """
    from datetime import UTC  # noqa: PLC0415 - keep the module import list tight

    text = value.strip()
    if text.isdigit() and len(text) >= 10:
        return datetime.fromtimestamp(int(text), tz=UTC)
    parts = text.split()
    stamp = " ".join(parts[:2])
    tz_name = parts[2] if len(parts) > 2 else None
    parsed = datetime.strptime(stamp, "%Y%m%d %H:%M:%S")  # noqa: DTZ007 - tz applied below
    if tz_name:
        try:
            from zoneinfo import ZoneInfo  # noqa: PLC0415

            return parsed.replace(tzinfo=ZoneInfo(tz_name)).astimezone(UTC)
        except Exception:  # noqa: BLE001,S110 - fall through to the UTC reading below
            pass
    return parsed.replace(tzinfo=UTC)


__all__ = [
    "CONNECTIVITY_CODES",
    "CONTRACT_CODES",
    "IB_STATUS_MAP",
    "INFO_CODES",
    "ORDER_REJECT_CODES",
    "PERMISSION_CODES",
    "TRANSMITTABLE_ORDER_TYPES",
    "IBKRBroker",
    "account_type_for_accounts",
    "account_type_from_account_id",
    "classify_ib_error",
    "ibapi_available",
]
