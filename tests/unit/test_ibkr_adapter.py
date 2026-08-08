"""IBKR adapter -- the parts that can be tested without a gateway.

This file covers the pure decision logic: error classification, account-type
determination, order-type mapping, and the error-signature parser. The socket
paths cannot be exercised here (see the module docstring in
``app/broker/ibkr_broker.py``); the read-only checkout in ``RUNBOOK.md`` is
what validates those, and it has not been possible to run it while futures
permission is pending.
"""

from __future__ import annotations

import pytest

from app.broker.ibkr_broker import (
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
from app.enums import AccountType, OrderType


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
