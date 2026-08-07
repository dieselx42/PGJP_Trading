"""MockBroker behaviour and determinism."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.broker.mock_broker import MockBroker
from app.broker.models import (
    BrokerConnectionError,
    BrokerContractError,
    BrokerOrderRejectedError,
    BrokerPermissionError,
    BrokerPosition,
    OrderRequest,
)
from app.contracts.models import ContractSpec
from app.enums import AccountType, ConnectionState, OrderSide, OrderType, SecurityType


async def test_connect_reports_a_simulated_account() -> None:
    broker = MockBroker()
    info = await broker.connect()
    assert info.connected
    assert info.account_type is AccountType.SIMULATED
    assert broker.get_connection_state() is ConnectionState.CONNECTED


async def test_a_disconnected_mock_reports_unknown_account() -> None:
    broker = MockBroker()
    assert broker.get_connection_info().account_type is AccountType.UNKNOWN
    await broker.connect()
    await broker.disconnect()
    assert broker.get_connection_info().account_type is AccountType.UNKNOWN


async def test_calls_before_connect_raise(contract) -> None:
    broker = MockBroker()
    with pytest.raises(BrokerConnectionError):
        await broker.get_account_summary()
    with pytest.raises(BrokerConnectionError):
        await broker.get_positions()


async def test_fail_connect_injection() -> None:
    broker = MockBroker(fail_connect=True)
    with pytest.raises(BrokerConnectionError):
        await broker.connect()
    assert broker.get_connection_state() is ConnectionState.FAILED


async def test_qualification_requires_an_explicit_contract_month() -> None:
    broker = MockBroker()
    await broker.connect()
    with pytest.raises(BrokerContractError, match="explicit contract month"):
        await broker.qualify_contract(ContractSpec(symbol="MSL"))


async def test_continuous_futures_are_never_qualified() -> None:
    broker = MockBroker()
    await broker.connect()
    spec = ContractSpec(symbol="MSL", sec_type=SecurityType.CONTINUOUS_FUTURE)
    with pytest.raises(BrokerContractError, match="analytics-only"):
        await broker.get_contract_details(spec)


async def test_unknown_symbol_is_refused_not_substituted() -> None:
    broker = MockBroker()
    await broker.connect()
    spec = ContractSpec(symbol="ZZZ", last_trade_date_or_contract_month="202612")
    with pytest.raises(BrokerContractError):
        await broker.qualify_contract(spec)


async def test_contract_ids_are_stable_across_broker_instances() -> None:
    spec = ContractSpec(symbol="MSL", last_trade_date_or_contract_month="202612")
    first, second = MockBroker(seed=1), MockBroker(seed=999)
    await first.connect()
    await second.connect()
    a = await first.qualify_contract(spec)
    b = await second.qualify_contract(spec)
    assert a.con_id == b.con_id, "conId must not depend on the RNG seed"
    assert a.con_id > 0


async def test_market_data_is_deterministic_for_a_given_seed(contract) -> None:
    first, second = MockBroker(seed=7), MockBroker(seed=7)
    await first.connect()
    await second.connect()
    a = await first.request_market_data(contract)
    b = await second.request_market_data(contract)
    assert a.bid == b.bid
    assert a.ask == b.ask
    assert a.last == b.last


async def test_market_data_differs_between_seeds(contract) -> None:
    first, second = MockBroker(seed=1), MockBroker(seed=2)
    await first.connect()
    await second.connect()
    a = await first.request_market_data(contract)
    b = await second.request_market_data(contract)
    assert (a.bid, a.ask) != (b.bid, b.ask)


async def test_ticks_carry_a_bid_ask_spread(contract) -> None:
    broker = MockBroker()
    await broker.connect()
    tick = await broker.request_market_data(contract)
    assert tick.bid is not None and tick.ask is not None
    assert tick.ask > tick.bid
    assert tick.mid is not None


async def test_permission_denied_injection_blocks_data_and_orders(contract) -> None:
    broker = MockBroker(permission_denied=True)
    await broker.connect()
    with pytest.raises(BrokerPermissionError):
        await broker.request_market_data(contract)
    with pytest.raises(BrokerPermissionError):
        await broker.get_contract_details(contract.to_spec())


async def test_market_order_fills_and_moves_the_position(contract) -> None:
    broker = MockBroker()
    await broker.connect()
    result = await broker.place_order(_request(contract, OrderSide.BUY, 2))
    assert result.accepted
    fills = await broker.get_fills()
    assert len(fills) == 1
    assert fills[0].quantity == 2
    positions = await broker.get_positions()
    assert len(positions) == 1
    assert positions[0].quantity == 2


async def test_closing_trade_removes_the_position(contract) -> None:
    broker = MockBroker()
    await broker.connect()
    await broker.place_order(_request(contract, OrderSide.BUY, 1))
    await broker.place_order(_request(contract, OrderSide.SELL, 1))
    assert await broker.get_positions() == ()


async def test_limit_orders_rest_and_can_be_cancelled(contract) -> None:
    broker = MockBroker()
    await broker.connect()
    result = await broker.place_order(
        _request(contract, OrderSide.BUY, 1, OrderType.LIMIT, Decimal("100"))
    )
    assert len(await broker.get_open_orders()) == 1
    assert await broker.cancel_order(result.broker_order_id or "")
    assert await broker.get_open_orders() == ()


async def test_cancel_all_orders_leaves_positions_untouched(contract) -> None:
    broker = MockBroker()
    await broker.connect()
    await broker.place_order(_request(contract, OrderSide.BUY, 1))  # fills -> position
    await broker.place_order(_request(contract, OrderSide.BUY, 1, OrderType.LIMIT, Decimal("50")))
    positions_before = await broker.get_positions()

    cancelled = await broker.cancel_all_orders()

    assert cancelled == 1
    assert await broker.get_open_orders() == ()
    assert await broker.get_positions() == positions_before, "cancel must not flatten"


async def test_unsupported_order_types_are_refused(contract) -> None:
    broker = MockBroker()
    await broker.connect()
    with pytest.raises(BrokerOrderRejectedError, match="does not simulate"):
        await broker.place_order(_request(contract, OrderSide.BUY, 1, OrderType.STOP, None))


async def test_force_disconnect_simulates_a_dropped_session(contract) -> None:
    broker = MockBroker()
    await broker.connect()
    broker.force_disconnect()
    assert not broker.is_connected()
    with pytest.raises(BrokerConnectionError):
        await broker.get_positions()


async def test_seeded_positions_are_reported() -> None:
    position = BrokerPosition(
        account_id="DUMOCK0001",
        con_id=123,
        symbol="MSL",
        local_symbol="MSLZ6",
        sec_type="FUT",
        exchange="CME",
        currency="USD",
        quantity=3,
        average_cost=Decimal("3750"),
    )
    broker = MockBroker(initial_positions=[position])
    await broker.connect()
    assert (await broker.get_positions())[0].quantity == 3


async def test_account_type_override_enables_mismatch_testing() -> None:
    broker = MockBroker(account_type_override=AccountType.LIVE)
    await broker.connect()
    assert broker.get_connection_info().account_type is AccountType.LIVE


def _request(
    contract,
    side: OrderSide,
    quantity: int,
    order_type: OrderType = OrderType.MARKET,
    limit_price: Decimal | None = None,
) -> OrderRequest:
    return OrderRequest(
        internal_order_id="ord_test",
        correlation_id="cor_test",
        contract=contract,
        side=side,
        quantity=quantity,
        order_type=order_type,
        limit_price=limit_price,
        transmit=True,
    )
