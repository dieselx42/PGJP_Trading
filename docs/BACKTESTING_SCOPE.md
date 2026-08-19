# Backtesting: scope

Proposal, not yet built. Three decisions at the end need your answer before I
start.

## Why this is worth doing now

US futures permission is in cooldown, so paper orders are blocked for at least
four days. Backtesting is the only substantial work not gated on IBKR, and it is
what any real strategy needs before it is trusted with an order.

It also exercises parts of the system that mock mode does not. `MockBroker`
generates deterministic prices from a seed — pleasant, well-behaved, and nothing
like a real market. A replay over real Solana price history puts gaps, spikes,
thin sessions and overnight moves through the same risk and gate code that will
face a live account.

## The principle that shapes everything else

**The backtest runs the real `RiskManager` and the real `TransmitGate`.** Not a
copy, not a simplified version. A backtest that bypasses the interlocks is a
statement about a system that does not exist.

That has a consequence worth stating plainly: if the gate refuses an order in a
backtest, the backtest is *correct* to record no trade. A strategy whose
backtested results depend on ignoring the risk limits is a strategy that will
not perform that way live, and finding that out here is the point.

**And it must be structurally incapable of reaching IBKR.** Same approach as the
read-only checkout: a dedicated `BacktestBroker`, its own `Config` built from
explicit parameters rather than the server `.env`, and a test asserting the
module never constructs `IBKRBroker`. A backtest that can touch a broker is a
trading system with extra steps.

---

## The data problem, and why it is not IBKR

`reqHistoricalData` for MSL requires the same CME market-data entitlement that
currently returns `354` — which in turn requires the futures permission that is
in cooldown. **IBKR cannot supply this data today**, and possibly not for weeks.

So the source has to be something else. Options, with the trade-offs:

**A. Public SOL spot (Binance / Coinbase / Kraken).** Free, immediate, deep
history, minute resolution. It is *spot*, not futures: no basis, no roll, no
CME session breaks, and volume that does not reflect the futures book. For
developing and sanity-checking a strategy, that is usually acceptable. For
estimating fills on MSL, it is not.

**B. A CSV you supply.** If you have futures data from elsewhere, this is
strictly better and the loader is the same work either way.

**C. Wait for IBKR.** Correct data, correct contract, correct hours — and
blocked behind the cooldown, which is what we are trying to work around.

My recommendation is **A now, with the loader written so B and C drop in
later** — the ingestion interface should not care where bars came from, and the
stored bars should record their provenance so a backtest run can never
misrepresent spot data as futures data.

---

## Phase 1 — Ingestion and storage

A `bars` table, forward-only migration, same conventions as the rest of the
schema: ISO-8601 UTC text timestamps, decimal strings for prices.

```
bars(source, symbol, interval, opened_at, open, high, low, close, volume)
UNIQUE(source, symbol, interval, opened_at)
```

`source` is not decoration. It is what stops a backtest run on Binance spot from
being read later as a statement about CME futures.

- A `HistoricalSource` protocol: fetch a range, yield bars.
- A CSV loader (accepts anything, requires an explicit column mapping).
- One HTTP fetcher for whichever public source you pick.
- `app.cli bars-import`, `bars-info` — what is stored, over what range, with
  gaps identified rather than silently interpolated.

**Gaps get reported, never filled.** A missing hour is a fact about the data;
inventing a price to cover it is how a backtest starts lying.

## Phase 2 — The replay engine

A virtual clock drives everything. `MarketDataManager` already takes an
injectable `clock`, which is the seam this needs.

For each bar, in time order:

1. Convert to a `Quote` and hand it to `MarketDataManager` — so staleness,
   freshness and the delayed-data interlock all apply exactly as they do live.
2. `Strategy.on_quote` returns intents.
3. Intents go through `SignalValidator` → `RiskManager` → `OrderManager` →
   `TransmitGate` — the real ones.
4. Orders that pass reach `BacktestBroker`, which fills them against the *next*
   bar.

**Fills are modelled pessimistically and explicitly:**

- Fill at the **next bar's open**, never the current bar's close. A strategy
  that trades on a close it has already seen is reading the future, and it is
  the single most common way backtests flatter themselves.
- Configurable slippage in ticks, defaulting to something unkind.
- Commission per contract, set from real IBKR figures.
- No fills outside the contract's trading hours, which the checkout already
  showed IBKR reports in `contractDetails`.

Two modelling limits I want stated in the output rather than buried: **bars are
not ticks**, so intrabar highs and lows cannot be traded against, and **a bar
has no spread**, so bid/ask must be synthesised from close ± half a configured
spread. Both make results optimistic relative to reality.

## Phase 3 — Results

Per-run: return, max drawdown, Sharpe, win rate, average win/loss, exposure,
number of trades, total commission and slippage paid.

Per-trade: entry and exit time and price, size, P&L, and **the reason any intent
was refused** — which of risk or the gate blocked it, and why. That last part is
the one this system can offer that a generic backtester cannot: it shows where
the safety machinery would have stopped a strategy, not just what the strategy
wanted to do.

Output as JSON, same as every other command, so runs are diffable.

## What was actually built, and where it departs from this plan

Phases 1–3 are implemented. Two departures from the design above, both
deliberate, both stated here rather than left for someone to discover.

**`MarketDataManager` is not in the loop.** The plan routed each bar through it
so the staleness and delayed-data interlocks would apply live. In practice its
clock would be the virtual one, so a synthesised quote is always exactly zero
seconds old and the interlock can never fire — the same value the engine now
asserts directly, reached through more machinery. Wiring it would also mean
constructing it with a `Broker`, which is precisely what this package must not
be able to do. So the market-data conditions are **asserted, not measured**, and
the engine docstring and every result say so.

**`OrderManager` is not in the loop either.** Its `delta_to_side_and_quantity`
is used — that is the arithmetic that decides BUY versus SELL and how many — but
not its persistence and idempotency layer. Idempotency exists to survive a crash
mid-flight between our database and the broker. A replay is deterministic,
single-pass, and has no broker to be out of step with, so wiring it would write
hundreds of thousands of rows to prove a property the replay cannot exhibit.

**Both approvers run on every intent**, rather than stopping at the first
refusal. The risk manager and the transmit gate check the configuration
interlocks independently; short-circuiting on risk left the gate unable to fire
in a replay at all, which would have meant every backtest silently not testing
it. A refusal now names each party that objected (`risk+gate`) and lists what
each found.

**The contract is never invented.** `backtest` loads the multiplier, tick size
and conId from a qualification IBKR actually returned, stored in
`contract_metadata`. With none stored it refuses and says to run
`ibkr-checkout`. A guessed multiplier silently scales every P&L figure in the
result, and a fabricated conId would defeat the check that makes contract
identity unambiguous.

**Limits come from the deployed configuration** unless overridden on the command
line, and the output records which. Note the consequence on a halted server:
every limit is zero, zero means NOT CONFIGURED, and the replay therefore refuses
every order — correctly. The result says so explicitly rather than presenting a
flat equity curve, because "the strategy is flat" and "the strategy was never
allowed to trade" produce identical numbers and mean opposite things.

## Phase 4 — Later, once permission clears

Swap in IBKR historical data for the real contract, and compare against the spot
proxy. The difference between them is a measurement of how much the proxy was
lying, which is worth knowing before trusting any result from Phase 1–3.

---

## Effort

| Phase | Rough size |
|---|---|
| 1 — ingestion, storage, CLI | half a day |
| 2 — replay, fills, wiring | a day |
| 3 — metrics and reporting | half a day |
| 4 — IBKR historical | blocked on permission |

Phases 1–3 are usable together; there is no point shipping 1 alone.

---

## Running one

```bash
cd /opt/sol-futures-trading-bot

# 1. Verify the source with ten bars before pulling a year.
docker compose exec -T sol-trading-bot python -m app.cli bars-import --limit 10
docker compose exec -T sol-trading-bot python -m app.cli bars-import        # a year
docker compose exec -T sol-trading-bot python -m app.cli bars-info

# 2. Replay. Limits default to the deployed (halted) ones, which refuse
#    everything; pass explicit limits to evaluate a strategy under limits you
#    are actually considering.
docker compose exec -T sol-trading-bot python -m app.cli backtest \
  --max-order-size 1 --max-position 1 --max-orders-per-hour 60 \
  --max-open-orders 2 --max-daily-loss 500 --max-notional 20000

# 3. Confirm the interlocks bite, using the deployed switches as they stand.
docker compose exec -T sol-trading-bot python -m app.cli backtest --inherit-switches
```

`backtest` reads `bars` and `contract_metadata` and writes nothing. It cannot
reach a broker, cannot change a switch, and cannot affect a running bot.

## Decisions I need

**1. Data source.** Public spot proxy (recommended, available now), a CSV you
supply, or wait for IBKR?

**2. Bar interval.** 1-minute gives realistic intraday behaviour and a lot of
rows; hourly is easier to reason about; daily is nearly useless for a futures
strategy. I would take **1-minute**, stored raw, and aggregate up when needed.

**3. How much history.** A year of 1-minute SOL is roughly 500k bars — fine for
SQLite. Solana futures only listed on CME in 2025, so a longer window means more
spot-proxy data with no futures counterpart to check it against.

Nothing here changes the deployed system. The backtester is a separate entry
point, reads no server `.env`, and cannot construct a broker that talks to
IBKR.
