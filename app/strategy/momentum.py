"""SOL daily momentum: regime-filtered, cost-gated, volatility-sized.

What this is, and what it is not
--------------------------------
This is the strategy the ORB post-mortem's arithmetic points at. It is **not**
a claim of profit. Every P&L result measured on this instrument so far has a
t-statistic under 0.2 -- the only statistically significant number in the whole
body of evidence is the *cost*. So what is engineered here is precisely the
part the evidence CAN support: that costs and risk are controlled, and that
nothing in the rules is a guaranteed loser. Whether a directional edge exists
is a question for out-of-sample data, and `docs/STRATEGY_ANALYSIS.md` says how
to answer it.

Three changes from :mod:`app.strategy.trend`, each aimed at a *diagnosed*
failure rather than at making a backtest look better:

**1. Positions are sized by risk, not by decree.** The prior strategies traded
a fixed 40 contracts whatever the market was doing, so risk per trade swung
with volatility -- at ATR $5 a 2xATR stop risks $10,000, at ATR $15 it risks
$30,000, for the same nominal "40 contracts". Here size is
``floor(risk_budget / (stop_distance_per_sol * 25))``, so **every trade risks
the same dollars at its stop**. This is the single least-fitted improvement
available: it is standard practice, it needs no parameter tuned on this data,
and it cuts exposure exactly when the market is most dangerous.

**2. Entries are gated on the cost-to-risk ratio.** The ORB died paying
$0.373/SOL to chase $0.40-$1.50 moves. Here an entry is refused unless the
stop distance is at least ``min_stop_cost_multiple`` times the round-trip cost
-- so the toll is bounded as a fraction of what is being risked, structurally,
rather than being something a reader has to check afterwards. The default 25x
puts costs at 4% of risk. A market too quiet to clear it is a market this
strategy declines to trade.

**3. Entries are filtered by regime.** A breakout against the long-term trend
is the one most likely to be noise. Longs require the close above the
``regime_days`` mean, shorts below it. This is the most "opinionated" choice
here and the one most exposed to the fitting critique -- it is included
because it is long-standing published trend-following practice, not because it
improved a number on this data.

The rules, as implemented
-------------------------
* 1-minute bars are aggregated into UTC calendar days (see
  :mod:`app.strategy.daily`). Entries and channel exits decide once per
  completed day; stops are checked on every minute.
* **Entry**: the completed day closes strictly beyond the prior
  ``entry_channel_days`` Donchian channel, **and** on the trend side of the
  ``regime_days`` mean close, **and** the cost gate passes, **and** the sized
  position is at least one contract.
* **Stop**: ``stop_atr_mult`` x ATR(``atr_days``) from the actual fill, frozen
  at the signal day's ATR.
* **Trail**: ``trail_atr_mult`` x that same frozen ATR behind the best price
  reached. Never loosens.
* **Channel exit**: the completed day closes strictly through the opposite
  ``exit_channel_days`` channel.
* One position at a time; after an exit, the next completed day is the
  earliest that can signal again.
* **No breakeven rule**, structurally: there is no rule here that can lock in
  less than the cost floor, which is what made 64 of the ORB's exits
  arithmetically guaranteed losses.

Sizing, worked through
----------------------
``risk_budget`` is dollars risked from entry to stop, before costs::

    stop_per_sol = stop_atr_mult * ATR
    risk_per_contract = stop_per_sol * 25          # 25 SOL per MSL contract
    contracts = floor(risk_budget / risk_per_contract)

At the $5,000 default and a 2x stop: ATR $5 -> $250/contract -> 20 contracts;
ATR $10 -> $500/contract -> 10 contracts; ATR $20 -> $1,000 -> 5 contracts.
Size halves as volatility doubles, which is the whole point. The result is
clamped to ``position_contracts`` (40 by default, the ORB document's size, so
the two strategies' dollars remain comparable) and a signal that sizes to zero
contracts is **skipped**, not rounded up to one -- rounding up is how a risk
framework quietly becomes a suggestion.

Size can be overridden DOWN via ``STRATEGY_POSITION_CONTRACTS``, which lowers
the cap rather than the risk budget, so paper trading at 1 contract proves
execution mechanics without changing any other rule.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from typing import Any

from app.backtest.models import Bar
from app.enums import Direction, OrderSide
from app.logging_config import get_logger
from app.signals.models import TradeIntent
from app.strategy.base import BarStrategy
from app.strategy.daily import DailyAggregator, average_true_range, channel, mean_close

_LOG = get_logger("strategy.sol_momentum")

#: SOL per MSL contract. The contract's real multiplier is loaded from a
#: stored IBKR qualification and used for P&L; this is the same number, needed
#: here to turn a per-SOL stop distance into dollars per contract at DECISION
#: time, before any broker is involved.
SOL_PER_CONTRACT = Decimal("25")

#: Round-trip cost per SOL: 2 x $3.41 commission + 2 x 1 tick ($0.05) x 25 SOL,
#: all over 25 SOL. Measured, not assumed -- the commission is from a real IBKR
#: whatIf on MSLQ6 and the slippage is the replay's own default fill model.
#: The cost gate is expressed as a multiple of THIS, so if the fill model or
#: the commission changes, the gate moves with it rather than silently
#: describing a cost structure that no longer exists.
COST_PER_SOL_ROUND_TRIP = Decimal("0.3728")


@dataclass(frozen=True, slots=True)
class MomentumParams:
    """Defaults are priors, not fitted values. See the module docstring."""

    #: Upper bound on size, not the size itself. 40 = 1,000 SOL, the ORB
    #: document's size, kept so results compare at the same ceiling.
    position_contracts: int = 40

    #: Dollars risked from entry to stop per trade, before costs.
    risk_budget: Decimal = Decimal("5000")

    entry_channel_days: int = 20
    exit_channel_days: int = 10
    atr_days: int = 20
    regime_days: int = 100

    stop_atr_mult: Decimal = Decimal("2")
    trail_atr_mult: Decimal = Decimal("3")

    #: Refuse an entry whose stop distance is under this multiple of the
    #: round-trip cost. 25x puts costs at 4% of risk; the ORB ran at roughly
    #: 1.7x on its $0.65 stop, which is 57%.
    min_stop_cost_multiple: Decimal = Decimal("25")


_TUNABLE_DECIMALS: dict[str, tuple[Decimal, Decimal]] = {
    # name -> (exclusive minimum, inclusive maximum). Sanity rails, not
    # opinions: a 20xATR stop on a daily system is not a stop, and a risk
    # budget above $100k cannot be expressed within a 40-contract cap anyway.
    "stop_atr_mult": (Decimal("0"), Decimal("20")),
    "trail_atr_mult": (Decimal("0"), Decimal("20")),
    "min_stop_cost_multiple": (Decimal("0"), Decimal("1000")),
    "risk_budget": (Decimal("0"), Decimal("100000")),
}
_TUNABLE_INTS: dict[str, tuple[int, int]] = {
    # Minimum 2: a 1-day channel is yesterday's bar wearing a channel's name.
    "entry_channel_days": (2, 200),
    "exit_channel_days": (2, 200),
    "atr_days": (2, 200),
    # 0 disables the regime filter, which is a legitimate EXPERIMENT (it is
    # the most opinionated rule here, so measuring without it is exactly the
    # check a skeptic should run) rather than a misconfiguration.
    "regime_days": (0, 400),
}
_HANDLED_ELSEWHERE = frozenset({"position_contracts"})


def _apply_tunables(p: MomentumParams, params: dict[str, Any]) -> MomentumParams:
    """Override defaults for one replay: validated, echoed, unknowns refused."""
    changes: dict[str, object] = {}
    for name, (low, high) in _TUNABLE_DECIMALS.items():
        if name not in params:
            continue
        try:
            value = Decimal(str(params[name]))
        except ArithmeticError as exc:
            raise ValueError(f"{name}={params[name]!r} is not a number") from exc
        if not value.is_finite():
            # Decimal("nan") parses without raising and then poisons every
            # comparison it touches; refuse it like any other bad value.
            raise ValueError(f"{name}={params[name]!r} is not a number")
        if not low < value <= high:
            raise ValueError(f"{name}={value} is outside ({low}, {high}]")
        changes[name] = value
    for name, (int_low, int_high) in _TUNABLE_INTS.items():
        if name not in params:
            continue
        try:
            parsed = int(str(params[name]))
        except ValueError as exc:
            raise ValueError(f"{name}={params[name]!r} is not an integer") from exc
        if not int_low <= parsed <= int_high:
            raise ValueError(f"{name}={parsed} is outside [{int_low}, {int_high}]")
        changes[name] = parsed
    return replace(p, **changes) if changes else p  # type: ignore[arg-type]


def _reject_unknown_params(params: dict[str, Any]) -> None:
    known = _HANDLED_ELSEWHERE | set(_TUNABLE_DECIMALS) | set(_TUNABLE_INTS)
    unknown = set(params) - known
    if unknown:
        raise ValueError(f"unknown strategy parameter(s) {sorted(unknown)}; known: {sorted(known)}")


@dataclass
class _Trade:
    """The open position, managed bar by bar from its actual fill price."""

    direction: int  # +1 long, -1 short
    entry: Decimal
    stop: Decimal
    initial_stop: Decimal
    atr: Decimal
    peak: Decimal
    contracts: int
    exiting: bool = False
    exit_reason: str | None = None
    """Stored at decision time, so an exit re-emitted after a cancelled fill
    reports the ORIGINAL reason rather than whatever the levels look like a
    bar later -- which would mislabel exactly the trades an attribution
    exists to explain."""


class SolMomentumStrategy(BarStrategy):
    """Observes 1-minute bars; trades regime-filtered daily breakouts."""

    name = "sol-momentum"

    def __init__(self, *, enabled: bool = True, params: dict[str, Any] | None = None) -> None:
        super().__init__(enabled=enabled, params=params)
        self._p = MomentumParams()
        size = (params or {}).get("position_contracts")
        if size is not None:
            # DOWN only: this lowers the CAP, leaving the risk budget and
            # every other rule untouched, so a 1-contract paper run is the
            # same strategy proving execution rather than a different one.
            size = int(size)
            if not 1 <= size <= self._p.position_contracts:
                raise ValueError(
                    f"position_contracts override must be within "
                    f"1..{self._p.position_contracts}, got {size}"
                )
            self._p = replace(self._p, position_contracts=size)
        self._p = _apply_tunables(self._p, params or {})
        _reject_unknown_params(params or {})
        if self._p.trail_atr_mult < self._p.stop_atr_mult:
            # A trail tighter than the initial stop governs from the first
            # bar, making every exit report as "trail" and the initial stop
            # unreachable -- an inversion no experiment should express silently.
            raise ValueError(
                f"trail_atr_mult={self._p.trail_atr_mult} must be >= "
                f"stop_atr_mult={self._p.stop_atr_mult}"
            )
        if self._p.exit_channel_days > self._p.entry_channel_days:
            # The exit channel would spend stretches unable to fire, silently.
            raise ValueError(
                f"exit_channel_days={self._p.exit_channel_days} must be <= "
                f"entry_channel_days={self._p.entry_channel_days}"
            )

        self._daily = DailyAggregator(
            keep=max(
                self._p.entry_channel_days,
                self._p.exit_channel_days,
                self._p.atr_days,
                self._p.regime_days,
            )
        )
        self._completed_today = False
        self._trade: _Trade | None = None
        self._position = 0
        self._pending_entry = False
        self._pending_atr: Decimal | None = None
        self._pending_contracts = 0
        self._pending_direction = 0
        self._no_entry_on_or_before: date | None = None
        self._counts: dict[str, int] = {
            "days_completed": 0,
            "days_in_warmup": 0,
            "signals_long": 0,
            "signals_short": 0,
            "skipped_regime": 0,
            "skipped_cost_gate": 0,
            "skipped_size_below_one": 0,
            "signals_while_in_trade": 0,
            "entries_suppressed_post_exit": 0,
            "entries_cancelled_unfilled": 0,
            "exits_stop": 0,
            "exits_trail": 0,
            "exits_channel": 0,
        }
        #: Sizes actually requested, so a result can show the sizing working
        #: rather than requiring the reader to take it on trust.
        self._sizes: list[int] = []

    @property
    def position(self) -> int:
        """Signed contracts this strategy believes it holds. See base class."""
        return self._position

    # ------------------------------------------------------------------
    # Fill feedback: the ONLY place position and entry price come from
    # ------------------------------------------------------------------

    def on_fill(self, *, side: OrderSide, quantity: int, price: Decimal) -> None:
        previous = self._position
        self._position += quantity * side.sign

        if previous == 0 and self._position != 0:
            direction = 1 if self._position > 0 else -1
            if self._pending_direction and direction != self._pending_direction:
                _LOG.error(
                    "entry fill direction contradicts the signal",
                    extra={
                        "event": "momentum.fill_direction_mismatch",
                        "signalled": self._pending_direction,
                        "filled": direction,
                    },
                )
            # Live, a slow fill can land after the next bar cleared the
            # pending state; fall back to the current ATR rather than crash
            # mid-position. With neither, this is a fill the strategy never
            # asked for: the stop goes AT entry so the next bar closes it --
            # an unmanaged position is the one state never worth holding.
            atr = self._pending_atr if self._pending_atr is not None else self._atr()
            if atr is None:
                _LOG.error(
                    "entry fill with no ATR context; emergency stop at entry",
                    extra={"event": "momentum.fill_without_atr", "price": str(price)},
                )
                atr = Decimal(0)
            stop = price - self._p.stop_atr_mult * atr * direction
            self._trade = _Trade(
                direction=direction,
                entry=price,
                stop=stop,
                initial_stop=stop,
                atr=atr,
                peak=price,
                contracts=abs(self._position),
            )
            self._pending_entry = False
            self._pending_atr = None
            self._pending_contracts = 0
            self._pending_direction = 0
        elif self._position == 0:
            self._trade = None

    # ------------------------------------------------------------------
    # Bar handling
    # ------------------------------------------------------------------

    def on_bar(self, bar: Bar) -> Sequence[TradeIntent]:
        completed = self._daily.feed(bar)
        self._completed_today = completed is not None
        if completed is not None:
            self._counts["days_completed"] += 1

        if self._pending_entry:
            self._pending_entry = False
            self._pending_atr = None
            self._pending_contracts = 0
            self._pending_direction = 0
            if self._position == 0:
                self._counts["entries_cancelled_unfilled"] += 1

        if self._trade is not None:
            if self._completed_today and self._breakout_direction() != 0:
                self._counts["signals_while_in_trade"] += 1
            return self._manage(bar)
        return self._maybe_enter(bar)

    # -- indicators --------------------------------------------------------

    def _atr(self) -> Decimal | None:
        return average_true_range(self._daily.days, self._p.atr_days)

    def _breakout_direction(self) -> int:
        """+1/-1 when the just-completed day closed through the entry channel,
        else 0. Callers have already checked that a day just completed."""
        bounds = channel(self._daily.days, self._p.entry_channel_days)
        if bounds is None:
            return 0
        high, low = bounds
        close = self._daily.days[-1].close
        if close > high:
            return 1
        if close < low:
            return -1
        return 0

    # -- entries -----------------------------------------------------------

    def _maybe_enter(self, bar: Bar) -> Sequence[TradeIntent]:
        if not self._completed_today:
            return ()

        direction = self._breakout_direction()
        if direction == 0:
            if channel(self._daily.days, self._p.entry_channel_days) is None:
                self._counts["days_in_warmup"] += 1
            return ()

        blocked_until = self._no_entry_on_or_before
        if blocked_until is not None and self._daily.days[-1].day <= blocked_until:
            self._counts["entries_suppressed_post_exit"] += 1
            return ()

        # Regime: a breakout against the long-term trend is the one most
        # likely to be noise. regime_days=0 disables the filter, deliberately,
        # so it can be measured rather than argued about.
        if self._p.regime_days:
            average = mean_close(self._daily.days, self._p.regime_days)
            if average is None:
                self._counts["days_in_warmup"] += 1
                return ()
            close = self._daily.days[-1].close
            if (close - average) * direction <= 0:
                self._counts["skipped_regime"] += 1
                return ()

        atr = self._atr()
        if atr is None or atr <= 0:
            self._counts["days_in_warmup"] += 1
            return ()

        stop_per_sol = self._p.stop_atr_mult * atr

        # The cost gate. Refusing here is the whole lesson of the ORB
        # post-mortem expressed as a rule: a market too quiet for the stop to
        # dwarf the toll is a market this strategy does not trade.
        if stop_per_sol < self._p.min_stop_cost_multiple * COST_PER_SOL_ROUND_TRIP:
            self._counts["skipped_cost_gate"] += 1
            return ()

        contracts = self._size(stop_per_sol)
        if contracts < 1:
            # Rounding up to one contract here would mean taking a trade at
            # more risk than the budget allows -- the exact moment a risk
            # framework turns into a suggestion. Skip it instead.
            self._counts["skipped_size_below_one"] += 1
            return ()

        self._pending_entry = True
        self._pending_atr = atr
        self._pending_contracts = contracts
        self._pending_direction = direction
        side = "long" if direction > 0 else "short"
        self._counts[f"signals_{side}"] += 1
        self._sizes.append(contracts)
        completed = self._daily.days[-1]
        _LOG.info(
            "momentum breakout",
            extra={
                "event": "momentum.breakout",
                "direction": side,
                "day": completed.day.isoformat(),
                "close": str(completed.close),
                "atr": str(atr),
                "contracts": contracts,
            },
        )
        return (
            self._intent(
                contracts * direction,
                bar,
                session=side,
                trade_n=1,
                signal_day=completed.day.isoformat(),
                atr=str(atr),
                contracts=contracts,
                stop_per_sol=str(stop_per_sol),
            ),
        )

    def _size(self, stop_per_sol: Decimal) -> int:
        """Contracts such that a stop-out costs about ``risk_budget``.

        Integer division floors, which errs toward less risk than the budget
        rather than more -- the direction a sizing rule should err in.
        """
        risk_per_contract = stop_per_sol * SOL_PER_CONTRACT
        if risk_per_contract <= 0:
            return 0
        return min(int(self._p.risk_budget / risk_per_contract), self._p.position_contracts)

    # -- trade management --------------------------------------------------

    def _manage(self, bar: Bar) -> Sequence[TradeIntent]:
        trade = self._trade
        assert trade is not None
        if trade.exiting:
            # The exit should have filled at this bar's open and cleared the
            # trade via on_fill. Still here means it was cancelled on an
            # untradeable bar: re-emit with the original reason.
            return (self._intent(0, bar, exit_reason=trade.exit_reason or "unknown"),)

        d = trade.direction
        adverse = bar.low if d > 0 else bar.high
        favourable = bar.high if d > 0 else bar.low

        # 1. Stop first, at its level from the PREVIOUS bar. Bars are not
        #    ticks: a bar spanning both the stop and new highs is recorded as
        #    a stop-out, pessimistically.
        if (adverse - trade.stop) * d <= 0:
            reason = "exits_stop" if trade.stop == trade.initial_stop else "exits_trail"
            return self._exit(trade, bar, reason)

        # 2. Channel exit, once per completed day, decided on that day's close
        #    rather than on the minute after midnight -- so it is evaluated
        #    before the trail advances off the current bar.
        if self._completed_today:
            bounds = channel(self._daily.days, self._p.exit_channel_days)
            if bounds is not None:
                high, low = bounds
                level = low if d > 0 else high
                if (self._daily.days[-1].close - level) * d < 0:
                    return self._exit(trade, bar, "exits_channel")

        # 3. Trail follows the peak, never loosens. At 3xATR it starts wider
        #    than the 2xATR initial stop, so the initial stop governs until
        #    the trade is about 1 ATR ahead.
        trade.peak = max(trade.peak, favourable) if d > 0 else min(trade.peak, favourable)
        trailed = trade.peak - self._p.trail_atr_mult * trade.atr * d
        trade.stop = max(trade.stop, trailed) if d > 0 else min(trade.stop, trailed)

        return ()

    def _exit(self, trade: _Trade, bar: Bar, reason: str) -> Sequence[TradeIntent]:
        # After an exit, the next COMPLETED day is the earliest that can
        # signal again. Recording the decision day covers the day-roll edge:
        # an exit decided on day D's last bar settles on D+1's first bar --
        # the same call that completes day D -- and without this a stop-out
        # could chain straight into a same-day re-entry.
        current = self._daily.current_day
        if current is not None:
            self._no_entry_on_or_before = current
        trade.exiting = True
        trade.exit_reason = reason.removeprefix("exits_")
        self._counts[reason] += 1
        return (self._intent(0, bar, exit_reason=trade.exit_reason),)

    # -- plumbing ----------------------------------------------------------

    def _intent(self, target: int, bar: Bar, **meta: object) -> TradeIntent:
        if target > 0:
            direction = Direction.LONG
        elif target < 0:
            direction = Direction.SHORT
        else:
            direction = Direction.FLAT
        return TradeIntent(
            strategy_name=self.name,
            symbol="MSL",
            direction=direction,
            requested_position=target,
            # The bar's close time: the decision became possible when the
            # minute ended, and the validator's staleness clock measures from
            # here. Stamping the open would make every live intent arrive
            # ~60s old against a 60s limit.
            created_at=bar.closed_at,
            metadata={"bar_close": str(bar.close), **meta},
        )

    def describe(self) -> dict[str, object]:
        sizes = self._sizes
        return {
            **super().describe(),
            "position_contracts": self._p.position_contracts,
            "params_effective": {
                name: str(getattr(self._p, name)) for name in (*_TUNABLE_DECIMALS, *_TUNABLE_INTS)
            },
            "cost_gate": {
                "cost_per_sol_round_trip": str(COST_PER_SOL_ROUND_TRIP),
                "min_stop_per_sol": str(
                    self._p.min_stop_cost_multiple * COST_PER_SOL_ROUND_TRIP
                ),
                "note": (
                    "an entry is refused unless its stop distance is at least this far, so "
                    "the round trip is a bounded fraction of what the trade risks"
                ),
            },
            # Sizing is the headline change from sol-trend, so the result
            # shows it working rather than asserting it.
            "sizing": {
                "risk_budget": str(self._p.risk_budget),
                "sizes_requested": list(sizes),
                "min": min(sizes) if sizes else None,
                "max": max(sizes) if sizes else None,
                "mean": str(sum(sizes) / len(sizes)) if sizes else None,
                "note": (
                    "contracts = floor(risk_budget / (stop_per_sol * 25)), capped at "
                    "position_contracts. A spread of sizes is the volatility targeting "
                    "working; all-identical sizes means the cap or the budget bound every "
                    "trade and the risk normalisation never engaged."
                ),
            },
            "counters": dict(self._counts),
            "note": (
                "regime-filtered daily Donchian, sized by risk and gated on cost. Built "
                "against the ORB's diagnosed failure modes; it is NOT a claim of edge -- "
                "see docs/STRATEGY_ANALYSIS.md for what the evidence does and does not "
                "support, and what out-of-sample test would settle it."
            ),
        }


__all__ = ["COST_PER_SOL_ROUND_TRIP", "MomentumParams", "SolMomentumStrategy"]
