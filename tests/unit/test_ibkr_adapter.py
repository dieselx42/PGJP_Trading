"""IBKR adapter -- the parts that can be tested without a gateway.

This file covers the pure decision logic: error classification, account-type
determination, order-type mapping, and the error-signature parser. The socket
paths cannot be exercised here (see the module docstring in
``app/broker/ibkr_broker.py``); the read-only checkout in ``RUNBOOK.md`` is
what validates those, and it has not been possible to run it while futures
permission is pending.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from app.broker.ibkr_broker import (
    _FIRST_TICK_POLL_SECONDS,
    CONNECTIVITY_CODES,
    INFO_CODES,
    PERMISSION_CODES,
    TRANSMITTABLE_ORDER_TYPES,
    IBKRBroker,
    _parse_error_args,
    account_type_for_accounts,
    account_type_from_account_id,
    classify_ib_error,
    ibapi_available,
)
from app.broker.models import (
    BrokerConnectionError,
    BrokerContractError,
    BrokerError,
    BrokerOrderRejectedError,
    BrokerPermissionError,
)
from app.enums import AccountType, ConnectionState, OrderType


class TestAccountTypeDetection:
    @pytest.mark.parametrize("account", ["DU1234567", "du1234567", "DF999999", "DI42"])
    def test_d_prefixed_accounts_are_paper(self, account: str) -> None:
        assert account_type_from_account_id(account) is AccountType.PAPER

    @pytest.mark.parametrize("account", ["U1234567", "u7654321"])
    def test_u_prefixed_accounts_are_live(self, account: str) -> None:
        assert account_type_from_account_id(account) is AccountType.LIVE

    @pytest.mark.parametrize("account", [None, "", "   ", "XYZ123", "U", "UNKNOWN"])
    def test_anything_unrecognised_is_unknown(self, account: str | None) -> None:
        """Unknown is safe; guessing is not."""
        assert account_type_from_account_id(account) is AccountType.UNKNOWN

    def test_a_single_account_list_resolves(self) -> None:
        assert account_type_for_accounts(["DU111111"]) is AccountType.PAPER
        assert account_type_for_accounts(["U111111"]) is AccountType.LIVE

    def test_a_mixed_account_list_is_unknown(self) -> None:
        """A session managing both paper and live accounts is not tradeable."""
        assert account_type_for_accounts(["DU111", "U222"]) is AccountType.UNKNOWN

    def test_an_empty_account_list_is_unknown(self) -> None:
        assert account_type_for_accounts([]) is AccountType.UNKNOWN

    def test_account_type_is_never_derived_from_configuration(self) -> None:
        """A live-port broker with a paper account must report PAPER, not LIVE."""
        broker = IBKRBroker(host="127.0.0.1", port=4001, client_id=1)
        # Nothing about the constructor may pre-decide the account identity.
        assert broker.get_connection_info().account_type is AccountType.UNKNOWN


class TestErrorClassification:
    @pytest.mark.parametrize("code", sorted(INFO_CODES))
    def test_informational_codes_are_not_errors(self, code: int) -> None:
        assert classify_ib_error(code) is None

    @pytest.mark.parametrize("code", sorted(CONNECTIVITY_CODES))
    def test_connectivity_codes_are_retryable(self, code: int) -> None:
        cls = classify_ib_error(code)
        assert cls is BrokerConnectionError
        assert cls.retryable is True

    @pytest.mark.parametrize("code", sorted(PERMISSION_CODES))
    def test_permission_codes_are_never_retryable(self, code: int) -> None:
        """Retrying a permission refusal is how a bot ends up hammering IBKR."""
        cls = classify_ib_error(code)
        assert cls is BrokerPermissionError
        assert cls.retryable is False

    def test_contract_codes_map_to_contract_errors(self) -> None:
        assert classify_ib_error(200, "No security definition found") is BrokerContractError

    def test_order_rejections_map_to_order_errors(self) -> None:
        assert classify_ib_error(201, "Order rejected") is BrokerOrderRejectedError

    def test_permission_wording_is_detected_for_unknown_codes(self) -> None:
        assert (
            classify_ib_error(99999, "Account not enabled for this product")
            is BrokerPermissionError
        )

    def test_unknown_codes_default_to_non_retryable(self) -> None:
        """Assuming an unrecognised failure is transient starts retry storms."""
        cls = classify_ib_error(424242, "something new")
        assert cls is BrokerError
        assert cls.retryable is False


class TestErrorSignatureParsing:
    def test_pre_10_30_signature(self) -> None:
        assert _parse_error_args((7, 201, "Order rejected"), {}) == (7, 201, "Order rejected")

    def test_10_30_signature_with_error_time(self) -> None:
        """ibapi 10.30 inserted errorTime before errorCode."""
        assert _parse_error_args((7, 1717171717, 201, "Order rejected"), {}) == (
            7,
            201,
            "Order rejected",
        )

    def test_keyword_signature(self) -> None:
        parsed = _parse_error_args(
            (), {"reqId": 3, "errorCode": 504, "errorString": "Not connected"}
        )
        assert parsed == (3, 504, "Not connected")

    def test_degenerate_input_does_not_raise(self) -> None:
        assert _parse_error_args((), {}) == (None, -1, "")


class TestTransmissionPolicy:
    def test_only_market_and_limit_are_transmittable_today(self) -> None:
        assert {OrderType.MARKET, OrderType.LIMIT} == TRANSMITTABLE_ORDER_TYPES
        for order_type in (OrderType.STOP, OrderType.STOP_LIMIT, OrderType.BRACKET):
            assert order_type not in TRANSMITTABLE_ORDER_TYPES

    async def test_connect_without_the_optional_extra_explains_itself(self) -> None:
        if ibapi_available():
            pytest.skip("the ibkr extra is installed in this environment")
        broker = IBKRBroker(host="127.0.0.1", port=4002, client_id=1)
        with pytest.raises(BrokerConnectionError, match="ibapi"):
            await broker.connect()

    async def test_place_order_refuses_transmit_false_before_touching_a_socket(
        self, contract
    ) -> None:
        from app.broker.models import OrderRequest
        from app.enums import OrderSide

        broker = IBKRBroker(host="127.0.0.1", port=4002, client_id=1)
        request = OrderRequest(
            internal_order_id="ord",
            correlation_id="cor",
            contract=contract,
            side=OrderSide.BUY,
            quantity=1,
            order_type=OrderType.MARKET,
            transmit=False,
        )
        # Refused for the transmit flag, not for being disconnected: the flag is
        # checked first, deliberately.
        with pytest.raises(BrokerOrderRejectedError, match="transmit=False"):
            await broker.place_order(request)


class TestUnattributedErrors:
    """Errors IBKR reports with reqId -1.

    Observed against a real gateway: with Read-Only API mode on, reqOpenOrders
    is refused with code 321 and reqId=-1. The adapter cannot know which pending
    request that belongs to, so the request waits out its full timeout -- and
    reported only "did not complete within 20.0s", which describes the symptom
    and hides an immediate, explicit refusal.
    """

    def _session(self):
        from app.broker.ibkr_broker import _IBSession

        return _IBSession.__new__(_IBSession)

    def _blank(self):
        import threading

        session = self._session()
        session._lock = threading.Lock()
        session._unattributed_error = None
        return session

    def test_nothing_recorded_means_nothing_reported(self) -> None:
        from app.utilities.timeutils import utc_now

        assert self._blank().unattributed_error_since(utc_now()) is None

    def test_an_error_during_the_wait_is_reported(self) -> None:
        from app.utilities.timeutils import utc_now

        session = self._blank()
        started = utc_now()
        session.record_unattributed_error(
            321,
            "Error validating request.-'cq' : cause - The API interface is "
            "currently in Read-Only mode.",
        )
        found = session.unattributed_error_since(started)
        assert found is not None
        assert "321" in found
        assert "Read-Only mode" in found

    def test_an_error_from_before_the_request_is_not_blamed_for_it(self) -> None:
        """A connect-time notice must not be reported as a later timeout's cause."""
        import time

        from app.utilities.timeutils import utc_now

        session = self._blank()
        session.record_unattributed_error(321, "stale error from connect time")
        time.sleep(0.01)
        started = utc_now()

        assert session.unattributed_error_since(started) is None

    def test_read_only_refusal_classifies_as_not_retryable(self) -> None:
        """321 under Read-Only mode must never be retried in a loop."""
        error_class = classify_ib_error(
            321,
            "Error validating request.-'cq' : cause - The API interface is "
            "currently in Read-Only mode.",
        )
        assert error_class is not None
        assert error_class.retryable is False


class TestStreamingSubscriptionErrors:
    """Errors aimed at a request that has no pending future.

    Market data is a streaming subscription: there is no single response to
    complete, so `_reject` finds nothing to fail and the refusal used to vanish.
    The caller then saw an empty tick -- identical to a quiet market, and a
    completely different problem.
    """

    def _session(self):
        import threading

        from app.broker.ibkr_broker import _IBSession

        session = _IBSession.__new__(_IBSession)
        session._lock = threading.Lock()
        session._pending = {}
        session._request_errors = {}
        return session

    def test_reject_reports_when_there_was_no_pending_request(self) -> None:
        assert self._session()._reject(4242, BrokerError("boom")) is False

    def test_reject_reports_when_it_did_fail_one(self) -> None:
        from concurrent.futures import Future

        from app.broker.ibkr_broker import _PendingRequest

        session = self._session()
        future: Future = Future()
        session._pending[4242] = _PendingRequest(future=future)

        assert session._reject(4242, BrokerError("boom")) is True
        assert isinstance(future.exception(), BrokerError)

    def test_a_recorded_error_is_retrievable_by_request_id(self) -> None:
        session = self._session()
        session.record_request_error(7, BrokerPermissionError("IBKR 354: not subscribed"))

        found = session.request_error(7)
        assert isinstance(found, BrokerPermissionError)
        assert "354" in str(found)
        assert session.request_error(8) is None


class _DelayedTicks(dict):
    """A ``ticks`` mapping that yields its value only after N consultations.

    Counting the consultations is the point: it distinguishes "polled until the
    tick arrived" from "slept once and happened to find it", which a plain
    time-based fake cannot.
    """

    def __init__(self, deliver_on_poll: int, value: dict[str, Decimal]) -> None:
        super().__init__()
        self._deliver_on_poll = deliver_on_poll
        self._value = value
        self.polls = 0

    def get(self, key, default=None):
        self.polls += 1
        if self.polls >= self._deliver_on_poll:
            self[key] = self._value
        return super().get(key, default)


class _FakeSession:
    """The narrow slice of `_IBSession` that `request_market_data` touches."""

    def __init__(self, ticks=None, error=None) -> None:
        self.ticks = {} if ticks is None else ticks
        self.delayed_requests: set[int] = set()
        self.subscribed: list[int] = []
        self._error = error

    def isConnected(self) -> bool:  # noqa: N802 -- ibapi's spelling
        return True

    def allocate_request_id(self) -> int:
        return 4242

    def reqMktData(self, request_id, *args) -> None:  # noqa: N802 -- ibapi's spelling
        self.subscribed.append(request_id)

    def request_error(self, request_id: int):
        return self._error


class TestFirstTickWait:
    """Waiting for the first tick of a streaming subscription.

    Streaming market data has no completion callback, so the first tick has to
    be waited for rather than requested. That wait used to be a fixed
    `await asyncio.sleep(0.5)`, which reported an empty tick whenever a thin
    contract took longer than half a second to quote -- observationally
    identical to a market where nobody is quoting, and a completely different
    problem.

    The defect stayed hidden because every earlier run against the real gateway
    was refused with 354 before the timing mattered at all. It surfaced the
    first minute the subscription actually worked.
    """

    @pytest.fixture(autouse=True)
    def _stub_ib_contract(self, monkeypatch):
        """`Contract` comes from ibapi, which is not a dev dependency.

        Stubbed unconditionally rather than only when the extra is absent:
        these tests are about the wait, not about contract translation, and
        they must not behave differently depending on which environment they
        happen to run in.
        """
        import app.broker.ibkr_broker as module

        class _Contract:
            pass

        monkeypatch.setattr(module, "Contract", _Contract)

    def _broker(self, session, **kwargs):
        broker = IBKRBroker(host="127.0.0.1", port=4002, client_id=1, **kwargs)
        broker._session = session
        broker._state = ConnectionState.CONNECTED
        return broker

    async def test_it_waits_past_the_half_second_that_used_to_be_the_whole_wait(
        self, contract
    ) -> None:
        # Delivered on the fourth poll: ~0.75s at the real interval, which the
        # old fixed sleep would have given up on long before.
        ticks = _DelayedTicks(4, {"bid": Decimal("180.25"), "ask": Decimal("180.30")})
        broker = self._broker(_FakeSession(ticks))

        tick = await broker.request_market_data(contract)

        assert ticks.polls >= 4
        assert tick.bid == Decimal("180.25")
        assert tick.ask == Decimal("180.30")

    async def test_it_returns_as_soon_as_the_tick_is_there(self, contract) -> None:
        """A wait that always ran to its deadline would be its own defect."""
        loop = asyncio.get_running_loop()
        session = _FakeSession(_DelayedTicks(1, {"last": Decimal("180.00")}))
        broker = self._broker(session, first_tick_timeout_seconds=30.0)

        started = loop.time()
        tick = await broker.request_market_data(contract)

        assert tick.last == Decimal("180.00")
        assert loop.time() - started < 5.0
        assert session.subscribed == [4242]

    async def test_a_refusal_ends_the_wait_instead_of_running_out_the_clock(self, contract) -> None:
        """354 is answerable in a second; making a human wait 30s for it is not."""
        loop = asyncio.get_running_loop()
        refusal = BrokerPermissionError("IBKR 354: requested market data is not subscribed")
        broker = self._broker(_FakeSession(error=refusal), first_tick_timeout_seconds=30.0)

        started = loop.time()
        with pytest.raises(BrokerPermissionError, match="354"):
            await broker.request_market_data(contract)

        assert loop.time() - started < 5.0

    async def test_an_empty_result_after_the_full_wait_is_a_quiet_market(self, contract) -> None:
        """Having waited properly, empty is a fact about the market, not an error."""
        broker = self._broker(_FakeSession(), first_tick_timeout_seconds=0.01)

        tick = await broker.request_market_data(contract)

        assert tick.bid is None
        assert tick.ask is None
        assert tick.last is None
        assert tick.is_delayed is False

    async def test_a_timeout_shorter_than_the_poll_is_not_rounded_up_to_it(self, contract) -> None:
        """The final sleep is clamped to what is left, so it cannot overshoot."""
        loop = asyncio.get_running_loop()
        broker = self._broker(_FakeSession(), first_tick_timeout_seconds=0.01)

        started = loop.time()
        await broker.request_market_data(contract)

        assert loop.time() - started < _FIRST_TICK_POLL_SECONDS

    async def test_the_subscription_is_only_created_once(self, contract) -> None:
        """A repeat call reads the running subscription; it does not re-wait."""
        session = _FakeSession(_DelayedTicks(1, {"last": Decimal("180.00")}))
        broker = self._broker(session)

        await broker.request_market_data(contract)
        await broker.request_market_data(contract)

        assert session.subscribed == [4242]
