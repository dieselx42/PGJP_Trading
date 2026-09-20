"""SOL failed channel break: fade a 20-day break that closes back inside.

The rule in one sentence
------------------------
When a day closes below the lowest close of the previous 20 days and, within
the next two days, a close is back above that level, the breakdown failed:
buy at the next open, with the stop at the lowest close of the break and the
target at the middle of the 20-day range. Mirror it for a failed breakup.
Nothing else is a signal. Exit on the stop, the target, or after 10 days.

Where it comes from
-------------------
Connors & Raschke's "Turtle Soup" (Street Smarts, 1995) fades the 20-day
channel break the Turtle system buys, on the observation that most breaks of
a well-formed range fail. Its conditions are kept and translated to a closing
basis: the 20-day channel; the prior extreme at least four days old (a range,
not a slide); the failure confirmed within a day or two; a stop just beyond
the break's extreme; a short hold. The one substantive change is that levels
are CLOSES, not highs and lows: the live bars are sampled from quotes and
their highs and lows are understated (see app.market_data.bar_builder), and
closes are what the feed can be trusted on.

Why it sits beside sol-sma, not instead of it
---------------------------------------------
A trend rule pays a flip every time price crosses its line and earns only
when price leaves it. This rule earns when price rejects an edge of its range
and loses when a break is real. They are wrong in different weather. It is
pre-registered in docs/STRATEGY_ANALYSIS.md section 12 as a COMPLEMENT: its
job is to be positive in the months sol-sma is not, and it is read that way.

The rules, as implemented
-------------------------
* 1-minute bars roll into UTC days (DailyAggregator). Only closes are read;
  highs, lows and volume are never consulted.
* On each completed day D, with no position and no pending break:
  L = lowest close of the ``channel_days`` completed days before D; H = the
  highest. Breakdown: close(D) < L, and the day that set L is at least
  ``min_extreme_age_days`` days before D. Breakup: the mirror against H. A
  break opens a PENDING state carrying the level, the range midpoint
  (H + L) / 2 as the eventual target, and the break's extreme close as the
  eventual stop.
* While pending, each completed day extends the extreme (the lowest or
  highest close seen during the break) and counts. A close back on the
  inside of the level within ``confirm_days`` completed days after the break
  day is the signal: target +size after a failed breakdown, -size after a
  failed breakup. No reclaim within the window means the break was real:
  back to flat, no trade, counted.
* In a position, each completed day in this order: close at or beyond the
  stop -> exit "stop"; close at or beyond the target -> exit "target";
  ``hold_days`` completed days since the signal -> exit "time". Levels are
  checked on closes, once a day. A daily-close stop can lose more than its
  distance on a gap day; that is the price of never acting intraday on a
  contract that prints in two percent of its minutes.
* Orders are market, filling at the next bar's open (+/- slippage). While
  the held position differs from the target the same intent is re-emitted
  each bar, exactly as sol-sma does. One position at a time: no new break is
  considered while one is pending or open.
* Size: ``position_contracts`` (40 for the harness rows, 1 live). Entries
  are from flat and exits to flat, so the largest order is the size itself.
* Not adoptable. Stop and target come from the break that opened the trade,
  which a restart does not know; on a position mismatch the runtime
  disables the strategy and the operator flattens by hand.
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
from app.strategy.daily import DailyAggregator, DailyBar, close_channel

_LOG = get_logger("strategy.sol_fade")


@dataclass(frozen=True, slots=True)
class FadeParams:
    """The published rule's numbers, on a closing basis."""

    #: Harness size, matching every other candidate's rows; 1 live.
    position_contracts: int = 40
    #: The Turtle entry channel, which is the level being faded.
    channel_days: int = 20
    #: The prior extreme must be at least this old: a range, not a slide.
    min_extreme_age_days: int = 4
    #: Completed days after the break within which a close back inside counts.
    confirm_days: int = 2
    #: Completed days since the signal after which the trade is closed anyway.
    hold_days: int = 10


_TUNABLE_INTS: dict[str, tuple[int, int]] = {
    "channel_days": (5, 200),
    "min_extreme_age_days": (0, 30),
    "confirm_days": (1, 10),
    "hold_days": (1, 60),
}
_HANDLED_ELSEWHERE = frozenset({"position_contracts"})


def _apply_tunables(p: FadeParams, params: dict[str, Any]) -> FadeParams:
    changes: dict[str, int] = {}
    for name, (low, high) in _TUNABLE_INTS.items():
        if name not in params:
            continue
        try:
            parsed = int(str(params[name]))
        except ValueError as exc:
            raise ValueError(f"{name}={params[name]!r} is not an integer") from exc
        if not low <= parsed <= high:
            raise ValueError(f"{name}={parsed} is outside [{low}, {high}]")
        changes[name] = parsed
    return replace(p, **changes) if changes else p


def _reject_unknown_params(params: dict[str, Any]) -> None:
    known = _HANDLED_ELSEWHERE | set(_TUNABLE_INTS)
    unknown = set(params) - known
    if unknown:
        raise ValueError(f"unknown strategy parameter(s) {sorted(unknown)}; known: {sorted(known)}")


@dataclass
class _Pending:
    """A break waiting to fail. ``direction`` is the trade it would become."""

    direction: int
    level: Decimal
    midpoint: Decimal
    extreme: Decimal
    break_day: date
    days: int = 0


@dataclass
class _Trade:
    direction: int
    stop: Decimal
    target: Decimal
    level: Decimal
    signal_day: date
    days_held: int = 0


class SolFadeStrategy(BarStrategy):
    """Fades a 20-day channel break that closes back inside within two days."""

    name = "sol-fade"

    def __init__(self, *, enabled: bool = True, params: dict[str, Any] | None = None) -> None:
        super().__init__(enabled=enabled, params=params)
        self._p = FadeParams()
        size = (params or {}).get("position_contracts")
        if size is not None:
            try:
                size = int(str(size))
            except ValueError as exc:
                raise ValueError(f"position_contracts={size!r} is not an integer") from exc
            if not 1 <= size <= self._p.position_contracts:
                raise ValueError(
                    f"position_contracts override must be within "
                    f"1..{self._p.position_contracts}, got {size}"
                )
            self._p = replace(self._p, position_contracts=size)
        self._p = _apply_tunables(self._p, params or {})
        _reject_unknown_params(params or {})

        self._daily = DailyAggregator(keep=self._p.channel_days + 2)
        self._position = 0
        self._target = 0
        self._pending: _Pending | None = None
        self._trade: _Trade | None = None
        self._exit_reason: str | None = None
        self._emitted_last_bar = False
        self._counts: dict[str, int] = {
            "days_completed": 0,
            "days_in_warmup": 0,
            "breaks_down": 0,
            "breaks_up": 0,
            "breaks_too_young": 0,
            "breaks_not_failed": 0,
            "signals_long": 0,
            "signals_short": 0,
            "exits_stop": 0,
            "exits_target": 0,
            "exits_time": 0,
            "orders_emitted": 0,
            "orders_reemitted": 0,
            "fills_off_target": 0,
        }

    # ------------------------------------------------------------------

    @property
    def position(self) -> int:
        return self._position

    @property
    def required_order_size(self) -> int:
        """Entries from flat, exits to flat: never more than the size."""
        return self._p.position_contracts

    @property
    def daily_seed_days(self) -> int:
        return self._p.channel_days + 2

    def on_fill(self, *, side: OrderSide, quantity: int, price: Decimal) -> None:
        self._position += quantity * side.sign
        self._emitted_last_bar = False
        if self._position == self._target:
            if self._target == 0:
                self._trade = None
                self._exit_reason = None
            return
        self._counts["fills_off_target"] += 1
        _LOG.error(
            "fill left the position off target",
            extra={
                "event": "fade.fill_off_target",
                "position": str(self._position),
                "target": str(self._target),
                "price": str(price),
            },
        )

    # ------------------------------------------------------------------

    def on_bar(self, bar: Bar) -> Sequence[TradeIntent]:
        completed = self._daily.feed(bar)
        if completed is not None:
            self._on_day(completed)
            if self.on_day_completed is not None and not self.seeding:
                self.on_day_completed(completed)
        if self.seeding:
            return ()
        return self._reconcile(bar)

    def _on_day(self, completed: DailyBar) -> None:
        self._counts["days_completed"] += 1
        if self._trade is not None:
            self._manage(completed)
            return
        days = self._daily.days
        levels = close_channel(days, self._p.channel_days, excluding_last=True)
        if levels is None:
            self._counts["days_in_warmup"] += 1
            return
        high, low = levels
        if self._pending is None:
            self._maybe_break(completed, days, high, low)
            return
        self._maybe_fail(completed)

    def _maybe_break(
        self, completed: DailyBar, days: Sequence[DailyBar], high: Decimal, low: Decimal
    ) -> None:
        close = completed.close
        window = days[-(self._p.channel_days + 1) : -1]
        if close < low:
            direction, level, extreme_close = 1, low, low
        elif close > high:
            direction, level, extreme_close = -1, high, high
        else:
            return
        # Age of the day that set the level: 1 means yesterday. The LAST day
        # touching the level is the one that matters -- a level re-touched
        # yesterday is a slide, whatever set it first.
        age = next(
            len(window) - i
            for i in range(len(window) - 1, -1, -1)
            if window[i].close == extreme_close
        )
        self._counts["breaks_down" if direction > 0 else "breaks_up"] += 1
        if age < self._p.min_extreme_age_days:
            self._counts["breaks_too_young"] += 1
            return
        self._pending = _Pending(
            direction=direction,
            level=level,
            midpoint=(high + low) / 2,
            extreme=close,
            break_day=completed.day,
        )
        _LOG.info(
            "fade break pending",
            extra={
                "event": "fade.break",
                "day": completed.day.isoformat(),
                "side": "down" if direction > 0 else "up",
                "level": str(level),
                "close": str(close),
                "extreme_age_days": age,
            },
        )

    def _maybe_fail(self, completed: DailyBar) -> None:
        p = self._pending
        assert p is not None
        p.days += 1
        close = completed.close
        p.extreme = min(p.extreme, close) if p.direction > 0 else max(p.extreme, close)
        if (close - p.level) * p.direction > 0:
            self._trade = _Trade(
                direction=p.direction,
                stop=p.extreme,
                target=p.midpoint,
                level=p.level,
                signal_day=completed.day,
            )
            self._target = self._p.position_contracts * p.direction
            self._counts["signals_long" if p.direction > 0 else "signals_short"] += 1
            self._pending = None
            _LOG.info(
                "fade signal",
                extra={
                    "event": "fade.signal",
                    "signal_day": completed.day.isoformat(),
                    "target": str(self._target),
                    "stop": str(self._trade.stop),
                    "take_profit": str(self._trade.target),
                },
            )
            return
        if p.days >= self._p.confirm_days:
            self._counts["breaks_not_failed"] += 1
            self._pending = None

    def _manage(self, completed: DailyBar) -> None:
        t = self._trade
        assert t is not None
        if self._target == 0:
            return  # exit already asked for; waiting on the fill
        t.days_held += 1
        d, close = t.direction, completed.close
        if (close - t.stop) * d <= 0:
            self._exit("stop")
        elif (close - t.target) * d >= 0:
            self._exit("target")
        elif t.days_held >= self._p.hold_days:
            self._exit("time")

    def _exit(self, reason: str) -> None:
        self._counts["exits_" + reason] += 1
        self._exit_reason = reason
        self._target = 0
        self._emitted_last_bar = False

    # ------------------------------------------------------------------

    def _reconcile(self, bar: Bar) -> Sequence[TradeIntent]:
        if self._position == self._target:
            self._emitted_last_bar = False
            return ()
        t = self._trade
        meta: dict[str, object] = {
            "signal_day": str(t.signal_day) if t else None,
            "level": str(t.level) if t else None,
            "stop": str(t.stop) if t else None,
            "take_profit": str(t.target) if t else None,
        }
        if self._target != 0:
            meta["session"] = "long" if self._target > 0 else "short"
            meta["trade_n"] = 1
        else:
            meta["exit_reason"] = self._exit_reason or "unsignalled"
        self._counts["orders_emitted"] += 1
        if self._emitted_last_bar:
            self._counts["orders_reemitted"] += 1
        self._emitted_last_bar = True
        return (self._intent(self._target, bar, **meta),)

    def _intent(self, target: int, bar: Bar, **meta: object) -> TradeIntent:
        direction = (
            Direction.LONG if target > 0 else Direction.SHORT if target < 0 else Direction.FLAT
        )
        return TradeIntent(
            strategy_name=self.name,
            symbol="MSL",
            direction=direction,
            requested_position=target,
            created_at=bar.closed_at,
            metadata={"bar_close": str(bar.close), **meta},
        )

    def describe(self) -> dict[str, object]:
        t, p = self._trade, self._pending
        return {
            **super().describe(),
            "position_contracts": self._p.position_contracts,
            "params_effective": {name: str(getattr(self._p, name)) for name in _TUNABLE_INTS},
            "counters": dict(self._counts),
            "target_position": self._target,
            "pending": None
            if p is None
            else {
                "side": "long" if p.direction > 0 else "short",
                "level": str(p.level),
                "extreme": str(p.extreme),
                "days": p.days,
                "break_day": p.break_day.isoformat(),
            },
            "trade": None
            if t is None
            else {
                "side": "long" if t.direction > 0 else "short",
                "stop": str(t.stop),
                "take_profit": str(t.target),
                "signal_day": t.signal_day.isoformat(),
                "days_held": t.days_held,
            },
            "note": (
                f"fades a {self._p.channel_days}-day channel break on closes that closes back "
                f"inside within {self._p.confirm_days} day(s); stop at the break's extreme close, "
                f"target the channel midpoint, time exit after {self._p.hold_days} days"
            ),
        }


__all__ = ["FadeParams", "SolFadeStrategy"]
