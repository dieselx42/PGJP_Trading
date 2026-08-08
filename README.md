# sol-futures-trading-bot

Automated trading infrastructure for **CME Solana futures** through **Interactive
Brokers**.

> **Current state: this system cannot place an order.**
> It is deployed in `mock` mode with the kill switch engaged, order transmission
> disabled, and every risk limit unconfigured. That is deliberate and verified by
> the test suite. Enabling trading later requires explicit configuration changes
> on the server — not a code change, and not a deployment.

---

## Contents

- [What this is](#what-this-is)
- [Safety model](#safety-model)
- [Architecture](#architecture)
- [Project structure](#project-structure)
- [Installation](#installation)
- [Local development](#local-development)
- [Mock mode](#mock-mode)
- [Trading modes](#trading-modes)
- [Configuration](#configuration)
- [Testing](#testing)
- [Docker](#docker)
- [CI/CD](#cicd)
- [Deployment](#deployment)
- [Operator commands](#operator-commands)
- [IBKR integration](#ibkr-integration)
- [What is deliberately not built yet](#what-is-deliberately-not-built-yet)

---

## What this is

The **infrastructure** for an automated futures trading system: broker
abstraction, contract resolution, market data, risk management, order
management, reconciliation, persistence, monitoring, and deployment.

It is explicitly **not** a trading strategy. The only strategy that exists is
`NoOpStrategy`, which consumes market data and never trades. The real strategy
is a later phase, and the architecture is built so that adding it changes
nothing below the strategy layer.

**Instruments**

| Symbol | Product              | Exchange | Currency |
|--------|----------------------|----------|----------|
| `MSL`  | Micro Solana Futures | CME      | USD      |
| `SOL`  | Solana Futures       | CME      | USD      |

`MSL` is the default because it is the smaller contract.

---

## Safety model

This is financial software. It **fails closed**: a bug, a missing variable, a
dropped connection, or a restart results in orders being *refused*, never sent.

### The interlock chain

Every order passes through both an independent risk assessment and a final
transmit gate. They check different things and both must approve:

```
Strategy → Signal validation → Risk Manager → Order Manager → Transmit Gate → Broker Adapter → IBKR
```

There is no path around this chain. Strategies have no broker handle and cannot
express an order — only a `TradeIntent`.

### What the transmit gate requires

All of these must be positively true, or the order is refused:

| Check | Refusal reason |
|---|---|
| Kill switch off | `KILL_SWITCH_ENGAGED` |
| Transmission allowed | `ORDER_TRANSMIT_NOT_ALLOWED` |
| Mode is not `disabled` | `TRADING_MODE_DISABLED` |
| Live mode also has `LIVE_TRADING_ENABLED=true` | `LIVE_TRADING_NOT_ENABLED` |
| Account type known | `BROKER_ACCOUNT_TYPE_UNKNOWN` |
| Account type matches the mode | `ACCOUNT_TYPE_MISMATCH_EXPECTED_*` |
| Application state is `READY` | `APPLICATION_NOT_READY` |
| Broker connected | `BROKER_NOT_CONNECTED` |
| Account data retrieved | `ACCOUNT_DATA_UNAVAILABLE` |
| Contract qualified | `CONTRACT_NOT_QUALIFIED` |
| Contract is a dated future | `CONTRACT_IS_CONTINUOUS_FUTURE` |
| Positions reconciled | `POSITIONS_NOT_RECONCILED` |
| Open orders reconciled | `OPEN_ORDERS_NOT_RECONCILED` |
| Market data freshness configured | `MARKET_DATA_MAX_AGE_NOT_CONFIGURED` |
| Market data present and fresh | `MARKET_DATA_UNAVAILABLE` / `MARKET_DATA_STALE` |
| Clock sane (no future ticks) | `MARKET_DATA_TIMESTAMP_IN_FUTURE` |
| Futures permission configured (paper/live) | `SOL_FUTURES_PERMISSION_NOT_CONFIGURED_READY` |
| Broker reports permission | `TRADING_PERMISSION_UNAVAILABLE_AT_BROKER` |
| Strategy enabled | `STRATEGY_DISABLED` |
| Risk approved | `RISK_CHECKS_NOT_PASSED` |

These are independent, so several fail at once and fixing one changes nothing.
On the verified deployment, a fully connected and reconciled bot still reports
**five** simultaneous refusals:

```
KILL_SWITCH_ENGAGED
ORDER_TRANSMIT_NOT_ALLOWED
APPLICATION_NOT_READY
MARKET_DATA_MAX_AGE_NOT_CONFIGURED
RISK_CHECKS_NOT_PASSED
```

Before the broker connects, all **fifteen** fail.

### Zero means prohibited

A risk limit of `0` means **not configured**, which means **trading not
authorised**. It never means unlimited. Each unconfigured limit produces its own
rejection reason, e.g. `MAX_POSITION_CONTRACTS_NOT_CONFIGURED`.

### Independent account verification

Account type is determined from the **broker-reported account id**, never from
configuration and never from which port was dialled. `paper` mode connected to a
live account refuses every order — and the IBKR adapter drops the session
entirely rather than staying attached to the wrong account.

### No half-armed configurations

The application **refuses to start** if `LIVE_TRADING_ENABLED=true` while
`TRADING_MODE` is not `live`, and vice versa. Going live always requires two
coordinated, deliberate edits.

See [SECURITY.md](SECURITY.md) for the full security posture.

---

## Architecture

```
                    ┌──────────────┐
   IB Gateway ─────►│ IBKRBroker   │─┐
   (or nothing)     └──────────────┘ │   ┌──────────────────┐
                                     ├──►│  Broker (ABC)    │
                    ┌──────────────┐ │   └────────┬─────────┘
                    │ MockBroker   │─┘            │
                    └──────────────┘              │
                                                  ▼
                             ┌────────────────────────────────────┐
                             │        MarketDataManager           │
                             │  (timestamps, freshness, staleness)│
                             └────────────────┬───────────────────┘
                                              │ Quote
                                              ▼
                             ┌────────────────────────────────────┐
                             │           Strategy                 │
                             │  (NoOpStrategy — no broker access) │
                             └────────────────┬───────────────────┘
                                              │ TradeIntent
                                              ▼
                             ┌────────────────────────────────────┐
                             │        SignalValidator             │
                             │  (shape, symbol, staleness, dupes) │
                             └────────────────┬───────────────────┘
                                              ▼
                             ┌────────────────────────────────────┐
                             │          RiskManager               │
                             │  (limits, exposure, connectivity)  │
                             └────────────────┬───────────────────┘
                                              ▼
                             ┌────────────────────────────────────┐
                             │          OrderManager              │
                             │  (idempotency, persistence)        │
                             └────────────────┬───────────────────┘
                                              ▼
                             ┌────────────────────────────────────┐
                             │        TransmitGate  ⛔            │
                             │  every interlock must pass         │
                             └────────────────┬───────────────────┘
                                              ▼
                                       Broker Adapter
```

[ARCHITECTURE.md](ARCHITECTURE.md) has the full detail, including the state
machine and the order lifecycle.

---

## Project structure

```
app/
    main.py               Application runtime and lifecycle
    cli.py                Operator commands
    config.py             Strict, fail-closed environment configuration
    enums.py              Shared domain enumerations
    logging_config.py     Structured JSON logging with secret redaction

    safety/               ── the interlocks ──
        gate.py           TransmitGate: the last check before any order
        killswitch.py     Config flag OR durable latch; no way to clear it

    broker/
        base.py           Abstract Broker interface
        models.py         Broker-neutral models and error taxonomy
        mock_broker.py    Deterministic simulated broker
        ibkr_broker.py    ALL IBKR-specific code lives here, and only here

    contracts/
        models.py         ContractSpec (request) vs QualifiedContract (answer)
        solana.py         MSL / SOL product definitions
        resolver.py       Qualification; refuses ambiguity and continuous futures

    market_data/          Quotes, freshness, staleness protection
    strategy/             Strategy base class + NoOpStrategy
    signals/              TradeIntent + validation
    risk/                 RiskManager + numeric limits
    execution/            Order models + OrderManager (idempotency)
    portfolio/            Position book + reconciliation
    state/                SQLite, migrations, repositories
    monitoring/           Health, status, loopback HTTP server
    utilities/            Time (UTC-only) and identifier helpers

tests/
    unit/                 277 tests, including test_critical_safety.py
    integration/          38 tests against the real loop, DB, and HTTP server

scripts/
    vps_audit.sh          Read-only VPS audit (run this on the server)
    verify_safety.sh      Asserts a server .env is not live-configured
    deploy.sh             Server-side deploy; never writes .env
    bootstrap_dirs.sh     Creates ./data and ./logs with the container's uid
    healthcheck.py        Docker healthcheck (runs inside the container)
```

### Deviations from the originally suggested layout

| Change | Why |
|---|---|
| Added `app/safety/` | Interlocks are invariants, not tunable risk numbers. Keeping them in their own package makes it obvious which file must never be softened. |
| Added `app/enums.py` | One dependency-free definition of every shared enum, so no import cycles are possible between config, safety, broker, and persistence. |
| Added `app/contracts/models.py` | Separates a contract *request* from a broker-confirmed *answer*; only the latter can carry an order. |
| Added `app/monitoring/server.py` | The HTTP server is transport, separate from the health/status logic it serves. |
| Added `app/cli.py` | Operator commands, rather than a pile of shell scripts that would each need their own safety checks. |
| No `app/utilities/__init__.py` exports | Helpers are imported explicitly so it is always clear where `utc_now()` comes from. |

---

## Installation

**Requirements:** Python 3.12+, Docker with the Compose v2 plugin, Git.

```bash
git clone https://github.com/dieselx42/PGJP_Trading.git
cd PGJP_Trading
make install          # creates .venv and installs dev dependencies
```

There are **no runtime dependencies**. In `disabled` and `mock` modes the
application runs on the Python standard library alone. Only the optional `ibkr`
extra adds a third-party package, and only when you deliberately install it.

---

## Local development

```bash
make check            # lint + typecheck + tests, the same as CI
make test             # tests only
make test-safety      # only the critical trading-safety tests
make lint             # ruff check + format check
make typecheck        # mypy, strict
make format           # apply formatting and safe fixes
make coverage         # tests with a coverage report
```

To run the bot outside Docker:

```bash
cp .env.example .env.local
# edit .env.local: set DEFAULT_CONTRACT_MONTH, DATABASE_PATH, LOG_DIR to local paths
make run-local
```

Or directly:

```bash
python -m app.main
```

---

## Mock mode

`TRADING_MODE=mock` uses `MockBroker`, which is entirely in-process and never
opens a socket to IBKR. It provides:

- a **deterministic** seeded price walk — the same seed gives the same prices,
  so tests are reproducible;
- synthetic but stable contract ids, derived from symbol and expiry rather than
  from the RNG, so they agree across processes;
- a simulated order lifecycle (market orders fill, limit orders rest);
- failure injection for connection failures, permission denials, dropped
  sessions, and wrong account types.

`MockBroker` reports account type `simulated`. It can never satisfy the
paper/live account-identity interlock, so mock mode cannot accidentally look
like a real session.

---

## Trading modes

| Mode | Broker | Market data | Orders |
|---|---|---|---|
| `disabled` | none constructed at all | none | impossible — there is no broker object |
| `mock` | `MockBroker` | deterministic simulation | simulated only, never leaves the process |
| `paper` | `IBKRBroker` → paper port | IBKR | only after the full interlock chain passes |
| `live` | `IBKRBroker` → live port | IBKR | additionally requires `LIVE_TRADING_ENABLED=true` |

In `paper` mode the live port is not merely discouraged — the code path that
selects a port cannot return it.

---

## Configuration

All configuration is environment-based. See [.env.example](.env.example) for the
annotated full list. Configuration is parsed **strictly**: an unparseable value
stops the process rather than falling back to a default.

### The deployed configuration

```ini
TRADING_MODE=mock
ALLOW_ORDER_TRANSMIT=false
LIVE_TRADING_ENABLED=false
KILL_SWITCH=true
SOL_FUTURES_PERMISSION_READY=false

MAX_POSITION_CONTRACTS=0
MAX_ORDER_SIZE=0
MAX_DAILY_LOSS_USD=0
MAX_ORDERS_PER_HOUR=0
MAX_OPEN_ORDERS=0
MAX_NOTIONAL_EXPOSURE_USD=0

MARKET_DATA_MAX_AGE_SECONDS=0
```

### Key variables

| Variable | Default | Meaning |
|---|---|---|
| `TRADING_MODE` | `disabled` | `disabled` \| `mock` \| `paper` \| `live` |
| `ALLOW_ORDER_TRANSMIT` | `false` | Master transmission switch |
| `LIVE_TRADING_ENABLED` | `false` | Required *in addition* for live mode |
| `KILL_SWITCH` | `true` | Engaged by default |
| `SOL_FUTURES_PERMISSION_READY` | `false` | Operator's attestation that IBKR approved futures |
| `IB_HOST` / `IB_PAPER_PORT` / `IB_LIVE_PORT` | `127.0.0.1` / `4002` / `4001` | Never exposed publicly |
| `IB_CLIENT_ID` / `IB_ADMIN_CLIENT_ID` | `10` / `110` | Must differ, so operator commands cannot evict the bot |
| `DEFAULT_FUTURES_SYMBOL` | `MSL` | `MSL` or `SOL` only |
| `DEFAULT_CONTRACT_MONTH` | *(empty)* | `YYYYMM` or `YYYYMMDD`; **must be set explicitly** |
| `MAX_*` | `0` | `0` = not configured = prohibited |
| `MARKET_DATA_MAX_AGE_SECONDS` | `0` | `0` = not configured = every order refused |
| `DATABASE_PATH` | `/app/data/trading.db` | Persisted outside the container |
| `HEALTH_HOST` / `HEALTH_PORT` | `127.0.0.1` / `8787` | Loopback only, never published |

**No credential of any kind belongs in the configuration.** IB Gateway owns
IBKR authentication.

---

## Testing

```bash
make test          # 315 tests
make test-safety   # the ten critical safety tests plus their control
```

`tests/unit/test_critical_safety.py` implements the ten mandated safety tests.
Each starts from a **fully authorised baseline** and breaks exactly one thing,
so a pass proves that *that specific condition* blocks transmission.

The file also contains `test_00_all_green_baseline_allows_transmission`. This is
the control: without it, a gate that refused unconditionally would pass all ten
safety tests while being useless.

| # | Guarantee |
|---|---|
| 1 | Default configuration cannot transmit |
| 2 | `paper` mode + live account rejects everything |
| 3 | `live` mode + paper account rejects everything |
| 4 | `KILL_SWITCH=true` rejects everything, in every mode |
| 5 | `ALLOW_ORDER_TRANSMIT=false` rejects everything |
| 6 | Unknown account type rejects everything |
| 7 | Failed reconciliation rejects everything |
| 8 | Stale, missing, or future-stamped market data rejects everything |
| 9 | A restart does not duplicate an existing order |
| 10 | A zero limit disables the activity rather than meaning unlimited |

---

## Docker

```bash
make build      # build the image (stamps GIT_COMMIT and BUILD_TIMESTAMP)
make run        # verify safety, prepare dirs, start detached
make status     # full application status
make logs       # follow logs
make stop       # stop
make restart    # restart
```

The image:

- is based on `python:3.12-slim-bookworm` and installs **no packages**;
- runs as non-root `trader` (uid/gid 10001);
- has a read-only root filesystem, `no-new-privileges`, and all capabilities dropped;
- persists `/app/data` and `/app/logs` to host bind mounts;
- uses `restart: unless-stopped`;
- has a healthcheck that runs **inside** the container over loopback;
- contains no `.env` — configuration is injected at run time.

`docker-compose.yml` publishes **no ports** and carries **no Traefik labels**.
CI fails the build if either changes.

Prefer `make build` over a bare `docker compose build`: the Makefile stamps
`GIT_COMMIT` and `BUILD_TIMESTAMP` into the image, and without them `/status`
reports the running version as `unknown`.

---

## CI/CD

`.github/workflows/ci.yml` runs on every push and pull request to `main` and
`develop`:

1. **lint** — `ruff check`, `ruff format --check`, `mypy --strict`
2. **test** — the full suite, then the safety tests again on their own
3. **safety-audit** — fails if `.env` is tracked, if `.env.example` carries a
   trading-enabling value, if broker credentials appear anywhere, if
   `docker-compose.yml` publishes a port or mentions Traefik, or if `deploy.sh`
   could write `.env`
4. **docker** — builds the image, asserts it runs as non-root and contains no
   `.env`, starts a container, waits for healthy, asserts via `/status` that the
   transmit gate refuses, restarts it, and asserts no orders exist

`.github/workflows/deploy.yml` is **`workflow_dispatch` only**. There is no
`push` trigger: a git push cannot reach the trading server on its own.

### Making changes

`main` is protected. Direct pushes are rejected — every change goes through a
pull request, and all four CI jobs must pass before it can merge:

```bash
git checkout -b my-change
# ... edit, then:
make check                    # lint + mypy + tests, the same as CI
git commit -am "..."
git push -u origin my-change
# open a PR against main; merge once CI is green
```

The protection also requires branches to be **up to date** before merging, so a
PR that passed CI against a stale `main` has to be refreshed and re-tested. That
closes the gap where two independently-green changes break once combined —
which, in a system whose whole job is refusing unsafe orders, is exactly the
kind of interaction worth catching before it lands.

Force pushes and branch deletion on `main` are blocked.

---

## Deployment

Target: `srv1792440.hstgr.cloud`, directory `/opt/sol-futures-trading-bot`.

See [RUNBOOK.md](RUNBOOK.md) for first-time setup, the GitHub secrets you must
add yourself, and every operational procedure.

**CI/CD can never alter trading safety.** The server `.env` is created and
edited only by a human. `scripts/deploy.sh` reads it, verifies it with
`scripts/verify_safety.sh`, and refuses to start the container if the
configuration is not the approved one. It has no code path that writes it.

---

## Operator commands

> `make` is a convenience wrapper. It is **not** installed on the production
> host, and every target below is a one-line alias for a `docker compose exec`
> command. `RUNBOOK.md` gives the raw form of each, which is what to use in an
> incident.

```bash
make status                                 # full status
make broker-status                          # broker configuration and events
make positions                              # current positions
make open-orders                            # working orders
make contract-info                          # qualified contract metadata
make db-info                                # database file and row counts
make kill-switch-status                     # kill switch state
make kill-switch-on REASON="why"            # engage the kill switch, durably
make cancel-all-orders                      # cancel working orders (NOT positions)
make verify-running                         # assert the running config is approved
```

One command has no `make` target, because it is not part of daily operations:

```bash
python -m app.cli ibkr-checkout [--contract-month YYYYMM]
```

It connects to IB Gateway read-only and reports whether the adapter actually
works — see [Verifying the adapter](#verifying-the-adapter) below.

There is deliberately **no** `kill-switch-off` and **no** `enable-live`.
Resuming trading is a configuration change plus a restart. The friction is the
feature.

Cancelling orders and closing positions are different actions. The kill switch
does not liquidate anything, and no automatic emergency liquidation exists.

---

## IBKR integration

All IBKR code lives in `app/broker/ibkr_broker.py`. Nothing else in the project
knows Interactive Brokers exists.

The adapter targets the **official IBKR TWS API** (`ibapi`, `EClient`/`EWrapper`),
not a third-party wrapper. There is a real packaging caveat — IBKR does not
publish `ibapi` to PyPI themselves — which is documented in
[docs/IBKR_API_NOTES.md](docs/IBKR_API_NOTES.md) along with the alternatives and
what I recommend. **Read that before installing the extra.**

> **The IBKR adapter has never been run against a live IB Gateway.** US futures
> permission for the account is still pending, so there was no session to test
> against. Its pure logic (error classification, account-type determination,
> signature parsing, order-type policy) is unit tested; every socket path is
> unverified.

IB Gateway owns authentication. This project stores no IBKR username, password,
or 2FA material, and does nothing to bypass IBKR's security controls.

### Verifying the adapter

```bash
python -m app.cli ibkr-checkout [--contract-month YYYYMM]
```

`app/broker/checkout.py` connects once, runs every read-only call the system
depends on, and reports PASS / FAIL / SKIP per probe with the evidence it saw.
It exits non-zero if anything failed, and an empty report is a failure rather
than a pass — a checkout that observed nothing has verified nothing.

It **cannot place an order**, enforced three overlapping ways:

1. The broker is typed as `ReadOnlyBroker`, a Protocol with no write methods, so
   `mypy --strict` rejects `place_order` here as an attribute error rather than
   as a policy violation. A test parses the module's AST and asserts the same.
2. It refuses to run unless `ALLOW_ORDER_TRANSMIT=false`, `KILL_SWITCH=true` and
   the mode is `paper`. In the deployed `mock` posture it returns
   `CHECKOUT_REFUSED` without opening a socket.
3. Its final probe forces every session-side condition to its most permissive
   value and requires `TransmitGate` to refuse anyway.

The third is the one that matters. The first two say the checkout is safe; the
third says the *system* is at the moment a real broker session exists — the
first point at which that claim is testable against something other than a fake.

It connects on `IB_ADMIN_CLIENT_ID`, never the trading process's client id, so
it cannot disconnect a running bot. `RUNBOOK.md` step 7 has the full sequence
and what each failure means.

---

## What is deliberately not built yet

| Not built | Why |
|---|---|
| A trading strategy | The brief is infrastructure. `NoOpStrategy` proves the pipeline. |
| Automatic contract rollover | Contract selection is explicit in this phase. |
| A backtesting engine | Groundwork is laid: strategies take standard `Quote` objects and have no broker dependency, so historical data can be replayed through the same code. |
| Bracket / stop / stop-limit transmission | Modelled and persisted, but only market and limit orders transmit. Widening that set is a deliberate change with its own tests. |
| Emergency liquidation | Cancelling orders and flattening a book are different decisions. Not automated without explicit instruction. |
| A monitoring dashboard / Traefik route | A later phase. Nothing is exposed today. |

---

## Documentation

| Document | Contents |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Data flow, state machine, order lifecycle, extension points |
| [SECURITY.md](SECURITY.md) | Secrets, network isolation, live-trading safeguards, IBKR port protection |
| [RUNBOOK.md](RUNBOOK.md) | Every operational procedure, including recovery |
| [CHANGELOG.md](CHANGELOG.md) | Release history |
| [docs/IBKR_API_NOTES.md](docs/IBKR_API_NOTES.md) | TWS API packaging problem and options |
| [docs/IBKR_PAPER_CHECKOUT.md](docs/IBKR_PAPER_CHECKOUT.md) | Step-by-step for the first real gateway session, and where the IBKR credential goes |
| [docs/IBKR_GATEWAY_DOCKER.md](docs/IBKR_GATEWAY_DOCKER.md) | IB Gateway as a container with IBC — the current setup, and what storing the credential costs |
| [docs/IBKR_GATEWAY_VPS.md](docs/IBKR_GATEWAY_VPS.md) | Superseded: host-installed gateway with manual login, the only credential-free option |
