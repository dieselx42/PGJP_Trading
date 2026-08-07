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

| Task | Command |
|---|---|
| Start | `docker compose up -d` (or `make run`) |
| Stop | `docker compose down` (or `make stop`) |
| Restart | `docker compose restart sol-trading-bot` |
| Status | `make status` |
| Verify the running config is approved | `make verify-running` |
| Logs | `make logs` |
| Container state | `docker compose ps` |

`restart: unless-stopped` means the bot returns automatically after a VPS
reboot. `KILL_SWITCH=true` persists in `.env`, so it comes back halted.

---

## Checking health

```bash
docker compose ps                                    # health column
docker inspect --format '{{.State.Health.Status}}' sol-trading-bot
make health                                          # run the healthcheck directly
```

**Healthy does not mean trading.** A kill-switched bot is healthy — it is doing
exactly what it was told. Only an `ERROR` state or an unusable database reports
unhealthy. To see whether it *would* trade, read the transmit gate in `make
status`.

Reading the full status:

```bash
make status
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
make logs                                    # follow container stdout
docker compose logs --tail=200 sol-trading-bot
tail -f logs/trading.log                     # the rotated file on the host
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
make broker-status
```

In mock mode this reports `broker_implementation: mock` and
`ib_port_in_use: null` — correct, because mock mode never opens a socket.

Once IB Gateway exists, `make status` shows `broker.connection_state`,
`broker.account_type`, and `broker.last_heartbeat_age_seconds`.

**On disconnect the bot enters `SAFE`, refuses all orders, clears its market
data cache, and reconnects with capped exponential backoff (2s → 300s).** It
becomes `READY` again only after reconciliation succeeds. Permission errors are
never retried — they will not resolve by trying again.

---

## Positions and orders

```bash
make positions       # from the reconciled local book
make open-orders     # working orders
make contract-info   # qualified contract metadata
make db-info         # schema version and row counts
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
make kill-switch-on REASON="describe what is happening"
```

This latches the switch durably in the database. The running process stops
producing actionable trades on its next tick.

**Then make it survive a redeploy:**

```bash
nano .env      # set KILL_SWITCH=true
docker compose restart sol-trading-bot
```

**Check it:**

```bash
make kill-switch-status
```

There is deliberately **no** `kill-switch-off` command. To resume trading, edit
`KILL_SWITCH` in `.env` and restart. The friction is intentional.

The kill switch does **not** close positions. That is a separate decision.

---

## Emergency: cancel all orders

```bash
make cancel-all-orders REASON="describe what is happening"
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
make status | grep -A6 '"broker"'
make logs | grep broker
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
make status                      # gate still refusing?
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
make status | python3 -c "import json,sys;print(json.dumps(json.load(sys.stdin)['reconciliation'], indent=2))"
```

| Discrepancy | Meaning | Action |
|---|---|---|
| `position_discrepancies` | broker and local position sizes differ | compare against IBKR directly; decide which is right |
| `unknown_locally` | an order is working at the broker that we did not create or have forgotten | investigate before doing anything; do not cancel blindly |
| `unknown_at_broker` with a broker id | we think an order is working, the broker does not | it probably filled or was cancelled; check IBKR's history |
| `unknown_at_broker` with no broker id | an order stuck in `PENDING_SUBMIT` — we crashed mid-transmission | **check IBKR for the order before touching anything** |

Once you have established the truth:

1. Engage the kill switch: `make kill-switch-on REASON="reconciliation mismatch"`
2. Resolve the underlying discrepancy at IBKR.
3. Correct the local record if needed (below).
4. Restart and confirm reconciliation succeeds.

### Database problems

```bash
make db-info
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
make status               # expect reconciliation to fail until state is restored
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

On the VPS, or on a machine the VPS can reach privately. Bind it to `127.0.0.1`.

> **Unattended operation.** IB Gateway requires an interactive login and 2FA,
> and re-authentication on its own schedule. Running it unattended usually means
> a third-party supervisor such as IBC. **I have not installed anything of the
> sort and will not without your approval** — it is a third-party component in
> the authentication path for a brokerage account, which is a decision for you,
> not a default. Until then, expect to authenticate manually.

### 4. Enable and lock down the API

In IB Gateway: Configuration → API → Settings:

- ✅ Enable ActiveX and Socket Clients
- ✅ Read-Only API — **leave this on for the entire read-only checkout below**
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

Restart and confirm from `make status`:

- `broker.connection_state` is `connected`
- `broker.account_type` is **`paper`** — if it says `live`, stop immediately;
  the adapter will have refused the session
- `broker.account_id` is masked and starts with `DU`

### 7. Verify the contract

```bash
make contract-info
```

Confirm against IBKR's contract search: `conId`, `localSymbol`, `multiplier`,
`minTick`, `expiration`, `tradingHours`. **Do not trust the reference constants
in `app/contracts/solana.py`** — they have never been checked against a live
session, which is why the resolver overwrites them from `contractDetails`.

Set the verified expiry:

```ini
DEFAULT_CONTRACT_MONTH=<verified YYYYMM>
```

### 8. Verify market data

Confirm CME market data appears and is timestamped sensibly. Then configure
freshness — until this is non-zero, every order is refused:

```ini
MARKET_DATA_MAX_AGE_SECONDS=30
```

### 9. Read-only testing

With `ALLOW_ORDER_TRANSMIT=false` and IB Gateway still in Read-Only API mode,
run for a full session and confirm:

- reconciliation succeeds on every reconnect
- market data stays fresh during liquid hours
- no orders are created (`make open-orders` stays at zero)
- disconnect/reconnect recovers cleanly

### 10. Only then: controlled paper orders

Turn off IB Gateway's Read-Only API mode and set **small** limits first:

```ini
SOL_FUTURES_PERMISSION_READY=true
MAX_ORDER_SIZE=1
MAX_POSITION_CONTRACTS=1
MAX_DAILY_LOSS_USD=100
MAX_ORDERS_PER_HOUR=2
MAX_OPEN_ORDERS=1
MAX_NOTIONAL_EXPOSURE_USD=10000
ALLOW_ORDER_TRANSMIT=true
KILL_SWITCH=false
```

Restart, confirm `application.state` is `READY` and the transmit gate reports
`allowed: true`, and watch the first order end to end.

**Live trading remains out of scope and requires a separate, explicit decision.**
