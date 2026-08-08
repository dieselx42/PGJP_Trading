# Connecting IBKR: a step-by-step for the paper read-only checkout

This is the concrete walkthrough for getting a real IBKR session in front of
`app/broker/ibkr_broker.py` for the first time. `RUNBOOK.md` states the sequence;
this states the keystrokes.

## Where the credential goes — read this first

**Your IBKR username and password go into IB Gateway's login window. Nowhere
else.**

Not into `.env`. Not into `docker-compose.yml`. Not into GitHub secrets. Not into
the Hostinger panel. The bot has no username field and no password field, because
it never authenticates to IBKR — IB Gateway does that, and the bot then talks to
the already-authenticated gateway over a local socket.

Everything the bot needs to find the gateway is already in `.env`, and none of it
is secret:

```ini
IB_HOST=127.0.0.1
IB_PAPER_PORT=4002
IB_CLIENT_ID=10
IB_ADMIN_CLIENT_ID=110
```

A host, a port, and a client id. The TWS socket API exchanges no credentials at
all; the gateway answers because *it* is logged in. That is the entire reason the
architecture is shaped this way — the credential never enters anything in this
repository, so there is no place for it to leak from.

Three things enforce that rather than merely documenting it:

- `scripts/verify_safety.sh` **fails the deployment** if it finds `IB_USERNAME`,
  `IB_PASSWORD`, `IBKR_USER`, `IBKR_PASSWORD` or `TWS_PASSWORD` in `.env`.
- `docker-compose.yml` carries no credentials and no `4001`/`4002`, listed in its
  header as a deliberate omission.
- **This repository is public.** Anything committed to it is published to the
  internet the moment it is pushed.

The one place in this entire project where an IBKR password would ever be stored
is IBC, the unattended-login supervisor — which is why it is an open decision in
`docs/IBKR_API_NOTES.md` and not something already installed. You do not need it
for this checkout.

---

## Run this on your Mac, not the VPS

Both the gateway and the bot on one machine means nothing crosses a network
boundary, and 2FA is trivial because there is a screen in front of you. The
headless-X and unattended-login problems are real, but they are a *separate*
problem from the one this phase asks: **does the adapter work at all?**

Leave the VPS exactly as it is — `TRADING_MODE=mock`, halted, kill switch
engaged. Nothing below touches it.

---

## Step 1 — Get the paper account credentials (browser, ~5 minutes)

1. Log into **IBKR Client Portal** with your live credentials.
2. Go to **Settings → Account Settings → Paper Trading Account**.
   (Older layout: **Manage Account → Settings → Paper Trading Account**.)
3. If no paper account exists, create one. IBKR provisions it within a few
   minutes.
4. Note the **paper account id**. It starts with `DU`. Write it down — the
   checkout verifies the broker reports exactly this shape, and refuses if not.
5. Note the **paper username**. It is *not* your live username; IBKR issues a
   separate one.
6. **Set the paper password.** IBKR requires you to set it explicitly the first
   time. It is independent of your live password.

Two things worth knowing here, because they make the rest much easier:

- **Paper login has no 2FA.** IB Key is not required for a paper session. This is
  why the paper checkout can be done in one sitting and why the IBC question does
  not arise yet.
- On the same page there is usually an option to **share live market data with
  the paper account**. Turn it on. Without it, the `MARKET_DATA` probe will fail
  with a subscription error that has nothing to do with the code.

---

## Step 2 — Install IB Gateway (Mac, ~10 minutes)

1. Download **IB Gateway** — the *stable* channel, not TWS, not the latest
   channel — from IBKR's software page.
2. Install and launch it.
3. At the login window:
   - Select **Paper Trading** (not Live Trading). The toggle is on the login
     screen itself.
   - Enter the **paper username** and **paper password** from Step 1.
   - **This is the only place your IBKR credential is ever typed.** It stays
     inside IB Gateway.
4. Let it finish connecting. You should see the gateway's small status window.

If you accidentally log into Live here, quit and start again. The adapter would
refuse the session anyway — `account_type_from_account_id` resolves a `U`-prefixed
id to `live`, and `IBKRBroker` disconnects when the observed type does not match
the configured mode — but do not rely on that as your first line of defence.

---

## Step 3 — Lock the API down *before* anything connects

In IB Gateway: **Configure → Settings → API → Settings**

| Setting | Value | Why |
|---|---|---|
| Enable ActiveX and Socket Clients | ✅ on | otherwise nothing can connect |
| **Read-Only API** | ✅ **on** | the gateway itself refuses order submission |
| Socket port | `4002` | matches `IB_PAPER_PORT` |
| Allow connections from localhost only | ✅ on | keeps it off the network entirely |
| Trusted IPs | `127.0.0.1` only | |
| Master API client ID | *blank* | |
| Create API message log file | optional | useful if a probe fails oddly |

Click **OK**, then **File → Save Settings**.

**Read-Only API is the outermost of four independent layers.** The other three
are `ALLOW_ORDER_TRANSMIT=false`, `KILL_SWITCH=true`, and all six risk limits at
`0`. Leave every one of them in place for the whole checkout. Do not turn
Read-Only off "just to see" — that is RUNBOOK step 10, and it comes after the
contract has been verified against IBKR's own contract search.

Confirm the gateway is listening only on loopback:

```bash
lsof -nP -iTCP:4002 -sTCP:LISTEN
```

You want `127.0.0.1:4002`. If you see `*:4002` or `0.0.0.0:4002`, the API is
exposed to your local network — go back and tick "Allow connections from
localhost only" before continuing.

---

## Step 4 — Get the code running on the Mac

```bash
git clone https://github.com/dieselx42/PGJP_Trading.git
cd PGJP_Trading

python3.12 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'

python -m pytest -q          # expect: all tests pass, no network needed
```

---

## Step 5 — Install the IBKR API package

This is the packaging decision from `docs/IBKR_API_NOTES.md`. **Option A —
vendoring IBKR's own source — is what these instructions use**, because for the
one component that can move money, removing a third-party uploader from the
supply chain is worth a manual step. If you prefer Option B, say so; only
`app/broker/ibkr_broker.py` imports this package, so switching later is cheap.

1. Download the **TWS API** from `interactivebrokers.github.io` (the "TWS API
   Stable" macOS/Unix zip).
2. Install from the extracted source:

```bash
unzip ~/Downloads/twsapi_macunix.*.zip -d ~/twsapi
cd ~/twsapi/IBJts/source/pythonclient
pip install .          # into the same .venv you created above
cd -
```

3. Confirm the adapter can see it:

```bash
python -c "from app.broker.ibkr_broker import ibapi_available; print(ibapi_available())"
```

`True` means installed. `False` means the adapter will refuse to connect with an
explanatory message rather than an `ImportError` — by design, but it means the
install did not land in this virtualenv.

---

## Step 6 — Create a local `.env`

```bash
cp .env.example .env
chmod 600 .env
```

Then edit three things and **only** three things:

```ini
TRADING_MODE=paper           # was mock

DATABASE_PATH=./data/trading.db   # was /app/data/trading.db
LOG_DIR=./logs                    # was /app/logs
```

The last two matter: `/app/...` are *container* paths. On a Mac they do not exist
and are not writable, and the run will fail on the first migration with an error
that looks nothing like its cause.

```bash
mkdir -p data logs
```

**Everything else stays exactly as it is.** In particular:

```ini
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
```

There is still no IBKR username or password anywhere in this file, and
`scripts/verify_safety.sh` will fail the run if one appears.

---

## Step 7 — Run the checkout

```bash
python -m app.cli ibkr-checkout
```

It connects once on `IB_ADMIN_CLIENT_ID` (110), never the trading process's
client id, so it can never disconnect a running bot. It runs every read-only call
the system depends on and reports PASS / FAIL / SKIP per probe with the evidence
it saw, then hangs up. It exits non-zero if anything failed.

It **cannot place an order.** The broker is typed as a Protocol with no write
methods, so `mypy --strict` rejects an order call as an attribute error; it
refuses to run at all unless transmit is disabled, the kill switch is engaged and
the mode is paper; and it re-evaluates the transmit gate against a deliberately
perfect session and requires it to still refuse.

Then add the expiration, once you know which contract you mean:

```bash
python -m app.cli ibkr-checkout --contract-month 202512
```

Without it, `CONTRACT_QUALIFIES` is **skipped, never guessed** — picking a front
month implicitly is how an order lands on the wrong contract. The flag applies to
that run only and is never written back to `.env`.

`RUNBOOK.md` step 7 has the table of what each probe failure means. Two are worth
repeating here:

- **`CONTRACT_QUALIFIES` failing with `BrokerPermissionError` is the expected
  result** while US futures permission is pending. It is a *useful* failure: it
  proves the error classification table works and that permission errors are
  marked non-retryable. The probe reports what happened; you decide what it means.
- **`GATE_REFUSES_WHEN_EVERYTHING_ELSE_IS_GREEN` failing means stop.** It forces
  every session-side condition to its most permissive value, so anything that
  refuses is refusing because of your configuration. If it ever reports
  `allowed: true`, engage the kill switch and do not continue.

---

## Step 8 — One expected surprise

```bash
python -m app.cli verify
```

On the Mac this reports **`POSTURE_NOT_APPROVED`**, with `TRADING_MODE` as the
failure. That is correct. The approved *deployed* posture is `mock`, and
`app/safety/posture.py` checks for exactly that. The Mac is a deliberate,
temporary, local deviation for the duration of the checkout.

**The VPS must stay in `mock` and must keep reporting `APPROVED_POSTURE`.** If it
ever reports anything else, that is a real problem. Do not "fix" the posture
checker to accept paper mode — the whole point of that file is that it does not
bend.

---

## What not to do

- Do not put IBKR credentials in `.env`, `docker-compose.yml`, the repository, or
  the Hostinger panel. There is nothing to put there.
- Do not turn off IB Gateway's Read-Only API mode during the checkout.
- Do not change any of the six risk limits from `0`. Zero means *not configured*,
  which means *not authorised* — never *unlimited*.
- Do not move the gateway to the VPS until the checkout passes locally.
- Do not install IBC yet. You do not need it for paper, and it is the one
  component that would hold your IBKR password.
