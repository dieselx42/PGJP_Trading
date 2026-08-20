#!/usr/bin/env python3
"""Compare saved ORB backtest results as one small, paste-friendly table.

Separate from the runner so results can be re-digested without re-replaying,
and so a single run's json can be summarised on its own.

Reads the json `app.cli backtest` writes: `performance`, `trades`,
`attribution`, and `strategy.counters`. Prints roughly 90% less than the full
report while keeping every figure a decision actually turns on.

Usage:
    scripts/orb_digest.py orb-experiments/          # a directory of runs
    scripts/orb_digest.py one-run.json              # a single run
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

#: Compared against every other run. Chosen because it is the document
#: verbatim: a delta only means something relative to what was specified.
BASELINE = "e1-baseline"

#: Sum of the column widths in the table below, so the rules cannot drift
#: out of step with the row format the way two hand-counted literals do.
WIDTH = 26 + 12 + 12 + 12 + 8 + 7 + 13


def _load(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        print(f"  ! {path.name}: unreadable ({exc})", file=sys.stderr)
        return None


def _money(raw: object) -> Decimal:
    try:
        return Decimal(str(raw))
    except ArithmeticError:
        return Decimal(0)


def _row(name: str, doc: dict) -> dict:
    perf, trades = doc.get("performance", {}), doc.get("trades", {})
    net = _money(perf.get("net_pnl"))
    costs = _money(perf.get("commission_paid")) + _money(perf.get("slippage_paid"))
    win_rate = trades.get("win_rate")
    return {
        "name": name,
        "net": net,
        # Gross is reconstructed rather than read: the report states net and
        # costs, and net+costs is the only definition of gross consistent
        # with both. Shown because "edge before costs" is the question E0 asks.
        "gross": net + costs,
        "costs": costs,
        "trades": trades.get("count", 0),
        "win_rate": win_rate,
        "max_dd": _money(perf.get("max_drawdown")),
    }


def _fmt(value: Decimal) -> str:
    return f"{value:>12,.0f}"


def main(argv: list[str]) -> int:
    target = Path(argv[1] if len(argv) > 1 else "orb-experiments")
    if target.is_file():
        paths = [target]
    elif target.is_dir():
        paths = sorted(target.glob("*.json"))
    else:
        print(f"no such file or directory: {target}", file=sys.stderr)
        return 2
    if not paths:
        print(f"no result json found in {target}", file=sys.stderr)
        return 1

    docs = {p.stem: d for p in paths if (d := _load(p)) is not None}
    if not docs:
        return 1
    rows = [_row(name, doc) for name, doc in sorted(docs.items())]
    base = next((r for r in rows if r["name"] == BASELINE), None)
    # A lone result is its own baseline. The commonest use is digesting one
    # run, and there the attribution is the whole point -- withholding it
    # because the file was not named `e1-baseline` would be pedantry.
    baseline_key = BASELINE if base is not None else (rows[0]["name"] if len(rows) == 1 else None)
    if base is None and baseline_key is not None:
        base = rows[0]

    print("=" * WIDTH)
    print(f"{'experiment':<26}{'net':>12}{'gross':>12}{'costs':>12}"
          f"{'trades':>8}{'win%':>7}{'vs base':>13}")
    print("-" * WIDTH)
    for r in rows:
        win = "  n/a" if r["win_rate"] is None else f"{r['win_rate'] * 100:5.1f}"
        delta = ""
        if base is not None and r["name"] != BASELINE:
            delta = f"{r['net'] - base['net']:>+13,.0f}"
        print(f"{r['name']:<26}{_fmt(r['net'])}{_fmt(r['gross'])}{_fmt(r['costs'])}"
              f"{r['trades']:>8}{win:>7}{delta:>13}")
    print("=" * WIDTH)

    # The attribution is the point of the exercise, so it is printed in full
    # for the baseline rather than summarised into the table above.
    if baseline_key is not None and (attr := docs[baseline_key].get("attribution")):
        for slice_name in ("by_exit_reason", "by_session", "by_trade_number"):
            buckets = attr.get(slice_name) or []
            if not buckets:
                continue
            print(f"\n{baseline_key} :: {slice_name}  (worst first)")
            for b in buckets:
                share = b.get("share_of_net")
                share_txt = "     n/a" if share is None else f"{share * 100:7.1f}%"
                print(f"  {str(b['bucket']):<22}"
                      f"n={b['count']:<6}wins={b['wins']:<6}"
                      f"net={_money(b['net']):>12,.0f}"
                      f"  avg={_money(b['avg_net']):>9,.0f}  {share_txt}")
        counters = docs[baseline_key].get("strategy", {}).get("counters", {})
        # Zero-valued counters are dropped: a dozen zeroes bury the two or
        # three numbers that explain why the strategy traded less than the
        # document promised.
        if live := {k: v for k, v in counters.items() if v}:
            print(f"\n{baseline_key} :: counters (non-zero)")
            for key, value in live.items():
                print(f"  {key:<34}{value}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except BrokenPipeError:
        # Piping to `head`/`less` closes stdout early. That is the reader's
        # choice, not an error; without this it prints a traceback over the
        # output it just produced.
        sys.stderr.close()
        raise SystemExit(0) from None
