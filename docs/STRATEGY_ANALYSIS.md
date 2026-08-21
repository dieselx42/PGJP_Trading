# Strategy analysis: what failed, what replaces it, and what would prove it

Everything below is measured on **one year of Coinbase SOL-USD 1-minute spot
bars**, replayed through the real risk manager and transmit gate, at the ORB
document's 40 contracts (1,000 SOL). Fills are at the next bar's open with one
tick of slippage against; commission is $3.41/contract/side, taken from a real
IBKR `whatIf` preview on MSLQ6.

Read the caveats at the end before acting on any number here. The short
version: **one year is not enough evidence to trust any of this**, and this
document is as much about what would settle the question as about what the
numbers currently say.

---

## 1. The cost floor, which determines everything else

```
commission   2 x $3.41                    = $6.82  per contract
slippage     2 x 1 tick x $0.05 x 25 SOL  = $2.50  per contract
                                            ------
round trip                                  $9.32  per contract
                                          ÷ 25 SOL = $0.373 per SOL
at 40 contracts (1,000 SOL)               = $373   per round trip
```

Two consequences worth stating plainly:

- **73% of the cost is commission**, which no order type can avoid. Limit
  orders can only attack the $2.50 slippage component, and buy that at the
  price of adverse selection — a resting limit fills preferentially when the
  market is about to go against you, and misses when it runs your way.
- The floor is **fixed per contract**, so it is a large fraction of a small
  move and a small fraction of a large one. That single fact decides which
  strategy families can work here at all.

| Target move | Cost as % of target |
|---|---|
| $0.40 (ORB breakeven trigger) | 93% |
| $1.50 (ORB target) | 25% |
| $6.00 (big-range ORB target) | 6% |
| $10–30 (multi-day trend leg) | 1–4% |

## 2. Why the ORB failed — and it is not what it looked like

The headline was a 25% win rate against the document's claimed 57.8%. The
attribution says the win rate was never the real story.

```
e1-baseline    net -92,984 | gross   -530 | costs 92,454 | 248 trades | 25.0% win
e0-orb-zerocost net -1,190 | gross -1,190 | costs      0 | 249 trades | 49.8% win
```

**With every cost removed, the ORB is a coin flip**: 49.8% win rate, gross
−$530 over 248 trades. There is no directional edge to rescue. The $92k of
costs did not spoil a good strategy; they converted a zero into a large
negative.

Where the money went, worst first:

| Exit rule | n | wins | net | Reading |
|---|---|---|---|---|
| stop | 97 | **0** | −$94,612 | the $0.65 stop *is* the loss |
| breakeven | 64 | 1 | −$22,429 | arithmetically guaranteed, see below |
| target | 7 | 7 | +$6,480 | too rare to matter |
| trail | 80 | 54 | +$17,576 | the only rule that worked |

The **breakeven rule** is the clearest single defect and the one that
generalises: it moved the stop to entry + $0.05 once price was $0.40 in
favour. But $0.05 is **below the $0.373 cost floor**, so every one of those 64
exits was a loss *by arithmetic* — +$50 gross, −$322.80 net. They are also
precisely the trades the two results would count differently: a breakeven exit
is a **win** on gross counting and a **loss** on net. If the document's 57.8%
counted them as wins, a large part of the win-rate gap is a definitional
difference, not a disagreement about price.

**The clock was never the problem.** The NY session was fixed at 14:30 UTC,
which is 9:30 ET only in winter — it ran an hour late for the whole DST
period. That was a real bug and it is fixed (sessions are now anchored to 9:30
`America/New_York` and track DST). Re-running at the corrected clock produced
essentially the same result. Worth fixing, but it explains nothing.

## 3. What the design has to satisfy

From the above, any strategy on this instrument must:

1. **Capture moves much larger than $0.373/SOL.** This rules out the entire
   intraday-scalp family at these costs.
2. **Trade rarely.** 248 round trips a year is $92k of costs at 40 contracts
   before any opinion about direction. A dozen well-chosen trades cost $4.5k.
3. **Contain no rule that can lock in less than the cost floor.** The
   breakeven trap must be structurally impossible, not merely re-tuned.
4. **Have an edge that survives costs, or be abandoned.** Nothing in points
   1–3 manufactures an edge; they only stop costs destroying one that exists.

## 4. The candidates, measured

```
z-hold-benchmark  passive long, rolled quarterly           <- run this; see §5
e1-baseline       ORB verbatim              net  -92,984 | 248 trades | 25.0%
a-big-range       ORB, $3 range / $6 target net   +1,637 |   1 trade  |  n/a
b-trend           daily Donchian 20/10      net  -11,354 |  12 trades | 25.0%
```

**Candidate A (big-range ORB)** re-derived the ORB's numbers against the cost
floor: only $3+ opening ranges, a $6 target, a $2 stop, one entry per session,
and a breakeven lock above the floor. It produced **one trade in a year**. The
filter is too tight on this data to say anything; +$1,637 on n=1 is a coin
that landed heads once.

**Candidate B (daily Donchian trend)** enters on a 20-day channel breakout,
stops at 2×ATR(20), trails at 3×ATR, exits on the opposite 10-day channel. Its
**cost engineering worked exactly as designed** — $373/trade, roughly 4% of
the average move, against the ORB's 25–93%. It still lost, on 12 trades, and
12 trades cannot distinguish a bad strategy from a bad year. Trend systems
routinely have losing years at 25% win rates; that is the shape of the return
distribution, not evidence of failure.

## 5. The row that was missing: does trading beat not trading?

Three strategies were measured and none was ever compared against **doing
nothing**. `sol-hold` is that comparison: passive long exposure, flattened and
re-entered every 90 days.

It rolls for two reasons. The replay's `net_pnl` is
`realized_pnl - commission_paid`, so an open position at the window's end
contributes nothing — a never-exiting hold would report only its entry
commission, which is a benchmark of zero information. More importantly, **that
is what passive exposure actually costs on futures**: MSL is a quarterly
contract, so a year of passive long is four round trips (~$1,492 at 40
contracts), not one. A benchmark ignoring the roll would flatter every active
strategy.

It does not model the calendar basis a real roll pays, so it slightly
*overstates* passive returns — which makes it a conservative bar for an active
strategy to clear.

**This is the row to read first.** Any strategy that does not beat it, after
costs and adjusted for risk, is destroying value relative to doing nothing.

## 6. What would actually settle this

None of the above is decidable on one year of data. In rough order of value
per unit of effort:

1. **More history.** One year gives 12 trend signals. Importing 2021→2025 SOL
   history gives roughly 60 — enough for the trend result to mean something.
   This is the single highest-value next step and costs one command.
2. **Out-of-sample discipline.** Any parameter chosen by looking at a result
   is fitted to it. The honest procedure is to fix parameters on one period
   and measure on another that was never examined.
3. **The document author's answers** (`docs/ORB_DOCUMENT_QUESTIONS.md`). One
   question could still change the ORB verdict: whether the 5-year table
   assumed **resting stops filling at their exact price**. This replay fills
   exits at the next bar's open, which is materially more pessimistic on a
   $0.65 stop applied to every loser. If the table assumed the optimistic
   fill, an optimistic-fill run is needed to bracket the truth.
4. **Futures data instead of spot.** Every number here is measured on spot.
   No basis, no roll, no CME session breaks, and volume that does not reflect
   the futures book. Phase 4 of `BACKTESTING_SCOPE.md` compares the two once
   IBKR historical data is reachable; the difference is a measurement of how
   much the proxy was lying.

## 7. Caveats that should travel with every number here

- **Spot, not futures.** Different instrument. Stated on every result.
- **One year.** Twelve trades for the trend candidate, one for candidate A.
- **Bars are not ticks.** Intrabar highs and lows cannot be traded against,
  and a bar has no spread — both make results *optimistic* relative to
  reality, and the replay says so in its output.
- **Backtests flatter.** Every result here was produced by a system whose
  parameters were chosen by people who had seen the data. The trend strategy's
  parameters were chosen from published trend-following practice rather than
  by fitting, which is a weaker form of the same problem, not an escape from
  it.
- **No result here demonstrates a profitable strategy.** The most defensible
  reading of the evidence is that intraday breakout on SOL has no edge at
  these costs, that longer-horizon trend is not yet refuted, and that passive
  exposure is the bar to beat.
