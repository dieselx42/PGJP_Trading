"""Reconciliation must see every order at the broker, not just its own.

Found 2026-08-19 against a live paper account. `place-order` placed a real
resting order on the admin client id; the trading process, on its own client
id, reconciled and reported:

    "kind": "unknown_at_broker",
    "detail": "local order is marked working but the broker does not have it"

and dropped to SAFE. The order was fine. `reqOpenOrders` returns only the
*calling client's* orders, so the trading process could not see one this system
had placed moments earlier.

The false alarm is the harmless direction. The same root cause produces a false
**clear**: an order placed by a human in TWS, another process, or a stale client
id is equally invisible, reconciliation reports success, and the system trades
alongside exposure it does not know about. That is the exact failure
reconciliation exists to prevent, and it could never have caught it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.portfolio.reconciliation import Reconciler

SOURCE = Path("app/broker/ibkr_broker.py")


class TestTheAdapterAsksForAllOrders:
    """Structural. The distinction is one API call and it is invisible at runtime."""

    def _get_open_orders(self) -> ast.AsyncFunctionDef:
        tree = ast.parse(SOURCE.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "get_open_orders":
                return node
        raise AssertionError("get_open_orders is gone")

    def _calls(self, fn: ast.AST) -> set[str]:
        return {
            node.func.attr
            for node in ast.walk(fn)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }

    @pytest.mark.safety
    def test_it_requests_orders_from_every_client(self) -> None:
        assert "reqAllOpenOrders" in self._calls(self._get_open_orders())

    @pytest.mark.safety
    def test_it_does_not_use_the_per_client_call(self) -> None:
        """`reqOpenOrders` is the bug. Reconciliation cannot be built on it."""
        assert "reqOpenOrders" not in self._calls(self._get_open_orders())


class TestTheDiscrepancyDirectionsBothMatter:
    """Reconciler behaviour given what the broker reports.

    These drive the pure comparison, which was never wrong -- it faithfully
    reported what it was given. The defect was upstream, in what the adapter
    asked the broker for. Both directions are pinned so a future change to
    either side has to confront them.
    """

    def _reconcile_orders(self, local, broker_ids):
        from app.broker.models import BrokerOrderSnapshot
        from app.enums import OrderSide, OrderStatus, OrderType

        snapshots = [
            BrokerOrderSnapshot(
                broker_order_id=oid,
                account_id="DU111111",
                con_id=1,
                symbol="MSL",
                side=OrderSide.BUY,
                quantity=1,
                order_type=OrderType.LIMIT,
                status=OrderStatus.ACKNOWLEDGED,
            )
            for oid in broker_ids
        ]
        return Reconciler._compare_orders(broker_open_orders=snapshots, local_open_orders=local)

    @pytest.mark.safety
    def test_an_order_the_broker_does_not_have_is_a_discrepancy(self) -> None:
        """The false alarm we hit -- correct behaviour on incorrect input."""
        found = self._reconcile_orders([("ord_1", "3", "MSL")], [])
        assert len(found) == 1
        assert found[0].kind == "unknown_at_broker"

    @pytest.mark.safety
    def test_an_order_we_do_not_know_about_is_a_discrepancy(self) -> None:
        """The dangerous direction: exposure this system did not create.

        Only reachable at all once the adapter asks for every client's orders.
        With `reqOpenOrders` a third party's order simply never appeared, and
        this check could not fire however correct it was.
        """
        found = self._reconcile_orders([], ["99"])
        assert len(found) == 1
        assert found[0].kind == "unknown_locally"

    def test_a_matching_book_is_clean(self) -> None:
        assert self._reconcile_orders([("ord_1", "3", "MSL")], ["3"]) == ()

    def test_a_local_order_with_no_broker_id_is_a_discrepancy(self) -> None:
        """Transmitted-but-unacknowledged is unknown, and unknown is not fine."""
        found = self._reconcile_orders([("ord_1", None, "MSL")], [])
        assert len(found) == 1
        assert found[0].kind == "unknown_at_broker"


class TestReconciliationGatesTheApplication:
    """A discrepancy must stop the system, not just be reported."""

    @pytest.mark.safety
    def test_a_failed_reconciliation_keeps_the_app_out_of_ready(self) -> None:
        from app.config import Config
        from app.enums import ApplicationState
        from app.main import TradingApplication
        from app.portfolio.reconciliation import ReconciliationResult
        from tests.conftest import permissive_env

        app = TradingApplication(Config.from_env(permissive_env()), is_admin_instance=True)
        app.reconciliation = ReconciliationResult(
            positions_reconciled=True,
            orders_reconciled=False,
            account_available=True,
        )
        assert app._compute_ready_state() is ApplicationState.SAFE


class TestOrderStatusIsRecordedNotDiscarded:
    """`orderStatus` used to log and return.

    `_IBSession.order_statuses` was declared and never written to, so the
    adapter received every status update IBKR sent and threw it away. Nothing
    downstream could learn whether an order was accepted, rejected, or
    cancelled -- an order counted as "submitted" because bytes had reached a
    socket.

    Observed 2026-08-19: an order was accepted (permId 2106979881) and then
    cancelled seconds later when the placing client disconnected. Both events
    were reported by IBKR. Neither was recorded, and finding out took an hour
    of manual probing.
    """

    def _order_status_callback(self) -> ast.FunctionDef:
        tree = ast.parse(SOURCE.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "orderStatus":
                return node
        raise AssertionError("orderStatus is gone")

    @pytest.mark.safety
    def test_the_callback_writes_the_status_somewhere(self) -> None:
        """Logging is not recording. A log line cannot be waited on."""
        body = ast.dump(self._order_status_callback())
        assert "order_statuses" in body, "orderStatus does not record what it was told"

    def test_an_unmapped_ibkr_status_is_an_error_not_a_guess(self) -> None:
        """An unrecognised status must not resolve to something benign."""
        source = SOURCE.read_text()
        assert "IB_STATUS_MAP.get(status, OrderStatus.ERROR)" in source

    def test_the_adapter_exposes_a_way_to_wait_for_one(self) -> None:
        from app.broker.ibkr_broker import IBKRBroker

        assert hasattr(IBKRBroker, "await_order_status")


class TestStatusMeaning:
    def _update(self, status):
        from app.broker.models import OrderStatusUpdate

        return OrderStatusUpdate(broker_order_id="3", status=status, ib_status=status.value)

    def test_cancelled_is_terminal_and_not_working(self) -> None:
        from app.enums import OrderStatus

        update = self._update(OrderStatus.CANCELLED)
        assert update.is_terminal
        assert not update.is_working

    def test_rejected_is_terminal_and_not_working(self) -> None:
        from app.enums import OrderStatus

        update = self._update(OrderStatus.REJECTED)
        assert update.is_terminal
        assert not update.is_working

    def test_acknowledged_is_working(self) -> None:
        from app.enums import OrderStatus

        assert self._update(OrderStatus.ACKNOWLEDGED).is_working


class TestTheOperatorReportTellsTheTruth:
    """A cancelled order must not read as a success."""

    @pytest.mark.safety
    def test_a_cancelled_order_says_so_plainly(self) -> None:
        from app.enums import OrderStatus
        from app.execution.operator_order import _status_note

        note = _status_note(self._update(OrderStatus.CANCELLED))
        assert "CANCELLED" in note

    @pytest.mark.safety
    def test_no_status_is_undetermined_not_failure(self) -> None:
        """Silence from the broker says nothing. Claiming it does is the bug."""
        from app.execution.operator_order import _status_note

        note = _status_note(None)
        assert "UNDETERMINED" in note
        assert "may well" in note

    def test_a_working_order_says_the_broker_holds_it(self) -> None:
        from app.enums import OrderStatus
        from app.execution.operator_order import _status_note

        assert "holding" in _status_note(self._update(OrderStatus.ACKNOWLEDGED))

    def _update(self, status):
        from app.broker.models import OrderStatusUpdate

        return OrderStatusUpdate(broker_order_id="3", status=status, ib_status=status.value)
