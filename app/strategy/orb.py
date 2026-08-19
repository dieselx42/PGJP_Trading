"""SOL 5-minute opening range breakout, per the written instructions.

Source: "SOL 5-Minute ORB Strategy -- Opening Range Breakout, London & NY
Sessions" (operator-supplied document, 2026-08). Parameters below are that
document's, verbatim. Where the document is ambiguous, the reading chosen is
recorded here, because a backtest of a strategy nobody can state precisely is
a number nobody can trust.

The rules, as implemented
-------------------------
* Two independent sessions: London 08:00 UTC, NY 14:30 UTC. Each lasts 90
  minutes for ENTRIES; an open trade is managed to its exit regardless.
* The first five 1-minute bars form the opening range (high/low across all
  five). Range under $0.80 -> the whole session is skipped.
* After the range is fixed: a 1-minute bar CLOSING strictly beyond it is a
  signal. A wick through is not. Entry is a market order on the signal.
* Stops: $0.65 from entry; at +$0.40 the stop moves to entry+$0.05; at +$0.65
  the $1.50 target is cancelled and a $0.40 trail follows the peak. The trail
  never loosens.
* At most 2 filled entries per session, one position at a time, and no entry
  while a trade from ANY session is still open.
* 1,000 SOL fixed = 40 MSL contracts at 25 SOL each. Never scaled.

Ambiguities resolved (and worth confirming against the document's author)
-------------------------------------------------------------------------
* **NY open.** The document says both "14:30 UTC" and "9:30 AM ET". Those
  coincide only in winter; during US DST 9:30 ET is 13:30 UTC. The UTC column
  is implemented as written. If the 5-year table was computed on exchange-local
  time, results for roughly two-thirds of the year describe a different hour
  than this replays.
* **Within-bar sequence.** Bars are not ticks: when one bar spans both the
  stop and a level upgrade, the order of events inside it is unknowable. The
  stop is checked FIRST, at its level from the previous bar. Pessimistic by
  construction: a bar that touched the old stop and then rallied is recorded
  as a stop-out.
* **Re-entry.** Any eligible bar closing beyond the range is a signal --
  including the bar after a stop-out, if price still sits outside the range.
  The document's "wait for a 1-minute candle to fully close outside the range"
  says nothing that forbids it, and 2-per-session bounds it.
* **Fills are the engine's, levels are from the real fill.** Entry executes at
  the NEXT bar's open plus slippage (see `app.backtest.broker`); the stop,
  break-even and trail are then computed from that actual fill price, exactly
  as the document instructs ("as soon as the order fills, set..."). Exits are
  likewise market orders filling on the next bar: this replay does not assume
  a resting stop filled at its exact price. Tight stops therefore cost more
  here than in a model that fills at the stop level; that is the pessimism,
  not a bug.
* **Data gaps.** If any of the five opening-range minutes is missing from the
  data, the session is skipped. A range measured from four bars is not the
  range, and inventing the fifth is how a backtest starts lying.

This is a :class:`~app.strategy.base.BarStrategy`: the range is a candle's
high and low and the trail follows the highest price REACHED, neither of which
exists in a stream of closes. The live runtime refuses to run it until a
tick-to-bar feed exists -- backtest first, wire live second.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import Any

from app.backtest.models import Bar
from app.enums import Direction, OrderSide
from app.logging_config import get_logger
from app.signals.models import TradeIntent
from app.strategy.base import BarStrategy

_LOG = get_logger("strategy.sol_orb")


@dataclass(frozen=True, slots=True)
class OrbParams:
    """The document's parameters, verbatim, in per-SOL dollars."""

    #: 1,000 SOL / 25 SOL per MSL contract. "Every single trade. No adjustments."
    position_contracts: int = 40

    #: Session opens, UTC. The document's UTC column; see the module docstring
    #: on the NY/DST ambiguity.
    session_opens: tuple[time, ...] = (time(8, 0), time(14, 30))

    orb_minutes: int = 5
    min_orb_range: Decimal = Decimal("0.80")
    entry_window_minutes: int = 90
    max_trades_per_session: int = 2

    stop_distance: Decimal = Decimal("0.65")
    target_distance: Decimal = Decimal("1.50")
    breakeven_trigger: Decimal = Decimal("0.40")
    breakeven_lock: Decimal = Decimal("0.05")
    trail_trigger: Decimal = Decimal("0.65")
    trail_width: Decimal = Decimal("0.40")


@dataclass
class _Session:
    """One session's lifecycle, from open to exhausted."""

    opened_at: datetime
    orb_bars: list[Bar] = field(default_factory=list)
    range_high: Decimal | None = None
    range_low: Decimal | None = None
    skipped: str | None = None
    trades_filled: int = 0

    @property
    def armed(self) -> bool:
        return self.range_high is not None and self.skipped is None


@dataclass
class _Trade:
    """The open position, managed bar by bar from its actual fill price."""

    direction: int  # +1 long, -1 short
    entry: Decimal
    stop: Decimal
    target: Decimal | None  # None once the trail replaces it
    peak: Decimal  # best price reached, in the trade's favour
    trailing: bool = False
    exiting: bool = False


class SolOrbStrategy(BarStrategy):
    """Observes 1-minute bars; trades the document's ORB rules."""

    name = "sol-orb"

    def __init__(self, *, enabled: bool = True, params: dict[str, Any] | None = None) -> None:
        super().__init__(enabled=enabled, params=params)
        self._p = OrbParams()
        self._session: _Session | None = None
        self._trade: _Trade | None = None
        self._position = 0
        self._pending_entry = False
        # Observability: why sessions produced nothing. Reported in describe()
        # so a replay's "no trades" has a stated cause, not a shrug.
        self._counts: dict[str, int] = {
            "sessions_seen": 0,
            "sessions_skipped_small_range": 0,
            "sessions_skipped_gap": 0,
            "signals_taken": 0,
            "signals_skipped_busy": 0,
            "signals_skipped_window": 0,
            "signals_skipped_session_full": 0,
            "entries_cancelled_unfilled": 0,
            "exits_stop": 0,
            "exits_breakeven": 0,
            "exits_trail": 0,
            "exits_target": 0,
        }

    # ------------------------------------------------------------------
    # Fill feedback: the ONLY place position and entry price come from
    # ------------------------------------------------------------------

    def on_fill(self, *, side: OrderSide, quantity: int, price: Decimal) -> None:
        previous = self._position
        self._position += quantity * side.sign

        if previous == 0 and self._position != 0:
            # Entry filled: levels are set from the REAL fill, per the
            # document ("as soon as the order fills, set...").
            direction = 1 if self._position > 0 else -1
            self._trade = _Trade(
                direction=direction,
                entry=price,
                stop=price - self._p.stop_distance * direction,
                target=price + self._p.target_distance * direction,
                peak=price,
            )
            self._pending_entry = False
            if self._session is not None:
                self._session.trades_filled += 1
        elif self._position == 0:
            self._trade = None

    # ------------------------------------------------------------------
    # Bar handling
    # ------------------------------------------------------------------

    def on_bar(self, bar: Bar) -> Sequence[TradeIntent]:
        self._roll_session(bar)

        if self._pending_entry:
            # The entry emitted on the previous bar should have filled at this
            # bar's open, before this method ran. Still flat means the order
            # was cancelled (an untradeable bar); the slot is not consumed and
            # the machine must not wedge waiting for a fill that never comes.
            self._pending_entry = False
            if self._position == 0:
                self._counts["entries_cancelled_unfilled"] += 1

        if self._trade is not None:
            # The document: "if trade 1 is still running when a second signal
            # appears, skip the second signal." Skipping happens by routing --
            # entry logic never runs while a trade is open -- but the missed
            # signal is still counted, so a replay can say how often the
            # one-at-a-time rule actually cost an entry.
            self._count_missed_signal(bar)
            return self._manage(bar)
        return self._maybe_enter(bar)

    def _count_missed_signal(self, bar: Bar) -> None:
        session = self._session
        if session is None or not session.armed:
            return
        assert session.range_high is not None and session.range_low is not None
        breaks_out = bar.close > session.range_high or bar.close < session.range_low
        in_window = bar.opened_at < session.opened_at + timedelta(
            minutes=self._p.entry_window_minutes
        )
        if breaks_out and in_window and session.trades_filled < self._p.max_trades_per_session:
            self._counts["signals_skipped_busy"] += 1

    # -- session lifecycle -------------------------------------------------

    def _roll_session(self, bar: Bar) -> None:
        at = bar.opened_at
        for open_time in self._p.session_opens:
            nominal = at.replace(
                hour=open_time.hour, minute=open_time.minute, second=0, microsecond=0
            )
            # The session is keyed on its NOMINAL open, and starts on any bar
            # inside the opening-range span -- so a missing 08:00 bar still
            # starts the session (which then skips itself for the gap) rather
            # than the session silently never existing.
            in_orb_span = nominal <= at < nominal + timedelta(minutes=self._p.orb_minutes)
            already = self._session is not None and self._session.opened_at == nominal
            if in_orb_span and not already:
                self._session = _Session(opened_at=nominal)
                self._counts["sessions_seen"] += 1
                break

        session = self._session
        if session is None or session.skipped is not None:
            return

        elapsed = at - session.opened_at
        orb_span = timedelta(minutes=self._p.orb_minutes)

        if elapsed < orb_span:
            session.orb_bars.append(bar)
            return

        if session.range_high is None:
            # First bar past the opening range: fix it, or skip the session.
            if len(session.orb_bars) < self._p.orb_minutes:
                session.skipped = "gap_in_opening_range"
                self._counts["sessions_skipped_gap"] += 1
                return
            high = max(b.high for b in session.orb_bars)
            low = min(b.low for b in session.orb_bars)
            if high - low < self._p.min_orb_range:
                session.skipped = "range_below_minimum"
                self._counts["sessions_skipped_small_range"] += 1
                return
            session.range_high = high
            session.range_low = low
            _LOG.info(
                "opening range fixed",
                extra={
                    "event": "orb.range_fixed",
                    "session": session.opened_at.isoformat(),
                    "high": str(high),
                    "low": str(low),
                },
            )

    # -- entries -------------------------------------------------------------

    def _maybe_enter(self, bar: Bar) -> Sequence[TradeIntent]:
        session = self._session
        if session is None or not session.armed:
            return ()
        assert session.range_high is not None and session.range_low is not None

        if bar.close > session.range_high:
            direction = 1
        elif bar.close < session.range_low:
            direction = -1
        else:
            return ()

        # A signal fired. Every reason to skip it is counted, because "the
        # strategy traded less than the document promised" needs a why. The
        # window and session-full cases also END the session -- no later bar
        # can revive it -- so they count once, not once per bar.
        if bar.opened_at >= session.opened_at + timedelta(minutes=self._p.entry_window_minutes):
            session.skipped = "entry_window_closed"
            self._counts["signals_skipped_window"] += 1
            return ()
        if session.trades_filled >= self._p.max_trades_per_session:
            session.skipped = "session_trade_limit_reached"
            self._counts["signals_skipped_session_full"] += 1
            return ()
        self._pending_entry = True
        self._counts["signals_taken"] += 1
        target = self._p.position_contracts * direction
        return (self._intent(target, bar),)

    # -- trade management ------------------------------------------------

    def _manage(self, bar: Bar) -> Sequence[TradeIntent]:
        trade = self._trade
        assert trade is not None
        if trade.exiting:
            # The exit should have filled at this bar's open and cleared
            # self._trade via on_fill. Still here means it was cancelled on an
            # untradeable bar: re-emit. An open position with no working exit
            # is not a state this strategy is ever willing to hold.
            return (self._intent(0, bar),)

        d = trade.direction
        favourable = bar.high if d > 0 else bar.low
        adverse = bar.low if d > 0 else bar.high

        # 1. Stop first, at its level from the PREVIOUS bar. Bars are not
        #    ticks: when one bar spans both the stop and an upgrade, the
        #    pessimistic reading -- adverse extreme first -- is taken.
        if (adverse - trade.stop) * d <= 0:
            reason = (
                "exits_trail"
                if trade.trailing
                else "exits_breakeven"
                if (trade.stop - trade.entry) * d > 0
                else "exits_stop"
            )
            return self._exit(trade, bar, reason)

        gain = (favourable - trade.entry) * d

        # 2. The resting target: while it exists, a touch fills it. Checked
        #    before trail activation because the $1.50 order is already at the
        #    exchange; nobody cancels it mid-bar.
        if trade.target is not None and gain >= self._p.target_distance:
            return self._exit(trade, bar, "exits_target")

        # 3. Trail activation: cancel the target, follow the peak.
        if not trade.trailing and gain >= self._p.trail_trigger:
            trade.trailing = True
            trade.target = None

        # 4. Break-even lock.
        if gain >= self._p.breakeven_trigger:
            locked = trade.entry + self._p.breakeven_lock * d
            trade.stop = max(trade.stop, locked) if d > 0 else min(trade.stop, locked)

        # 5. Trail follows the peak, never loosens.
        if trade.trailing:
            trade.peak = max(trade.peak, favourable) if d > 0 else min(trade.peak, favourable)
            trailed = trade.peak - self._p.trail_width * d
            trade.stop = max(trade.stop, trailed) if d > 0 else min(trade.stop, trailed)

        return ()

    def _exit(self, trade: _Trade, bar: Bar, reason: str) -> Sequence[TradeIntent]:
        trade.exiting = True
        self._counts[reason] += 1
        return (self._intent(0, bar),)

    # -- plumbing ----------------------------------------------------------

    def _intent(self, target: int, bar: Bar) -> TradeIntent:
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
            created_at=bar.opened_at,
            metadata={"bar_close": str(bar.close)},
        )

    def describe(self) -> dict[str, object]:
        return {
            **super().describe(),
            "position_contracts": self._p.position_contracts,
            "sessions_utc": [t.isoformat() for t in self._p.session_opens],
            "counters": dict(self._counts),
            "note": (
                "NY session runs at the document's 14:30 UTC, which is 9:30 ET only in "
                "winter; confirm which clock the document's 5-year table used."
            ),
        }


__all__ = ["OrbParams", "SolOrbStrategy"]
