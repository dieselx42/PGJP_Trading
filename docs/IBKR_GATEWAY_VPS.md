# Running IB Gateway on the Hostinger VPS (host install)

> **Superseded by [`IBKR_GATEWAY_DOCKER.md`](IBKR_GATEWAY_DOCKER.md).** The
> gateway now runs as a compose service with IBC handling login, on an operator
> decision recorded in `SECURITY.md`. That trades a stored IBKR password for
> unattended operation.
>
> Kept because it is the only way to run this **without storing a credential**,
> and because two findings in it apply to any IB Gateway install: the API socket
> binds `*:4002` regardless of the localhost-only checkbox, and `pgrep -f
> ibgateway` also matches the VNC server.

Steps for installing IB Gateway on `srv1792440.hstgr.cloud`, logging into it
interactively over an SSH-tunnelled VNC session, and pointing the bot at it.

**Paper account only.** Nothing here applies to a live account, and live trading
remains out of scope.

## What this does and does not do

| | |
|---|---|
| ✅ | IB Gateway runs on the VPS host, owned by a dedicated non-login user |
| ✅ | Port 4002 is not reachable from the internet — enforced by ufw, **not** by the bind address; see below |
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

## The firewall is load-bearing. Verified, not assumed.

IB Gateway's API settings offer a checkbox — *"Allow connections from localhost
only"* — and no bind-address field. It is natural to read that as "the socket
binds `127.0.0.1`". **It does not.** With the box ticked, on a real install:

```
$ ss -tlpn | grep 4002
LISTEN 0  50  *:4002  *:*  users:(("java",pid=15147,fd=72))
```

`*:4002` — every interface, public one included. The gateway accepts the TCP
connection from anywhere and then filters at the *application* layer, refusing
the API handshake for a peer that is not trusted. That is meaningfully weaker
than not listening: unauthenticated Java code still parses input from anyone who
can reach the port.

So the promise "4002 is not reachable from the internet" rests entirely on ufw,
and that is worth stating plainly rather than discovering later. Step 7 adds
explicit `deny` rules and Step 7b proves the result from off-host.

### What this means for the container

A Docker container's `127.0.0.1` is its own loopback, not the host's, so a
bridged container cannot reach the host's gateway on `127.0.0.1` regardless of
any of the above. The bot therefore joins the host network namespace
(`network_mode: host`, Step 9) so that `IB_HOST=127.0.0.1` means what it says.

An earlier draft of this document justified that choice by claiming it preserved
a *bind-address* guarantee — 4002 physically absent from any public interface —
which a bridged setup would have had to trade away for a firewall rule. **That
guarantee was never available**, because IB Gateway does not bind loopback-only
under any setting. The firewall is load-bearing either way. Host networking is
still the right call, but for the narrower reason that it is the simplest way to
reach a host process from the container, not because it buys a stronger
guarantee than the alternative.

What host networking costs: the bot container no longer has its own network
namespace. What it does not cost: `read_only`, `cap_drop: ALL`,
`no-new-privileges` and the non-root uid are unaffected. The bot publishes no
ports and its health server binds loopback either way.

One related trap, for whenever a container *does* need a published port here:
**Docker inserts its DNAT rules ahead of ufw's INPUT chain**, so a published
container port is reachable from the internet even when ufw would deny it. It
does not apply to a host process like IB Gateway, but it is why this project
treats "no `ports:` mapping" as the primary control and ufw as the backstop.

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
    tigervnc-standalone-server tigervnc-common tigervnc-tools \
    xfonts-base x11-xserver-utils \
    icewm \
    libxext6 libxrender1 libxtst6 libxi6 libxrandr2 \
    libfreetype6 fontconfig fonts-dejavu-core
```

`icewm` is a deliberately tiny window manager — the gateway needs *a* window
manager, not a desktop environment.

Three of those are Ubuntu *Recommends* of `tigervnc-standalone-server`, which
`--no-install-recommends` skips. Each one fails later rather than here, so name
them explicitly:

- **`tigervnc-tools`** provides `vncpasswd`. Without it Step 3 dies with
  `vncpasswd: command not found` — `tigervnc-common` registers the `vncconfig`
  alternative but not the password tool.
- **`xfonts-base`** provides the `fixed` font. Xvnc refuses to start without it
  (`could not open default font 'fixed'`).
- **`x11-xserver-utils`** provides `xrdb` / `xsetroot`, which session startup
  expects.

Keeping `--no-install-recommends` is still correct — it is what stops a full
desktop environment landing on a trading server — but it means the genuinely
required Recommends have to be listed by hand.

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

**Run this on the Mac, not on the VPS.** Run it in your SSH session by mistake
and the VPS resolves its own hostname to `127.0.1.1`, tries to SSH to itself, and
fails with `Permission denied (publickey)` — an error that looks like a key
problem and is not one.

`-N` means "forward the port, run no command", so success looks like nothing
happening: no output, no prompt returning. Leave the window open; closing it
drops the tunnel. Confirm from another Mac window with:

```bash
lsof -nP -iTCP:5901 -sTCP:LISTEN     # expect ssh listening on 127.0.0.1:5901
```

If the key has a passphrase and you would rather not retype it every reconnect:

```bash
ssh-add --apple-use-keychain ~/.ssh/id_ed25519
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

## Step 5 — Install IB Gateway

The installer is graphical, so it needs a display. **There is no terminal
emulator on that desktop** — `xterm` is only a *Suggests*, so
`--no-install-recommends` skipped it, and IceWM's "Terminal" menu entry points at
a binary that is not there.

Rather than install one, drive the installer from your SSH shell and let its
window appear in VNC. That keeps a trading server free of packages it does not
need.

In the **SSH session**, one command at a time (`su -` starts a subshell, so
pasting the block loses everything after it):

```bash
su - ibgw
```

```bash
export DISPLAY=:1
```

Then:

```bash
cd /opt/ibgateway
wget https://download2.interactivebrokers.com/installers/ibgateway/stable-standalone/ibgateway-stable-standalone-linux-x64.sh
chmod +x ibgateway-stable-standalone-linux-x64.sh
./ibgateway-stable-standalone-linux-x64.sh
```

The SSH session will sit there while **the installer window appears in the VNC
desktop**. Accept the default install location. Take the **stable** channel, not
latest.

If the URL 404s, get the current one from IBKR's software page — they move it
occasionally.

Every later launch works the same way: `su - ibgw`, `export DISPLAY=:1`, run the
binary, and interact with it in VNC.

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
| **Read-Only API** | ✅ **on** — leave it on for the whole checkout |
| Socket port | `4002` |
| **Allow connections from localhost only** | ✅ **on** (it ships this way) |
| Trusted IPs | `127.0.0.1` |
| Master API client ID | *blank* |

*"Enable ActiveX and Socket Clients" is a TWS setting and does not appear in
Gateway — the API is the whole point of Gateway. The status window showing
`Interactive Brokers API Server: connected` settles it; do not go hunting.*

`Allow connections from localhost only` and `Trusted IPs` are near the **bottom**
of that pane, below `Reset API order ID sequence`. The dialog also tends to open
partly off-screen; drag its title bar right to see the left-hand nav tree.

**Configure → Settings → Lock and Exit**

- Enable **Auto Restart**. The gateway restarts daily on IBKR's schedule; with
  this on it does so without asking for credentials again.
- Disable any auto-logoff timer.

**File → Save Settings.**

### 7a — See what the socket actually did

```bash
ss -tlpn | grep 4002
```

**Expect `*:4002`.** Not `127.0.0.1:4002`. The localhost-only checkbox does not
change the bind address — the gateway listens everywhere and filters peers at the
application layer. This is the real behaviour, not a misconfiguration, and it is
why the next two steps are mandatory rather than belt-and-braces.

### 7b — Deny the ports explicitly

```bash
ufw deny 4002/tcp comment 'IB Gateway API - never reachable off-host'
ufw deny 4001/tcp comment 'IB Gateway live API - never reachable off-host'
ufw status numbered
```

ufw's default `deny (incoming)` already blocks these, so this changes nothing
today. It changes what happens *later*: an implicit block is the absence of a
rule, which someone reopens without noticing by loosening the default policy or
adding a broad allow. An explicit `DENY IN` has to be actively removed and is
visible in `ufw status`, where an operator will look.

4001 gets a rule too, even though nothing listens on it. The moment something
does is exactly the wrong moment to find out the firewall was never configured
for it.

### 7c — Prove it from off the machine

**On your Mac** — this is the only test that means anything:

```bash
nc -z -w3 srv1792440.hstgr.cloud 4002 && echo "REACHABLE - PROBLEM" || echo "blocked - good"
```

Run this on the VPS by mistake and it resolves the hostname to `127.0.1.1`, the
machine's own loopback, and reports `succeeded!` — which is correct, expected,
and completely uninformative. **Check the IP in the output.** `127.0.1.1` means
you tested the wrong thing. The public address means you tested the right one.

A correct run hangs for the full 3-second timeout and then prints `blocked - good`.
ufw drops rather than rejects, so from outside the port looks absent rather than
closed.

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

The gateway does not survive a reboot, and that is the deliberate consequence of
not installing login automation. Everything else does.

### What survives, verified on a real reboot (2026-08-08)

| | |
|---|---|
| SSH, key-only | `sshd -T` still reports `passwordauthentication no` |
| ufw, including the 4001/4002 denies | rules persist across boot |
| The bot container | `restart: unless-stopped` brings it back **halted**, `APPROVED_POSTURE`, with no intervention |
| Traefik and other containers | untouched |
| **IB Gateway** | **gone** — needs a manual login |
| **The VNC server** | **gone** — unless the systemd unit below is installed |

Check the survivors before rebuilding anything on top of them. `sshd -T` rather
than reading the config file: this project has had a setting that looked applied
and was being overridden by a file loaded earlier.

```bash
sshd -T | grep -E 'permitrootlogin|passwordauthentication|kbdinteractiveauthentication'
ufw status numbered
cd /opt/sol-futures-trading-bot && docker compose ps && \
  docker compose exec -T sol-trading-bot python -m app.cli verify
ss -tlpn | grep -E ':4002|:5901' || echo "gateway and VNC down - expected"
```

### Bringing the gateway back

Both the display and the gateway run as **systemd services** (see below). After a
reboot they start themselves and the gateway comes back sitting at its login
window. All you do is tunnel in and log in.

```bash
systemctl status vncserver@1 ibgateway --no-pager | head -20
```

If either is down: `systemctl restart vncserver@1 ibgateway`.

Then from your Mac:

```bash
ssh -N -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes \
    -L 5901:127.0.0.1:5901 root@srv1792440.hstgr.cloud
```

```bash
lsof -nP -iTCP:5901 -sTCP:LISTEN     # expect ssh on 127.0.0.1:5901
```

Finder → ⌘K → `vnc://localhost:5901`, then log in: **Paper Trading** tab, the
**paper** username (the one beginning `D`, not your live/master username), no 2FA.

Confirm, as root:

```bash
ss -tlpn | grep 4002                                          # expect *:4002 -- see 7a
pgrep -u ibgw -f 'install4j.ibgateway.GWClient' | wc -l       # expect exactly 1
```

**Match the Java main class, never the bare string `ibgateway`.** Xtigervnc runs
with `-auth /opt/ibgateway/.Xauthority` on its command line, so `pgrep -f
ibgateway` matches the *display server* as well and reports 2 when one gateway is
running. Worse, `pkill -u ibgw -f ibgateway` kills the VNC server — and a gateway
launched afterwards against `DISPLAY=:1` then dies silently, because the display
it was told to use no longer exists. That exact sequence cost an hour here.

**Exactly one gateway.** Two instances fight over the same login: the second
knocks out the first and bounces you back to the login screen after what looks
like a successful sign-in. Same reason not to open Client Portal or the IBKR
mobile app against this account while the gateway is up — that is error 10197,
classified non-retryable so the bot never joins the fight.

### Why systemd, and not a background job

The first version of this procedure said to launch the gateway with `&`. It died
twice in one evening, for two different reasons, and only one of them was
understood at the time.

The first death is unexplained. A plain `&` leaves the process attached to the
SSH session's terminal, so `SIGHUP` on a dropped session is the obvious
candidate, but the shell that owned the job was still alive afterwards — so that
is a hypothesis, not a finding.

The second death was self-inflicted, by a diagnostic in this very document:
`pkill -u ibgw -f ibgateway` killed the *VNC server* alongside the gateway,
because Xtigervnc carries `/opt/ibgateway/.Xauthority` on its command line. The
gateway launched immediately afterwards was pointed at a display that no longer
existed and exited without complaint. The lesson is narrow and worth keeping:
**a process-matching pattern that also matches a home directory path is a
loaded gun**, and the `pgrep`/`pkill` commands above now match the Java main
class instead.

Neither cause survives the fix, which is why the fix is worth having regardless
of which explanation was right. A process holding a brokerage session for days
should not live inside a login session at all. These units put both in
`/system.slice`, independent of anyone being logged in, and start them at boot:

```ini
# /etc/systemd/system/vncserver@.service
[Unit]
Description=TigerVNC display for IB Gateway login (display only)
After=network.target

[Service]
Type=forking
User=ibgw
ExecStartPre=-/usr/bin/vncserver -kill :%i
ExecStart=/usr/bin/vncserver :%i -localhost yes -geometry 1440x900 -depth 24
ExecStop=/usr/bin/vncserver -kill :%i
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/ibgateway.service
[Unit]
Description=IB Gateway (process only -- login is manual, over VNC)
After=vncserver@1.service
Requires=vncserver@1.service

[Service]
Type=simple
User=ibgw
Environment=DISPLAY=:1
Environment=XAUTHORITY=/opt/ibgateway/.Xauthority
ExecStart=/bin/sh -c 'exec /opt/ibgateway/Jts/ibgateway/*/ibgateway'
Restart=on-failure
RestartSec=30
StartLimitBurst=3

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now vncserver@1 ibgateway
```

**These start processes. They do not log in.** No IBKR credential appears in
either unit, nothing is stored on disk, and the login window still waits for a
human. That boundary is the whole point: automating the *process* is operations,
automating the *authentication* is the IBC decision, and only the first is done
here.

`StartLimitBurst=3` stops a genuinely broken gateway restart-looping forever.

### Meanwhile the bot is safe without you

The connection fails, the state goes `DISCONNECTED`, and the transmit gate
refuses on connection state before any other interlock is consulted. Reconnection
backs off 2s → 300s and retries at that capped interval indefinitely: it does not
hammer IBKR and it does not exit. On reconnect it reconciles, and **if
reconciliation fails the state becomes `SAFE` and stays there** until a human
resolves the discrepancy.

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
