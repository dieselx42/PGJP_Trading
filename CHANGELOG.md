# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
