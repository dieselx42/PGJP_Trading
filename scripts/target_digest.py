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

The margin is also split into its two causes, which is what makes a failing
sweep informative rather than merely disappointing. A driftless random walk
hits ``+target`` before ``-stop`` with probability ``stop / (target + stop)``,
so that ratio is what an entry carrying NO directional information produces at
each geometry. Subtracting it from the measured hit rate leaves the entry's
edge in percentage points; the rest of the margin is ``cost / (target + stop)``,
the toll expressed on the same scale. The identity is exact:

    margin = (actual - coinflip) - cost / (target + stop)
           =        edge         -      cost term

An entry with no edge therefore fails by precisely the cost term, and the two
columns say which of the two problems a strategy actually has: no signal, or
an instrument too expensive to trade the signal on.
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
            print(
                f"  {label}: {ticks} slippage ticks/side -> "
                f"${cost_per_sol(ticks):.3f} per SOL round trip"
            )
    print()

    head = (
        f"{'target':>7} {'trades':>7} {'hit rate':>9} {'coinflip':>9} "
        f"{'edge':>6} {'cost':>6} {'needs':>7} {'margin':>8} {'net':>11}"
    )
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

        need_pess = required_hit_rate(stop, t, cost_per_sol(Decimal("3")))
        margin = actual - need_pess
        # What an entry carrying no information at all would hit, and the toll
        # on the same scale. The two account for the margin exactly.
        coinflip = stop / (t + stop)
        edge = actual - coinflip
        cost_term = cost_per_sol(Decimal("3")) / (t + stop)

        def net(tag: str, target: Decimal = t) -> str:
            doc = load(root / f"t{target}-{tag}.json")
            if doc is None:
                return "n/a"
            return f"{Decimal(str(doc.get('performance', {}).get('net_pnl', 0))):+,.0f}"

        print(
            f"{'$' + str(t):>7} {n:>7} {actual * 100:>8.1f}% {coinflip * 100:>8.1f}% "
            f"{edge * 100:>+6.1f} {cost_term * 100:>6.1f} {need_pess * 100:>6.1f}% "
            f"{margin * 100:>+8.1f} {net('net3'):>11}"
        )
        verdict_rows.append((t, margin, n, edge))

    print("=" * len(head))
    print()

    if verdict_rows:
        edges = [r[3] for r in verdict_rows]
        mean_edge = sum(edges) / len(edges)
        # Standard error of a hit rate near 25% on this many trades. A mean
        # edge inside it is not distinguishable from no edge at all.
        se = Decimal(str((0.25 * 0.75 / max(verdict_rows[0][2], 1)) ** 0.5))
        print(
            f"Mean edge over a coin flip: {mean_edge * 100:+.2f} points "
            f"(1 SE on a single row \u2248 {se * 100:.1f} points)."
        )
        print()

    clears = [r for r in verdict_rows if r[1] > 0]
    if not clears:
        print("No target clears its own break-even line. Read the edge column for")
        print("which problem this is. An edge hovering around zero means the entry")
        print("carries no directional information at any geometry, and no stop/target")
        print("pair can rescue an entry that behaves like a coin flip -- the family is")
        print("finished. An edge that is consistently positive but smaller than the")
        print("cost column means the entry works and the instrument is too expensive,")
        print("which is a different problem with a different fix. Either way this is")
        print("an answer, not a starting point for more tuning.")
    else:
        best = max(clears, key=lambda r: r[1])
        print(
            f"{len(clears)} target(s) clear the line; widest margin at ${best[0]} "
            f"({best[1] * 100:+.1f} pts, n={best[2]}, edge {best[3] * 100:+.1f} pts)."
        )
        print()
        print("This is a HYPOTHESIS, not a setting. It was chosen by looking at this")
        print("year's result, which is the definition of fitting. Confirm it on data")
        print("that was not examined before trading it, and check the margin against")
        print("the sign-flip null -- with n this small, a few points of margin is")
        print("well inside what randomised directions produce.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
