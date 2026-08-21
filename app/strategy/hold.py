"""Passive long exposure, rolled quarterly. The benchmark every strategy must beat.

Why this exists
---------------
The ORB post-mortem answered "does this strategy make money" (no) but never
asked the prior question: **does trading beat not trading?** A strategy that
returns +$20k a year is worthless if simply holding the instrument returned
+$60k over the same window, and the comparison table had no row that could
say so. This is that row.

It is deliberately the dumbest thing in the repository. It has no signal, no
stop, no view. It buys once and stays long. Any strategy that cannot beat it
-- risk-adjusted, after costs -- is destroying value relative to doing
nothing, and should be abandoned rather than tuned.

Why it ROLLS rather than simply holding
---------------------------------------
Two reasons, and the second is the honest one.

1. The replay's ``net_pnl`` is ``realized_pnl - commission_paid``: an open
   position at the end of the window contributes nothing to it. A strategy
   that buys on the first bar and never sells would report only its entry
   commission -- a benchmark of exactly zero information.

2. More importantly, **this is what passive exposure actually costs on
   futures.** MSL is a quarterly contract; it expires. Holding long SOL
   futures for a year is not one trade, it is four -- each roll paying a full
   round trip. A "buy and hold" benchmark that ignored the roll would be
   comparing active strategies against a passive one that cannot exist, and
   flattering them by roughly ``4 x $373 = $1,492`` a year at 40 contracts.

So: enter long, flatten and re-enter every ``roll_days``, forever. The
default 90 days is the quarterly cycle MSL actually trades on.

What it still does not model
----------------------------
The roll's *basis* -- the price difference between the expiring contract and
the next one. On spot proxy data there is no second contract to roll into, so
the re-entry happens at the same series' next bar. Real rolls also pay the
calendar spread, which can run either way. The result therefore overstates a
real futures hold by whatever the basis cost, and says so in ``describe``.

Like the other bar strategies, size can be overridden DOWN via
``STRATEGY_POSITION_CONTRACTS``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from app.backtest.models import Bar
from app.enums import Direction, OrderSide
from app.logging_config import get_logger
from app.signals.models import TradeIntent
from app.strategy.base import BarStrategy

_LOG = get_logger("strategy.sol_hold")


@dataclass(frozen=True, slots=True)
class HoldParams:
    """Passive exposure's only two choices: how big, and how often to roll."""

    #: 1,000 SOL / 25 SOL per contract -- the same exposure the other
    #: strategies carry, so the dollars compare like for like.
    position_contracts: int = 40

    #: MSL is a quarterly contract. 90 days is that cycle; it is not a tuned
    #: parameter and must never become one.
    roll_days: int = 90


_TUNABLE_INTS: dict[str, tuple[int, int]] = {
    # Lower bound 7: a "passive" benchmark rolling weekly is not passive, it
    # is a high-frequency strategy wearing a benchmark's name.
    "roll_days": (7, 3650),
}
_HANDLED_ELSEWHERE = frozenset({"position_contracts"})


def _reject_unknown_params(params: dict[str, Any]) -> None:
    known = _HANDLED_ELSEWHERE | set(_TUNABLE_INTS)
    unknown = set(params) - known
    if unknown:
        raise ValueError(f"unknown strategy parameter(s) {sorted(unknown)}; known: {sorted(known)}")


class SolHoldStrategy(BarStrategy):
    """Long, always, rolled every ``roll_days``. The do-nothing benchmark."""

    name = "sol-hold"

    def __init__(self, *, enabled: bool = True, params: dict[str, Any] | None = None) -> None:
        super().__init__(enabled=enabled, params=params)
        self._p = HoldParams()
        size = (params or {}).get("position_contracts")
        if size is not None:
            # DOWN only, same ceiling as the other strategies: a benchmark at
            # a different size than the strategies it benchmarks is not a
            # benchmark, it is a different question.
            size = int(size)
            if not 1 <= size <= self._p.position_contracts:
                raise ValueError(
                    f"position_contracts override must be within "
                    f"1..{self._p.position_contracts}, got {size}"
                )
            self._p = replace(self._p, position_contracts=size)
        roll = (params or {}).get("roll_days")
        if roll is not None:
            try:
                parsed = int(str(roll))
            except ValueError as exc:
                raise ValueError(f"roll_days={roll!r} is not an integer") from exc
            low, high = _TUNABLE_INTS["roll_days"]
            if not low <= parsed <= high:
                raise ValueError(f"roll_days={parsed} is outside [{low}, {high}]")
            self._p = replace(self._p, roll_days=parsed)
        _reject_unknown_params(params or {})

        self._position = 0
        # The roll is CALENDAR-anchored, not "90 days after I happened to
        # re-enter". Real futures roll on fixed expiry dates; a clock that
        # restarted from each fill would drift later every quarter and
        # quietly under-count the rolls a passive hold actually pays.
        self._next_roll: date | None = None
        self._pending = False
        self._rolling = False
        self._counts: dict[str, int] = {
            "entries": 0,
            "rolls": 0,
            "entries_cancelled_unfilled": 0,
        }

    @property
    def position(self) -> int:
        return self._position

    def on_fill(self, *, side: OrderSide, quantity: int, price: Decimal) -> None:
        del price
        previous = self._position
        self._position += quantity * side.sign
        self._pending = False
        if previous == 0 and self._position != 0:
            self._counts["entries"] += 1
        elif self._position == 0:
            # Flattened for a roll; the next bar re-enters. The roll schedule
            # is untouched -- it belongs to the calendar, not to this fill.
            self._rolling = False

    def on_bar(self, bar: Bar) -> Sequence[TradeIntent]:
        if self._pending:
            # The order emitted last bar should have filled at this bar's
            # open. Still where we were means it was cancelled on an
            # untradeable bar; clear the flag so the benchmark cannot wedge.
            self._pending = False
            if self._position == 0 and self._next_roll is None and not self._rolling:
                self._counts["entries_cancelled_unfilled"] += 1

        today = bar.opened_at.date()

        if self._position == 0:
            self._pending = True
            return (self._intent(self._p.position_contracts, bar, reason="enter"),)

        if self._next_roll is None:
            # First bar holding a position: this fixes the roll calendar for
            # the whole replay.
            self._next_roll = today + timedelta(days=self._p.roll_days)
            return ()

        if self._rolling:
            # The flatten was emitted and has not filled yet; re-emit rather
            # than hold a position the benchmark has decided to close.
            return (self._intent(0, bar, reason="roll"),)

        if today >= self._next_roll:
            self._rolling = True
            self._pending = True
            self._counts["rolls"] += 1
            due = self._next_roll
            # Advance by whole periods, so a gap in the data cannot make the
            # benchmark roll twice in a row to "catch up".
            while self._next_roll <= today:
                self._next_roll += timedelta(days=self._p.roll_days)
            _LOG.info(
                "quarterly roll",
                extra={"event": "hold.roll", "due": due.isoformat(), "at": today.isoformat()},
            )
            return (self._intent(0, bar, reason="roll"),)

        return ()

    def _intent(self, target: int, bar: Bar, *, reason: str) -> TradeIntent:
        direction = Direction.LONG if target > 0 else Direction.FLAT
        return TradeIntent(
            strategy_name=self.name,
            symbol="MSL",
            direction=direction,
            requested_position=target,
            created_at=bar.closed_at,
            # "session"/"trade_n"/"exit_reason" are the keys the engine
            # threads into its attribution, so the benchmark's trades slice
            # the same way every other strategy's do.
            metadata={
                "bar_close": str(bar.close),
                "session": "hold",
                "trade_n": 1,
                "exit_reason": reason,
            },
        )

    def describe(self) -> dict[str, object]:
        return {
            **super().describe(),
            "position_contracts": self._p.position_contracts,
            "roll_days": self._p.roll_days,
            "counters": dict(self._counts),
            "note": (
                "passive long, rolled every "
                f"{self._p.roll_days} days -- the benchmark an active strategy must beat. "
                "Rolls are priced at the same series' next bar: real futures rolls also pay "
                "the calendar basis, which this does NOT model, so a real passive hold costs "
                "somewhat more than this shows. The replay's final open position is excluded "
                "from net_pnl, so the last partial period is not counted."
            ),
        }


__all__ = ["HoldParams", "SolHoldStrategy"]
