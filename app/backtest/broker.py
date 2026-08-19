"""The simulated broker a replay fills against.

Fills are modelled **pessimistically and explicitly**, because a backtest's job
is to be disappointing in the same places reality will be.

Fill at the next bar's open, never the current bar's close
----------------------------------------------------------
A strategy decides on a bar it has already seen in full. If it then filled at
that bar's close, it would be trading on information it could not have had at
the moment of the decision. That is the single most common way a backtest
flatters itself, and it is usually invisible in the results -- the equity curve
just looks good.

So an order placed while bar *N* is being processed fills at the open of bar
*N+1*, or not at all.

Two modelling limits, stated rather than buried
-----------------------------------------------
**Bars are not ticks.** The intrabar high and low were reached at some unknown
moment, so a limit order cannot be filled against them without inventing the
order in which prices arrived. This broker only ever looks at the next open.
The consequence is that some limit orders that would have filled in reality do
not fill here.

**A bar has no spread.** Bid and ask are synthesised from the close plus and
minus half a configured spread. Real spreads widen exactly when a strategy most
wants to trade, and this model does not capture that.

Both make results optimistic relative to reality, which is why they are named
in every result rather than left in a docstring nobody reads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from app.backtest.models import Bar
from app.broker.models import OrderRequest, PlaceOrderResult
from app.enums import OrderSide, OrderStatus, OrderType
from app.logging_config import get_logger

_LOG = get_logger("backtest.broker")


@dataclass(frozen=True, slots=True)
class FillModel:
    """How pessimistic to be. Defaults are deliberately unkind."""

    #: Slippage in ticks, applied against the order every time. A buy pays
    #: more, a sell receives less; there is no configuration for it to help.
    slippage_ticks: int = 1

    #: Per contract, per side. Measured from a real IBKR whatIf preview on
    #: MSLQ6, 2026-08-19: initial margin $1,179.88, commission $3.41.
    commission_per_contract: Decimal = Decimal("3.41")

    #: Full synthetic spread, in ticks. Bid/ask are close -/+ half of this.
    spread_ticks: int = 2

    def describe(self) -> dict[str, object]:
        return {
            "slippage_ticks": self.slippage_ticks,
            "commission_per_contract": str(self.commission_per_contract),
            "spread_ticks": self.spread_ticks,
        }


@dataclass(frozen=True, slots=True)
class SimulatedFill:
    """One execution, with the cost of it broken out."""

    order_id: str
    side: OrderSide
    quantity: int
    price: Decimal
    filled_at: str
    commission: Decimal
    slippage_cost: Decimal
    reference_price: Decimal
    """The bar open before slippage, so the modelling cost is auditable."""

    def describe(self) -> dict[str, object]:
        return {
            "order_id": self.order_id,
            "side": self.side.value,
            "quantity": self.quantity,
            "price": str(self.price),
            "filled_at": self.filled_at,
            "commission": str(self.commission),
            "slippage_cost": str(self.slippage_cost),
            "reference_price": str(self.reference_price),
        }


@dataclass
class _Pending:
    request: OrderRequest
    order_id: str


@dataclass
class BacktestBroker:
    """Accepts orders during a bar; fills them against the next one.

    Deliberately **not** a :class:`~app.broker.base.Broker`. It implements only
    what the replay needs, so it cannot be handed to the trading runtime by
    accident, and it has no connect, no socket and no credentials.
    """

    min_tick: Decimal
    multiplier: Decimal
    fills: FillModel = field(default_factory=FillModel)

    _pending: list[_Pending] = field(default_factory=list, init=False)
    _next_id: int = field(default=0, init=False)
    executed: list[SimulatedFill] = field(default_factory=list, init=False)

    def place_order(self, request: OrderRequest) -> PlaceOrderResult:
        """Queue an order. Nothing fills within the bar that created it."""
        self._next_id += 1
        order_id = f"bt-{self._next_id}"
        self._pending.append(_Pending(request=request, order_id=order_id))
        return PlaceOrderResult(
            accepted=True, broker_order_id=order_id, status=OrderStatus.PENDING_SUBMIT
        )

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def settle(self, bar: Bar, *, tradeable: bool = True) -> list[SimulatedFill]:
        """Fill everything queued against ``bar``'s open. Returns the fills.

        ``tradeable`` is False outside the contract's trading hours. Orders are
        then **cancelled rather than carried**, because a resting order that
        survives a session break in a backtest but not in reality is another
        way to flatter the result.
        """
        if not self._pending:
            return []

        pending, self._pending = self._pending, []
        if not tradeable:
            _LOG.info(
                "cancelled orders that had no tradeable bar to fill against",
                extra={
                    "event": "backtest.cancelled",
                    "count": len(pending),
                    "at": bar.opened_at.isoformat(),
                },
            )
            return []

        settled: list[SimulatedFill] = []
        for item in pending:
            fill = self._fill(item, bar)
            if fill is not None:
                settled.append(fill)
                self.executed.append(fill)
        return settled

    def _fill(self, item: _Pending, bar: Bar) -> SimulatedFill | None:
        request = item.request
        reference = bar.open
        slip = self.min_tick * self.fills.slippage_ticks
        # Slippage always works against the order. There is no branch here that
        # helps a strategy, deliberately.
        price = reference + slip if request.side is OrderSide.BUY else reference - slip

        if request.order_type is OrderType.LIMIT and request.limit_price is not None:
            # Marketability is judged against the SAME price the fill would
            # take, so an order cannot be accepted at a price it would not have
            # received. Intrabar highs and lows are not consulted: see the
            # module docstring on why bars are not ticks.
            if request.side is OrderSide.BUY and price > request.limit_price:
                self._pending.append(item)  # still working; try the next bar
                return None
            if request.side is OrderSide.SELL and price < request.limit_price:
                self._pending.append(item)
                return None

        commission = self.fills.commission_per_contract * Decimal(request.quantity)
        return SimulatedFill(
            order_id=item.order_id,
            side=request.side,
            quantity=request.quantity,
            price=price,
            filled_at=bar.opened_at.isoformat(),
            commission=commission,
            slippage_cost=slip * Decimal(request.quantity) * self.multiplier,
            reference_price=reference,
        )

    def cancel_all(self) -> int:
        """Drop every working order. Used at the end of a replay."""
        count = len(self._pending)
        self._pending.clear()
        return count


__all__ = ["BacktestBroker", "FillModel", "SimulatedFill"]
