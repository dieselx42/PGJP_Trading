# Architecture

## Design principles

1. **Fail closed.** Every default is the one that refuses. Absent config,
   missing broker state, an unparseable value, or a dropped connection all
   result in orders being rejected. There is no branch anywhere that permits
   transmission by omission.

2. **Two independent approvers.** The risk manager and the transmit gate check
   different things and both must approve. Risk asks *"is this trade within our
   limits?"*; the gate asks *"is this system in a state where any trade may be
   sent at all?"*. Neither can override the other.

3. **Strategies know nothing.** A strategy receives `Quote` objects and returns
   `TradeIntent` objects. It has no broker handle, no order types, and no way to
   express "send this". That boundary is also what makes backtesting possible
   later without touching strategy code.

4. **Broker isolation.** Exactly one module — `app/broker/ibkr_broker.py` —
   knows Interactive Brokers exists. Adding a second broker means implementing
   one interface; it does not mean touching risk, execution, or strategy.

5. **Observed, not assumed.** Account identity comes from broker-reported data.
   Contract details come from the broker. Configuration is treated as a claim to
   be checked, because configuration is exactly what we are guarding against
   being wrong.

6. **UTC everywhere.** Naive datetimes raise. Timestamps are stored as ISO-8601
   with an explicit offset. Local time is a presentation concern only.

---

## The pipeline

```
Market Data → Strategy → Signal → Risk → Order Manager → Transmit Gate → Broker → IBKR
```

Expanded, with what each stage can refuse:

```
┌─────────────────────────────────────────────────────────────────────┐
│ MarketDataManager                                                   │
│   • timestamps every tick on receipt, in UTC                        │
│   • drops price-less ticks (they would falsely refresh the age)     │
│   • clears its cache on disconnect                                  │
│   REFUSES: nothing — it reports, it does not decide                 │
└────────────────────────────┬────────────────────────────────────────┘
                             │ Quote
┌────────────────────────────▼────────────────────────────────────────┐
│ Strategy (NoOpStrategy)                                             │
│   • pure with respect to the outside world                          │
│   • returns zero or more TradeIntent                                │
│   REFUSES: produces nothing when disabled                           │
└────────────────────────────┬────────────────────────────────────────┘
                             │ TradeIntent
┌────────────────────────────▼────────────────────────────────────────┐
│ SignalValidator                                                     │
│   SIGNAL_SYMBOL_NOT_SUPPORTED / NOT_CONFIGURED_INSTRUMENT           │
│   SIGNAL_FROM_UNKNOWN_STRATEGY                                      │
│   SIGNAL_TIMESTAMP_STALE / IN_FUTURE                                │
│   SIGNAL_REQUESTED_POSITION_UNREASONABLE                            │
│   DUPLICATE_SIGNAL  (seeded from the database at startup)           │
└────────────────────────────┬────────────────────────────────────────┘
                             │
┌────────────────────────────▼────────────────────────────────────────┐
│ RiskManager                                                         │
│   MAX_ORDER_SIZE / MAX_POSITION_CONTRACTS / MAX_DAILY_LOSS_USD      │
│   MAX_ORDERS_PER_HOUR / MAX_OPEN_ORDERS / MAX_NOTIONAL_EXPOSURE_USD │
│     …each with a *_NOT_CONFIGURED and a *_EXCEEDED variant          │
│   KILL_SWITCH_ENGAGED / TRADING_MODE_DISABLED                       │
│   BROKER_NOT_CONNECTED / ACCOUNT_DATA_UNAVAILABLE                   │
│   TRADING_MODE_ACCOUNT_TYPE_MISMATCH                                │
│   POSITIONS_NOT_RECONCILED / OPEN_ORDERS_NOT_RECONCILED             │
│   MARKET_DATA_* / CONTRACT_* / STRATEGY_DISABLED                    │
│   DUPLICATE_ORDER_FOR_SIGNAL                                        │
└────────────────────────────┬────────────────────────────────────────┘
                             │ RiskDecision(approved=True)
┌────────────────────────────▼────────────────────────────────────────┐
│ OrderManager                                                        │
│   1. compute the position delta; no delta → no order                │
│   2. derive the deterministic idempotency key                       │
│   3. PERSIST the order before any broker work                       │
│   4. if the key exists and reached the broker → STOP (replay)       │
│   5. risk approval                                                  │
│   6. transmit gate                                                  │
│   7. mark PENDING_SUBMIT and flush to disk                          │
│   8. transmit                                                       │
└────────────────────────────┬────────────────────────────────────────┘
                             │
┌────────────────────────────▼────────────────────────────────────────┐
│ TransmitGate  ⛔  — the last thing that runs                        │
│   pure function: frozen GateContext → frozen GateDecision           │
│   collects ALL failing reasons, not just the first                  │
└────────────────────────────┬────────────────────────────────────────┘
                             │ transmit=True
┌────────────────────────────▼────────────────────────────────────────┐
│ Broker adapter                                                      │
│   refuses transmit=False unconditionally — an independent barrier   │
│   refuses order types outside TRANSMITTABLE_ORDER_TYPES             │
│   refuses continuous futures                                        │
└─────────────────────────────────────────────────────────────────────┘
```

### Why the gate is a pure function

`TransmitGate.evaluate` performs no I/O and holds no state. Its behaviour is
entirely determined by a frozen `GateContext`, which means it is exhaustively
testable and cannot be influenced by ordering, timing, or a stale cache.

`GateContext`'s fields all default to their unsafe-to-trade values, so a context
assembled from incomplete information denies rather than permits. Forgetting to
populate a field is a refusal, not a hole.

---

## Application state machine

```
                 ┌──────────┐
                 │ STARTING │
                 └────┬─────┘
                      │ mode = disabled?
          ┌───────────┴────────────┐
          │ yes                    │ no
          ▼                        ▼
      ┌───────┐              ┌────────────┐
      │ SAFE  │              │ CONNECTING │◄──────────┐
      └───────┘              └─────┬──────┘           │
     (no broker                    │ connected        │ backoff
      object exists)               ▼                  │ (capped)
                            ┌─────────────┐     ┌─────┴────────┐
                            │ RECONCILING │     │ DISCONNECTED │
                            └──────┬──────┘     └──────────────┘
                                   │                   ▲
                  ┌────────────────┴──────────┐        │ session drops
                  │ reconciled?               │        │
         no ──────┤                           ├── yes  │
                  ▼                           ▼        │
             ┌────────┐                  ┌─────────┐   │
             │  SAFE  │                  │ HALTED  │◄──┼── kill switch
             └────────┘                  └─────────┘   │
        (human must resolve)                  │        │
                                    kill switch off,   │
                                    transmit allowed   │
                                              ▼        │
                                        ┌─────────┐    │
                                        │  READY  │────┘
                                        └─────────┘
                                   the only state that trades
```

**A running process is not a ready process.** `READY` is reachable only through
successful reconciliation, and any event that costs us confidence in our view of
the account — a disconnect, a failed reconcile, a permission refusal — drops
back to `SAFE`.

The post-reconciliation state is chosen by strongest-reason-first, so `/status`
shows the real cause rather than a generic `SAFE`:

1. reconciliation failed → `SAFE`
2. kill switch engaged → `HALTED`
3. mode is `disabled` → `SAFE`
4. `ALLOW_ORDER_TRANSMIT=false` → `SAFE`
5. otherwise → `READY`

`ERROR` is reserved for an unhandled exception in the main loop and is the only
state that reports unhealthy.

---

## Order lifecycle

```
   NEW ──────────► BLOCKED            (risk or gate refused; never transmitted)
    │
    │ both approvers pass
    ▼
PENDING_SUBMIT ──► SUBMITTED ──► ACKNOWLEDGED ──► PARTIALLY_FILLED ──► FILLED
    │                  │              │                  │
    │                  └──────────────┴──────────────────┴──► CANCEL_PENDING ──► CANCELLED
    │
    └──► REJECTED   (broker refused)
    └──► ERROR      (transport failure after transmission was authorised)
```

`OrderStatus.reached_broker` is `False` only for `NEW` and `BLOCKED`. Everything
else means the order *may* exist at the broker, and the idempotency guard will
never transmit it again.

### Why the order is persisted before the broker call

The write happens **before** `place_order`, not after. If the process dies
between the write and the acknowledgement, reconciliation finds an order stuck
in `PENDING_SUBMIT`, fails, and refuses to trade until a human resolves it.

Writing afterwards would be worse in exactly the case that matters: a crash
after a successful send would leave an order we no longer know we placed.

---

## Idempotency

Two independent mechanisms:

**Deterministic keys.** `TradeIntent.idempotency_key(con_id=...)` hashes the
economic content of the intent — intent id, strategy, contract, target position.
Nothing that varies across a restart contributes. The same intent always
produces the same key.

**A database constraint.** `orders.idempotency_key` is `UNIQUE`.
`OrderRepository.insert_if_absent` relies on the constraint rather than a
read-then-write check, so two concurrent attempts cannot both succeed, and turns
the collision into "here is the order you already created".

**Intents are targets, not deltas.** A strategy says "be long 2", never "buy 2".
Asking twice for a target is naturally idempotent; asking twice for a delta is
not.

---

## Broker abstraction

```python
class Broker(ABC):
    async def connect() -> ConnectionInfo
    async def disconnect() -> None
    def is_connected() -> bool
    def get_connection_state() -> ConnectionState
    def get_connection_info() -> ConnectionInfo

    async def get_account_summary() -> AccountSummary
    async def get_positions() -> Sequence[BrokerPosition]
    async def get_open_orders() -> Sequence[BrokerOrderSnapshot]
    async def get_fills(*, since_seconds: int) -> Sequence[BrokerFill]

    async def get_contract_details(spec) -> Sequence[QualifiedContract]
    async def qualify_contract(spec) -> QualifiedContract

    async def request_market_data(contract) -> MarketDataTick
    async def cancel_market_data(contract) -> None

    async def place_order(request) -> PlaceOrderResult
    async def modify_order(request, broker_order_id) -> PlaceOrderResult
    async def cancel_order(broker_order_id) -> bool
    async def cancel_all_orders() -> int

    def last_heartbeat_age_seconds() -> float | None
```

Two rules every implementation must honour:

1. `get_connection_info().account_type` returns `UNKNOWN` unless the adapter has
   **positively established** the account identity. Unknown is always safe;
   guessing never is.
2. `place_order` refuses `transmit=False` unconditionally. This is a second
   barrier behind the gate, and both must agree.

### Error taxonomy

`BrokerError` carries a `retryable` flag, and the reconnect logic consults it
rather than guessing:

| Class | Retryable | Covers |
|---|---|---|
| `BrokerConnectionError` | yes | transport failures |
| `BrokerTimeoutError` | yes | request deadlines |
| `BrokerPermissionError` | **no** | entitlements, product-not-enabled, market data |
| `BrokerContractError` | no | resolution failures, ambiguity |
| `BrokerOrderRejectedError` | no | order refusals |
| `BrokerUnsupportedOperationError` | no | not implemented this phase |

Unknown IBKR error codes default to **non-retryable**. Assuming an unrecognised
failure is transient is how retry storms start.

---

## Contract resolution

```
ContractSpec (a request)  ──qualify──►  QualifiedContract (a broker's answer)
   symbol, secType,                        conId (required, > 0)
   exchange, currency,                     localSymbol, expiration,
   expiration (required)                   lastTradeDate, multiplier,
                                           minTick, tradingClass,
                                           tradingHours, liquidHours
```

Only a `QualifiedContract` can carry an order, and it cannot be constructed
without a positive `conId`, a concrete expiration, and `secType == FUT`.

Three rules, enforced in `ContractResolver` and nowhere bypassable:

- **Explicit expirations only.** No front-month heuristic, no automatic rollover.
- **Continuous futures are never qualified.** They are an analytics construct;
  the gate has a dedicated `CONTRACT_IS_CONTINUOUS_FUTURE` refusal as a backstop.
- **Ambiguity is an error.** More than one match stops the process. We do not
  pick one.

The constants in `app/contracts/solana.py` (multiplier, minimum tick) are
**reference values used to build a lookup request and to drive mock mode only**.
Every number that affects a real order is overwritten by IBKR's `contractDetails`
at qualification time. They have never been verified against a live IBKR session,
which is exactly why nothing trusts them.

---

## Market data and freshness

Freshness is deliberately conservative. Data is fresh only when **all** hold:

- `MARKET_DATA_MAX_AGE_SECONDS > 0` (unconfigured is not "no limit")
- a quote exists for the contract
- `0 <= age <= max_age` (a negative age means a clock problem)

The manager clears its cache on disconnect. Retaining quotes across a
disconnect would let the system believe it has fresh data while the feed is
gone — precisely the failure the check exists to prevent.

---

## Persistence

SQLite with WAL, forward-only migrations, behind a `Database` abstraction so
callers never touch `sqlite3`.

| Table | Contents |
|---|---|
| `orders` | full lifecycle, `idempotency_key` UNIQUE |
| `fills` | executions, `execution_id` primary key (idempotent ingestion) |
| `positions` | current book |
| `signals` | every intent, accepted or rejected, with reasons |
| `risk_decisions` | every decision, with reasons and detail |
| `bot_events` | application lifecycle |
| `broker_events` | connection changes and API errors |
| `contract_metadata` | qualified contract detail |
| `daily_performance` | per-UTC-day totals, drives the daily-loss limit |
| `application_state` | durable key/value, including the kill-switch latch |
| `schema_migrations` | applied versions |

### PostgreSQL portability

The promise is kept by the schema, not just by the abstraction:

- **Timestamps** are ISO-8601 UTC *text*, not SQLite julian days. The same
  strings load into a `timestamptz`.
- **Money and prices** are decimal *strings*, not floats. Binary floating point
  is the wrong representation for prices, and text maps cleanly onto `NUMERIC`.
  `test_prices_survive_as_exact_decimals` asserts this end to end.
- **No SQLite-specific SQL** outside `app/state/database.py`.

Swapping the backend means a second `Database` implementation and a second
migration list. No repository or application code changes.

---

## Observability

Every application start gets a `run_id`. Every trade lifecycle gets a
`correlation_id` that flows through signal → risk decision → order → broker
response → fill, in both the logs and the database. One trade can be
reconstructed end to end from either.

Logs are one JSON object per line, rotated, persisted outside the container, and
passed through a redaction filter installed on every handler. Account
identifiers are masked (`DU1234567` → `DU***567`) — enough to distinguish
accounts during an incident, not enough to be worth harvesting.

---

## Extension points

The architecture is built so that the following are additive:

| To add | Touch only |
|---|---|
| A real strategy | `app/strategy/`, register it in `STRATEGY_REGISTRY` |
| Technical indicators | inside the strategy; it already receives standard `Quote` objects |
| Historical bars / backtesting | a `Broker` implementation that replays history, or feed `Quote` objects straight into `Strategy.handle_quote` |
| Multiple strategies | a strategy list in the runtime; `SignalValidator` already takes a set of known strategies |
| Another broker | one `Broker` implementation |
| PostgreSQL | one `Database` implementation |
| Bracket / stop orders | widen `TRANSMITTABLE_ORDER_TYPES`; the models and persistence already exist |
| A monitoring dashboard | read `/status`; add a Traefik route deliberately, with auth |

Nothing on that list requires changing the safety machinery — which is the point.
