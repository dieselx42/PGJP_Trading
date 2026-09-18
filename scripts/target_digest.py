#!/usr/bin/env python3
"""Read a target sweep and say whether any geometry clears its own line.

The comparison is between two numbers that move in opposite directions as the
target widens:

* the REQUIRED hit rate, ``(stop + cost) / (target + stop)``, which falls
  because a bigger win pays for more losers;
* the ACTUAL hit rate, measured with costs off, which falls because a further
  target is reached less often.

An edge exists only where actual exceeds required. Printing both as a single
margin keeps the reader from doing what everyone does with a sweep, which is
to find the largest net figure and call it the setting.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from decimal import Decimal

#: Commission per contract per side, from a live IBKR preview on MSLV6, over
#: the 25 SOL that contract controls.
COMMISSION_PER_SOL = Decimal("3.41") * 2 / 25
TICK = Decimal("0.05")

#: Slippage ticks per side -> label. The measured MSL spread is 5 ticks, so
#: crossing it costs 2.5 ticks a side; the integer flag brackets that rather
#: than hitting it, and both ends are reported so nobody reads one as precise.
COST_CASES = {
    "hit": (Decimal("0"), "no costs"),
    "net2": (Decimal("2"), "optimistic"),
    "net3": (Decimal("3"), "pessimistic"),
}


def cost_per_sol(slip_ticks: Decimal) -> Decimal:
    """Round-trip cost per SOL: commission both sides plus slippage both sides."""
    return COMMISSION_PER_SOL + slip_ticks * TICK * 2


def required_hit_rate(stop: Decimal, target: Decimal, cost: Decimal) -> Decimal:
    """p such that p*(target - cost) == (1 - p)*(stop + cost)."""
    return (stop + cost) / (target + stop)


def load(path: pathlib.Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("directory")
    ap.add_argument("--stop", default="1.00")
    args = ap.parse_args()

    root = pathlib.Path(args.directory)
    stop = Decimal(args.stop)

    targets: list[Decimal] = sorted(
        {
            Decimal(m.group(1))
            for p in root.glob("t*-hit.json")
            if (m := re.fullmatch(r"t(\d+\.\d+)-hit", p.stem))
        }
    )
    if not targets:
        print(f"no sweep results found in {root}", file=sys.stderr)
        return 1

    for label, (ticks, _) in COST_CASES.items():
        if label != "hit":
            print(f"  {label}: {ticks} slippage ticks/side -> "
                  f"${cost_per_sol(ticks):.3f} per SOL round trip")
    print()

    head = (f"{'target':>7} {'trades':>7} {'hit rate':>9} "
            f"{'needs (opt)':>12} {'needs (pess)':>13} {'margin':>8} "
            f"{'net (opt)':>11} {'net (pess)':>11}")
    print("=" * len(head))
    print(head)
    print("-" * len(head))

    verdict_rows = []
    for t in targets:
        hit_doc = load(root / f"t{t}-hit.json")
        if hit_doc is None:
            continue
        trades = hit_doc.get("trades", {})
        n = trades.get("count", 0)
        rate = trades.get("win_rate")
        if rate is None or not n:
            print(f"{'$' + str(t):>7} {n:>7}   no trades")
            continue
        actual = Decimal(str(rate))

        need_opt = required_hit_rate(stop, t, cost_per_sol(Decimal("2")))
        need_pess = required_hit_rate(stop, t, cost_per_sol(Decimal("3")))
        margin = actual - need_pess

        def net(tag: str) -> str:
            doc = load(root / f"t{t}-{tag}.json")
            if doc is None:
                return "n/a"
            return f"{Decimal(str(doc.get('performance', {}).get('net_pnl', 0))):+,.0f}"

        print(
            f"{'$' + str(t):>7} {n:>7} {actual * 100:>8.1f}% "
            f"{need_opt * 100:>11.1f}% {need_pess * 100:>12.1f}% "
            f"{margin * 100:>+7.1f} {net('net2'):>11} {net('net3'):>11}"
        )
        verdict_rows.append((t, margin, n))

    print("=" * len(head))
    print()

    clears = [r for r in verdict_rows if r[1] > 0]
    if not clears:
        print("No target clears its own break-even line. The entry has no edge at")
        print("any geometry tested -- widening the target lowers the bar more slowly")
        print("than it lowers the hit rate. This is a finished answer, not a")
        print("starting point for more tuning.")
    else:
        best = max(clears, key=lambda r: r[1])
        print(f"{len(clears)} target(s) clear the line; widest margin at ${best[0]} "
              f"({best[1] * 100:+.1f} pts, n={best[2]}).")
        print()
        print("This is a HYPOTHESIS, not a setting. It was chosen by looking at this")
        print("year's result, which is the definition of fitting. Confirm it on data")
        print("that was not examined before trading it, and check the margin against")
        print("the sign-flip null -- with n this small, a few points of margin is")
        print("well inside what randomised directions produce.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
