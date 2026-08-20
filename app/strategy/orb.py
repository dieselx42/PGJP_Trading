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
exists in a stream of closes. Live, it is fed by
:class:`~app.market_data.bar_builder.BarBuilder`, whose bars are *sampled*
from polled quotes -- the range measures slightly narrow and the trail's peak
slightly low relative to exchange bars; see that module. Size can be
overridden DOWN (never up) via ``STRATEGY_POSITION_CONTRACTS``, so first paper
trades run at 1 contract before anything runs at the document's 40.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import Any

from app.backtest.models import Bar
from app.enums import Direction, OrderSide
from app.logging_config import get_logger
from app.signals.models import TradeIntent
from app.strategy.base import BarStrategy
from app.utilities.timeutils import eastern_hhmm

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


#: The document's numeric rules, each overridable in a REPLAY to answer "what
#: if this number were different". Decimal fields are per-SOL dollars.
_TUNABLE_DECIMALS: dict[str, tuple[Decimal, Decimal]] = {
    # name -> (exclusive minimum, inclusive maximum). Maxima are sanity rails,
    # not strategy opinions: $20 per SOL on a ~$200 asset is a 10% move.
    "stop_distance": (Decimal("0"), Decimal("20")),
    "target_distance": (Decimal("0"), Decimal("20")),
    "breakeven_trigger": (Decimal("0"), Decimal("20")),
    "breakeven_lock": (Decimal("0"), Decimal("20")),
    "trail_trigger": (Decimal("0"), Decimal("20")),
    "trail_width": (Decimal("0"), Decimal("20")),
    "min_orb_range": (Decimal("0"), Decimal("20")),
}
_TUNABLE_INTS: dict[str, tuple[int, int]] = {
    "entry_window_minutes": (1, 600),
    "max_trades_per_session": (1, 10),
    "orb_minutes": (1, 60),
}
_HANDLED_ELSEWHERE = frozenset({"position_contracts", "session_opens"})


def _apply_tunables(p: OrbParams, params: dict[str, Any]) -> OrbParams:
    """Override the document's numbers for one replay.

    Every override is validated and every unknown key is refused (see
    :func:`_reject_unknown_params`): a typo like ``stop_distnace`` silently
    ignored would report the BASELINE as if it were the experiment, which is
    worse than any crash. The effective parameters are echoed in ``describe``
    so a result always records exactly what ran.
    """
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


def _parse_session_opens(raw: object) -> tuple[time, ...]:
    """Parse ``"HH:MM"`` strings into session opens, refusing to guess.

    Accepts a list/tuple of strings or one comma-separated string. UTC by
    definition, like everything else in this system.
    """
    if isinstance(raw, str):
        parts: list[object] = [p.strip() for p in raw.split(",") if p.strip()]
    elif isinstance(raw, (list, tuple)):
        parts = list(raw)
    else:
        raise ValueError(f"session_opens must be 'HH:MM[,HH:MM...]', got {raw!r}")
    if not parts:
        raise ValueError("session_opens is empty; a strategy with no sessions never trades")
    opens: list[time] = []
    for part in parts:
        text = str(part).strip()
        try:
            hour, minute = text.split(":")
            opens.append(time(int(hour), int(minute)))
        except (ValueError, TypeError) as exc:
            raise ValueError(f"session open {text!r} is not a valid HH:MM time") from exc
    if len(set(opens)) != len(opens):
        raise ValueError(f"session_opens contains duplicates: {sorted(str(o) for o in opens)}")
    return tuple(sorted(opens))


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
    exit_reason: str | None = None
    """Why the exit was emitted ("stop"/"breakeven"/"trail"/"target").

    Stored on the trade rather than recomputed so a re-emitted exit -- after a
    cancelled fill on an untradeable bar -- reports the ORIGINAL reason, not
    whatever the stop level happens to look like a bar later.
    """


class SolOrbStrategy(BarStrategy):
    """Observes 1-minute bars; trades the document's ORB rules."""

    name = "sol-orb"

    def __init__(self, *, enabled: bool = True, params: dict[str, Any] | None = None) -> None:
        super().__init__(enabled=enabled, params=params)
        self._p = OrbParams()
        size = (params or {}).get("position_contracts")
        if size is not None:
            # DOWN only. The document's 40 is the specification's size, and a
            # smaller override exists for exactly one reason: first paper
            # trades should prove execution mechanics at 1 contract before
            # anything trades at 40. Scaling UP past the document would be a
            # different strategy wearing this one's name, so it is refused.
            size = int(size)
            if not 1 <= size <= self._p.position_contracts:
                raise ValueError(
                    f"position_contracts override must be within "
                    f"1..{self._p.position_contracts}, got {size}"
                )
            self._p = replace(self._p, position_contracts=size)
        sessions = (params or {}).get("session_opens")
        if sessions is not None:
            # A DIAGNOSTIC knob, reachable only from the backtest CLI -- the
            # live runtime never passes it. It exists because the document
            # contradicts itself about the NY open (14:30 UTC vs 9:30 ET, an
            # hour apart during US DST), and a replay at each candidate hour
            # answers the question with data instead of an argument. Malformed
            # input is refused: a session at a guessed time is a backtest of a
            # strategy nobody specified.
            self._p = replace(self._p, session_opens=_parse_session_opens(sessions))
        self._p = _apply_tunables(self._p, params or {})
        _reject_unknown_params(params or {})
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
        return (
            self._intent(
                target,
                bar,
                session=session.opened_at.strftime("%H:%M"),
                trade_n=session.trades_filled + 1,
            ),
        )

    # -- trade management ------------------------------------------------

    def _manage(self, bar: Bar) -> Sequence[TradeIntent]:
        trade = self._trade
        assert trade is not None
        if trade.exiting:
            # The exit should have filled at this bar's open and cleared
            # self._trade via on_fill. Still here means it was cancelled on an
            # untradeable bar: re-emit. An open position with no working exit
            # is not a state this strategy is ever willing to hold.
            return (self._intent(0, bar, exit_reason=trade.exit_reason or "unknown"),)

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
            # Stamped with the bar's CLOSE, because that is when the decision
            # became possible -- a bar's high and close do not exist until the
            # minute ends. Stamping the open would make every live intent
            # arrive ~60 seconds old, and the validator's staleness limit is
            # exactly 60 seconds: the system would sit armed and refuse every
            # signal it ever generated, silently. The backtest never showed
            # this because the replay validator has no staleness clock.
            created_at=bar.closed_at,
            metadata={"bar_close": str(bar.close), **meta},
        )

    def describe(self) -> dict[str, object]:
        return {
            **super().describe(),
            "position_contracts": self._p.position_contracts,
            "sessions_utc": [t.isoformat() for t in self._p.session_opens],
            # What those UTC opens read as on the team's clock TODAY. The
            # date matters: 14:30 UTC is 09:30 EST in winter and 10:30 EDT in
            # summer, and making that visible is the point -- the strategy
            # trades fixed UTC, and a team thinking in Eastern should see
            # exactly which local hour that lands on.
            "sessions_eastern_today": [eastern_hhmm(t) for t in self._p.session_opens],
            # The full effective rule set, so an experiment's result records
            # exactly what ran and two runs can never be confused.
            "params_effective": {
                name: str(getattr(self._p, name)) for name in (*_TUNABLE_DECIMALS, *_TUNABLE_INTS)
            },
            "counters": dict(self._counts),
            "note": (
                "NY session runs at the document's 14:30 UTC, which is 9:30 ET only in "
                "winter; confirm which clock the document's 5-year table used."
            ),
        }


__all__ = ["OrbParams", "SolOrbStrategy"]
