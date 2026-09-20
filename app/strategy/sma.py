"""SOL close versus its 50-day average: long above, short below, always in.

The rule in one sentence
------------------------
Every day at 00:00 UTC compare yesterday's completed daily close with the
50-day simple moving average of daily closes -- the line every SOL chart
draws by default: above it hold one contract long, below it hold one contract
short, flip at the open of the next minute when the side changes, and
otherwise do nothing. No stop, no target, no trail; never flat once warmed up.

Why this rule, and why this shape
---------------------------------
* The cost work in ``docs/STRATEGY_ANALYSIS.md`` measured the round trip at
  $0.523/SOL ($0.573 at the pessimistic 3-tick bracket) and showed a new
  signal is cheapest to harvest on multi-day, $5-30 moves. A state rule that
  changes side ~25 times a year pays ~$14/SOL/yr in toll against a gross P&L
  standard deviation near $109/SOL/yr: the toll is ~13% of the noise, not the
  ORB's 25-93% of its target.
* It is DIFFERENT IN KIND from sol-trend: a position target, not a trade
  with a lifecycle. There is nothing to stop out, nothing to trail, nothing
  to re-enter, and one parameter instead of five.
* Close minus SMA(N) is a linearly decaying weighting of the last N-1 daily
  returns whose centre of mass sits near N/3 days; N=50 puts it at ~17 days,
  inside the 1-4 week horizon where time-series momentum in crypto is
  documented (Liu & Tsyvinski 2021; Detzel et al. 2021) and inside the
  long/short TSMOM tradition (Moskowitz, Ooi & Pedersen 2012).
* 50 was fixed before any replay for three stated reasons -- evidence
  horizon, ~25 sign changes a year on a random walk so a one-year sign-flip
  test has power, and chart-default checkability. A different window is a
  different candidate with its own pre-registration, never a tuning.

The rules, as implemented
-------------------------
* Bars in are 1-minute; the strategy rolls them into UTC calendar days with
  :class:`~app.strategy.daily.DailyAggregator`. Only CLOSES are read. Highs,
  lows, volume, ``bar.symbol`` and ``bar.source`` are never consulted.
* Day D completes on the first bar whose UTC date is later than D --
  normally D+1's 00:00 bar; if that bar is missing, the first later bar that
  exists. D's close is the close of the last 1-minute bar of D in the data.
* On that completing bar, exactly once per completed day: ``sma`` is the
  mean of the closes of the last 50 completed days the data contains,
  INCLUDING D. Fewer than 50 completed days is warm-up: the target stays 0
  and nothing is emitted.
* ``close(D) > sma`` -> target +size; ``close(D) < sma`` -> target -size;
  ``close(D) == sma`` -> target unchanged (a close AT a level is not a
  signal, the project's strictness convention; before the first decisive day
  that means still flat). The target changes nowhere else.
* On EVERY bar, after the decision: if the held position (changed only in
  ``on_fill``) differs from the target, emit exactly ONE intent for the
  target -- an absolute signed position -- else nothing. An intent equal to
  the held position (a ``no_change`` refusal) is never produced.
* The engine turns that into a MARKET order at the NEXT bar's open +/-
  slippage: the first entry is ``size`` contracts, every later change of
  side is ONE order of 2 x size (long 40 -> short 40 is an 80-lot; at the
  live size of 1 it is a 2-lot), so ``MAX_ORDER_SIZE`` must be >= 2 x size.
* The only exit is the flip. No stop, no profit target, no trail, no time
  exit, no flat state after warm-up. The position is held through the CME
  daily break, weekends and the quarterly roll (the operator rolls; the
  strategy knows nothing about contracts).

Decisions a reader would otherwise have to reverse-engineer
-----------------------------------------------------------
* **If an order does not fill.** In the replay an order is cancelled only
  when the next bar is untradeable, and the strategy is not told. It does
  not need to be: on the following bar the position still differs from the
  target, so the identical intent goes out again (counted in
  ``orders_reemitted``) until ``on_fill`` confirms. A partial fill is topped
  up the same way. A fill the strategy never asked for -- the position
  moving while the target is 0, or beyond it -- is counted in
  ``fills_off_target`` and corrected toward the target on the next bar with
  ``exit_reason="unsignalled"``.
* **Weekends.** The replay runs on 24/7 spot with ``--session all``, so
  Saturday and Sunday UTC are ordinary days whose decisions fill at 00:01
  UTC -- optimistic, since CME is closed Friday 21:00 to Sunday 22:00 UTC.
  Live, no bars arrive while CME is closed, so no decision and no order can
  happen then: Friday completes on the first Sunday-evening bar and fills at
  ~22:01 UTC Sunday; Saturday has no bars and is not a day; Sunday is a
  22:00-24:00 stub completed on Monday's 00:00 bar. A live week has six
  "days" and the 50-day average spans ~8.3 calendar weeks instead of ~7.1.
  That replay-vs-live divergence is a documented limitation, not a rule
  change; a ``cme-crypto`` session filter for the replay is the engine
  follow-up that would close it.
* **Gaps.** Missing days are skipped, never invented: the average always
  runs over the last 50 completed days that EXIST, so a gap stretches it in
  calendar time; a day cut short keeps its last available close; the first
  bar after a gap completes the last day before it and the decision
  proceeds. ``days_completed`` against the calendar exposes every gap.
* **Shorts are symmetric, deliberately.** The TSMOM evidence is long/short;
  a symmetric always-in rule has expected gross exactly zero on a random
  walk, so its coin-flip line is zero and the sign-flip null is clean. A
  long/flat rule's null is the drift share while long, and in a year SOL is
  known to have fallen it beats sol-hold by construction. A futures short
  costs nothing extra to carry. The accepted price is momentum-crash
  exposure on the short leg with no stop -- which is why the live size is 1.
* **No band, no hysteresis.** A band is a second parameter. sol-trend's
  Donchian channel is already the banded member of this family and is being
  tested on its own.
* **Metadata, for ``results._attribution``.** Every intent carries
  ``bar_close``, ``signal_day``, ``signal_close`` and ``sma``; a non-zero
  target carries ``session="long"|"short"`` and ``trade_n=1``; when the held
  position has the opposite sign to the target it carries
  ``exit_reason="flip"``; when the target is 0, or the position exceeds it
  on the same side, ``exit_reason="unsignalled"``. A flip intent therefore
  carries session, trade_n and exit_reason together, which is exactly what
  the engine's book reads from a flip-through-flat fill.
* **Replay flags.** ``--session all`` (under the default ``cme`` filter the
  aggregator's days would collapse to CME sessions), never ``--sessions``
  (the strategy refuses ``session_opens`` as an unknown parameter), and
  ``--max-order-size`` >= 2 x size.

Like the other bar strategies this is fed stored history by the backtest
engine and sampled 1-minute bars by the live runtime's bar builder; a rule on
daily closes is the one kind the sampled bars reproduce faithfully. Size can
be overridden DOWN (never up) via ``STRATEGY_POSITION_CONTRACTS``; size is
this rule's only risk control.
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
from app.strategy.daily import DailyAggregator, DailyBar, mean_close

_LOG = get_logger("strategy.sol_sma")


@dataclass(frozen=True, slots=True)
class SmaParams:
    """The rule's two numbers: how big, and how long a window."""

    #: 1,000 SOL / 25 SOL per contract -- the exposure sol-hold, sol-trend
    #: and sol-momentum carry in the harness, so the digest compares dollars
    #: like for like; P&L is exactly linear in size. Live is 1 contract via
    #: STRATEGY_POSITION_CONTRACTS: size is the ONLY risk control of a rule
    #: with no stop, so it stays at 1 until the rule has a record. Not a
    #: signal parameter.
    position_contracts: int = 40

    #: The one signal parameter, fixed before any replay for three reasons,
    #: none of which is a result. (1) Horizon: close minus SMA(N) weights the
    #: last N-1 daily returns linearly with a centre of mass near N/3 days,
    #: so 50 sits at ~17 days, inside the 1-4 week crypto time-series-
    #: momentum horizon. (2) Testability: on a random walk the sign of close
    #: minus SMA(50) changes ~0.08 times a day -- ~25 flips in the ~315
    #: post-warm-up days of a one-year replay, enough for the sign-flip null
    #: to have power (100 days gives ~15, 200 gives ~6, 20 gives ~43).
    #: (3) Checkability: the 50-day line is drawn by default on every chart.
    sma_days: int = 50


#: Overridable per REPLAY via ``--strategy-params``, exactly like the other
#: strategies' tunables: every override validated, every unknown key refused.
#:
#: Deliberately empty: the rule has no decimal-valued parameter -- no
#: multiple, no band, no distance. The table stays so the known-parameter
#: set and ``describe`` are built the same way as trend.py, and so adding
#: one later is a visible act rather than an accident.
_TUNABLE_DECIMALS: dict[str, tuple[Decimal, Decimal]] = {}
_TUNABLE_INTS: dict[str, tuple[int, int]] = {
    # 2 because SMA(1) IS the close and can never be crossed; 365 because
    # one year of data cannot warm up more. The range exists for the
    # pre-registered sign-agreement rows at 25 and 100 in
    # scripts/strategy_compare.sh and may never be used to move the setting.
    "sma_days": (2, 365),
}
_HANDLED_ELSEWHERE = frozenset({"position_contracts"})


def _apply_tunables(p: SmaParams, params: dict[str, Any]) -> SmaParams:
    """Override the defaults for one replay. Same contract as trend.py's:
    validated, echoed in ``describe``, unknowns refused elsewhere."""
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
    known = _HANDLED_ELSEWHERE | set(_TUNABLE_DECIMALS) | set(_TUNABLE_INTS)
    unknown = set(params) - known
    if unknown:
        raise ValueError(f"unknown strategy parameter(s) {sorted(unknown)}; known: {sorted(known)}")


class SolSmaStrategy(BarStrategy):
    """Observes 1-minute bars; holds the side of the 50-day average."""

    name = "sol-sma"

    def __init__(self, *, enabled: bool = True, params: dict[str, Any] | None = None) -> None:
        super().__init__(enabled=enabled, params=params)
        self._p = SmaParams()
        size = (params or {}).get("position_contracts")
        if size is not None:
            # DOWN only, same rule and same ceiling as the other strategies:
            # the live trial runs at 1 contract, and a larger size would
            # break comparability with the harness rows.
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

        #: Completed days, oldest first. mean_close INCLUDES the newest day,
        #: so the window itself is enough history.
        self._daily = DailyAggregator(keep=self._p.sma_days)
        self._position = 0
        """Changed ONLY in on_fill. Never set from an intent."""
        self._target = 0
        """Signed contracts the rule wants held. Changed ONLY by a completed day."""
        self._signal_day: date | None = None
        self._signal_close: Decimal | None = None
        self._sma: Decimal | None = None
        """The completed day, its close and the average that last CHANGED the
        target -- the decision every subsequent intent executes."""
        self._emitted_last_bar = False
        """Feeds the orders_reemitted and exits_unsignalled counters only:
        true when the previous on_bar emitted an intent and no fill at all has
        arrived since, so the next intent is that order sent again."""
        self._counts: dict[str, int] = {
            "days_completed": 0,
            "days_in_warmup": 0,
            "targets_long": 0,
            "targets_short": 0,
            "ties_held": 0,
            "flips": 0,
            "orders_emitted": 0,
            "orders_reemitted": 0,
            "fills_off_target": 0,
            "exits_unsignalled": 0,
        }

    @property
    def position(self) -> int:
        """Signed contracts this strategy believes it holds. See base class."""
        return self._position

    # ------------------------------------------------------------------
    # Fill feedback: the ONLY place position changes
    # ------------------------------------------------------------------

    def on_fill(self, *, side: OrderSide, quantity: int, price: Decimal) -> None:
        self._position += quantity * side.sign
        # Whatever the book does next is a new order, not the last one sent
        # again: a partial fill's top-up and an off-target correction are
        # fresh decisions and count as such.
        self._emitted_last_bar = False
        if self._position != self._target:
            # Every fill this strategy asks for lands the position exactly on
            # the target. Anything else -- a fill it never requested, a
            # partial, an over-fill -- is a routing or venue fault worth a
            # loud line; the next bar corrects toward the target regardless.
            self._counts["fills_off_target"] += 1
            _LOG.error(
                "fill left the position off target",
                extra={
                    "event": "sma.fill_off_target",
                    "position": str(self._position),
                    "target": str(self._target),
                    "price": str(price),
                },
            )

    # ------------------------------------------------------------------
    # Bar handling
    # ------------------------------------------------------------------

    def on_bar(self, bar: Bar) -> Sequence[TradeIntent]:
        completed = self._daily.feed(bar)
        if completed is not None:
            self._decide(completed)
        return self._reconcile(bar)

    # -- the decision, once per completed day ------------------------------

    def _decide(self, completed: DailyBar) -> None:
        """Set the target from the day that just completed. Nothing else."""
        self._counts["days_completed"] += 1
        sma = mean_close(self._daily.days, self._p.sma_days)
        if sma is None:
            self._counts["days_in_warmup"] += 1
            return
        close = completed.close
        size = self._p.position_contracts
        if close > sma:
            new = size
        elif close < sma:
            new = -size
        else:
            # A close AT the average is not a signal: hold whatever the
            # target already is, flat included.
            self._counts["ties_held"] += 1
            return
        if new == self._target:
            return
        self._counts["targets_long" if new > 0 else "targets_short"] += 1
        if self._target != 0:
            self._counts["flips"] += 1
        self._target = new
        self._signal_day = completed.day
        self._signal_close = close
        self._sma = sma
        _LOG.info(
            "sma target",
            extra={
                "event": "sma.target",
                "signal_day": completed.day.isoformat(),
                "close": str(close),
                "sma": str(sma),
                "target": str(new),
            },
        )

    # -- the order, on every bar the position is off target ----------------

    def _reconcile(self, bar: Bar) -> Sequence[TradeIntent]:
        """One intent for the target while the book disagrees with it; none
        otherwise, so a no_change refusal is never produced."""
        if self._position == self._target:
            self._emitted_last_bar = False
            return ()
        meta: dict[str, object] = {
            "signal_day": str(self._signal_day),
            "signal_close": str(self._signal_close),
            "sma": str(self._sma),
        }
        if self._target != 0:
            meta["session"] = "long" if self._target > 0 else "short"
            meta["trade_n"] = 1
        reason = self._exit_reason()
        if reason is not None:
            meta["exit_reason"] = reason
            if reason == "unsignalled" and not self._emitted_last_bar:
                self._counts["exits_unsignalled"] += 1
        self._counts["orders_emitted"] += 1
        if self._emitted_last_bar:
            self._counts["orders_reemitted"] += 1
        self._emitted_last_bar = True
        return (self._intent(self._target, bar, **meta),)

    def _exit_reason(self) -> str | None:
        """What the intent closes, if it closes anything.

        "flip" when the held position is on the opposite side to the target;
        "unsignalled" when the target is flat or the position exceeds it on
        the same side (a fill the rule never asked for); None for a pure
        entry or a same-side top-up.
        """
        if self._position == 0:
            return None
        if self._target == 0:
            return "unsignalled"
        if (self._position > 0) != (self._target > 0):
            return "flip"
        if abs(self._position) > abs(self._target):
            return "unsignalled"
        return None

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
            # Stamped with the bar's close time, same as the other strategies
            # and for the same reason: the decision became possible when the
            # minute ended, and the validator's staleness clock measures
            # from here.
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
            # What the rule WANTS beside what is held, so the live operator
            # and the reconcile log can read a disagreement at a glance.
            "target_position": self._target,
            "signal_day": None if self._signal_day is None else self._signal_day.isoformat(),
            "note": (
                f"close of the last completed UTC day versus its {self._p.sma_days}-day "
                "simple moving average: above -> long, below -> short, equal -> unchanged; "
                "one absolute target re-emitted until fills confirm; no stop, no target, "
                f"no trail; always in after {self._p.sma_days} completed days; days are UTC "
                "calendar days built from 1-minute bars, missing days skipped never "
                "invented. A change of side is ONE order of 2 x size."
            ),
        }


__all__ = ["SmaParams", "SolSmaStrategy"]
