# Running IB Gateway on the Hostinger VPS

Steps for installing IB Gateway on `srv1792440.hstgr.cloud`, logging into it
interactively over an SSH-tunnelled VNC session, and pointing the bot at it.

**Paper account only.** Nothing here applies to a live account, and live trading
remains out of scope.

## What this does and does not do

| | |
|---|---|
| ✅ | IB Gateway runs on the VPS host, owned by a dedicated non-login user |
| ✅ | Port 4002 binds to `127.0.0.1` and **cannot** be reached from the internet |
| ✅ | You log in by hand, over an SSH tunnel, with a real login window |
| ✅ | The bot connects with `IB_HOST` / port / `IB_CLIENT_ID` and nothing else |
| ❌ | **No IBC, no login automation, no stored IBKR password** |
| ❌ | **Not unattended.** Every reboot and every IBKR re-auth needs you at a keyboard |

Your IBKR username and password are typed into IB Gateway's login window and go
nowhere else. Not `.env`, not the image, not the compose file, not GitHub, not
the database. The bot has no field to receive them.

There is exactly one secret created below — a **VNC password**, which protects
the remote display, not your brokerage account. It lives in
`/opt/ibgateway/.vnc/passwd`, mode `0600`, owned by the `ibgw` user, and never
enters this repository.

---

## Why the container joins the host network

IB Gateway's API settings offer a checkbox — *"Allow connections from localhost
only"* — and no bind-address field. Ticked, it listens on `127.0.0.1:4002` and
nothing else, which is a property of the socket rather than a rule that has to be
maintained. Unticked, it listens on **every** interface, including the public
one, and only a firewall rule stands between 4002 and the internet.

A Docker container's `127.0.0.1` is its own loopback, not the host's, so a
bridged container cannot reach a loopback-bound gateway. Resolving that means
either unticking the box (and depending on ufw) or putting the bot in the host's
network namespace. This document takes the second route: **the checkbox stays
ticked** and the container joins the host netns, so `IB_HOST=127.0.0.1` means
what it says.

What that costs: the bot container no longer has its own network namespace. What
it does not cost: `read_only`, `cap_drop: ALL`, `no-new-privileges` and the
non-root uid are all unaffected. The bot publishes no ports and its health server
binds loopback either way.

There is one more thing the ufw route would have to get right. **Docker inserts
its DNAT rules ahead of ufw's INPUT chain**, so a published container port is
reachable from the internet even when ufw would deny it. That does not apply to a
host process like IB Gateway, but it is the kind of subtlety that makes
"firewall-enforced" a weaker promise than "not bound to that interface."

---

## Step 0 — Preflight

IB Gateway bundles a JRE and wants real memory. Check before installing:

```bash
free -h        # want >= 2 GB total; 1 GB will thrash
df -h /        # want >= 3 GB free
lsb_release -a # these instructions assume Debian/Ubuntu
```

If the VPS has under 2 GB of RAM, stop and tell me — the gateway and the bot on
one small box is a real constraint, not a detail.

---

## Step 1 — A dedicated user for the gateway

The gateway holds brokerage credentials in memory and renders a GUI. It should
not be root.

```bash
useradd --create-home --home-dir /opt/ibgateway --shell /bin/bash ibgw
passwd -l ibgw          # no password login; reach it with `su - ibgw` from root
chmod 750 /opt/ibgateway
```

---

## Step 2 — X libraries and a VNC server

IB Gateway is a Java GUI application; a headless VPS has none of what it needs.

```bash
apt-get update
apt-get install -y --no-install-recommends \
    tigervnc-standalone-server tigervnc-common \
    icewm \
    libxext6 libxrender1 libxtst6 libxi6 libxrandr2 \
    libfreetype6 fontconfig fonts-dejavu-core
```

`icewm` is a deliberately tiny window manager — the gateway needs *a* window
manager, not a desktop environment.

**This installs nothing that touches Traefik, Docker, or the existing stack.**

---

## Step 3 — Configure VNC, bound to loopback

> **Run 3a and 3b separately.** `su -` starts a new interactive shell and
> `vncpasswd` prompts for input. Pasting them as one block means the subshell
> swallows everything after `su` instead of running it, and you end up at an
> `ibgw@` prompt with nothing done and no error to tell you so.

**3a.** Become the gateway user, then set the VNC password when the prompt
appears. This protects the remote *display*, not your IBKR account. Answer `n`
to the view-only password.

```bash
su - ibgw
```

```bash
vncpasswd
```

**3b.** Configure the session. Nothing here prompts, so this block is safe to
paste whole:

```bash
mkdir -p ~/.vnc
cat > ~/.vnc/xstartup <<'EOF'
#!/bin/sh
unset SESSION_MANAGER DBUS_SESSION_BUS_ADDRESS
exec icewm-session
EOF
chmod +x ~/.vnc/xstartup
```

**3c.** Start it — `-localhost yes` is the part that matters:

```bash
vncserver :1 -localhost yes -geometry 1440x900 -depth 24
```

Expect a few lines ending in `New Xtigervnc server 'srv1792440:1 (ibgw)' on port
5901 for display :1.` Then `exit` back to root.

**3d.** Verify from the **root** shell:

```bash
ss -tlpn | grep 5901
```

You must see `127.0.0.1:5901`. If you see `0.0.0.0:5901` or `*:5901`, the display
is reachable from the internet — kill it (`su - ibgw -c 'vncserver -kill :1'`)
and redo 3c with `-localhost yes`. Do not continue until it is loopback-only.

---

## Step 4 — Tunnel in from your Mac

On your Mac, in its own terminal window — leave it running:

```bash
ssh -N -L 5901:127.0.0.1:5901 root@srv1792440.hstgr.cloud
```

Then connect with macOS's built-in Screen Sharing: **Finder → Go → Connect to
Server** (⌘K), and enter:

```
vnc://localhost:5901
```

Enter the VNC password from Step 3. You should get an empty IceWM desktop.

The VNC port is never exposed. It is reachable only through this tunnel, which is
authenticated by your SSH key.

---

## Step 5 — Install IB Gateway inside that session

Run the installer *in the VNC desktop*, not over plain SSH — it is a graphical
installer and this way it has a display.

Open a terminal inside the VNC session (IceWM: right-click the desktop →
Terminal), then:

```bash
cd /opt/ibgateway
wget https://download2.interactivebrokers.com/installers/ibgateway/stable-standalone/ibgateway-stable-standalone-linux-x64.sh
chmod +x ibgateway-stable-standalone-linux-x64.sh
./ibgateway-stable-standalone-linux-x64.sh
```

Accept the default install location. Take the **stable** channel, not latest.

If the URL 404s, get the current one from IBKR's software page — they move it
occasionally.

---

## Step 6 — Log in

Launch IB Gateway from the IceWM menu or `/opt/ibgateway/Jts/ibgateway/*/ibgateway`.

At the login window:

- Select **Paper Trading**. Not Live.
- Enter your **paper** username and password (a separate credential from your
  live login — see `docs/IBKR_PAPER_CHECKOUT.md` Step 1 if you have not set it).
- **Paper login has no 2FA.** IB Key is not involved. This is the single biggest
  reason the paper phase is manageable without login automation.

**This window is the only place your IBKR credential is ever entered.**

If you find yourself on the Live tab, quit and start again. The adapter would
refuse the session anyway — a `U`-prefixed account id resolves to `live`, and
`IBKRBroker` disconnects when the observed type does not match `TRADING_MODE` —
but do not lean on that as your first defence.

---

## Step 7 — Lock the API down

**Configure → Settings → API → Settings**

| Setting | Value |
|---|---|
| Enable ActiveX and Socket Clients | ✅ on |
| **Read-Only API** | ✅ **on** — leave it on for the whole checkout |
| Socket port | `4002` |
| **Allow connections from localhost only** | ✅ **on** — this is the load-bearing one |
| Trusted IPs | `127.0.0.1` |
| Master API client ID | *blank* |

**Configure → Settings → Lock and Exit**

- Enable **Auto Restart**. The gateway restarts daily on IBKR's schedule; with
  this on it does so without asking for credentials again.
- Disable any auto-logoff timer.

**File → Save Settings.**

Now verify from a root shell on the VPS — this is the check that answers
"can 4002 be reached from the internet":

```bash
ss -tlpn | grep 4002
```

**Required:** `127.0.0.1:4002`.
**Stop immediately** if you see `0.0.0.0:4002` or `*:4002` — the localhost-only
box is unticked and the API is exposed to the network. Re-tick it, save, restart
the gateway, and check again.

Belt and braces, from your Mac:

```bash
nc -z -w3 srv1792440.hstgr.cloud 4002 && echo "EXPOSED - STOP" || echo "not reachable, correct"
```

---

## Step 8 — Prove the adapter works, without touching Docker

Do this **before** changing anything about the container. It separates "does the
IBKR adapter work" from "can the container reach the gateway" — two questions
that fail in similar-looking ways and are much easier to debug apart.

As root on the VPS:

```bash
cd /opt/sol-futures-trading-bot
python3 -m venv /opt/ibgateway/checkout-venv
/opt/ibgateway/checkout-venv/bin/pip install -e '.[dev]'
```

Install the TWS API — Option A from `docs/IBKR_API_NOTES.md`, vendoring IBKR's
own source rather than the third-party PyPI redistribution:

```bash
cd /opt/ibgateway
wget https://interactivebrokers.github.io/downloads/twsapi_macunix.<version>.zip
unzip twsapi_macunix.*.zip
cd IBJts/source/pythonclient
/opt/ibgateway/checkout-venv/bin/pip install .
```

Run the checkout against a **temporary** environment. Note that these values are
passed inline and **never written to `/opt/sol-futures-trading-bot/.env`** — the
deployed configuration stays `TRADING_MODE=mock` and the bot stays halted
throughout:

```bash
cd /opt/sol-futures-trading-bot
TRADING_MODE=paper \
IB_HOST=127.0.0.1 \
DATABASE_PATH=/opt/ibgateway/checkout.db \
LOG_DIR=/opt/ibgateway \
/opt/ibgateway/checkout-venv/bin/python -m app.cli ibkr-checkout
```

The checkout connects on `IB_ADMIN_CLIENT_ID` (110), never the trading process's
client id, so it cannot disturb the running container. It cannot place an order:
the broker is typed as a Protocol with no write methods, it refuses to run unless
transmit is off and the kill switch is engaged, and its final probe forces every
session-side condition green and requires the transmit gate to refuse anyway.

`RUNBOOK.md` step 7 has the table of what each probe means. In short:

- `CONTRACT_QUALIFIES` failing with `BrokerPermissionError` is **expected** while
  US futures permission is pending, and is a useful result — it proves the error
  classification works and that permission errors are never retried.
- `GATE_REFUSES_WHEN_EVERYTHING_ELSE_IS_GREEN` failing means **stop**.

Add the expiration once you know which contract you mean:

```bash
... python -m app.cli ibkr-checkout --contract-month 202512
```

**Do not proceed to Step 9 until this passes.**

---

## Step 9 — Let the container reach the gateway

Only after Step 8 passes. This is a change to `docker-compose.yml` that needs a
PR and your merge; it cannot reach the VPS on its own.

```yaml
    # Joins the host network namespace so IB_HOST=127.0.0.1 reaches the gateway
    # bound to the host's loopback. The alternative -- a bridged container --
    # would require IB Gateway to listen on all interfaces, which puts 4002 on
    # the public interface with only a firewall rule in front of it.
    network_mode: host
```

and remove both the service's `networks:` list and the top-level `networks:`
block, which are incompatible with `network_mode: host`.

Then, and only then, on the server:

```ini
TRADING_MODE=paper      # was mock
```

Everything else in `.env` stays exactly as it is — `ALLOW_ORDER_TRANSMIT=false`,
`KILL_SWITCH=true`, `SOL_FUTURES_PERMISSION_READY=false`, all six risk limits
at `0`.

Two things to expect after this change:

- The bot's health service moves from the container's loopback to the **host's**
  loopback at `127.0.0.1:8787`. Confirm nothing else on the host wants that port
  (`ss -tlpn | grep 8787`) before deploying.
- `python -m app.cli verify` will report **`POSTURE_NOT_APPROVED`** with
  `TRADING_MODE` as the failure. That is correct: `app/safety/posture.py` encodes
  `mock` as the approved deployed posture. **Do not edit the posture checker to
  accept paper** — that file exists precisely because it does not bend.

---

## After a reboot

Nothing here survives a reboot on its own, and that is the deliberate consequence
of not installing login automation.

1. `vncserver :1 -localhost yes -geometry 1440x900 -depth 24` as `ibgw`
2. SSH tunnel from your Mac, connect VNC
3. Launch IB Gateway, log in by hand
4. Verify `ss -tlpn | grep 4002` shows `127.0.0.1:4002`

Meanwhile the bot is safe without intervention: the connection fails, the state
goes `DISCONNECTED`, the transmit gate refuses on connection state before any
other interlock is consulted, and reconnection backs off 2s → 300s and retries at
that capped interval indefinitely. It does not hammer IBKR and it does not exit.

**Optional:** a systemd unit that brings the *display* up at boot, so you only
have to tunnel in and log in rather than start the VNC server first. It starts
the X/VNC session only and **never touches IBKR authentication**:

```ini
# /etc/systemd/system/vncserver@.service
[Unit]
Description=TigerVNC server for IB Gateway login (display only)
After=network.target

[Service]
Type=forking
User=ibgw
ExecStart=/usr/bin/vncserver :%i -localhost yes -geometry 1440x900 -depth 24
ExecStop=/usr/bin/vncserver -kill :%i

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now vncserver@1
```

---

## When IBKR requires reauthentication

Three separate events:

| Event | Cadence | What you do |
|---|---|---|
| Daily gateway restart | Daily | Nothing — Auto Restart (Step 7) handles it without credentials |
| Full re-authentication | Roughly weekly, around IBKR's Sunday reset — verify against your own account | Log in again via VNC. Paper: username + password, no 2FA |
| Competing session | Whenever you log into Client Portal or the mobile app with the same account | Log the gateway back in. Error 10197 is classified **non-retryable** — reconnecting into a fight over one session is how an account gets locked out |

In all three the bot behaves as after a reboot: disconnected, gate refuses,
capped backoff, no orders, positions at the broker untouched. On reconnect it
reconciles; **if reconciliation fails the state becomes `SAFE` and stays `SAFE`**
until a human resolves the discrepancy. It does not retry its way back into
trading.

---

## The honest limitation

**This is not an unattended system, and it cannot become one under your current
constraints.** Every path to unattended operation runs through IBC or an
equivalent supervisor holding your IBKR password — the one credential this
architecture otherwise never stores. Nothing of the sort is installed and nothing
will be without your explicit approval.

For paper trading this is entirely workable: sessions are long-lived, there is no
2FA, and a dropped connection costs a re-login and nothing else.
