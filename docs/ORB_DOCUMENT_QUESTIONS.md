# Questions for the ORB document's author

The strategy in `app/strategy/orb.py` implements the operator-supplied document
"SOL 5-Minute ORB Strategy — Opening Range Breakout, London & NY Sessions"
(2026-08) verbatim. Replayed over a year of SOL bars at the document's 40
contracts, it returns **−$81k with a 25% win rate**, against the document's
claimed **57.8%**.

That gap is too large to be noise, and the replay's own attribution says it is
not mainly a *directional* disagreement: with costs removed the raw edge is
roughly zero, so the two results may be describing the same price behaviour
under different assumptions about what a trade costs and when it happens.

Five questions, ordered by how much the answer would change. The first three
can each account for a large part of the gap on their own.

---

## 1. What instrument and date range produced the 5-year table?

**Why it matters most:** CME listed Solana futures in **2025**. A five-year
table ending 2026 therefore cannot have been computed on MSL — there is no
such price history. It was presumably computed on SOL **spot** or on a
perpetual swap.

If so, the table carries **no per-contract commission**, because spot has none.
The document's rules were then applied to 40 futures contracts at $3.41 per
contract per side, a cost structure the table never saw. That alone reverses
the sign of a whole class of trades — see question 3.

- What series (venue, symbol, spot vs. perp) and what exact date range?
- Were futures commissions and slippage applied to it at all?

## 2. How do the stop, target and trail fill?

The document says to set a $0.65 stop "as soon as the order fills". Two
readings, and they differ by roughly the width of the stop itself:

- **A resting stop order** that fills **at $0.65**, as a limit-like fill.
- **A market exit** triggered when price trades through, filling at whatever
  comes next.

The replay implements the second and fills exits at the **next bar's open**,
because it never assumes a resting order filled at its exact price. On a $0.65
stop that pessimism is not a rounding difference — it is a large fraction of
the risk per trade, and it is applied to every losing trade in the year.

- Did the table assume fills **at** the stop/target price?
- If yes: over five years of 1-minute data, was any slippage assumed on them?

## 3. Was the 57.8% win rate counted gross or net?

This is the question with an arithmetic answer, and it is the one we can
already show is decisive.

The document's break-even rule moves the stop to **entry + $0.05** once price
is $0.40 in favour. But the cost of a round trip is:

```
commission   2 × $3.41                  = $6.82  per contract
slippage     2 × 1 tick × $0.05 × 25 SOL = $2.50  per contract
                                         -------
                                          $9.32  per contract
                                        ÷ 25 SOL = $0.373 per SOL
```

**$0.05 is below the $0.373 cost floor.** Every break-even exit is therefore a
guaranteed loss — not an unlucky one, an arithmetic one — of **+$50 gross /
−$322.80 net** at 40 contracts. There were **58** of them in the baseline year,
about **−$18.7k**.

They are also exactly the trades the two results would count differently: a
break-even exit is a **win** on gross counting and a **loss** on net. If the
57.8% counted them as wins, a large part of the win-rate gap is a definitional
difference rather than a disagreement about price.

- Were the table's wins counted **before** or **after** costs?
- Was the $0.05 lock chosen against a specific cost assumption, or as a
  nominal "scratch"? On spot with no commission it is a small genuine win; on
  40 MSL contracts it cannot be.

## 4. Which clock is the NY session on — 14:30 UTC or 9:30 ET?

The document states both. They coincide only in winter: during US DST, 9:30 ET
is **13:30 UTC**, an hour earlier than the document's UTC column.

The implementation follows the UTC column as written, which today reads:

```
sessions ET today: ['04:00 EDT', '10:30 EDT']
```

So the deployed strategy opens its NY session at **10:30 ET, not 9:30** — and
did so for roughly two-thirds of the replayed year, DST being in effect from
March to November. If the table was computed on exchange-local time, the
replay and the table describe **different hours** for most of their history,
and the comparison between them is not like-for-like.

- Which clock did the table use?
- Was the London 08:00 UTC open likewise local (London observes BST)?

## 5. Was the 1,000 SOL size constant across the whole table?

The document specifies 1,000 SOL — 40 MSL contracts at 25 SOL each — "every
single trade, no adjustments".

- Was the table computed at a fixed 1,000 SOL, or as a percentage of equity?
  A fixed size compounds nothing; a percentage size produces a very different
  five-year curve from identical trades.
- At 40 contracts, one $0.65 stop-out is roughly **$650** of risk plus
  **$373** of costs. Was drawdown in the table consistent with that?

---

## What we would do with the answers

- **Q1/Q3 confirm costs were absent** → the strategy is not broken so much as
  mis-specified for this instrument, and the break-even and stop distances need
  re-deriving against the $0.373/SOL floor before it is worth replaying again.
- **Q2 confirms fills at the stop price** → our replay is too pessimistic on
  every loser, and the honest comparison needs an optimistic-fill run alongside
  the current one to bracket the truth.
- **Q4 confirms exchange-local** → the deployed session times are wrong and
  `--sessions 08:00,13:30` becomes the baseline rather than an experiment.

None of these require the author to share the table itself; each is answerable
in a sentence.
