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

### Fixed

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
