"""Metrics over a completed replay.

Every number here is derived from `BacktestRun` and nothing else, so a result
can be recomputed from a stored run and cannot drift from what the replay
actually did.

The report leads with its own limitations rather than burying them. A backtest
result that does not say what it could not model is a number people will quote
without the caveat, and the caveat is usually the important part.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise
from math import sqrt
from typing import Final

from app.backtest.engine import BacktestRun

#: 1-minute bars in a 365-day year, used to annualise. Approximate by nature:
#: a futures contract does not trade every minute of every day, and the figure
#: is reported alongside the Sharpe it produced so nobody has to guess.
_MINUTES_PER_YEAR: Final = 365 * 24 * 60

#: Above this share of bars with no trades, the venue is too thin at this
#: interval for a result to mean much, and the note leads rather than trails.
_THIN_SERIES_SHARE: Final = 0.25

_BARS_PER_YEAR: Final[dict[str, int]] = {
    "1m": _MINUTES_PER_YEAR,
    "5m": _MINUTES_PER_YEAR // 5,
    "15m": _MINUTES_PER_YEAR // 15,
    "1h": 365 * 24,
    "1d": 365,
}


@dataclass(frozen=True, slots=True)
class BacktestReport:
    run: BacktestRun

    # -- headline ------------------------------------------------------
    net_pnl: Decimal
    realized_pnl: Decimal
    commission_paid: Decimal
    slippage_paid: Decimal
    max_drawdown: Decimal
    sharpe: float | None

    # -- trades --------------------------------------------------------
    trade_count: int
    win_count: int
    loss_count: int
    win_rate: float | None
    average_win: Decimal | None
    average_loss: Decimal | None

    # -- behaviour -----------------------------------------------------
    exposure: float
    refusal_count: int
    refusals_by_reason: dict[str, int]

    def describe(self) -> dict[str, object]:
        run = self.run
        return {
            "data": {
                "symbol": run.symbol,
                "source": run.source,
                "interval": run.interval,
                "bars": run.bars_seen,
                "bars_tradeable": run.bars_tradeable,
                "bars_without_trades": run.bars_without_trades,
                "volume_reported": run.volume_reported,
                "first_bar": run.first_bar,
                "last_bar": run.last_bar,
                "is_proxy_data": run.is_proxy_data,
            },
            "performance": {
                "net_pnl": str(self.net_pnl),
                "realized_pnl": str(self.realized_pnl),
                "commission_paid": str(self.commission_paid),
                "slippage_paid": str(self.slippage_paid),
                "max_drawdown": str(self.max_drawdown),
                "sharpe": self.sharpe,
                "sharpe_basis": f"{_BARS_PER_YEAR.get(run.interval, 0)} bars per year",
            },
            "trades": {
                "count": self.trade_count,
                "wins": self.win_count,
                "losses": self.loss_count,
                "win_rate": self.win_rate,
                "average_win": None if self.average_win is None else str(self.average_win),
                "average_loss": None if self.average_loss is None else str(self.average_loss),
                "final_position": run.final_position,
                "detail": [t.describe() for t in run.trades[:50]],
                "detail_truncated": max(0, len(run.trades) - 50),
            },
            "refusals": {
                "count": self.refusal_count,
                "by_reason": self.refusals_by_reason,
                "detail": [r.describe() for r in run.refusals[:50]],
                "detail_truncated": max(0, len(run.refusals) - 50),
                "note": (
                    "a refusal is the risk manager or the transmit gate doing its job. "
                    "A strategy whose results depend on these not firing will not perform "
                    "that way live."
                ),
            },
            "model": {
                **run.fill_model.describe(),
                "session_filtered": run.session_filtered,
                "fills_at": "the NEXT bar's open, never the current bar's close",
            },
            "limitations": _limitations(run),
        }


def _limitations(run: BacktestRun) -> list[str]:
    """Stated in the output, because a result without them gets misquoted.

    Ordered by how badly a reader would be misled without them, most severe
    first, rather than by the order the checks happen to run in. Wrong
    *instrument* outranks wrong *fills*: a spot result mistaken for a futures
    one is wrong about what was traded at all.
    """
    leading: list[str] = []
    trailing: list[str] = []

    if run.is_proxy_data:
        leading.append(
            f"THIS RAN ON {run.source.upper()} SPOT DATA, NOT CME FUTURES. No basis, no "
            "roll, no CME session breaks, and volume that does not reflect the futures "
            "book. Useful for developing a strategy; not a fill estimate for MSL."
        )

    share = run.bars_without_trades / run.bars_seen if run.bars_seen else 0.0
    if not run.volume_reported:
        leading.append(
            "THIS SOURCE REPORTS NO VOLUME, so bars where nothing traded could not be "
            "identified and were all treated as tradeable. On a thin venue that "
            "manufactures fills at prices nobody could have got."
        )
    elif run.bars_without_trades:
        note = (
            f"{run.bars_without_trades} of {run.bars_seen} bars ({share:.1%}) had zero "
            "volume: nothing traded, so they were not tradeable here and pending orders "
            "were cancelled against them rather than filled."
        )
        if share >= _THIN_SERIES_SHARE:
            leading.append(
                note + " That is a large share of the history. This venue is too thin at "
                "this interval for the result to mean much; prefer a deeper book or a "
                "longer bar interval."
            )
        else:
            trailing.append(note)

    notes = [
        *leading,
        "Bars are not ticks: intrabar highs and lows are never traded against, so some "
        "orders that would have filled in reality did not fill here.",
        "A bar has no spread: bid and ask are synthesised around the close. Real spreads "
        "widen exactly when a strategy most wants to trade.",
        "Fills assume the full size was available at the next open. Depth is not modelled.",
        *trailing,
    ]
    if not run.session_filtered:
        notes.append(
            "No session filter was applied, so bars outside CME trading hours were traded. "
            "Pass a session filter for a futures-realistic run."
        )
    if run.final_position != 0:
        notes.append(
            f"The replay ended holding {run.final_position} contract(s). That position is "
            "marked to the last close and never actually exited, so its P&L is unrealised."
        )
    return notes


def build_report(run: BacktestRun) -> BacktestReport:
    wins = [t for t in run.trades if t.net_pnl > 0]
    losses = [t for t in run.trades if t.net_pnl < 0]
    equity = [value for _, value in run.equity_curve]

    return BacktestReport(
        run=run,
        net_pnl=run.realized_pnl - run.commission_paid,
        realized_pnl=run.realized_pnl,
        commission_paid=run.commission_paid,
        slippage_paid=run.slippage_paid,
        max_drawdown=_max_drawdown(equity),
        sharpe=_sharpe(equity, interval=run.interval),
        trade_count=len(run.trades),
        win_count=len(wins),
        loss_count=len(losses),
        win_rate=(len(wins) / len(run.trades)) if run.trades else None,
        average_win=(sum((t.net_pnl for t in wins), Decimal(0)) / len(wins)) if wins else None,
        average_loss=(
            (sum((t.net_pnl for t in losses), Decimal(0)) / len(losses)) if losses else None
        ),
        exposure=_exposure(run),
        refusal_count=len(run.refusals),
        refusals_by_reason=_by_reason(run),
    )


def _max_drawdown(equity: list[Decimal]) -> Decimal:
    """Largest peak-to-trough fall in the equity curve."""
    if not equity:
        return Decimal(0)
    peak = equity[0]
    worst = Decimal(0)
    for value in equity:
        peak = max(peak, value)
        worst = min(worst, value - peak)
    return abs(worst)


def _sharpe(equity: list[Decimal], *, interval: str) -> float | None:
    """Annualised Sharpe of per-bar P&L changes.

    Returns ``None`` rather than a number when there is nothing to measure --
    fewer than two points, or no variation at all. A Sharpe of zero and "not
    computable" are different statements and should not share a value.
    """
    if len(equity) < 2:
        return None
    changes = [float(b - a) for a, b in pairwise(equity)]
    n = len(changes)
    mean = sum(changes) / n
    variance = sum((c - mean) ** 2 for c in changes) / n
    if variance <= 0:
        return None
    periods = _BARS_PER_YEAR.get(interval)
    if not periods:
        return None
    return (mean / sqrt(variance)) * sqrt(periods)


def _exposure(run: BacktestRun) -> float:
    """Fraction of the replay's bars spent holding a position.

    The denominator is every bar, not just the tradeable ones, because a
    position is still held -- and still at risk -- through a session break.

    Deltas are summed per timestamp rather than looked up one-to-one: several
    orders can settle against the same bar open, and keeping only one of them
    would leave this walking a position the replay never held.
    """
    if not run.equity_curve:
        return 0.0
    deltas: dict[str, int] = {}
    for fill in run.fills:
        deltas[fill.filled_at] = deltas.get(fill.filled_at, 0) + fill.quantity * fill.side.sign
    held = 0
    position = 0
    for at, _ in run.equity_curve:
        position += deltas.get(at, 0)
        if position != 0:
            held += 1
    return held / len(run.equity_curve)


def _by_reason(run: BacktestRun) -> dict[str, int]:
    counts: dict[str, int] = {}
    for refusal in run.refusals:
        for reason in refusal.reasons:
            counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


__all__ = ["BacktestReport", "build_report"]
