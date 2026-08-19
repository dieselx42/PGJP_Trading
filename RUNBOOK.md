# Runbook

Operational procedures for `sol-futures-trading-bot` on `srv1792440.hstgr.cloud`.

Everything below assumes:

```bash
cd /opt/sol-futures-trading-bot
```

---

## Contents

- [First-time server setup](#first-time-server-setup)
- [What you must do yourself](#what-you-must-do-yourself)
- [Daily operations](#daily-operations)
- [Checking health](#checking-health)
- [Inspecting logs](#inspecting-logs)
- [Broker connectivity](#broker-connectivity)
- [Positions and orders](#positions-and-orders)
- [Emergency: kill switch](#emergency-kill-switch)
- [Emergency: cancel all orders](#emergency-cancel-all-orders)
- [Recovery procedures](#recovery-procedures)
- [Deployment](#deployment)
- [Next phase: enabling paper trading](#next-phase-enabling-paper-trading)

---

## First-time server setup

### 1. Audit the server first

Nothing gets installed before you know what is there. This script is
**read-only** — it changes nothing, touches no container, and does not modify
Traefik or the firewall.

```bash
git clone https://github.com/dieselx42/PGJP_Trading.git /tmp/pgjp-audit
bash /tmp/pgjp-audit/scripts/vps_audit.sh > ~/vps-audit.txt 2>&1
less ~/vps-audit.txt
```

Review specifically:

- Traefik is running (it must be left alone)
- OpenClaw remnants — the script **reports** them and deletes nothing
- publicly listening ports
- firewall state
- SSH configuration
- whether ports 4001/4002 are listening (they should not be yet)

The output contains no secrets or key material and is safe to share.

### 2. Create a deployment user (recommended)

Running the bot as root is unnecessary. As root:

```bash
adduser --disabled-password --gecos "" soldeploy
usermod -aG docker soldeploy
mkdir -p /opt/sol-futures-trading-bot
chown soldeploy:soldeploy /opt/sol-futures-trading-bot
```

> `docker` group membership is effectively root-equivalent on the host. It is
> still an improvement over deploying as root — the account exists only for this
> purpose, has no password, and can be revoked independently.

### 3. Clone the repository

```bash
sudo -u soldeploy git clone https://github.com/dieselx42/PGJP_Trading.git \
    /opt/sol-futures-trading-bot
cd /opt/sol-futures-trading-bot
```

### 4. Create the configuration

```bash
sudo -u soldeploy cp .env.example .env
sudo -u soldeploy chmod 600 .env
sudo -u soldeploy nano .env
```

Set exactly this for the current phase:

```ini
APP_ENV=production
TRADING_MODE=mock
ALLOW_ORDER_TRANSMIT=false
LIVE_TRADING_ENABLED=false
KILL_SWITCH=true
SOL_FUTURES_PERMISSION_READY=false

DEFAULT_FUTURES_SYMBOL=MSL
DEFAULT_EXCHANGE=CME
DEFAULT_CURRENCY=USD
# Any valid YYYYMM. In mock mode the contract is synthetic, so this only
# exercises the data path. It MUST be re-verified against IBKR before paper.
DEFAULT_CONTRACT_MONTH=202612

MAX_POSITION_CONTRACTS=0
MAX_ORDER_SIZE=0
MAX_DAILY_LOSS_USD=0
MAX_ORDERS_PER_HOUR=0
MAX_OPEN_ORDERS=0
MAX_NOTIONAL_EXPOSURE_USD=0

MARKET_DATA_MAX_AGE_SECONDS=0
DATABASE_PATH=/app/data/trading.db
LOG_DIR=/app/logs
LOG_LEVEL=INFO
```

**No IBKR credentials belong in this file.** IB Gateway owns authentication.

### 5. Verify, then start

```bash
bash scripts/verify_safety.sh .env    # must print "RESULT: safe."
sudo bash scripts/bootstrap_dirs.sh   # creates ./data and ./logs as uid 10001

# Stamp the image with its provenance. A bare `docker compose build` leaves
# GIT_COMMIT and BUILD_TIMESTAMP empty, and /status then reports "unknown" --
# which makes it impossible to tell which commit is actually running.
export GIT_COMMIT="$(git rev-parse --short HEAD)"
export BUILD_TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
docker compose build
docker compose up -d
```

> If `git rev-parse` reports *"detected dubious ownership"*, the repository is
> owned by `soldeploy` and you are running as `root`. Fix it once with:
> `git config --global --add safe.directory /opt/sol-futures-trading-bot`

### 6. Confirm

```bash
docker compose ps                                    # healthy
docker compose exec -T sol-trading-bot python -m app.cli status
docker ps --format '{{.Names}}: {{.Ports}}'          # bot publishes nothing
docker ps --filter name=traefik                      # Traefik still running
```

`status` must show `"can_transmit_live_orders": false` and a transmit gate with
`"allowed": false`.

---

## What you must do yourself

These need your credentials or your decision. I cannot do them and have not
attempted to.

### A. Confirm the GitHub repository

The remote is `https://github.com/dieselx42/PGJP_Trading`. If you would prefer
the project to live in a repository named `sol-futures-trading-bot`, create it
and tell me the URL — the code is repository-name agnostic.

### B. Add the deployment secrets to GitHub

**Where:** `https://github.com/dieselx42/PGJP_Trading` → Settings → Secrets and
variables → Actions → *New repository secret*

Generate a dedicated deploy key **on your machine** (not on the server, and not
here):

```bash
ssh-keygen -t ed25519 -f ~/.ssh/pgjp_deploy -C "github-actions-deploy" -N ""
```

Install the **public** half on the server:

```bash
sudo -u soldeploy mkdir -p /home/soldeploy/.ssh
sudo -u soldeploy tee -a /home/soldeploy/.ssh/authorized_keys < ~/.ssh/pgjp_deploy.pub
sudo -u soldeploy chmod 700 /home/soldeploy/.ssh
sudo -u soldeploy chmod 600 /home/soldeploy/.ssh/authorized_keys
```

Then add these secrets:

| Secret | Value | How to get it |
|---|---|---|
| `DEPLOY_SSH_KEY` | contents of `~/.ssh/pgjp_deploy` (the **private** key, whole file including header/footer) | `cat ~/.ssh/pgjp_deploy` |
| `DEPLOY_HOST` | `srv1792440.hstgr.cloud` | — |
| `DEPLOY_USER` | `soldeploy` | — |
| `DEPLOY_KNOWN_HOSTS` | the server's host key | `ssh-keyscan -H srv1792440.hstgr.cloud` |
| `DEPLOY_APP_DIR` | `/opt/sol-futures-trading-bot` | optional; this is the default |
| `DEPLOY_PORT` | your SSH port | optional; defaults to `22` |

Never paste the private key into a chat, an issue, or a commit. Only into the
GitHub secret field.

### C. Optional: require a reviewer for deploys

Settings → Environments → `production` → *Required reviewers*. The deploy
workflow already targets this environment, so adding a reviewer gives every
production deploy four eyes.

### D. Set branch protection

Settings → Branches → protect `main`: require the CI status checks to pass, and
require a pull request. This is what makes "do not deploy if CI fails" real
rather than advisory.

---

## Daily operations

> **`make` is a convenience, not a dependency.** It is not installed on
> `srv1792440`, and every `make` target in this document is a one-line alias for
> the `docker compose` command shown beside it. The raw command is authoritative
> — reach for it first, especially in an incident. Install the shorthand with
> `apt install -y make` if you want it.

| Task | Command | Shorthand |
|---|---|---|
| Start | `docker compose up -d` | `make run` |
| Stop | `docker compose down` | `make stop` |
| Restart | `docker compose restart sol-trading-bot` | `make restart` |
| Status | `docker compose exec -T sol-trading-bot python -m app.cli status` | `... python -m app.cli status` |
| Verify running config | `docker compose exec -T sol-trading-bot python -m app.cli verify` | `make verify-running` |
| Positions | `docker compose exec -T sol-trading-bot python -m app.cli positions` | `make positions` |
| Open orders | `docker compose exec -T sol-trading-bot python -m app.cli open-orders` | `... python -m app.cli open-orders` |
| Logs | `docker compose logs -f --tail=200 sol-trading-bot` | `make logs` |
| Container state | `docker compose ps` | `make ps` |

`restart: unless-stopped` means the bot returns automatically after a VPS
reboot. `KILL_SWITCH=true` persists in `.env`, so it comes back halted.

---

## Checking health

```bash
docker compose ps                                    # health column
docker inspect --format '{{.State.Health.Status}}' sol-trading-bot
docker compose exec -T sol-trading-bot python /app/scripts/healthcheck.py
```

**Healthy does not mean trading.** A kill-switched bot is healthy — it is doing
exactly what it was told. Only an `ERROR` state or an unusable database reports
unhealthy. To see whether it *would* trade, read the transmit gate in `make
status`.

Reading the full status:

```bash
docker compose exec -T sol-trading-bot python -m app.cli status
```

| Field | Meaning |
|---|---|
| `application.state` | `HALTED` expected in this phase |
| `safety.can_transmit_live_orders` | **must be `false`** |
| `safety.transmit_gate.allowed` | **must be `false`** |
| `safety.transmit_gate.reasons` | every interlock currently refusing |
| `broker.account_type` | `simulated` in mock mode |
| `reconciliation.succeeded` | `true` once broker and local state agree |
| `market_data.fresh` | `false` while `MARKET_DATA_MAX_AGE_SECONDS=0` |
| `risk.unconfigured` | every limit still at zero |

---

## Inspecting logs

```bash
docker compose logs -f --tail=200 sol-trading-bot   # follow container stdout
tail -f logs/trading.log                            # the rotated file on the host
```

Logs are one JSON object per line. Useful queries:

```bash
# everything for one trade lifecycle
grep '"correlation_id":"cor_abc123"' logs/trading.log | python3 -m json.tool

# errors only
jq 'select(.level=="ERROR")' logs/trading.log

# state transitions
jq 'select(.event=="app.state_changed")' logs/trading.log

# every refusal
jq 'select(.event=="order.gate_rejected" or .event=="risk.rejected")' logs/trading.log
```

Durable events are also in the database:

```bash
docker compose exec -T sol-trading-bot python -m app.cli broker-status
sqlite3 data/trading.db "SELECT occurred_at, level, event, message FROM bot_events ORDER BY id DESC LIMIT 20;"
```

---

## Broker connectivity

```bash
docker compose exec -T sol-trading-bot python -m app.cli broker-status
```

In mock mode this reports `broker_implementation: mock` and
`ib_port_in_use: null` — correct, because mock mode never opens a socket.

Once IB Gateway exists, `app.cli status` shows `broker.connection_state`,
`broker.account_type`, and `broker.last_heartbeat_age_seconds`.

**On disconnect the bot enters `SAFE`, refuses all orders, clears its market
data cache, and reconnects with capped exponential backoff (2s → 300s).** It
becomes `READY` again only after reconciliation succeeds. Permission errors are
never retried — they will not resolve by trying again.

---

## Positions and orders

```bash
E=(docker compose exec -T sol-trading-bot python -m app.cli)
"${E[@]}" positions       # from the reconciled local book
"${E[@]}" open-orders     # working orders
"${E[@]}" contract-info   # qualified contract metadata
"${E[@]}" db-info         # schema version and row counts
```

These read the database, so they are safe to run while the bot is trading and
need no broker connection.

Direct queries:

```bash
sqlite3 data/trading.db "SELECT internal_order_id, status, symbol, side, quantity, block_reasons FROM orders ORDER BY created_at DESC LIMIT 20;"
sqlite3 data/trading.db "SELECT decided_at, approved, reasons FROM risk_decisions ORDER BY id DESC LIMIT 20;"
```

---

## Emergency: kill switch

**Stop all new orders right now:**

```bash
docker compose exec -T sol-trading-bot \
  python -m app.cli kill-switch-on --reason "describe what is happening"
```

(`make kill-switch-on REASON="..."` is the same thing, if `make` is installed.
It is not, on `srv1792440` — do not reach for it under pressure.)

This latches the switch durably in the database. The running process stops
producing actionable trades on its next tick.

**Then make it survive a redeploy:**

```bash
nano .env      # set KILL_SWITCH=true
docker compose restart sol-trading-bot
```

**Check it:**

```bash
docker compose exec -T sol-trading-bot python -m app.cli kill-switch-status
```

There is deliberately **no** `kill-switch-off` command. To resume trading, edit
`KILL_SWITCH` in `.env` and restart. The friction is intentional.

The kill switch does **not** close positions. That is a separate decision.

---

## Emergency: cancel all orders

```bash
docker compose exec -T sol-trading-bot \
  python -m app.cli cancel-all-orders --confirm --reason "describe what is happening"
```

Cancels every working order at the broker. **It does not close positions.**
Closing positions is a human decision, taken in IBKR, and is deliberately not
automated.

---

## Recovery procedures

### Broker disconnected

The bot handles this itself: `SAFE`, orders refused, capped exponential backoff,
reconcile, then `READY`. No action needed unless it persists.

If it persists:

```bash
docker compose exec -T sol-trading-bot python -m app.cli status | grep -A6 '"broker"'
docker compose logs --tail=200 sol-trading-bot | grep broker
```

- `BrokerConnectionError` → is IB Gateway running? Is it listening on the
  configured port? `ss -tlpn | grep -E ':(4001|4002)'`
- `BrokerPermissionError` → an account setting at IBKR. Retrying will not help;
  the bot correctly stops trying.
- Wrong account type → the adapter refused the session on purpose. Check which
  gateway is running against `TRADING_MODE`.

### After a VPS restart

`restart: unless-stopped` brings the container back automatically.

```bash
docker compose ps                # healthy?
docker compose exec -T sol-trading-bot python -m app.cli verify   # posture approved?
docker ps --filter name=traefik  # Traefik back?
```

If it did not come back:

```bash
cd /opt/sol-futures-trading-bot
bash scripts/verify_safety.sh .env
docker compose up -d
```

### Reconciliation failed

**This is the case that requires a human. Do not work around it.**

A reconciliation failure means the broker and our records disagree: either we
missed a fill, or something else is trading the account. Neither is a situation
where more automated trading is the right answer. The bot stays in `SAFE` and
refuses everything.

```bash
docker compose exec -T sol-trading-bot python -m app.cli status \
  | python3 -c "import json,sys;print(json.dumps(json.load(sys.stdin)['reconciliation'], indent=2))"
```

| Discrepancy | Meaning | Action |
|---|---|---|
| `position_discrepancies` | broker and local position sizes differ | compare against IBKR directly; decide which is right |
| `unknown_locally` | an order is working at the broker that we did not create or have forgotten | investigate before doing anything; do not cancel blindly |
| `unknown_at_broker` with a broker id | we think an order is working, the broker does not | it probably filled or was cancelled; check IBKR's history |
| `unknown_at_broker` with no broker id | an order stuck in `PENDING_SUBMIT` — we crashed mid-transmission | **check IBKR for the order before touching anything** |

Once you have established the truth:

1. Engage the kill switch:
   `docker compose exec -T sol-trading-bot python -m app.cli kill-switch-on --reason "reconciliation mismatch"`
2. Resolve the underlying discrepancy at IBKR.
3. Correct the local record if needed (below).
4. Restart and confirm reconciliation succeeds.

### Database problems

```bash
docker compose exec -T sol-trading-bot python -m app.cli db-info
sqlite3 data/trading.db "PRAGMA integrity_check;"
```

Back up before touching anything (WAL and SHM matter):

```bash
docker compose stop sol-trading-bot
cp data/trading.db      data/trading.db.bak
cp data/trading.db-wal  data/trading.db-wal.bak 2>/dev/null || true
cp data/trading.db-shm  data/trading.db-shm.bak 2>/dev/null || true
```

Checkpoint the WAL into the main file:

```bash
sqlite3 data/trading.db "PRAGMA wal_checkpoint(TRUNCATE);"
```

If it is corrupt beyond repair, **starting fresh is not free**: the bot loses
its record of orders and positions, and the next reconciliation will report
every broker position as unknown — which is correct and will keep it in `SAFE`.
Reconcile against IBKR first, then:

```bash
mv data/trading.db data/trading.db.corrupt
docker compose up -d      # migrations recreate the schema
docker compose exec -T sol-trading-bot python -m app.cli status   # expect reconciliation to fail
```

### Container unhealthy

```bash
docker compose logs --tail=100 sol-trading-bot
docker inspect --format '{{json .State.Health}}' sol-trading-bot | python3 -m json.tool
```

Common causes:

- `data/` or `logs/` not writable by uid 10001 → `sudo bash scripts/bootstrap_dirs.sh`
- invalid configuration → the process exits with code 2 and prints the reason;
  `bash scripts/verify_safety.sh .env`
- database unreadable → see above

### Configuration was wiped by the hosting control panel

**Symptom:** `app.cli verify` reports values that do not match `.env` — most
visibly `TRADING_MODE: disabled (approved: mock)`, because `disabled` is what an
absent `TRADING_MODE` falls back to.

**Cause:** Hostinger's Docker Manager "Environment" panel *edits* `.env`; it
does not overlay it. Saving an empty panel writes an empty file.

**Confirm:**

```bash
ls -l .env && wc -c .env          # a healthy file is ~5-6 KB
grep -c '^[A-Z_]*=' .env          # expect 38
```

**Recover:**

```bash
cd /opt/sol-futures-trading-bot
sudo -u soldeploy cp .env.example .env
sudo -u soldeploy chmod 600 .env
sudo -u soldeploy sed -i 's/^DEFAULT_CONTRACT_MONTH=$/DEFAULT_CONTRACT_MONTH=202612/' .env
bash scripts/verify_safety.sh .env                # must print "RESULT: safe."
docker compose up -d --force-recreate
docker compose exec -T sol-trading-bot python -m app.cli verify
```

`--force-recreate` is required. `docker compose restart` does **not** re-read
`env_file`, so a plain restart keeps the broken configuration.

**Prevent:** do not save that panel. Edit `.env` on the server directly.

### Rolling back a deployment

```bash
cd /opt/sol-futures-trading-bot
git log --oneline -10
bash scripts/deploy.sh <previous-commit-sha>
```

`deploy.sh` also rolls back automatically if the new container fails its health
check.

---

## Deployment

### From GitHub (preferred)

Actions → **Deploy** → *Run workflow*:

- **ref**: `main`
- **confirm**: type `deploy`

CI re-runs on the target ref first. The deploy fails closed if the server `.env`
is not the approved configuration.

### From the server

```bash
cd /opt/sol-futures-trading-bot
bash scripts/deploy.sh main
```

**Use `deploy.sh` rather than running `docker compose` by hand.** It verifies
`.env` *before* building and verifies the running process *after* starting, and
rolls back if the container does not become healthy. A manual
`docker compose build && docker compose up -d` skips both checks — that is how
an emptied `.env` reached a running container once already.

If you do deploy by hand, run both checks yourself:

```bash
bash scripts/verify_safety.sh .env                                 # before
docker compose exec -T sol-trading-bot python -m app.cli verify     # after
```

### What a deployment can and cannot do

| Can | Cannot |
|---|---|
| Update application code | Create or modify `.env` |
| Rebuild and restart the container | Change any safety variable |
| Verify safety configuration | Enable live trading |
| Roll back on a failed health check | Touch Traefik or other containers |

---

## Next phase: enabling paper trading

**Do none of this until IBKR approves US futures permission.** These steps are
listed so the sequence is settled in advance; each one needs your explicit
decision.

### 1. Confirm IBKR approval

Log into IBKR Account Management and confirm **United States (Futures)** trading
permission is approved, not pending. Note which account it applies to.

### 2. Create / verify the paper account

Confirm the paper account exists and mirrors the live account's permissions.
Paper account ids start with `DU`.

### 3. Install IB Gateway

**Run it on your own machine first, not the VPS.** Both the gateway and the bot
on one machine means nothing crosses a network boundary, 2FA is trivial because
there is a screen in front of you, and the headless-X and unattended-login work
is entirely separate from the question this phase actually asks — does the
adapter work at all. Move it to the VPS once the answer is yes.

Bind it to `127.0.0.1` wherever it runs.

> **Unattended operation.** IB Gateway requires an interactive login and 2FA,
> and re-authentication on its own schedule. Running it unattended usually means
> a third-party supervisor such as IBC. **I have not installed anything of the
> sort and will not without your approval** — it is a third-party component in
> the authentication path for a brokerage account, which is a decision for you,
> not a default. Until then, expect to authenticate manually.

### 4. Enable and lock down the API

In IB Gateway: Configuration → API → Settings:

- ✅ Enable ActiveX and Socket Clients
- ✅ Read-Only API — **leave this on through step 8**; it comes off at step 9
- Socket port `4002`
- Trusted IPs: `127.0.0.1` only
- ❌ Do **not** allow connections from other hosts

Verify from the server:

```bash
ss -tlpn | grep 4002      # must show 127.0.0.1:4002, never 0.0.0.0:4002
```

### 5. Install the IBKR API package

Read `docs/IBKR_API_NOTES.md` **first** — there is a real packaging decision to
make, and I want your call on it before anything is installed.

### 6. Switch to paper mode — read-only

```ini
TRADING_MODE=paper
ALLOW_ORDER_TRANSMIT=false     # still false
LIVE_TRADING_ENABLED=false
KILL_SWITCH=true               # still true
SOL_FUTURES_PERMISSION_READY=false
```

### 7. Run the read-only checkout

```bash
python -m app.cli ibkr-checkout
```

This is the step that converts "the adapter is written" into "the adapter
works". It connects once on `IB_ADMIN_CLIENT_ID` — never the trading process's
client id — runs every read-only call the system depends on, and reports
PASS / FAIL / SKIP per probe with the evidence it saw. It exits non-zero if
anything failed.

It **cannot place an order**: the broker is typed as a Protocol with no write
methods, and it refuses to run at all unless `ALLOW_ORDER_TRANSMIT=false`,
`KILL_SWITCH=true`, and the mode is `paper`. Running it in the deployed `mock`
posture returns `CHECKOUT_REFUSED` and touches no socket.

What each probe tells you:

| Probe | A failure means |
|---|---|
| `SOCKET_CONNECT` | the gateway is not listening where the process expects, or the API is not enabled |
| `ACCOUNT_TYPE_IS_PAPER` | **stop.** The account the broker reported is not a paper account |
| `ACCOUNT_SUMMARY` | the account-data callbacks do not complete |
| `POSITIONS_READABLE` / `FILLS_READABLE` | reconciliation would fail on every reconnect |
| `OPEN_ORDERS_EMPTY` | orders exist that this software did not create — investigate before continuing |
| `CONTRACT_QUALIFIES` | see below; a permission error here is expected while futures permission is pending |
| `MARKET_DATA` | no subscription, or you are outside CME trading hours |
| `GATE_REFUSES_WHEN_EVERYTHING_ELSE_IS_GREEN` | **stop and engage the kill switch.** The transmit gate would allow an order |

The last one is the important one. It forces every session-side condition to
its most permissive value and requires the gate to refuse anyway, so what
refuses is the deployed configuration rather than the accident of a
disconnected session. It is the first point in the project's life at which that
claim is testable against a real broker rather than a fake.

`CONTRACT_QUALIFIES` is skipped, never guessed, when no expiration is known.
Pass one for the run:

```bash
python -m app.cli ibkr-checkout --contract-month 202512
```

The flag applies to that run only and is never written back to `.env` —
choosing an expiration is a decision you record deliberately.

A `BrokerPermissionError` on this probe is the **expected** result while US
futures permission is pending, and it is a useful result: it confirms the error
classification works and that permission errors are marked non-retryable.

### 8. Verify the contract

Compare the `CONTRACT_QUALIFIES` evidence against IBKR's contract search:
`con_id`, `local_symbol`, `multiplier`, `min_tick`, `expiration`,
`trading_hours`. **Do not trust the reference constants in
`app/contracts/solana.py`** — they have never been checked against a live
session, which is why the resolver overwrites them from `contractDetails`.

Set the verified expiry:

```ini
DEFAULT_CONTRACT_MONTH=<verified YYYYMM>
```

### 9. Turn off Read-Only API mode — and only that

In `.env.ibgateway` on the server:

```ini
READ_ONLY_API=no
```

Then bring the stack up. **`docker compose up -d` is enough** — changing
`.env.ibgateway` changes the gateway's `env_file`, so compose recreates it on
its own, and it recreates the bot with it:

```bash
docker compose up -d          # or: bash scripts/deploy.sh main
```

Do **not** add `--force-recreate` on top of a deploy. `scripts/deploy.sh`
already runs `docker compose up -d`, so a second recreate restarts a gateway
that was seconds into logging in and resets its clock — which looks exactly
like a broken gateway and is not one.

**Now wait for the login.** IBC takes 30–60 seconds, and `deploy.sh` reports
success before it finishes — it waits on the *bot's* container health, which
says nothing about whether a broker session exists:

```bash
docker compose logs ib-gateway | grep -E "Login has completed|Read-Only API checkbox"
```

Want both lines. The second is the gateway reporting what it actually did:

```
IBC: Read-Only API checkbox is now set to: false
```

Running the checkout before that appears gives `502 Couldn't connect to TWS`,
which reads as a networking fault and is only impatience.

**This does not enable trading, and it is not step 10 arriving early.** It
removes the outermost of four independent layers; the bot's own three are
untouched and any one of them alone still refuses every order:

| Layer | Where | State after this step |
|---|---|---|
| Gateway refuses order submission | `.env.ibgateway` | **off** |
| `ALLOW_ORDER_TRANSMIT=false` | bot `.env` | still engaged |
| `KILL_SWITCH=true` | bot `.env` + DB latch | still engaged |
| All six risk limits `0` | bot `.env` | still engaged |

The reason to do it now, separately, is that **the soak below cannot be
performed with Read-Only mode on.** Reconciliation depends on
`get_open_orders`, and the gateway refuses `reqOpenOrders` outright in that mode
(code 321, `reqId` -1). Verifying "no orders exist" through a call that is being
refused verifies nothing. This was written the other way round until a live
session showed the contradiction.

Confirm the effect rather than the setting:

```bash
docker compose exec -T -e TRADING_MODE=paper sol-trading-bot \
  python -m app.cli ibkr-checkout --contract-month <YYYYMMDD>
```

`OPEN_ORDERS_EMPTY` should now pass, and the checkout should be **13/13**. If it
still reports 321, the gateway did not pick up the change — confirm the
`Read-Only API checkbox is now set to: false` line is present in its log.

**Confirmed 2026-08-19**: 13/13, `OPEN_ORDERS_EMPTY` passing, and
`GATE_REFUSES_WHEN_EVERYTHING_ELSE_IS_GREEN` still refusing with all four
reasons. That last one earns its keep here specifically: on every earlier run
the gateway would have blocked an order regardless, so the probe was partly
free. This was the first run where the gateway *would* have accepted one and
the bot's own three layers were the only thing in the way.

### 9b. Read-only soak

With `ALLOW_ORDER_TRANSMIT=false`, run for a full session and confirm:

- reconciliation succeeds on every reconnect
- market data stays fresh during liquid hours
- no orders are created (`app.cli open-orders` stays at zero — a call that now
  genuinely completes)
- disconnect/reconnect recovers cleanly

Configure freshness once market data has been observed — until this is
non-zero, every order is refused:

```ini
MARKET_DATA_MAX_AGE_SECONDS=30
```

### 10. Only then: controlled paper orders

**Confirm US futures permission is actually granted at IBKR first.** It is
separate from the market-data subscription, it has its own application and
cooldown, and nothing in this system can tell you its state — the TWS API
exposes no such flag, which is why `SOL_FUTURES_PERMISSION_READY` is a hand-set
variable. Setting it `true` while permission is pending produces rejected orders,
not trades.

Confirmed granted on 2026-08-19 via a `whatIf` order preview, which IBKR priced
rather than refusing: initial margin $1,179.88, maintenance $1,025.98,
commission $3.41 on a 1-lot MSLQ6. A permission-blocked account returns
`10187`/`10276`/`460` there instead.

**Edit the server `.env` by hand** — `nano /opt/sol-futures-trading-bot/.env`.
These keys already exist, so change them in place; appending creates duplicates
and the *last* one wins, which is a confusing way to find out you edited the
wrong line. Deploy never writes this file, by design.

```ini
TRADING_MODE=paper
SOL_FUTURES_PERMISSION_READY=true
MARKET_DATA_MAX_AGE_SECONDS=30
DEFAULT_CONTRACT_MONTH=20260828
MAX_ORDER_SIZE=1
MAX_POSITION_CONTRACTS=1
MAX_DAILY_LOSS_USD=100
MAX_ORDERS_PER_HOUR=2
MAX_OPEN_ORDERS=1
MAX_NOTIONAL_EXPOSURE_USD=10000
ALLOW_ORDER_TRANSMIT=true
KILL_SWITCH=false
```

Then deploy, **naming the posture you intend**:

```bash
DEPLOY_POSTURE=paper-armed bash scripts/deploy.sh main
```

Without that variable the deploy checks for the halted posture, finds ten
differences, and refuses — which is correct, and is what it does for a deploy
that was not supposed to arm anything. `paper-armed` is not "skip the checks":
it is a different set, and several are *stricter*. A limit left at `0` fails,
because zero means NOT CONFIGURED and an armed system carrying one would refuse
every order while looking ready. Sanity ceilings catch a fat-fingered extra
digit. No posture approves a live configuration.

Confirm the running process agrees:

```bash
docker compose exec -T sol-trading-bot python -m app.cli status
```

Want `application_state: READY` — reachable only through successful
reconciliation, so it doubles as proof that `get_open_orders` works in the real
runtime and not only in the checkout.

`app.cli verify` will now report `POSTURE_NOT_APPROVED`. That is expected: it
checks the halted posture specifically, and you have deliberately left it.

#### The first order is sent by hand, not by a strategy

**Do not expect the running bot to trade at this point.** `NoOpStrategy` returns
no intents, ever, so an armed system with the shipped strategy sits there
producing nothing — which is indistinguishable from a broken one. Arming a
strategy is also the wrong first experiment: it fires on a market tick, at a
moment nobody chose, with nobody watching.

Send exactly one order, deliberately, with `place-order`. **Preview first** —
without `--confirm` it sends nothing:

```bash
docker compose exec -T sol-trading-bot python -m app.cli place-order \
  --target-position 1 --limit-price 75.00
```

Read the report. `plan.resulting_order` is what would be sent,
`approvals.risk` and `approvals.gate` are what both approvers say, and
`would_be_allowed` is the summary. A refusal here is the interlocks working,
not a fault — fix the named reason rather than routing around it.

The preview prints the exact confirmation to add. The token is the contract's
broker-reported `local_symbol`, so it can only come from a preview that actually
resolved a contract at IBKR:

```bash
docker compose exec -T sol-trading-bot python -m app.cli place-order \
  --target-position 1 --limit-price 75.00 --confirm MSLQ6
```

`--target-position` is an **absolute target, not a delta**. Running it twice
leaves you long 1, not 2. To close, ask for `0`.

The limit price defaults to a limit order for a reason: a first-ever order
should not be able to fill at a surprising price. Set it below the market for a
buy and it will rest rather than fill, which lets you watch the submit and
acknowledge path before anything executes. Then move it to marketable to watch
the fill.

Watch it end to end — this is the first time `place_order`, `orderStatus`,
`execDetails` and `commissionReport` have ever run:

```bash
docker compose exec -T sol-trading-bot python -m app.cli open-orders
docker compose exec -T sol-trading-bot python -m app.cli positions
docker compose exec -T sol-trading-bot python -m app.cli status
```

`cancel-all-orders --confirm` is the way out at any point.

**Live trading remains out of scope and requires a separate, explicit decision.**
`place-order` refuses live mode outright, before it constructs a broker.
