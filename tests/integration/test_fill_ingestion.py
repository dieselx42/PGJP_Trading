"""Fills must move the position book, or reconciliation deadlocks.

Observed 2026-08-19 against a live paper account, immediately after the first
fill this system ever produced:

    position_discrepancies: [{con_id: 859040592, local_quantity: 0,
                              broker_quantity: 1, delta: 1}]
    fills_seen: 1

The broker held one contract. The book held none. Reconciliation failed and the
application went SAFE -- correctly, on the information it had.

Three things combined:

1. Nothing applied the fill to the book. `place-order` recorded the order's
   status and stopped.
2. `main._reconcile` adopted broker positions **only when reconciliation
   succeeded**, and it could not succeed while they disagreed. A discrepancy was
   therefore self-perpetuating: the system could open a position and then not
   close it, because opening created the state that blocked closing.
3. `fills_to_position_deltas` existed in `reconciliation.py` and had no callers.
   `reconcile` counted fills into `fills_seen` and drew no conclusion from them
   -- it saw the exact +1 that explained the exact +1 delta.

The distinction these tests protect is between a position that arrived through
a fill we watched, and a position that appeared from nowhere. The first is
explained and must clear. The second is not and must halt.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from app.broker.models import BrokerFill
from app.config import Config
from app.enums import ApplicationState, OrderSide
from app.main import TradingApplication
from app.utilities.timeutils import utc_now
from tests.conftest import permissive_env

pytestmark = pytest.mark.integration

CON_ID = 987654321


def _app(**overrides: str) -> TradingApplication:
    return TradingApplication(Config.from_env(permissive_env(**overrides)), is_admin_instance=True)


def _fill(execution_id: str, *, side: OrderSide = OrderSide.BUY, quantity: int = 1) -> BrokerFill:
    return BrokerFill(
        execution_id=execution_id,
        broker_order_id="1",
        account_id="DU111111",
        con_id=CON_ID,
        symbol="MSL",
        side=side,
        quantity=quantity,
        price=Decimal("82.50"),
        executed_at=utc_now() - timedelta(seconds=5),
    )


class TestFillsMoveTheBook:
    async def test_a_new_fill_is_applied(self) -> None:
        app = _app()
        await app.startup()
        try:
            applied = app._ingest_fills([_fill("exec-1")])
            assert len(applied) == 1
            assert app.position_book.quantity(CON_ID) == 1
        finally:
            await app.shutdown()

    @pytest.mark.safety
    async def test_the_same_fill_twice_does_not_double_count(self) -> None:
        """Reconnects re-read the same 24h execution window every time."""
        app = _app()
        await app.startup()
        try:
            app._ingest_fills([_fill("exec-1")])
            app._ingest_fills([_fill("exec-1")])
            assert app.position_book.quantity(CON_ID) == 1
        finally:
            await app.shutdown()

    @pytest.mark.safety
    async def test_a_fill_already_in_the_database_does_not_move_the_book(self) -> None:
        """A restart re-reads fills the book already reflects. It must not move."""
        app = _app()
        await app.startup()
        try:
            app._ingest_fills([_fill("exec-1")])
            # Simulate the restart: same DB, book re-seeded to where it ended.
            app.position_book.replace_all(app.position_book.all())
            before = app.position_book.quantity(CON_ID)
            applied = app._ingest_fills([_fill("exec-1")])
            assert applied == []
            assert app.position_book.quantity(CON_ID) == before
        finally:
            await app.shutdown()

    async def test_a_sell_reduces_the_position(self) -> None:
        app = _app()
        await app.startup()
        try:
            app._ingest_fills([_fill("exec-1", side=OrderSide.BUY)])
            app._ingest_fills([_fill("exec-2", side=OrderSide.SELL)])
            assert app.position_book.quantity(CON_ID) == 0
        finally:
            await app.shutdown()

    async def test_the_fill_is_recorded_durably(self) -> None:
        app = _app()
        await app.startup()
        try:
            app._ingest_fills([_fill("exec-1")])
            rows = app.database.query_all("SELECT execution_id FROM fills")
            assert [r["execution_id"] for r in rows] == ["exec-1"]
        finally:
            await app.shutdown()


class TestPeriodicReconciliation:
    """It used to run once per connection and never again."""

    def test_there_is_an_interval_and_it_is_not_zero_by_default(self) -> None:
        assert Config.from_env(permissive_env()).reconcile_interval_seconds > 0

    def test_it_can_be_disabled_deliberately(self) -> None:
        config = Config.from_env(permissive_env(RECONCILE_INTERVAL_SECONDS="0"))
        assert config.reconcile_interval_seconds == 0

    @pytest.mark.safety
    async def test_disabled_means_the_periodic_check_does_nothing(self) -> None:
        app = _app(RECONCILE_INTERVAL_SECONDS="0")
        await app.startup()
        try:
            await app._maybe_reconcile()
            assert app._next_reconcile_at is None
        finally:
            await app.shutdown()

    async def test_the_first_call_arms_the_timer_rather_than_reconciling(self) -> None:
        """Connect has just reconciled; doing it again immediately is noise."""
        app = _app()
        await app.startup()
        try:
            await app._maybe_reconcile()
            assert app._next_reconcile_at is not None
        finally:
            await app.shutdown()

    @pytest.mark.safety
    async def test_a_due_check_that_fails_drops_to_safe(self) -> None:
        app = _app()
        await app.startup()
        try:
            await app._connect_with_backoff()
            await app._after_connect()
            assert app.state is ApplicationState.READY

            # A position at the broker that no fill explains.
            app.position_book.apply_fill(
                con_id=CON_ID,
                symbol="MSL",
                local_symbol="MSLZ6",
                side=OrderSide.BUY,
                quantity=3,
                price=Decimal("80"),
            )
            app._next_reconcile_at = -1.0  # due
            await app._maybe_reconcile()

            assert not app.reconciliation.succeeded
            assert app.state is ApplicationState.SAFE
        finally:
            await app.shutdown()

    @pytest.mark.safety
    async def test_it_recovers_without_a_restart_once_the_books_agree(self) -> None:
        """A drop that needs a restart to clear is a drop nobody will trust."""
        app = _app()
        await app.startup()
        try:
            await app._connect_with_backoff()
            await app._after_connect()

            app.position_book.apply_fill(
                con_id=CON_ID,
                symbol="MSL",
                local_symbol="MSLZ6",
                side=OrderSide.BUY,
                quantity=3,
                price=Decimal("80"),
            )
            app._next_reconcile_at = -1.0
            await app._maybe_reconcile()
            assert app.state is ApplicationState.SAFE

            # The phantom goes away; the next due check must believe it.
            app.position_book.replace_all(())
            app._next_reconcile_at = -1.0
            await app._maybe_reconcile()

            assert app.reconciliation.succeeded
            assert app.state is ApplicationState.READY
        finally:
            await app.shutdown()


class TestTheDeadlockIsGone:
    """The property that matters, stated directly."""

    @pytest.mark.safety
    async def test_a_position_explained_by_a_fill_does_not_block_trading(self) -> None:
        """The exact shape of the live failure: fill arrives, book must follow."""
        app = _app()
        await app.startup()
        try:
            await app._connect_with_backoff()
            await app._after_connect()

            # A fill the broker knows about and the book does not -- which is
            # precisely what a filled order produced before this was wired up.
            applied = app._ingest_fills([_fill("exec-live")])
            assert len(applied) == 1
            assert app.position_book.quantity(CON_ID) == 1, (
                "the book must follow the fill; if it does not, reconciliation "
                "reports a discrepancy that nothing in the system can ever clear"
            )
        finally:
            await app.shutdown()
