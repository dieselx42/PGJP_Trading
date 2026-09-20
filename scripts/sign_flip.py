#!/usr/bin/env python3
"""The sign-flip null for an always-in rule, applied to one saved replay.

``docs/STRATEGY_ANALYSIS.md`` section 7 names this as the one inference with
power on a year of data: keep the rule's own dates, randomise only the
DIRECTION, and ask how often random directions would have done at least as
well. For a state rule such as sol-sma the natural unit is the SEGMENT --
each closed trade, plus the position still open when the replay ended -- so
the statistic is

    G  = sum over segments of gross P&L, in $/SOL
    G* = sum of s_i x gross_i over independent equiprobable signs s_i

and p = P(G* >= G). Every segment costs the same under every sign assignment
(an always-in rule pays one flip per boundary whatever the direction), so p
on gross equals p on net, and "beats the 95th percentile of the null" is the
same statement as p < 0.05.

Per SOL, not per contract, so the number does not depend on the harness
size: gross_pnl / (25 x quantity).

Exact when n <= 30: every one of the 2^n sign vectors is counted, by
meet-in-the-middle, so 2^30 is never materialised. Above that, 200,000
draws from a seed fixed before any replay ran. The 5th and 95th percentiles
of G* are printed beside p so a reader can see where the observed sum sits.

Caveat that travels with the number: the segment boundaries are the dates
on which the rule's OWN sign changed, so they are not independent of the
realised direction; the test is approximate, and it is the one pre-registered
for this candidate (S3 and K1 in app/strategy/sma.py's plan).

Usage:
    scripts/sign_flip.py strategy-compare/f0-sma-zerocost.json
"""

from __future__ import annotations

import json
import math
import random
import sys
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

SOL_PER_CONTRACT = Decimal(25)

#: Fixed before any replay ran, so the Monte Carlo branch is reproducible.
SEED = 20260919
DRAWS = 200_000

#: Up to this many segments every sign vector is enumerated; above, drawn.
EXACT_UP_TO = 30

#: The pre-registered thresholds, printed beside the number so the reading
#: cannot drift from the plan. Both require n >= MIN_SEGMENTS to be read.
MIN_SEGMENTS = 15
SUCCESS_P = Decimal("0.05")
KILL_P = Decimal("0.95")


@dataclass(frozen=True, slots=True)
class SignFlipResult:
    n: int
    total: Decimal
    """G: the observed sum of per-segment gross, $/SOL."""
    mean: Decimal
    p_high: Decimal
    """P(G* >= G): small when the rule's own directions beat random ones."""
    p_low: Decimal
    """P(G* <= G): small when random directions beat the rule's own."""
    pct5: Decimal
    pct95: Decimal
    """The q-th percentile is the smallest v with P(G* <= v) >= q."""
    method: str
    """``exact`` (all 2^n sign vectors) or ``monte-carlo`` (DRAWS draws)."""
    configurations: int


def segments_from_report(doc: dict[str, object]) -> list[Decimal]:
    """Per-segment gross in $/SOL: every closed trade, then the open tail.

    Refuses a truncated trade list rather than testing part of a year: the
    report caps ``trades.detail`` at 50 rows, and a strategy that exceeds it
    needs the cap lifted, not a silent subset.
    """
    trades = doc["trades"]
    assert isinstance(trades, dict)
    if trades.get("detail_truncated", 0):
        raise ValueError(
            f"trades.detail is truncated by {trades['detail_truncated']} rows; "
            "lift the 50-row cap in app/backtest/results.py before testing"
        )
    segments = [
        Decimal(str(t["gross_pnl"])) / (SOL_PER_CONTRACT * int(t["quantity"]))
        for t in trades["detail"]
    ]
    final_position = int(trades.get("final_position", 0))
    if final_position != 0:
        performance = doc["performance"]
        assert isinstance(performance, dict)
        unrealized = performance.get("final_unrealized")
        if unrealized is None:
            raise ValueError(
                "the replay ended holding a position but performance.final_unrealized "
                "is missing; re-run with a build that reports it"
            )
        segments.append(Decimal(str(unrealized)) / (SOL_PER_CONTRACT * abs(final_position)))
    return segments


def sign_flip(segments: list[Decimal], *, seed: int = SEED, draws: int = DRAWS) -> SignFlipResult:
    if not segments:
        raise ValueError("no segments: nothing to test")
    n = len(segments)
    total = sum(segments, Decimal(0))
    if n <= EXACT_UP_TO:
        return _exact(segments, total)
    return _monte_carlo(segments, total, seed=seed, draws=draws)


# -- exact: meet in the middle -------------------------------------------


def _half_sums(values: list[Decimal]) -> list[Decimal]:
    """Every +/- sum of ``values``, one entry per sign vector, sorted.

    An empty half has exactly one configuration, the empty sum -- which is
    what makes the pairing below count 2^n for every n, including n=1.
    """
    sums = [Decimal(0)]
    for v in values:
        sums = [s + v for s in sums] + [s - v for s in sums]
    sums.sort()
    return sums


def _count_at_most(a: list[Decimal], b: list[Decimal], x: Decimal) -> int:
    return sum(bisect_right(b, x - v) for v in a)


def _count_at_least(a: list[Decimal], b: list[Decimal], x: Decimal) -> int:
    return sum(len(b) - bisect_left(b, x - v) for v in a)


def _successor(a: list[Decimal], b: list[Decimal], x: Decimal, *, strict: bool) -> Decimal | None:
    """The smallest pair sum >= x (> x when strict), or None."""
    best: Decimal | None = None
    for v in a:
        i = bisect_right(b, x - v) if strict else bisect_left(b, x - v)
        if i < len(b) and (best is None or v + b[i] < best):
            best = v + b[i]
    return best


def _predecessor(a: list[Decimal], b: list[Decimal], x: Decimal) -> Decimal | None:
    """The largest pair sum < x, or None."""
    best: Decimal | None = None
    for v in a:
        i = bisect_left(b, x - v) - 1
        if i >= 0 and (best is None or v + b[i] > best):
            best = v + b[i]
    return best


def _kth_smallest(a: list[Decimal], b: list[Decimal], k: int) -> Decimal:
    """The k-th smallest (1-indexed) of every ``a_i + b_j``.

    Bisection on the VALUE using the count function, with every probe
    snapped to an actual pair sum, so it terminates on membership in the set
    rather than on a tolerance and the answer is exact.
    """
    lo = a[0] + b[0]
    if _count_at_most(a, b, lo) >= k:
        return lo
    hi = a[-1] + b[-1]
    # Invariant: lo and hi are pair sums with count(lo) < k <= count(hi).
    while True:
        between = _successor(a, b, lo, strict=True)
        if between is None or between >= hi:
            return hi
        mid = (lo + hi) / 2
        probe = _successor(a, b, mid, strict=False)
        if probe is None or probe >= hi:
            # Nothing in [mid, hi): the members left inside (lo, hi) all sit
            # below mid, and `between` proves there is at least one.
            probe = _predecessor(a, b, mid)
            assert probe is not None
        if _count_at_most(a, b, probe) >= k:
            hi = probe
        else:
            lo = probe


def _exact(segments: list[Decimal], total: Decimal) -> SignFlipResult:
    n = len(segments)
    half = n // 2
    a = _half_sums(segments[:half])
    b = _half_sums(segments[half:])
    configurations = len(a) * len(b)  # == 2**n
    return SignFlipResult(
        n=n,
        total=total,
        mean=total / n,
        p_high=Decimal(_count_at_least(a, b, total)) / configurations,
        p_low=Decimal(_count_at_most(a, b, total)) / configurations,
        pct5=_kth_smallest(a, b, _percentile_rank(Decimal("0.05"), configurations)),
        pct95=_kth_smallest(a, b, _percentile_rank(Decimal("0.95"), configurations)),
        method="exact",
        configurations=configurations,
    )


# -- Monte Carlo ---------------------------------------------------------


def _monte_carlo(
    segments: list[Decimal], total: Decimal, *, seed: int, draws: int
) -> SignFlipResult:
    n = len(segments)
    rng = random.Random(seed)  # noqa: S311 -- a seeded null distribution, not security
    sums: list[Decimal] = []
    for _ in range(draws):
        acc = Decimal(0)
        for g in segments:
            acc = acc + g if rng.random() < 0.5 else acc - g
        sums.append(acc)
    sums.sort()
    at_least = len(sums) - bisect_left(sums, total)
    at_most = bisect_right(sums, total)
    return SignFlipResult(
        n=n,
        total=total,
        mean=total / n,
        p_high=Decimal(at_least) / draws,
        p_low=Decimal(at_most) / draws,
        pct5=sums[_percentile_rank(Decimal("0.05"), draws) - 1],
        pct95=sums[_percentile_rank(Decimal("0.95"), draws) - 1],
        method="monte-carlo",
        configurations=draws,
    )


def _percentile_rank(q: Decimal, count: int) -> int:
    """1-indexed rank of the q-th percentile: the smallest k with k/count >= q."""
    return max(1, math.ceil(q * count))


# -- reading ---------------------------------------------------------------


def reading(result: SignFlipResult) -> list[str]:
    """The pre-registered S3 / K1 lines, verbatim thresholds, no judgement."""
    if result.n < MIN_SEGMENTS:
        return [f"n = {result.n} < {MIN_SEGMENTS}: neither S3 nor K1 may be read from this run"]
    s3 = "holds" if result.p_high < SUCCESS_P else "does not hold"
    k1 = "FIRES" if result.p_high >= KILL_P else "does not fire"
    return [
        f"S3 (p_high < {SUCCESS_P}): {s3}",
        f"K1 (p_high >= {KILL_P}, wrong side): {k1}",
    ]


def render(name: str, result: SignFlipResult) -> str:
    lines = [
        f"sign-flip null :: {name}",
        f"  segments (n)          {result.n}",
        f"  G, sum of gross       {result.total:+,.2f} $/SOL",
        f"  mean per segment      {result.mean:+,.2f} $/SOL",
        f"  null                  {result.method}, {result.configurations:,} sign vectors",
        f"  p_high = P(G* >= G)   {result.p_high:.4f}",
        f"  p_low  = P(G* <= G)   {result.p_low:.4f}",
        f"  G* 5th / 95th pct     {result.pct5:+,.2f} / {result.pct95:+,.2f} $/SOL",
        *(f"  {line}" for line in reading(result)),
        "  costs are identical under every sign assignment: p on gross is p on net",
    ]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {Path(argv[0]).name} <backtest-result.json>", file=sys.stderr)
        return 2
    path = Path(argv[1])
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        print(f"{path}: unreadable ({exc})", file=sys.stderr)
        return 2
    try:
        result = sign_flip(segments_from_report(doc))
    except (KeyError, ValueError, TypeError) as exc:
        print(f"{path}: {exc}", file=sys.stderr)
        return 1
    print(render(path.name, result))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except BrokenPipeError:
        sys.stderr.close()
        raise SystemExit(0) from None
