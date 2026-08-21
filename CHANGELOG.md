# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `app/broker/checkout.py` and `app.cli ibkr-checkout` — a read-only checkout of
  a real IB Gateway session. Every socket path in the IBKR adapter was
  unverified: the unit tests around it drive fakes because there has never been
  a gateway to point it at. The checkout connects once on the admin client id,
  runs each read-only call the system depends on, and reports PASS / FAIL / SKIP
  per probe with the evidence it saw, so a wrong answer is visible rather than
  something an operator has to notice.

  It cannot place an order. The broker is typed as a `ReadOnlyBroker` Protocol
  with no write methods, so `mypy --strict` rejects one; it refuses to run
  unless the interlocks are engaged and the mode is `paper`; and its final probe
  forces every session-side condition green and requires `TransmitGate` to
  refuse anyway. An empty report is a failure, not a pass — the distinction that
  `verify_running.sh` got wrong before it was deleted.

- `docs/IBKR_PAPER_CHECKOUT.md` — the step-by-step for the first real gateway
  session. It exists because "add IBKR credentials" is the obvious next thought
  and there is no such step: the credential is typed into IB Gateway's login
  window and never enters this repository. The guide leads with that, then
  covers the paper account, the API lockdown, the Mac-local `.env` (whose
  container paths are a real trap), and the one expected `POSTURE_NOT_APPROVED`
  that would otherwise look like a fault.

- `docs/IBKR_GATEWAY_VPS.md` — running IB Gateway on the Hostinger host, with
  interactive login over an SSH-tunnelled VNC session and no login automation.

  It revises an earlier recommendation. IB Gateway has no bind-address setting,
  only an "allow connections from localhost only" checkbox, so reaching it from
  a bridged container would mean unticking that box and letting the API listen
  on every interface — leaving a firewall rule as the only thing between port
  4002 and the internet. Joining the host network namespace instead keeps the
  box ticked, which makes "not reachable from outside" a property of the socket
  rather than a rule that has to stay correct.

  Sequenced so the adapter is proven from a host virtualenv before the container
  networking changes at all. The two fail in similar-looking ways and are much
  easier to diagnose apart.

- `MARKET_DATA_IS_DELAYED` — a new transmit-gate interlock. IBKR serves delayed
  quotes in the same tick fields as real-time ones, so an unsubscribed account
  produced data that passed every freshness check while carrying prices fifteen
  minutes old. Delayed ticks are flagged where they are parsed and refused at
  the gate, with no setting to permit them. `GateContext.market_data_is_delayed`
  defaults to `True`, the refusing answer, like every other field on that class.

- IB Gateway runs as a compose service (`ghcr.io/gnzsnz/ib-gateway`) with IBC
  handling login, replacing the host install with its manual VNC login. This
  reverses the original brief's prohibition on storing IBKR credentials, on an
  explicit operator decision recorded in `SECURITY.md` rather than left as a
  contradiction between the documentation and the running system.

  The bot still holds no credential. The gateway reads `.env.ibgateway`; the
  bot's `.env` is still checked on every deployment and still fails it if a
  credential-shaped variable appears. What the trade buys is unattended
  operation, persistent settings, and a stronger port guarantee — the API ports
  are never published to a host interface, so the bot reaches the gateway across
  a private bridge and there is nothing on the host for a firewall to protect.
  What it costs is that root on the VPS is now equivalent to the IBKR password.

- The TWS API is vendored into the image from IBKR's own distribution, pinned to
  a version and verified against a SHA-256 checksum in a build stage. This
  settles the packaging question in `docs/IBKR_API_NOTES.md` as Option A. The
  bot container was stdlib-only, so `TRADING_MODE=paper` inside Docker failed at
  `SOCKET_CONNECT` with the adapter's own "ibapi is not installed" refusal —
  correct behaviour, and a decision that could not be deferred any further.

  The checksum is the substance of it. Without one, "downloaded from IBKR" means
  "downloaded from whatever answered that hostname during the build". The pinned
  hash is of the archive already used to build the host virtualenv that first
  proved the adapter works against a live gateway.

- **Backtesting, phase 1 — ingestion and storage.** A `bars` table (schema
  version 2), `app/backtest/`, and `app.cli bars-import` / `bars-info`.

  `source` is part of the primary key, not decoration. The price history
  available today is Solana *spot* and this system trades CME futures — no
  basis, no roll, no CME session breaks. Provenance travels on every bar and
  `Bar.is_proxy` says so out loud, precisely so a run over spot data can never
  later be read as a statement about futures.

  **Gaps are reported, never filled.** A missing hour is a fact about the data;
  an invented price is indistinguishable from a real one by the time it reaches
  a strategy, and every number downstream of it is then wrong undetectably.

  Import is idempotent on `(source, symbol, interval, opened_at)` with
  `INSERT OR IGNORE` — a year of 1-minute data is hundreds of pages and any one
  can fail, and a stored bar must never be rewritten under a backtest already
  run against it.

  The CSV loader **requires an explicit column mapping** and refuses to guess.
  A CSV whose columns are inferred is one that silently loads high as low the
  day somebody exports it differently.

  Verification is uneven and the code says so: `CsvBarSource` is fully tested,
  and `BinanceBarSource`'s parsing is tested against a recorded response shape
  — the kline indices especially, since an off-by-one swaps high and low and
  every bar stays structurally valid. Its **network call is unverified**: this
  environment denies outbound access to `api.binance.com`. Hence `--limit`:
  fetch ten bars on a new source, look at them, then fetch a year.

- `app.cli place-order` and `app/execution/operator_order.py` — the only way a
  human can cause this system to send an order. The write path (`place_order`,
  `orderStatus`, `execDetails`, `commissionReport`) has never run, and arming a
  strategy to find out is the wrong first experiment: a strategy fires on a
  market tick, at a moment nobody chose, with nobody watching. It is also
  currently impossible, because `NoOpStrategy` returns no intents.

  **It is not a bypass.** The order travels the identical path a strategy order
  takes, by calling the same `_handle_intent` the tick loop calls. It sets no
  configuration, clears no kill switch and raises no limit; every refusal the
  gate or risk manager can produce, it produces here, and reports rather than
  works around. Three tests read the source with `ast` and fail if it ever calls
  a broker directly, constructs its own gate, or touches the kill switch.

  Without `--confirm` it previews and sends nothing. The confirmation token is
  the contract's broker-reported `local_symbol`, so it cannot be typed from
  memory — only from a preview that actually resolved a contract at IBKR. A
  `--yes` flag would prove the operator can type `--yes`; requiring a value only
  the system can supply proves they looked. Live mode is refused before a
  broker, database or application object is constructed.

  Positions are absolute targets, not deltas, as everywhere else: running it
  twice leaves you long 1, not 2.

### Changed

- `scripts/verify_safety.sh` takes a **posture**: `halted` (the default, and
  what it has always checked) or `paper-armed`, selected explicitly at the
  shell with `DEPLOY_POSTURE=paper-armed bash scripts/deploy.sh main`.

  RUNBOOK step 10 told you to arm the system, and `deploy.sh` then refused to
  deploy because the configuration was not the halted one — so the step was
  impossible to follow. The same shape as the step 9 contradiction, and found
  the same way: by running it.

  `paper-armed` is not "skip the checks". It is a different set, and several
  are stricter: a limit left at `0` **fails**, because zero means NOT CONFIGURED
  and an armed system carrying one would refuse every order while appearing
  ready. `MARKET_DATA_MAX_AGE_SECONDS=0` and an unset `DEFAULT_CONTRACT_MONTH`
  fail for the same reason. Sanity ceilings catch a fat-fingered extra digit.

  `LIVE_TRADING_ENABLED=false` and `TRADING_MODE != live` are asserted under
  **every** posture, and there is deliberately no `live-armed`. The default is
  `halted`, so a deploy that says nothing gets the refusing answer, and
  `deploy.yml` never sets the variable.

- The IBKR adapter, `docs/IBKR_API_NOTES.md` and `app/contracts/solana.py` no
  longer claim to be unverified. The adapter met a real gateway on 2026-08-08
  and most of what those files warned about is now confirmed working — a stale
  "do not trust this" is its own hazard. What remains unverified is listed
  explicitly rather than left as a blanket caveat.

- `RUNBOOK.md` recommends running IB Gateway on a local machine before the VPS.
  Whether the adapter works and whether it can run headless and unattended are
  independent questions, and answering the first does not require solving the
  second.
- The "ibapi is not installed" error no longer names a specific install command.
  Which distribution to install is the open supply-chain decision in
  `docs/IBKR_API_NOTES.md`, and the error should not pre-empt it.

### Added (strategy candidates)

- `app/strategy/trend.py` — `sol-trend`, a daily Donchian trend strategy
  (20-day breakout entry, 2×ATR stop, 3×ATR trail, 10-day channel exit),
  aggregating UTC calendar days from the 1-minute feed. Built for the cost
  floor the ORB post-mortem measured: a $0.373/SOL round trip is 25–93% of the
  ORB's targets but 1–4% of the multi-day legs this strategy rides. It has no
  breakeven rule, deliberately — the ORB's $0.05 lock below the cost floor
  produced 58 arithmetically guaranteed losses a year. Backtest-only until the
  team judges its replay; registered in the same registry, replayed by the
  same engine, costs and attribution identical to the ORB's runs.

- `scripts/strategy_compare.sh` — the ORB baseline, the re-derived big-range
  ORB (Candidate A), and `sol-trend` (Candidate B), each with real costs and
  with costs zeroed, six runs in one pass at the document's 40 contracts, then
  the digest. One command answers "which candidate, and how much of the answer
  is the cost model".

### Fixed

- **The daily-loss limit could refuse the order that closed the losing
  position.** `check_daily_loss` ran on every intent with no reduce-only
  branch, so once the day's loss crossed `MAX_DAILY_LOSS_USD` the exit was
  rejected too — the position stayed open and went on losing, with no way out
  but an operator noticing. A limit that traps a loser is the opposite of a
  risk control. A breach is now waived for an order that *strictly reduces*
  exposure, and for nothing else: opening, adding, and reversing through flat
  are all still refused, and an **unconfigured** limit (zero, meaning NOT
  CONFIGURED) still refuses everything including exits, so a misconfigured
  deployment cannot quietly acquire an exit path a correct one lacks. Found by
  an adversarial design review; four tests pin it, two of them
  mutation-verified in both directions (removing the exemption fails; widening
  it to permit reversals fails).

- The backtest fed the replay's **cumulative** realized P&L to the
  MAX_DAILY_LOSS_USD check, which is a *daily* limit. The first day a losing
  strategy's running total crossed the limit, every later entry in the replay
  was silently refused — a year-long result was really a truncated one wearing
  the year's name. At 40 contracts the ORB baseline crosses $30k months in, so
  every full-size comparison run was affected. The engine now resets the
  daily-loss baseline at each UTC midnight, matching the live path's
  per-trade-date `DailyPerformance` (which was already correct). Pinned by a
  mutation-verified test: a day-1 breach refuses day-1 entries and day-2
  entries fill.

- `Decimal("nan")` passed both strategies' tunable validation without raising
  and then poisoned every later comparison with `InvalidOperation`. Non-finite
  values are now refused with the same `ValueError` as any other bad input.

- The ORB NY session was pinned to a fixed **14:30 UTC**, which is the 9:30 ET
  equity open only in winter — through the whole DST period (March–November) it
  opened an hour late, at 10:30 ET. The document itself gave the NY open both
  ways ("14:30 UTC" and "9:30 AM ET"), a contradiction that resolves in favour
  of the equity open the strategy keys off. The session is now anchored to
  **9:30 America/New_York** and tracks DST: 13:30 UTC in summer, 14:30 in
  winter, both 9:30 ET. London stays the document's fixed 08:00 UTC. A new
  `SessionOpen(hour, minute, zone)` carries the anchor; `_roll_session` resolves
  it to the bar's local date each session. The fixed-UTC hours remain reachable
  for diagnostics via `--sessions HH:MM`, so the prior behaviour can still be
  replayed. Storage stays UTC throughout — only the session's *derivation*
  changed. Consequence: any backtest re-run now evaluates the 9:30 ET open, so
  the prior −$81k baseline (computed at 10:30 ET) will move.

- `request_market_data` waited a fixed half second for the first tick of a new
  subscription and then reported whatever it had. Streaming data has no
  completion callback, so the first tick has to be waited for rather than
  requested — and half a second is not long enough for a thin contract, which
  meant an empty tick was returned as though nobody were quoting. Two
  observationally identical results, one a fact about the market and one a bug.

  It now polls for the first tick, an error, or a ten-second deadline, whichever
  comes first, against a monotonic clock. Returning empty after the *full* wait
  is a real quiet market.

  The defect was invisible until the first minute market data actually worked:
  every earlier run against the gateway was refused with `354` before the timing
  could matter, so the code path had never once been exercised. Found by the
  read-only checkout, which is now the fourth defect it has found that the unit
  tests could not — they drive fakes that answer instantly.

  Confirmed fixed on 2026-08-19 against the live paper gateway: real-time
  bid/ask/last on MSLQ6 during liquid hours, `is_delayed` false. `MARKET_DATA`
  had never returned a green result before that run — which is not the same
  thing as a probe that returns green, and is worth distinguishing.

- **Closing a position was inexpressible.** `TradeIntent.is_actionable`
  returned `False` for every `FLAT`/`0` target, on the reasoning that
  "FLAT-to-flat intents carry no work" — true only if you are *already* flat,
  which an intent cannot know. The target is the destination; whether work is
  required depends on the origin.

  So the validator rejected every exit as `SIGNAL_NOT_ACTIONABLE`, while risk
  and the gate had already computed the correct `SELL 1`. The system could open
  a position and had no way to say "close it". Found trying to close the first
  position it ever held. A unit test asserted the broken behaviour as correct.

  Replaced by `requires_order_from(current_position)`, and the validator no
  longer asks: it validates the *shape* of a signal, not the state of the book.
  The genuine no-op — a target already held — is caught downstream where the
  position is known, by a computed quantity of zero and `NO_CHANGE`.

- **`place-order` reported `SUBMITTED` for orders it had not submitted**, and
  described an order that never reached the broker as "no status yet, it may
  well be working". Both are now reported for what they are: `NOT_SUBMITTED`,
  and a note pointing at whichever approver refused.

- **A fill never moved the position book, and the resulting discrepancy could
  never clear.** Three things combined, found within minutes of the first fill
  this system produced:

  `place-order` recorded the order's status and stopped, so nothing applied the
  execution to the book. `main._reconcile` adopted broker positions **only when
  reconciliation succeeded** — and it could not succeed while they disagreed, so
  a discrepancy was self-perpetuating. And `fills_to_position_deltas` existed in
  `reconciliation.py` with no callers: `reconcile` counted fills into
  `fills_seen` and drew no conclusion from them, having seen the exact `+1` that
  explained the exact `+1` delta.

  The effect was that **the system could open a position and then not close
  it** — opening created the state that blocked closing.

  Fills are now ingested before the comparison, deduplicated on the broker's own
  execution ids so re-reading the same 24-hour window cannot double-count. The
  distinction that matters is preserved: a position explained by a fill we
  watched clears, and a position that appeared from nowhere still halts.

- **Reconciliation ran once per connection and never again.** Everything it
  exists to catch — a missed fill, a changed position, an order from another
  client — could develop mid-session and stay invisible until the next
  reconnect, which with `restart: unless-stopped` and a healthy gateway could be
  days. Now on a timer (`RECONCILE_INTERVAL_SECONDS`, default 300).

  Recovery is as automatic as the drop, because a `SAFE` that needs a restart to
  clear is a `SAFE` nobody will trust. The first attempt at that recovery was
  gated on `state is SAFE`, which never fires because `_reconcile` leaves the
  state at `RECONCILING` — caught by the test written for it, not by review.

- **`orderStatus` logged and returned.** `_IBSession.order_statuses` was
  declared and never written to, so every status update IBKR sent was thrown
  away. Nothing downstream could learn whether an order was accepted, rejected
  or cancelled — an order counted as "submitted" because bytes had reached a
  socket.

  Observed 2026-08-19 on the first order this system ever sent: IBKR accepted
  it (permId 2106979881) and cancelled it moments later, because the ephemeral
  command that placed it disconnected five milliseconds after `placeOrder`.
  IBKR reported both events. Neither was recorded, and establishing what had
  happened took an hour of manual probing with `reqCompletedOrders`.

  Statuses are now recorded, `IBKRBroker.await_order_status` waits for one, and
  `place-order` waits before disconnecting and persists what it learns. Its
  report distinguishes *cancelled*, *rejected*, *working* and **undetermined** —
  silence from the broker says nothing, and claiming otherwise was the original
  defect in a different form.

  An unmapped IBKR status maps to `ERROR`, never to something benign.

- **Reconciliation could not see orders it did not place.** `get_open_orders`
  used `reqOpenOrders`, which returns only the *calling client's* orders. That
  made the check blind in both directions:

  *False alarm* — an order placed on the admin client id (`place-order`,
  `cancel-all-orders`) was invisible to the trading process, which reported
  `unknown_at_broker` and dropped to `SAFE` over a perfectly good order.
  Observed on 2026-08-19 with a real resting order: the first order this system
  ever sent broke the bot that sent it.

  *False clear*, which is the one that matters — an order placed by a human in
  TWS, another process, or a stale client id was equally invisible.
  Reconciliation reported success and the system would have traded alongside
  exposure it did not know existed. That is the precise failure reconciliation
  exists to prevent, and it could never have caught it.

  Now `reqAllOpenOrders`. The comparison logic was never wrong — it faithfully
  reported what it was given — so no test of it could have found this. The
  defect was one API call upstream, in what the adapter asked the broker for.

- **`TRADING_PERMISSION_UNAVAILABLE_AT_BROKER` was impossible to satisfy.** The
  TWS API exposes no "may this account trade CME futures" flag, so the IBKR
  adapter reports `None` for every account — and `None` was treated as *not
  permitted*, by both the risk manager and the gate. No configuration and no
  account could ever pass it: the deployed system could not have transmitted an
  order under any circumstances.

  It survived the entire life of the project because `MockBroker` reports
  `True`. Every test passed. Only a real account revealed it, on the first
  fully-armed run.

  `futures_permission` now means three things rather than two. `True` and
  `False` are both *observed* and both authoritative — an observed refusal
  **overrides** an operator's `SOL_FUTURES_PERMISSION_READY=true`, because the
  broker decides. `None` means undetermined and falls back to the operator's
  declaration. `status` reports which of the three produced the result, so a
  declaration is never mistaken for an observation.

- `app.cli check-permission` — observes futures permission with a `whatIf`
  preview rather than asking for a flag that does not exist. IBKR prices an
  order the account may trade and refuses one it may not, so the operator's
  declaration can rest on evidence; it is also the only thing here that would
  notice permission being *revoked*. It cannot place an order: `whatIf` is
  assigned a literal `True` on the line the order is built and is not a
  parameter, which two tests assert by parsing the AST. It lives outside
  `checkout.py` deliberately, so the read-only checkout keeps its no-write-
  methods guarantee.

- `app.cli verify --posture` completes the #15 fix. `verify_safety.sh` learned
  about postures but the post-deploy runtime check did not, so `deploy.sh`
  reported `POSTURE_NOT_APPROVED` and exited non-zero *after* deploying
  successfully. Under `paper-armed` the differences from the halted posture are
  expected and no longer reported as failures — but `LIVE_TRADING_ENABLED` and
  `CAN_TRANSMIT_LIVE_ORDERS` are still asserted exactly as under `halted`.

- The operator-order preview validated through the *same* `SignalValidator` the
  pipeline then used. `validate()` records an intent id whenever it accepts —
  that is what makes a replayed signal harmless — so the real submission was
  refused as a duplicate of its own preview, and reported `SUBMITTED` with a
  null outcome. Success-shaped silence, the worst possible form.

  The preview now asks a separate validator: asking a question must not answer
  it. Caught immediately by the one control test that proves the authorised
  baseline *does* transmit, without which all fourteen refusal tests around it
  would have passed against a command that could never send anything.

## [0.1.0] — 2026-08-07

Initial infrastructure. **This release cannot place an order**, by design and by
test.

### Safety

- `TransmitGate` — the single choke point before any order reaches a broker.
  Twenty independent interlocks, all of which must pass. Implemented as a pure
  function over a frozen context whose fields default to unsafe-to-trade, so an
  incomplete context denies rather than permits. Collects every failing reason
  rather than short-circuiting.
- `RiskManager` — an independent approver checking numeric limits, connectivity,
  account availability, reconciliation, market-data freshness, contract
  qualification, permissions, duplicates, and the kill switch. Both it and the
  gate must approve.
- Zero risk limits mean *not configured*, which means *trading not authorised*.
  Never unlimited. Each limit has its own `*_NOT_CONFIGURED` rejection reason.
- Kill switch with two sources — the `KILL_SWITCH` variable and a durable
  database latch — OR'd together, failing closed if the store is unreadable.
  Deliberately one-way: no `kill-switch-off` command and no `disengage` method.
- Configuration refuses to start on a half-armed live setup
  (`LIVE_TRADING_ENABLED` and `TRADING_MODE` must agree).
- Account identity derived from broker-reported account ids, never from
  configuration or from which port was dialled. The IBKR adapter disconnects
  rather than remaining attached to an account type that does not match the mode.
- Order idempotency from a deterministic key plus a `UNIQUE` database
  constraint. Orders are persisted *before* the broker call, so a crash mid-flight
  leaves evidence that stops trading rather than an order we forget we sent.
- Broker adapters refuse `transmit=False` unconditionally — a second barrier
  independent of the gate.

### Broker

- Abstract `Broker` interface; nothing above it knows Interactive Brokers exists.
- `MockBroker`: deterministic seeded prices, stable synthetic contract ids,
  simulated order lifecycle, and failure injection for connection failures,
  permission denials, dropped sessions, and wrong account types.
- `IBKRBroker` against the official TWS API (`ibapi`), as an optional extra.
  Tolerates the 10.30 `error` signature change. Classifies IBKR error codes so
  permission failures are never retried. **Untested against a live gateway** —
  see `docs/IBKR_API_NOTES.md`.

### Trading pipeline

- `MarketDataManager` with UTC timestamps, staleness protection, cache clearing
  on disconnect, and one-shot recording of permission denials.
- `Strategy` base class and `NoOpStrategy`. Strategies receive standard `Quote`
  objects, have no broker handle, and cannot express an order.
- `TradeIntent` expressing an absolute target position rather than a delta, so a
  replayed signal is naturally idempotent. Direction and target must agree.
- `SignalValidator` with durable duplicate detection seeded from the database.
- `OrderManager` implementing the full pipeline.
- Order models for market, limit, stop, stop-limit, and bracket. Only market and
  limit transmit in this phase.
- `ContractResolver` requiring explicit expirations, refusing continuous futures,
  and treating an ambiguous match as an error rather than picking one.

### State and observability

- SQLite with WAL, forward-only migrations, and eleven tables behind a
  `Database` abstraction. Timestamps as ISO-8601 UTC text and prices as decimal
  strings, so PostgreSQL can replace it without touching callers.
- Position book and reconciliation. Any discrepancy keeps the system in `SAFE`
  and requires human intervention.
- Explicit application state machine; `READY` is reachable only through
  successful reconciliation.
- Structured JSON logging, rotated, with a redaction filter on every handler and
  masked account identifiers.
- Run ids and correlation ids linking signal → risk → order → broker → fill in
  both logs and the database.
- Loopback-only `/health` and `/status`, stdlib-only, GET-only, size-capped.

### Operations

- `Dockerfile`: non-root uid 10001, no installed packages, read-only root
  filesystem, in-container healthcheck, no `.env`.
- `docker-compose.yml`: no published ports, no Traefik labels, dropped
  capabilities, `no-new-privileges`, log rotation.
- Operator CLI and Makefile targets. No target or command can enable trading or
  clear the kill switch.
- `scripts/vps_audit.sh` — read-only server audit.
- `scripts/verify_safety.sh` — asserts a server `.env` is not live-configured.
- `scripts/deploy.sh` — server-side deploy that never writes `.env` and rolls
  back on a failed health check.
- GitHub Actions CI: lint, strict mypy, 315 tests, a repository safety audit,
  and a Docker build that starts a container and asserts through `/status` that
  it cannot transmit.
- Deploy workflow is `workflow_dispatch` only. A git push cannot reach the
  trading server.

### Testing

- 315 tests: 277 unit, 38 integration.
- `tests/unit/test_critical_safety.py` implements the ten mandated safety tests,
  each degrading a single condition from a fully-authorised baseline, plus the
  control test that proves the baseline actually permits — without which the
  other ten would pass vacuously.

### Fixed during development

- `state/database.py` passed `name` in a logging `extra`, which collides with a
  reserved `LogRecord` attribute and raised `KeyError` on the first migration —
  i.e. on every fresh start. Caught by the test suite.
- `logging_config.redact_text` applied one substitution strategy to all
  patterns, so bare GitHub tokens and PEM private key blocks were echoed back
  verbatim instead of being redacted.

### Not included, deliberately

Trading strategy, automatic contract rollover, backtesting engine, bracket/stop
transmission, emergency liquidation, monitoring dashboard, Traefik route.

[0.1.0]: https://github.com/dieselx42/PGJP_Trading/releases/tag/v0.1.0
