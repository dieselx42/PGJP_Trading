# Candidate F — `sol-sma`: close versus its 50-day average

Written 2026-09-20, before any backtest of this rule has run. The numbers
that decide it are in `docs/STRATEGY_ANALYSIS.md` §9, fixed in advance; this
page is the plain-language version for the team.

## The rule in one sentence

Every day at 00:00 UTC, compare yesterday's close with the 50-day simple
moving average of daily closes — the line every SOL chart draws by default.
Above the line, hold one contract long. Below it, hold one contract short.
When the side changes, flip. Otherwise do nothing.

No stop, no profit target, no trail. Once warmed up it is never flat.

## The rule precisely

| Setting | Value | Why this value |
|---|---|---|
| Window | 50 days | Close − SMA(50) weights the last ~49 daily returns with a centre of mass near 17 days, inside the 1–4 week horizon where time-series momentum in crypto is documented (Moskowitz–Ooi–Pedersen 2012; Liu & Tsyvinski 2021). On a random walk it flips ~25 times a year, enough for the sign-flip test below to have power. And it is the default line on every chart, so anyone can check the position by eye. Fixed before any replay; a different window is a different candidate, not a tuning. |
| Decision | once per completed UTC day, on the first bar of the next day | The day has to be over before its close exists. Never intraday. |
| Input | daily closes only | Highs and lows are understated in the live sampled bars and are never read. |
| Direction | long/short, always in | The published rule is long/short. A symmetric always-in rule has expected gross of exactly zero on a coin flip, which makes the statistical test clean. A long/flat rule would beat buy-and-hold by construction in a year SOL fell. |
| Tie | close exactly equal to the average: hold the current side | A close *at* a level is not a signal. |
| Exit | the flip only | Nothing else. The position rides through the daily break, weekends and the quarterly roll. |
| Order | market, next bar's open | The first entry is 1 contract; every flip is one 2-contract order (close 1, open 1). `MAX_ORDER_SIZE` must be at least 2. |
| Size | 1 contract live | The accepted price of a short leg with no stop is a momentum crash, which is why size is 1. |

One parameter. The full rule with every edge case resolved (missing bars,
weekends, partial fills, restarts) is the module docstring of
`app/strategy/sma.py`.

## Why this rule and not the other four

Five designers each proposed one rule from a different angle — published
evidence, cost arithmetic, explainability, fitting risk, execution realism —
without seeing each other's work. Three judges then ranked all five
comparatively. Two tied for first; this one was chosen and took one idea from
the other.

**20-day average, long or flat** (from the evidence lens). Ranked first on
evidence, last on simplicity-of-reading. Its problem is the *flat* side: in a
year everyone knows SOL fell, a rule that is simply out of the market half
the time beats buy-and-hold whether or not it has any skill, so its result
cannot be read cleanly. The 20-day window also flips ~43 times a year, nearly
double the toll.

**Weekly 4-week momentum** (from the cost lens). Tied for first. Decide once
a week: long if this week's close is above the close four weeks ago, short if
below. Genuinely simpler in cadence and the cheapest per year. It lost the
tie on statistics, not on merit: ~12 decisions a year is too few for any test
to say anything within one year of data. Its insight that live orders only
fill while CME is open was written into this rule's plan.

**100-day average, checked each Sunday** (from the explainability lens).
The slowest rule proposed: ~7 trades a year. Easy to explain, impossible to
judge on one year.

**28-day momentum with a resting limit order walked daily** (from the
execution lens). The most realistic about fills on a thin contract, but the
limit-order mechanism adds moving parts — where the order rests, when it is
moved, what happens when it misses — and each is a place a result can be
tuned. Same weekly-n problem as the other momentum rule.

The winner is the one the fitting-risk judge ranked first: one number, no
exits, a decision cadence that produces enough flips to test, and a shape
whose coin-flip baseline is exactly zero.

## What it costs at one contract

A flip is one 2-contract order: close the old side, open the new one.

| | per flip | ~25 flips/year |
|---|---|---|
| Commission ($3.41 × 2) | $6.82 | $171 |
| Spread, measured (2½ ticks × $1.25 × 2) | $6.25 | $156 |
| **Total, measured** | **$13.07** | **$327** |
| Total, pessimistic bracket (3 ticks/side) | $14.32 | $358 |

Against that, the rule's gross P&L on one contract has a standard deviation
of roughly $2,700 a year (about $109 per SOL). The toll is around 13% of the
noise. The opening-range strategy paid $1,372 a year at one contract for 105
trades; this rule pays a quarter of that for a quarter of the trades, and
each trade is trying to catch a $5–30 move rather than a $2 one.

That does not make it profitable. It makes it the first rule tested here
whose cost structure would *let* it be profitable if the signal is real.

## What the backtest has to show — decided in advance

These criteria were written before the replay ran and must not be moved
afterwards. The canonical text, with every threshold, is
`docs/STRATEGY_ANALYSIS.md` §9. In short:

**Machinery gate (S0).** No refusals; between 12 and 45 flips; 49 warm-up
days; the cost model reconciles arithmetically; the zero-cost twin makes the
identical decisions. If any of this fails, it is a bug to fix, not evidence.

**S1 — Net at the pessimistic cost bracket is positive**, counting the
position still open at the end.

**S2 — Average gross per trade ≥ $1.15 per SOL**, twice the toll, with at
least 15 trades. Necessary, not sufficient — its standard error is three to
four times that.

**S3 — Sign-flip test, p < 0.05.** This is the only criterion with real
power. Keep the rule's own trade dates and sizes; randomise only which way
each one pointed; do that for every possible assignment (or 200,000 of them);
ask what fraction did at least as well as the real rule. Below 5% and the
direction calls carried information. Above, the result is consistent with
luck.

**S4 — The 25-day and 100-day windows agree on the sign of gross.** Can only
downgrade a success to inconclusive. May never be used to change the window.

**Kills.** Sign-flip p ≥ 0.95 (the rule was *systematically* wrong-sided — the
whole family ends). Gross loss worse than two years of toll. All three windows
lose after costs. More than 45 flips with nothing to show. Plus three live-trial
kills on parity, execution cost and capital.

**Verdict map.** All of S1–S4 → a 26-week paper trial at 1 contract, to
measure execution cost, not to re-test the edge. Some but not all → the rule
stays registered and untouched, re-tested when six more months of data exist.
Any kill → finished; no re-parameterisation.

## Running it

On the VPS:

```
cd /opt/sol-futures-trading-bot
git pull origin claude/test-tpxkgy
scripts/strategy_compare.sh --fresh --out /tmp/f
scripts/sign_flip.py /tmp/f/f0-sma-zerocost.json
```

`--fresh` builds the image and runs the replays in a throwaway container; the
live bot is untouched. The compare script prints every candidate; the `f`
rows are this rule:

- `f3-sma-net3` — the primary row (3 ticks/side). Read its **net**; the
  digest now includes the open final position.
- `f0-sma-zerocost` — the gross-edge row and the input to `sign_flip.py`.
- `f1-sma25-signcheck`, `f2-sma100-signcheck` — read for the sign of gross
  only.
- `f-sma` — the 1-tick row, for continuity with the older rows.

`sign_flip.py` prints the real gross, the null distribution's spread, and
`p_high` with the 0.05 and 0.95 thresholds beside it. Apply S1–S4 and K1–K4
exactly as written and record the verdict in §9.

## The one-year result, and the five-year run

The first replay (one year, 2025–26) came back **inconclusive**: 26 flips,
+$2.33 per SOL before costs, sign-flip p = 0.49 — a coin toss, neither
confirmed nor refuted. At 27 trades the test could not have seen anything
smaller than a very large edge.

The next run is five years of spot history, 2021-07-01 to 2026-09-20: about
150 flips, enough for the sign-flip test to distinguish a modest edge from
luck. Its criteria are fixed in `docs/STRATEGY_ANALYSIS.md` §10 before the
data is imported. One new check, S5: the 2021–23 half and the 2024–26 half
must agree on the sign of the result, so that one big trend cannot carry the
whole verdict. Import commands and the run are in §10.

Two honest caveats on that run: the one year already measured is inside the
five, and no CME Solana contract existed before 2025 — so the five-year
number measures whether the *signal* exists, not what the strategy would
have earned.

## Paper trading it

The bot already selects a strategy by name and sizes it from `.env`; the
rule's defaults are the registered spec, so there is nothing else to pass.
The paper configuration is six lines:

```
STRATEGY_NAME=sol-sma
STRATEGY_POSITION_CONTRACTS=1
MAX_POSITION_CONTRACTS=1
MAX_ORDER_SIZE=2
MAX_OPEN_ORDERS=2
TRADING_MODE=paper
```

`MAX_ORDER_SIZE=2` matters: a flip is one order that closes a side and
opens the other, so it is twice the position. The bot now refuses to start
if that ceiling is below what the strategy needs, rather than starting and
having every flip refused in silence.

Apply it with `docker compose up -d --force-recreate sol-trading-bot`
(`restart` does not re-read `.env`).

**What happens at start.** The rule needs 50 completed daily closes before
its first decision, and strategy state lives in memory — so without help,
every restart would mean 50 silent days, and a restart is how every `.env`
change is applied. Instead, at start the bot hands the strategy the last 50
completed days: days it recorded itself in earlier runs first (each
completed live day is written to the database as it happens), then the
stored spot series for anything older. It then adopts whatever position the
broker reports at the first reconciliation. The first live bar of a new UTC
day completes the last seeded day and makes the first decision; if the
seeded rule disagrees with the adopted position, that bar flips it. A
restart costs the day in progress, not the warm-up. The log line
`strategy.seeded` says how many days came from where.

**What to know about the first 50 live days.** Spot and the front-month
future differ by the basis — usually under a percent, a dollar or two. Until
50 live days have replaced the spot ones, the average is partly spot while
the close it is compared against is the future, so a flip that lands near
the line can come a day early or late. It fades day by day and is gone after
50. The alternative was silence.

**What the paper trial is for.** Not to re-test the edge — 26 weeks cannot.
It measures what a flip actually costs on MSL (pre-registered kill K6: over
$20 a contract ends it) and whether the live bar pipeline agrees with the
replay (K5). Those are the two facts no backtest can supply.

## What to distrust

- **Spot, not futures.** The replay is Coinbase spot; there is no basis, no
  roll, no CME closure. Live, the average will span ~8.3 calendar weeks
  instead of ~7.1 because Saturday does not exist as a trading day.
- **One year.** About 25 flips. The sign-flip test is the only statistic
  here that means anything at that n, and even it is approximate because
  where one trade ends and the next begins depends on the direction taken.
- **The 1-tick row flatters.** The measured spread is 5 ticks. Read the
  `net3` row; it is the one every criterion is written against.
- **Nothing here demonstrates profit.** A SUCCESS verdict means "not refuted,
  and the costs would allow it" — the reason the next step is a paper trial
  and not money.
- **The panel chose from priors, not from results**, which is the point:
  no number in this document was seen before the rule was fixed.
