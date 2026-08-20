"""SOL daily Donchian trend strategy, built for the cost floor.

Why this strategy exists
------------------------
The ORB post-mortem (see ``docs/ORB_DOCUMENT_QUESTIONS.md`` and the
attribution runs) found a round trip costs $0.373/SOL -- 25% of the ORB's
$1.50 target and 93% of its $0.40 breakeven trigger -- against a raw edge of
roughly zero. The design consequence: a viable strategy on this instrument
must capture moves where that toll is small. This one waits for a multi-day
breakout and rides it, targeting the $10-30 legs SOL's fat-tailed trends
produce, where the toll is 1-4% instead of 25-93%. Whether it has an edge is
the backtest's verdict, not this docstring's; what is engineered here is only
that costs cannot be the reason it fails.

The rules, as implemented
-------------------------
* Bars in are 1-minute; the strategy aggregates them into UTC calendar days.
  A day is complete when the first bar of a LATER day arrives.
* Entry, evaluated once per completed day: the day's close strictly above the
  highest high of the prior 20 days -> long; strictly below the lowest low of
  the prior 20 days -> short. A wick through the channel is not a signal.
* Initial stop: 2 x ATR(20) from the actual fill, using the ATR as of the
  signal day, frozen for the life of the trade.
* Trail: 3 x that same frozen ATR behind the best price reached. The stop is
  always the tighter of initial and trail, and never loosens.
* Channel exit, evaluated once per completed day: the day's close strictly
  through the opposite 10-day channel (long: below the prior 10-day low).
* One position at a time. Size fixed, default 40 contracts (1,000 SOL --
  the ORB document's size, kept so results compare like for like).

Decisions a reader would otherwise have to reverse-engineer
-----------------------------------------------------------
* **Days are UTC calendar days.** Crypto spot trades around the clock; UTC is
  the only day boundary this system uses anywhere. A "daily close" is the
  last 1-minute bar before midnight UTC.
* **Signals wait for the day to complete.** The entry intent is emitted on
  the first bar of the next day and fills on the bar after that -- one to two
  minutes past midnight UTC. Pessimistic and honest: nothing trades a close
  it has not seen.
* **Stops are checked on every 1-minute bar**, not once a day, against the
  bar's adverse extreme, and FIRST -- before the channel exit and before the
  trail advances. Same pessimism as the ORB: a bar that spans both the stop
  and a favourable move is recorded as a stop-out.
* **ATR is a simple average** of the true range over the last 20 observed
  days, not Wilder's smoothing -- auditable from the day table by hand. It is
  frozen at entry so every level of a trade derives from numbers that existed
  when the trade was taken.
* **Missing days are skipped, not invented.** Channels and ATR run over the
  last N days the data actually contains. A gap wide enough to matter shows
  up in ``bars-info``, not in a fabricated candle.
* **No same-day reversal.** An opposite 20-day breakout while in a trade
  exits via the 10-day channel (the 20-day channel lies at or beyond it) and
  the strategy waits for the NEXT completed day to consider a new entry. One
  decision per day keeps every trade attributable to exactly one signal.
* **No breakeven rule, deliberately.** The ORB's $0.05 lock below the $0.373
  cost floor produced 58 arithmetically guaranteed losses a year. This
  strategy has no rule that can lock in less than its own costs.

Like the ORB, this is a :class:`~app.strategy.base.BarStrategy`: fed stored
history by the backtest engine, and 1-minute sampled bars by the live
runtime's bar builder. Size can be overridden DOWN (never up) via
``STRATEGY_POSITION_CONTRACTS``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from itertools import pairwise
from typing import Any

from app.backtest.models import Bar
from app.enums import Direction, OrderSide
from app.logging_config import get_logger
from app.signals.models import TradeIntent
from app.strategy.base import BarStrategy

_LOG = get_logger("strategy.sol_trend")


@dataclass(frozen=True, slots=True)
class TrendParams:
    """The strategy's parameters. ATR multiples are unitless; days are days."""

    #: 1,000 SOL / 25 SOL per MSL contract -- the ORB document's size, kept so
    #: the two strategies' results compare at identical exposure.
    position_contracts: int = 40

    entry_channel_days: int = 20
    exit_channel_days: int = 10
    atr_days: int = 20
    stop_atr_mult: Decimal = Decimal("2")
    trail_atr_mult: Decimal = Decimal("3")


#: Overridable per REPLAY via ``--strategy-params``, exactly like the ORB's
#: tunables: every override validated, every unknown key refused.
_TUNABLE_DECIMALS: dict[str, tuple[Decimal, Decimal]] = {
    # name -> (exclusive minimum, inclusive maximum). The maxima are sanity
    # rails: a 20xATR stop on a daily system is not a stop.
    "stop_atr_mult": (Decimal("0"), Decimal("20")),
    "trail_atr_mult": (Decimal("0"), Decimal("20")),
}
_TUNABLE_INTS: dict[str, tuple[int, int]] = {
    # Minimum 2: a 1-day channel is yesterday's bar wearing a channel's name,
    # and ATR over 1 day is just that day's range.
    "entry_channel_days": (2, 200),
    "exit_channel_days": (2, 200),
    "atr_days": (2, 200),
}
_HANDLED_ELSEWHERE = frozenset({"position_contracts"})


def _apply_tunables(p: TrendParams, params: dict[str, Any]) -> TrendParams:
    """Override the defaults for one replay. Same contract as the ORB's:
    validated, echoed in ``describe``, unknowns refused elsewhere."""
    changes: dict[str, object] = {}
    for name, (low, high) in _TUNABLE_DECIMALS.items():
        if name not in params:
            continue
        try:
            value = Decimal(str(params[name]))
        except ArithmeticError as exc:
            raise ValueError(f"{name}={params[name]!r} is not a number") from exc
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
class _Day:
    """One UTC calendar day, aggregated from the 1-minute bars it contained."""

    day: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal

    def absorb(self, bar: Bar) -> None:
        self.high = max(self.high, bar.high)
        self.low = min(self.low, bar.low)
        self.close = bar.close


@dataclass
class _Trade:
    """The open position, managed bar by bar from its actual fill price."""

    direction: int  # +1 long, -1 short
    entry: Decimal
    stop: Decimal
    initial_stop: Decimal
    """Where the 2xATR stop started; the exit is a "stop" only while the stop
    still sits there. Once the trail has ratcheted it, the exit is a "trail"."""
    atr: Decimal
    """ATR as of the signal day, frozen -- every level derives from it."""
    peak: Decimal
    exiting: bool = False
    exit_reason: str | None = None
    """Stored at decision time so a re-emitted exit (after a cancelled fill on
    an untradeable bar) reports the ORIGINAL reason. Same rationale as the
    ORB's: recomputing from levels a bar later guesses wrong exactly on the
    trades an attribution exists to explain."""


class SolTrendStrategy(BarStrategy):
    """Observes 1-minute bars; trades daily Donchian breakouts."""

    name = "sol-trend"

    def __init__(self, *, enabled: bool = True, params: dict[str, Any] | None = None) -> None:
        super().__init__(enabled=enabled, params=params)
        self._p = TrendParams()
        size = (params or {}).get("position_contracts")
        if size is not None:
            # DOWN only, same rule and same ceiling as the ORB: paper proves
            # execution at 1 contract before anything trades at 40, and a
            # larger size would break comparability with the ORB baseline.
            size = int(size)
            if not 1 <= size <= self._p.position_contracts:
                raise ValueError(
                    f"position_contracts override must be within "
                    f"1..{self._p.position_contracts}, got {size}"
                )
            self._p = replace(self._p, position_contracts=size)
        self._p = _apply_tunables(self._p, params or {})
        _reject_unknown_params(params or {})

        #: Completed days, oldest first, trimmed to the longest window + 1.
        self._days: list[_Day] = []
        self._current: _Day | None = None
        self._just_completed: _Day | None = None
        self._trade: _Trade | None = None
        self._position = 0
        self._pending_entry = False
        self._pending_atr: Decimal | None = None
        self._pending_direction = 0
        self._counts: dict[str, int] = {
            "days_completed": 0,
            "days_in_warmup": 0,
            "entries_long": 0,
            "entries_short": 0,
            "signals_while_in_trade": 0,
            "entries_cancelled_unfilled": 0,
            "exits_stop": 0,
            "exits_trail": 0,
            "exits_channel": 0,
        }

    @property
    def position(self) -> int:
        """Signed contracts this strategy believes it holds. See base class."""
        return self._position

    @property
    def _max_window(self) -> int:
        return max(self._p.entry_channel_days, self._p.exit_channel_days, self._p.atr_days)

    # ------------------------------------------------------------------
    # Fill feedback: the ONLY place position and entry price come from
    # ------------------------------------------------------------------

    def on_fill(self, *, side: OrderSide, quantity: int, price: Decimal) -> None:
        previous = self._position
        self._position += quantity * side.sign

        if previous == 0 and self._position != 0:
            direction = 1 if self._position > 0 else -1
            # The ATR was stashed when the signal fired; a fill without one is
            # a fill this strategy never asked for, and trading on a level
            # computed from nothing is worse than stopping.
            atr = self._pending_atr
            assert atr is not None, "entry fill arrived with no signal-day ATR stashed"
            stop = price - self._p.stop_atr_mult * atr * direction
            self._trade = _Trade(
                direction=direction,
                entry=price,
                stop=stop,
                initial_stop=stop,
                atr=atr,
                peak=price,
            )
            self._pending_entry = False
            self._pending_atr = None
            self._pending_direction = 0
        elif self._position == 0:
            self._trade = None

    # ------------------------------------------------------------------
    # Bar handling
    # ------------------------------------------------------------------

    def on_bar(self, bar: Bar) -> Sequence[TradeIntent]:
        self._roll_day(bar)

        if self._pending_entry:
            # The entry emitted on the previous bar should have filled at this
            # bar's open, before this method ran. Still flat means the order
            # was cancelled (an untradeable bar); the machine must not wedge
            # waiting for a fill that never comes.
            self._pending_entry = False
            self._pending_atr = None
            self._pending_direction = 0
            if self._position == 0:
                self._counts["entries_cancelled_unfilled"] += 1

        if self._trade is not None:
            self._count_missed_signal()
            return self._manage(bar)
        return self._maybe_enter(bar)

    # -- day lifecycle -----------------------------------------------------

    def _roll_day(self, bar: Bar) -> None:
        """Aggregate the bar into its UTC day; complete the previous day when
        the date changes. ``self._just_completed`` holds the completed day for
        exactly one on_bar call -- signals are evaluated against it once."""
        self._just_completed = None
        day = bar.opened_at.date()
        if self._current is None:
            self._current = _Day(
                day=day, open=bar.open, high=bar.high, low=bar.low, close=bar.close
            )
            return
        if day == self._current.day:
            self._current.absorb(bar)
            return
        # Date changed: the previous day is complete. Bars arrive in time
        # order (the replay sorts; the live builder emits monotonically), so
        # a date change is always forward.
        self._just_completed = self._current
        self._days.append(self._current)
        del self._days[: -(self._max_window + 1)]
        self._counts["days_completed"] += 1
        self._current = _Day(day=day, open=bar.open, high=bar.high, low=bar.low, close=bar.close)

    def _prior_days(self, window: int) -> list[_Day] | None:
        """The ``window`` days BEFORE the just-completed day, or None during
        warmup. ``self._days[-1]`` is the completed day itself; the channel a
        breakout is measured against must not contain the breakout."""
        if len(self._days) < window + 1:
            return None
        return self._days[-(window + 1) : -1]

    def _atr(self) -> Decimal | None:
        """Simple average of the true range over the last ``atr_days`` pairs of
        observed days, the just-completed day included. None during warmup."""
        n = self._p.atr_days
        if len(self._days) < n + 1:
            return None
        window = self._days[-(n + 1) :]
        total = Decimal(0)
        for prev, this in pairwise(window):
            total += max(
                this.high - this.low, abs(this.high - prev.close), abs(this.low - prev.close)
            )
        return total / n

    def _breakout_direction(self) -> int:
        """+1/-1 when the just-completed day closed through the entry channel,
        else 0. Callers have already checked a day just completed."""
        prior = self._prior_days(self._p.entry_channel_days)
        if prior is None:
            self._counts["days_in_warmup"] += 1
            return 0
        completed = self._days[-1]
        if completed.close > max(d.high for d in prior):
            return 1
        if completed.close < min(d.low for d in prior):
            return -1
        return 0

    # -- entries -----------------------------------------------------------

    def _maybe_enter(self, bar: Bar) -> Sequence[TradeIntent]:
        if self._just_completed is None:
            return ()
        direction = self._breakout_direction()
        if direction == 0:
            return ()
        atr = self._atr()
        if atr is None or atr == 0:
            # A channel long enough to break out of but not enough days for an
            # ATR (or an ATR of zero -- a flat tape) leaves no honest way to
            # place the stop. Counted as warmup rather than guessed around.
            self._counts["days_in_warmup"] += 1
            return ()
        self._pending_entry = True
        self._pending_atr = atr
        self._pending_direction = direction
        side = "long" if direction > 0 else "short"
        self._counts[f"entries_{side}"] += 1
        completed = self._days[-1]
        _LOG.info(
            "trend breakout",
            extra={
                "event": "trend.breakout",
                "direction": side,
                "day": completed.day.isoformat(),
                "close": str(completed.close),
                "atr": str(atr),
            },
        )
        target = self._p.position_contracts * direction
        return (
            self._intent(
                target,
                bar,
                session=side,
                trade_n=1,
                signal_day=completed.day.isoformat(),
                atr=str(atr),
            ),
        )

    def _count_missed_signal(self) -> None:
        if self._just_completed is None:
            return
        if self._breakout_direction() != 0:
            self._counts["signals_while_in_trade"] += 1

    # -- trade management --------------------------------------------------

    def _manage(self, bar: Bar) -> Sequence[TradeIntent]:
        trade = self._trade
        assert trade is not None
        if trade.exiting:
            # The exit should have filled at this bar's open and cleared
            # self._trade via on_fill. Still here means it was cancelled on an
            # untradeable bar: re-emit with the ORIGINAL reason. An open
            # position with no working exit is not a state this strategy is
            # ever willing to hold.
            return (self._intent(0, bar, exit_reason=trade.exit_reason or "unknown"),)

        d = trade.direction
        adverse = bar.low if d > 0 else bar.high
        favourable = bar.high if d > 0 else bar.low

        # 1. Stop first, at its level from the PREVIOUS bar. Bars are not
        #    ticks: a bar that spans both the stop and new highs is recorded
        #    as a stop-out, pessimistically.
        if (adverse - trade.stop) * d <= 0:
            reason = "exits_stop" if trade.stop == trade.initial_stop else "exits_trail"
            return self._exit(trade, bar, reason)

        # 2. Channel exit, once per completed day: the day closed through the
        #    opposite channel. Evaluated before the trail advances off this
        #    bar, because the decision belongs to the completed day, not to
        #    the minute after midnight.
        if self._just_completed is not None:
            prior = self._prior_days(self._p.exit_channel_days)
            if prior is not None:
                completed = self._days[-1]
                channel = min(p.low for p in prior) if d > 0 else max(p.high for p in prior)
                if (completed.close - channel) * d < 0:
                    return self._exit(trade, bar, "exits_channel")

        # 3. Trail follows the peak, never loosens. Active from entry: at
        #    3xATR behind the peak it starts wider than the 2xATR initial
        #    stop, so the initial stop governs until the trade is 1 ATR ahead.
        trade.peak = max(trade.peak, favourable) if d > 0 else min(trade.peak, favourable)
        trailed = trade.peak - self._p.trail_atr_mult * trade.atr * d
        trade.stop = max(trade.stop, trailed) if d > 0 else min(trade.stop, trailed)

        return ()

    def _exit(self, trade: _Trade, bar: Bar, reason: str) -> Sequence[TradeIntent]:
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
            # Stamped with the bar's close time, same as the ORB and for the
            # same reason: the decision became possible when the minute ended,
            # and the validator's staleness clock measures from here.
            created_at=bar.closed_at,
            metadata={"bar_close": str(bar.close), **meta},
        )

    def describe(self) -> dict[str, object]:
        return {
            **super().describe(),
            "position_contracts": self._p.position_contracts,
            "params_effective": {
                name: str(getattr(self._p, name)) for name in (*_TUNABLE_DECIMALS, *_TUNABLE_INTS)
            },
            "counters": dict(self._counts),
            "note": (
                "daily Donchian trend on UTC calendar days aggregated from 1-minute "
                "bars; entries and channel exits decide once per completed day, stops "
                "are checked every minute. Built so the $0.373/SOL round-trip cost is "
                "1-4% of a typical captured move rather than the ORB's 25-93%."
            ),
        }


__all__ = ["SolTrendStrategy", "TrendParams"]
