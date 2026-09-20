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
z-hold-benchmark  passive long, rolled quarterly           <- run this; see §6
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

## 5. Candidate C (`sol-momentum`) — the design this points at

Built against the three diagnosed failures, and against nothing else. Every
default is a prior, not a value chosen by looking at a result.

| Change | Why | Prior or guess? |
|---|---|---|
| **Size by risk, not by decree.** `contracts = floor(risk_budget / (stop_per_sol x 25))`, capped at 40 | A fixed 40 contracts risks $10k at ATR $5 and $30k at ATR $15 — the same "size", triple the risk. Now every trade risks the same dollars at its stop, and exposure falls exactly when the market is most dangerous | **Prior.** Standard practice, no parameter tuned on this data |
| **Cost gate**: refuse entries whose stop is under 25x the round trip ($9.32/SOL) | The ORB paid $0.373 to chase $0.40. This bounds the toll at ~4% of risk *structurally*, rather than leaving it as something a reader checks afterwards | **Prior**, derived from the measured cost — the gate moves if the fill model does |
| **Regime filter**: longs only above the 100-day mean, shorts only below | A breakout against the long-term trend is the one most likely to be noise | **Weakest link.** Long-standing published practice, but the most exposed to the fitting critique — so it is measured, see below |
| **No breakeven rule** | 64 of the ORB's exits were arithmetically guaranteed losses | **Prior.** Structural: no rule here can lock in less than the cost floor |

Sizing worked through, at the $5,000 default and a 2xATR stop:

| ATR | Stop/SOL | Risk/contract | Contracts | Dollars risked |
|---|---|---|---|---|
| $5 | $10 | $250 | 20 | $5,000 |
| $10 | $20 | $500 | 10 | $5,000 |
| $20 | $40 | $1,000 | 5 | $5,000 |

Size halves as volatility doubles. A signal that sizes below one contract is
**skipped**, not rounded up — rounding up is the moment a risk framework
becomes a suggestion.

**The two opinionated rules are ablated on every run.** `strategy_compare.sh`
runs `c1-momentum-no-regime` and `c2-momentum-no-costgate` alongside the
strategy itself. If a rule is not earning its place, that pair says so without
anyone having to ask — which is the check that keeps a "designed" rule from
quietly being a fitted one.

### What this is not

It is not a claim of profit, and it should not be read as one. The design
panel that produced it included a lens whose whole job was to argue against
trading this at all, and its finding stands: **every P&L result measured on
this instrument has a t-statistic under 0.2.** The only statistically
significant number in the whole body of evidence is the cost. So what is
engineered here is exactly the part the evidence supports — that costs and
risk are controlled, and that no rule is a guaranteed loser. Whether a
directional edge exists is a question for out-of-sample data.

## 6. The row that was missing: does trading beat not trading?

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

## 7. What would actually settle this

None of the above is decidable on one year of data. In rough order of value
per unit of effort:

1. **More history — as a robustness check, not a verdict.** One year gives 12
   trend signals; importing 2021→2025 gives roughly 60. That is worth having,
   but it does **not** settle anything, and an earlier draft of this document
   wrongly said it would. At the per-trade Sharpe the trend result implies
   (0.4/√12 ≈ 0.116), the expected t-statistic is:

   | n | E[t] |
   |---|---|
   | 12 (today) | 0.40 |
   | 60 (2021–2025) | 0.89 |
   | 200 | 1.63 |

   Significance needs |t| ≈ 1.96. So even 200 trades would probably not clear
   the bar. The five-year run will produce a **confident-looking number
   carrying almost no information** — and the real danger is that it then gets
   tuned against. Label it a robustness check in writing *before* running it.

2. **A sign-flip null.** With n≈12 the only way to get a p-value without
   waiting years is to keep the strategy's actual signal dates and randomise
   only the *direction*, over a few hundred seeds, at identical costs and
   sizing. The strategy is interesting only if its net beats the 95th
   percentile of that distribution. This costs compute rather than years and
   is the highest-value inference work available.
3. **Out-of-sample discipline.** Any parameter chosen by looking at a result
   is fitted to it. The honest procedure is to fix parameters on one period
   and measure on another that was never examined.
4. **The document author's answers** (`docs/ORB_DOCUMENT_QUESTIONS.md`). One
   question could still change the ORB verdict: whether the 5-year table
   assumed **resting stops filling at their exact price**. This replay fills
   exits at the next bar's open, which is materially more pessimistic on a
   $0.65 stop applied to every loser. If the table assumed the optimistic
   fill, an optimistic-fill run is needed to bracket the truth.
5. **Futures data instead of spot.** Every number here is measured on spot.
   No basis, no roll, no CME session breaks, and volume that does not reflect
   the futures book. Phase 4 of `BACKTESTING_SCOPE.md` compares the two once
   IBKR historical data is reachable; the difference is a measurement of how
   much the proxy was lying.

## 8. Caveats that should travel with every number here

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

## 9. Candidate F (`sol-sma`) — pre-registration, written before any replay

Everything in this section was fixed before `scripts/strategy_compare.sh` ran
the `f` rows. Nothing below may be edited after a number has been seen; a
different window, cadence, band, direction mode or exit is a new candidate
with a new name and its own section.

**The rule, in one sentence.** Every day at 00:00 UTC compare yesterday's
completed daily close with the 50-day simple moving average of daily closes:
above the line hold one contract long, below it hold one contract short, flip
at the open of the next minute when the side changes, and otherwise do
nothing — no stop, no target, no trail, never flat after warm-up. The full
rule, every ambiguity resolved, is the module docstring of
`app/strategy/sma.py`.

**Why 50, and why it may not move.** Close minus SMA(N) weights the last N−1
daily returns linearly with a centre of mass near N/3 days, so 50 sits at
~17 days, inside the 1–4 week horizon where time-series momentum in crypto is
documented (Liu & Tsyvinski 2021; Detzel et al. 2021). On a driftless random
walk the sign of close − SMA(50) changes ~0.08 times a day, ~25 flips in the
~315 post-warm-up days of a one-year replay — inside the 15–100 band where
the sign-flip null below has power (100 days gives ~15 flips, 200 gives ~6,
20 gives ~43). The 50-day line is drawn by default on every chart, so a
non-quant checks the position by eye. Expected toll: ~25 × $0.573/SOL ≈
$14.3/SOL/yr, ~0.13 of the rule's annual gross standard deviation.

**Why long/short and always in.** The published TSMOM rules are long/short; a
symmetric always-in rule has expected gross exactly zero on a martingale, so
its coin-flip line is zero and the sign-flip null is clean. A long/flat rule's
null is the drift share while long, and in a year SOL is known to have fallen
it beats `sol-hold` by construction. The accepted price is momentum-crash
exposure on the short leg with no stop, which is why the live size is 1.

**The runs** (all `--session all`, harness COMMON limits, `--max-order-size 80`
because a flip at 40 contracts is one 80-lot order; every threshold is per
SOL, ×1000 at 40 contracts; "total" always means
`net_pnl + performance.final_unrealized`):

| row | cost model | read for |
|---|---|---|
| `f3-sma-net3` | 3 ticks/side, $3.41/side | **primary** for S1 and K3 |
| `f0-sma-zerocost` | none | S2, S3, K1, K2, K4; input to `scripts/sign_flip.py` |
| `f-sma` | 1 tick/side | continuity with the other rows only |
| `f1-sma25-signcheck`, `f2-sma100-signcheck` | none | S4, the SIGN of gross only |
| `f4-sma25-net3`, `f5-sma100-net3` | 3 ticks/side | K3 only |

**S0 — machinery gate** (must all hold before any P&L is read; a failure is
fixed and re-run, it is not evidence either way): `refusals.count == 0`;
`12 <= trades.count <= 45`; `counters.days_in_warmup == 49` (the 50th
completed day is the first decision); `targets_long + targets_short == flips
+ 1`; `orders_reemitted <= 5%` of `orders_emitted`; `fills_off_target == 0`;
`final_position` is ±40 and `limitations` flags it; `commission_paid == 3.41 ×
(40 + 80 × flips)` and `slippage_paid == slippage_ticks × 1.25 × (40 + 80 ×
flips)`; the zero-cost twin has the same `trades.count` and identical
`opened_at`/`closed_at` per trade; `trades.detail_truncated == 0`.

**S1 — net at the pessimistic bracket.** Total of `f3-sma-net3` > 0 per SOL.
Total versus `z-hold-benchmark` is reported but is *not* a criterion (subtract
4 × $0.573/SOL for the four unmodelled rolls if it is quoted).

**S2 — edge over the coin-flip line.** From `f0-sma-zerocost`, mean gross per
segment (closed trades plus the open final segment, each `gross / (25 ×
quantity)`) ≥ +$1.15/SOL, twice the toll, with n ≥ 15. Necessary only: its
standard error is ~$3.5–4.5/SOL.

**S3 — sign-flip null** (the only criterion with power). `p_high < 0.05` from
`scripts/sign_flip.py strategy-compare/f0-sma-zerocost.json` with n ≥ 15:
G = Σ per-segment gross, G* = Σ sᵢ·grossᵢ over equiprobable signs, all 2ⁿ
configurations when n ≤ 30, else 200,000 draws from seed 20260919, p = share
with G* ≥ G. Costs are identical under every sign assignment, so p on gross is
p on net. Segment boundaries depend on the realised sign, so it is
approximate; that caveat travels with the number.

**S4 — sign agreement** (can only downgrade). The zero-cost gross total of
`f1` and `f2` has the same sign as `f0`'s. Disagreement turns SUCCESS into
INCONCLUSIVE; agreement adds nothing; neither row may change `sma_days`.

**Not criteria:** win rate (null prediction 0.18–0.30; anything in 0.10–0.60
is uninformative), drawdown at 40 contracts, the per-minute Sharpe, the
1-tick row.

**Kill criteria.**
- **K1 wrong side:** `p_high ≥ 0.95` with n ≥ 15. Ends the whole
  close-versus-average family on this data.
- **K2 lost twice its cost budget before costs:** zero-cost total ≤
  −$28.60/SOL. Fires in ~40% of null years and ~27% of published-strength
  years; that is the price of having a kill on one year.
- **K3 the whole window family lost after costs:** the 3-tick total ≤ 0 for
  all of `f3`, `f4`, `f5`.
- **K4 chop:** `trades.count > 45` and zero-cost total ≤ 0. Ends the
  candidate; it is not re-cadenced.
- **K5 replay-vs-live parity** (live trial): disabled by reconcile mismatch
  more than twice in 26 weeks, or any operator override of a live order.
- **K6 execution cost** (live trial): average realised round trip over the
  first 10 live flips > $20 per contract (1.4× the $14.32 bracket).
- **K7 capital control** (live trial): cumulative live net at 1 contract ≤
  −$1,500 at any point.

**Verdict map.** S0 passes and S1–S4 all hold → SUCCESS: a pre-registered
26-week live trial at 1 contract (`STRATEGY_POSITION_CONTRACTS=1`,
`MAX_ORDER_SIZE ≥ 2`), paper-armed through the 50-day live warm-up, whose
purpose is to measure execution cost, not to re-test the edge. S0 passes, no
kill fires, S1–S4 not all met → INCONCLUSIVE: the candidate stays registered
and untouched, no live money, the identical criteria re-applied once six more
months of bars exist. Any kill → KILLED, no re-parameterisation.

**Run protocol.** (1) `scripts/strategy_compare.sh --out ./strategy-compare`.
(2) Read S0 only; fix and re-run on any failure. (3)
`python3 scripts/sign_flip.py strategy-compare/f0-sma-zerocost.json`. (4)
Apply S1–S4 and K1–K4 exactly as written and record the verdict here, with
the numbers, before anyone looks at `f1`/`f2` for anything but sign. (5)
Nothing in the `f` rows may be re-run with a changed parameter.

**Known replay-vs-live divergence.** The replay's days are 24/7 UTC days on
spot; live, no bars arrive while CME is closed, so Friday completes on the
first Sunday-evening bar, Saturday does not exist as a day, and the 50-day
average spans ~8.3 calendar weeks instead of ~7.1. A `cme-crypto` session
filter for the replay is the engine follow-up that would close this; it is
an additional pessimistic row, never a parameter choice.

**Verdict:** _not yet run._
