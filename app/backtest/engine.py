"""The replay.

What makes this worth having rather than a spreadsheet: **it runs the real
interlocks.** Every intent goes through the actual
:class:`~app.signals.validator.SignalValidator`, the actual
:class:`~app.risk.manager.RiskManager` and the actual
:class:`~app.safety.gate.TransmitGate` -- the same objects the live system
constructs, evaluated over contexts built here.

That has a consequence worth stating plainly rather than treating as a bug: **if
the gate refuses an order during a replay, the replay is correct to record no
trade.** A strategy whose backtested results depend on ignoring the risk limits
will not perform that way live, and finding that out here is the point. Every
refusal is counted and reported by reason, which is the one thing this can offer
that a generic backtester cannot.

Which refusals a replay can actually produce
--------------------------------------------
Worth stating so nobody reads "runs the real gate" as more than it is. A replay
has no broker, so the conditions a live system *observes* -- connection state,
account availability, reconciliation, market-data staleness -- are **asserted**
here rather than measured, and cannot fail. What remains genuinely live:

* every configured risk limit, evaluated against the replay's own book;
* both approvers' configuration interlocks -- kill switch, transmit permission,
  trading mode, futures permission, market-data-age *configuration*;
* the validator, in full;
* the contract check: a continuous future can never carry an order, here
  either.

Both approvers are evaluated on every intent, not just until one refuses. The
risk manager and the gate check the configuration interlocks independently, so
short-circuiting on risk would leave the gate unable to fire in a replay at
all -- an interlock that cannot fail in a backtest is not being tested by it.
A refusal therefore names every party that objected (``risk+gate``) and lists
what each of them found.

Bars where nothing traded
-------------------------
A thin venue emits a placeholder bar for every interval with no activity: OHLC
all equal to the last print, volume zero. Binance.US SOLUSD does this for long
stretches. Those are not prices anyone could have traded at, so a bar with zero
volume is **not tradeable** here -- treated exactly like a session break, with
pending orders cancelled rather than filled against a price that never existed.

The check applies only when the source reports volume at all. A CSV with no
volume column would otherwise have every bar refused, which is the same mistake
in the opposite direction. The question is asked once, of the whole series, and
the answer is reported in the result.

What is NOT exercised
---------------------
`OrderManager`'s persistence and idempotency layer. Idempotency exists to
survive a crash mid-flight between our database and the broker; a replay is
deterministic, single-pass and has no broker to be out of step with. Wiring a
real `OrderManager` here would mean writing hundreds of thousands of rows to
prove a property the replay cannot exhibit.

It cannot reach a broker
------------------------
Nothing in this module constructs `IBKRBroker`, and the `Config` it uses is
built from an explicit mapping -- never from the process environment, so a
server `.env` cannot leak into a backtest and a backtest cannot pick up live
credentials. Both are asserted by tests.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from app.backtest.broker import BacktestBroker, FillModel, SimulatedFill
from app.backtest.models import Bar, reports_volume
from app.config import Config
from app.contracts.models import QualifiedContract
from app.enums import AccountType, ApplicationState, ConnectionState, OrderType
from app.execution.order_manager import OrderManager
from app.logging_config import get_logger
from app.market_data.models import Quote
from app.risk.manager import RiskContext, RiskManager
from app.safety.gate import REASON_RISK_NOT_APPROVED, GateContext, TransmitGate
from app.signals.models import TradeIntent
from app.signals.validator import SignalValidator
from app.strategy.base import BarStrategy, Strategy

_LOG = get_logger("backtest.engine")

#: Bars whose open falls outside this UTC window are not traded when a session
#: filter is requested. 13:30-21:00 UTC is 08:30-16:00 US/Central, which is what
#: IBKR reports as MSL's liquid hours.
DEFAULT_SESSION_START_MINUTE = 13 * 60 + 30
DEFAULT_SESSION_END_MINUTE = 21 * 60


def cme_liquid_hours(moment: datetime) -> bool:
    """Weekday, inside MSL's reported liquid hours."""
    if moment.weekday() >= 5:  # Saturday, Sunday
        return False
    minute_of_day = moment.hour * 60 + moment.minute
    return DEFAULT_SESSION_START_MINUTE <= minute_of_day < DEFAULT_SESSION_END_MINUTE


def always_tradeable(moment: datetime) -> bool:
    """No session filter. Honest for spot data, wrong for futures."""
    del moment
    return True


def backtest_config(
    *,
    symbol: str,
    max_position_contracts: int,
    max_order_size: int,
    max_daily_loss_usd: str,
    max_orders_per_hour: int,
    max_open_orders: int,
    max_notional_exposure_usd: str,
    market_data_max_age_seconds: str = "60",
    overrides: Mapping[str, str] | None = None,
) -> Config:
    """A `Config` from explicit values, never from the environment.

    `Config.from_env()` with no argument reads `os.environ`. This always passes
    a mapping, so a server `.env` cannot reach a backtest and a backtest cannot
    inherit live credentials or a live trading mode. Asserted by test.

    ``overrides`` exists so a replay can be run under a *degraded*
    configuration -- "what would this strategy have done with the kill switch
    engaged" is a legitimate check, and the answer should be "nothing". It can
    only degrade: the two values below are reapplied afterwards, so no override
    can make a backtest claim live trading is enabled or reach a real database.
    """
    env = {
        "APP_ENV": "backtest",
        "TRADING_MODE": "mock",
        "ALLOW_ORDER_TRANSMIT": "true",
        "KILL_SWITCH": "false",
        "SOL_FUTURES_PERMISSION_READY": "true",
        "DEFAULT_FUTURES_SYMBOL": symbol,
        "MAX_POSITION_CONTRACTS": str(max_position_contracts),
        "MAX_ORDER_SIZE": str(max_order_size),
        "MAX_DAILY_LOSS_USD": max_daily_loss_usd,
        "MAX_ORDERS_PER_HOUR": str(max_orders_per_hour),
        "MAX_OPEN_ORDERS": str(max_open_orders),
        "MAX_NOTIONAL_EXPOSURE_USD": max_notional_exposure_usd,
        "MARKET_DATA_MAX_AGE_SECONDS": market_data_max_age_seconds,
        "LOG_LEVEL": "CRITICAL",
        "HEALTH_PORT": "0",
    }
    env.update(overrides or {})
    # Applied last, so they cannot be overridden. Order matters here.
    env["LIVE_TRADING_ENABLED"] = "false"
    env["DATABASE_PATH"] = ":memory:"
    return Config.from_env(env)


@dataclass(frozen=True, slots=True)
class Refusal:
    """An intent that produced no order, and why."""

    at: str
    stage: str
    """Who refused: ``validator``, ``no_change``, ``risk``, ``gate``, ``risk+gate``.

    ``risk+gate`` is the common case for a configuration interlock, and is the
    point of having two approvers: the kill switch is checked independently by
    both, so both name it. A stage of ``risk`` alone means the gate had no
    objection of its own beyond risk's verdict.
    """

    reasons: tuple[str, ...]
    requested_position: int
    current_position: int

    def describe(self) -> dict[str, object]:
        return {
            "at": self.at,
            "stage": self.stage,
            "reasons": list(self.reasons),
            "requested_position": self.requested_position,
            "current_position": self.current_position,
        }


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    """A round trip, realised."""

    opened_at: str
    closed_at: str
    side: str
    quantity: int
    entry_price: Decimal
    exit_price: Decimal
    gross_pnl: Decimal
    commission: Decimal
    """**Both** sides of the round trip, pro-rated for a partial close.

    Counting only the exit would make every trade look one commission cheaper
    than it was, and would leave the trade list disagreeing with the headline
    ``net_pnl``, which is computed from total commission paid. When a replay
    ends flat, the trades' commissions sum to exactly that total.
    """

    net_pnl: Decimal

    def describe(self) -> dict[str, object]:
        return {
            "opened_at": self.opened_at,
            "closed_at": self.closed_at,
            "side": self.side,
            "quantity": self.quantity,
            "entry_price": str(self.entry_price),
            "exit_price": str(self.exit_price),
            "gross_pnl": str(self.gross_pnl),
            "commission": str(self.commission),
            "net_pnl": str(self.net_pnl),
        }


@dataclass
class _Book:
    """Position, cost basis and realised P&L across a replay."""

    multiplier: Decimal
    quantity: int = 0
    average_cost: Decimal = Decimal(0)
    realized_pnl: Decimal = Decimal(0)
    commission_paid: Decimal = Decimal(0)
    slippage_paid: Decimal = Decimal(0)
    opened_at: str | None = None
    open_commission: Decimal = Decimal(0)
    """Commission already paid on the position currently held.

    Carried so a closed trade can report what the round trip actually cost
    rather than only what leaving it cost.
    """

    trades: list[ClosedTrade] = field(default_factory=list)

    def apply(self, fill: SimulatedFill) -> None:
        self.commission_paid += fill.commission
        self.slippage_paid += fill.slippage_cost
        delta = fill.quantity * fill.side.sign
        previous = self.quantity
        new_quantity = previous + delta

        if previous == 0:
            self.average_cost = fill.price
            self.opened_at = fill.filled_at
            self.open_commission = fill.commission
        elif (previous > 0) == (delta > 0):
            total = abs(previous) + abs(delta)
            self.average_cost = (
                self.average_cost * Decimal(abs(previous)) + fill.price * Decimal(abs(delta))
            ) / Decimal(total)
            self.open_commission += fill.commission
        else:
            closed = min(abs(previous), abs(delta))
            direction = Decimal(1) if previous > 0 else Decimal(-1)
            gross = (fill.price - self.average_cost) * direction * Decimal(closed) * self.multiplier
            self.realized_pnl += gross
            # Both legs, pro-rated: the share of the entry commission belonging
            # to the contracts being closed, plus the share of this fill's
            # commission spent closing them rather than opening a new position.
            entry_share = self.open_commission * Decimal(closed) / Decimal(abs(previous))
            exit_share = fill.commission * Decimal(closed) / Decimal(abs(delta))
            round_trip = entry_share + exit_share
            self.trades.append(
                ClosedTrade(
                    opened_at=self.opened_at or fill.filled_at,
                    closed_at=fill.filled_at,
                    side="LONG" if previous > 0 else "SHORT",
                    quantity=closed,
                    entry_price=self.average_cost,
                    exit_price=fill.price,
                    gross_pnl=gross,
                    commission=round_trip,
                    net_pnl=gross - round_trip,
                )
            )
            if new_quantity != 0 and (new_quantity > 0) != (previous > 0):
                # Flipped through flat: the leftover of this fill opened the
                # new, opposite position and is its entry cost.
                self.average_cost = fill.price
                self.opened_at = fill.filled_at
                self.open_commission = fill.commission - exit_share
            else:
                self.open_commission -= entry_share

        self.quantity = new_quantity
        if new_quantity == 0:
            self.average_cost = Decimal(0)
            self.opened_at = None
            self.open_commission = Decimal(0)

    def unrealized(self, price: Decimal) -> Decimal:
        if self.quantity == 0:
            return Decimal(0)
        return (price - self.average_cost) * Decimal(self.quantity) * self.multiplier

    def equity(self, price: Decimal) -> Decimal:
        return self.realized_pnl + self.unrealized(price) - self.commission_paid


@dataclass(frozen=True, slots=True)
class BacktestRun:
    """Everything a replay produced. Metrics are computed from this."""

    symbol: str
    source: str
    interval: str
    is_proxy_data: bool
    bars_seen: int
    bars_tradeable: int
    bars_without_trades: int
    """Bars whose volume was zero, on a source that does report volume.

    A thin venue emits one of these for every interval in which nothing
    changed hands. They are not tradeable prices.
    """

    volume_reported: bool
    first_bar: str | None
    last_bar: str | None
    fills: tuple[SimulatedFill, ...]
    trades: tuple[ClosedTrade, ...]
    refusals: tuple[Refusal, ...]
    equity_curve: tuple[tuple[str, Decimal], ...]
    final_position: int
    realized_pnl: Decimal
    commission_paid: Decimal
    slippage_paid: Decimal
    fill_model: FillModel
    session_filtered: bool


class BacktestEngine:
    """Drives bars through the real pipeline."""

    def __init__(
        self,
        *,
        config: Config,
        contract: QualifiedContract,
        strategy: Strategy,
        fill_model: FillModel | None = None,
        session: Callable[[datetime], bool] = always_tradeable,
        synthetic_spread_ticks: int | None = None,
    ) -> None:
        self.config = config
        self.contract = contract
        self.strategy = strategy
        self.fill_model = fill_model or FillModel()
        self.session = session
        self.spread_ticks = (
            self.fill_model.spread_ticks
            if synthetic_spread_ticks is None
            else synthetic_spread_ticks
        )
        # The real ones. Not copies, not simplified versions.
        self.risk_manager = RiskManager(config)
        self.gate = TransmitGate(config)
        self.validator = SignalValidator(
            configured_symbol=config.default_futures_symbol,
            known_strategies=[strategy.name],
            # A replayed bar from a year ago is "now" on the virtual clock. The
            # staleness check exists for a live feed and would reject every
            # historical intent for a reason that says nothing about the
            # strategy.
            max_signal_age_seconds=float("inf"),
        )
        self.broker = BacktestBroker(
            min_tick=contract.min_tick or Decimal("0.01"),
            multiplier=Decimal(contract.multiplier or "1"),
            fills=self.fill_model,
        )

    def run(self, bars: Sequence[Bar]) -> BacktestRun:
        book = _Book(multiplier=Decimal(self.contract.multiplier or "1"))
        refusals: list[Refusal] = []
        curve: list[tuple[str, Decimal]] = []
        tradeable_bars = 0
        empty_bars = 0
        orders_this_hour: list[datetime] = []

        # Asked once, of the whole series. A source that never reports volume
        # cannot have its bars judged by it; one that does can, and a zero
        # there means nothing traded that minute.
        volume_reported = reports_volume(bars)

        for bar in bars:
            has_liquidity = bar.had_trades or not volume_reported
            if not has_liquidity:
                empty_bars += 1
            tradeable = self.session(bar.opened_at) and has_liquidity

            # Settle first: an order placed while the PREVIOUS bar was being
            # processed fills at THIS bar's open. Never at the close of the bar
            # that produced it -- that would be trading on a price the strategy
            # had already seen.
            for fill in self.broker.settle(bar, tradeable=tradeable):
                book.apply(fill)
                # The strategy learns what its intent actually cost BEFORE it
                # sees this bar, exactly as live: the fill happened at this
                # bar's open, the bar completes after it. A strategy that sets
                # stops from its entry price needs the real one.
                self.strategy.on_fill(side=fill.side, quantity=fill.quantity, price=fill.price)

            curve.append((bar.opened_at.isoformat(), book.equity(bar.close)))
            if not tradeable:
                continue
            tradeable_bars += 1

            # A bar strategy receives the bar itself: an opening range is a
            # candle's high and low, which no stream of closes can carry.
            intents = (
                self.strategy.handle_bar(bar)
                if isinstance(self.strategy, BarStrategy)
                else self.strategy.handle_quote(self._quote(bar))
            )
            for intent in intents:
                refusal = self._handle(intent, bar=bar, book=book, orders=orders_this_hour)
                if refusal is not None:
                    refusals.append(refusal)

        cancelled = self.broker.cancel_all()
        if cancelled:
            _LOG.info(
                "replay ended with working orders; they are cancelled, not filled",
                extra={"event": "backtest.ended_with_working_orders", "count": cancelled},
            )

        return BacktestRun(
            symbol=self.contract.symbol,
            source=bars[0].source if bars else "none",
            interval=bars[0].interval if bars else "none",
            is_proxy_data=bool(bars) and bars[0].is_proxy,
            bars_seen=len(bars),
            bars_tradeable=tradeable_bars,
            bars_without_trades=empty_bars,
            volume_reported=volume_reported,
            first_bar=bars[0].opened_at.isoformat() if bars else None,
            last_bar=bars[-1].opened_at.isoformat() if bars else None,
            fills=tuple(self.broker.executed),
            trades=tuple(book.trades),
            refusals=tuple(refusals),
            equity_curve=tuple(curve),
            final_position=book.quantity,
            realized_pnl=book.realized_pnl,
            commission_paid=book.commission_paid,
            slippage_paid=book.slippage_paid,
            fill_model=self.fill_model,
            session_filtered=self.session is not always_tradeable,
        )

    def _quote(self, bar: Bar) -> Quote:
        """A bar has no spread, so one is synthesised around the close.

        Real spreads widen exactly when a strategy most wants to trade. This
        does not model that, which makes results optimistic.
        """
        half = (self.contract.min_tick or Decimal("0.01")) * Decimal(self.spread_ticks) / 2
        return Quote(
            contract_key=self.contract.key(),
            symbol=self.contract.symbol,
            received_at=bar.opened_at,
            source=f"backtest:{bar.source}",
            bid=bar.close - half,
            ask=bar.close + half,
            last=bar.close,
            is_delayed=False,
        )

    def _handle(
        self,
        intent: TradeIntent,
        *,
        bar: Bar,
        book: _Book,
        orders: list[datetime],
    ) -> Refusal | None:
        at = bar.opened_at.isoformat()

        validation = self.validator.validate(intent)
        if not validation.accepted:
            return Refusal(
                at=at,
                stage="validator",
                reasons=validation.reasons,
                requested_position=intent.requested_position,
                current_position=book.quantity,
            )

        side, quantity = OrderManager.delta_to_side_and_quantity(
            current_position=book.quantity, requested_position=intent.requested_position
        )
        if quantity == 0:
            return Refusal(
                at=at,
                stage="no_change",
                reasons=("ALREADY_AT_TARGET",),
                requested_position=intent.requested_position,
                current_position=book.quantity,
            )

        recent = [t for t in orders if (bar.opened_at - t).total_seconds() < 3600]
        orders[:] = recent

        risk_context = RiskContext(
            intent=intent,
            order_side=side,
            order_quantity=quantity,
            current_position=book.quantity,
            contract=self.contract,
            reference_price=bar.close,
            market_data_age_seconds=0.0,
            daily_pnl=book.realized_pnl,
            open_orders_count=self.broker.pending_count,
            orders_last_hour=len(recent),
            # Conditions a replay asserts rather than observes. They are the
            # things a live system checks against a broker that is not here.
            app_state=ApplicationState.READY,
            connection_state=ConnectionState.CONNECTED,
            account_type=AccountType.SIMULATED,
            account_available=True,
            positions_reconciled=True,
            open_orders_reconciled=True,
            broker_permission_ready=True,
            strategy_enabled=self.strategy.enabled,
            kill_switch_engaged=False,
            duplicate_order_exists=False,
            # The virtual clock, not the wall clock. Judging a historical bar
            # against today would refuse every replay the moment the contract
            # it prices against expires.
            today=bar.opened_at.date(),
        )
        risk = self.risk_manager.evaluate(risk_context)
        # The gate is evaluated even when risk has already refused. Skipping it
        # would leave it unreachable in a replay -- every configuration
        # interlock it checks is also checked by risk, which runs first -- and
        # an interlock that cannot fire in a backtest is not being tested by it.
        # Running both also means a refusal reports everything that objected,
        # not just whichever approver happened to be asked first.
        gate = self.gate.evaluate(
            GateContext(
                app_state=ApplicationState.READY,
                connection_state=ConnectionState.CONNECTED,
                broker_account_type=AccountType.SIMULATED,
                contract_qualified=True,
                contract_is_continuous=self.contract.is_continuous,
                # A replay is judged against the bar it is on, not today.
                # Using the wall clock would make every historical run refuse
                # as soon as the contract it prices against expires, which is
                # exactly when a replay is most useful.
                contract_expired=self.contract.is_expired(bar.opened_at.date()),
                account_available=True,
                positions_reconciled=True,
                open_orders_reconciled=True,
                market_data_age_seconds=0.0,
                market_data_is_delayed=False,
                broker_permission_ready=True,
                strategy_enabled=self.strategy.enabled,
                risk_approved=risk.approved,
            )
        )
        # RISK_CHECKS_NOT_PASSED is the gate restating risk's verdict, not an
        # objection of its own, so it does not make the gate a refusing party.
        gate_own = tuple(r for r in gate.reasons if r != REASON_RISK_NOT_APPROVED)
        if not risk.approved or gate_own:
            stages = (["risk"] if not risk.approved else []) + (["gate"] if gate_own else [])
            reasons = list(risk.reasons)
            reasons += [r for r in gate_own if r not in reasons]
            return Refusal(
                at=at,
                stage="+".join(stages),
                reasons=tuple(reasons),
                requested_position=intent.requested_position,
                current_position=book.quantity,
            )

        from app.broker.models import OrderRequest  # noqa: PLC0415 - avoids a cycle

        self.broker.place_order(
            OrderRequest(
                internal_order_id=f"bt-{intent.intent_id}",
                correlation_id=intent.correlation_id or intent.intent_id,
                contract=self.contract,
                side=side,
                quantity=quantity,
                order_type=OrderType.MARKET,
            )
        )
        orders.append(bar.opened_at)
        return None


__all__ = [
    "BacktestEngine",
    "BacktestRun",
    "ClosedTrade",
    "Refusal",
    "always_tradeable",
    "backtest_config",
    "cme_liquid_hours",
]
