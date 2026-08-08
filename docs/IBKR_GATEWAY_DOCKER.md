# IB Gateway as a container

The gateway runs as a compose service alongside the bot, authenticating itself
via IBC. This supersedes `docs/IBKR_GATEWAY_VPS.md`, which describes the
host-installed gateway with a manual VNC login.

## Read this first

**This setup stores your IBKR password on the server.** `ghcr.io/gnzsnz/ib-gateway`
embeds IBC and will not start without `TWS_USERID` and `TWS_PASSWORD` in its
environment. That reverses the original brief's prohibition, on a decision
recorded in `SECURITY.md` — read that section before going further.

Practical consequences to accept deliberately:

- **Root on the VPS is now equivalent to your IBKR password.** Treat the machine
  as holding a brokerage credential, because it does.
- **A third-party image sits in the authentication path of a brokerage account.**
- **Use a paper credential.** Live is a separate, explicit decision.
- **Rotate at IBKR** if the server is ever suspected compromised. The credential
  is no longer only in your head.

What did *not* change: **the trading bot still holds no credential.** The gateway
reads `.env.ibgateway`; the bot reads `.env`, which is still checked on every
deployment and still fails it if a credential-shaped variable appears. The bot
has no field to receive one and no code path that would use it.

## What this buys

Three things the host-installed version could not do:

**Unattended operation.** The gateway logs itself in after a reboot. No tunnel,
no VNC, no human. The manual procedure needed two starts before the login screen
was even reachable.

**A stronger port guarantee.** The API ports are never published to a host
interface. The bot reaches `ib-gateway:4004` across the private bridge, so
there is nothing on the host for a firewall rule to protect. The host-installed
gateway listened on `*:4002` and depended entirely on ufw — see
`docs/IBKR_GATEWAY_VPS.md` for why that was weaker than it looked.

**Persistent settings** in a named volume, rather than a home directory that a
reinstall would flatten.

---

## Setup

### 0. Stop the host-installed gateway first

If `docs/IBKR_GATEWAY_VPS.md` was followed and a host gateway is logged in,
**stop it before starting the container**:

```bash
systemctl disable --now ibgateway vncserver@1
ss -tlpn | grep -E ':400[1-4]' || echo "host gateway gone - correct"
```

IBKR permits one session per login. With both running, IBC authenticates and
then stops at a dialog:

```
IBC: detected dialog entitled: Existing session detected
IBC: User must choose whether to continue with this session (scenario 1)
```

It is waiting for an answer it has deliberately not been configured to give.

Stopping first looks like the wrong order — confirming the replacement works
before retiring what it replaces is normally right — but the two cannot run at
once, so there is nothing to confirm while both exist. `systemctl disable`
leaves everything on disk, so `systemctl enable --now ibgateway vncserver@1`
puts the old path back if the container disappoints.

### 1. Create the credential file

```bash
cd /opt/sol-futures-trading-bot
cp .env.ibgateway.example .env.ibgateway
chmod 600 .env.ibgateway
```

Edit it and set, at minimum:

```ini
TWS_USERID=DUQ181787          # the PAPER username, beginning with D
TWS_PASSWORD=<paper password>
TRADING_MODE=paper
READ_ONLY_API=yes
VNC_SERVER_PASSWORD=<something other than your IBKR password>
```

`TWS_ACCEPT_INCOMING=accept` and `EXISTING_SESSION_DETECTED_ACTION=primary` are
already in the template and both matter — see "Two settings whose absence is
silent" below before removing either.

`chmod 600` is enforced, not advised: `scripts/verify_safety.sh` **fails the
deployment** if this file is group- or world-readable, or if it is tracked by
git. Unlike the bot's `.env`, loose permissions here are a failure rather than a
warning, because this one holds a password.

> **`TRADING_MODE` appears in both files and means different things.** In
> `.env.ibgateway` it selects which IBKR account the *gateway* logs into. In
> `.env` it selects whether the *bot* uses a broker at all. Gateway `paper` +
> bot `mock` is the normal safe combination: the gateway is authenticated and
> idle, and the bot does not dial it.

### 2. Point the bot at the service

In the bot's `.env`:

```ini
IB_HOST=127.0.0.1
IB_PAPER_PORT=4002
IB_LIVE_PORT=4001
```

**Loopback, because the bot shares the gateway container's network namespace**
(`network_mode: "service:ib-gateway"` in `docker-compose.yml`). This is the one
thing that made the connection work, and it took a long evening to establish:

IB Gateway admits API clients only from loopback. On a bridge network the bot
connects from `172.16.x.x`, and the gateway **accepts the TCP connection and
closes it without a word** — nothing in its log, no dialog, and only ibapi's
generic `502 Couldn't connect to TWS` at the client. Unticking *"allow
connections from localhost only"* did not fix it. Adding the bridge address to
Trusted IPs did not fix it. Sharing the namespace so the connection genuinely
arrives as `127.0.0.1` fixed it immediately.

That also lets the gateway keep its strictest setting, which is a better place
to land than the permissive one that did not work anyway.

**Consequence worth knowing:** recreating `ib-gateway` destroys the namespace
the bot is attached to. Recreate the gateway and restart the bot too.

The image also runs `socat` bridging `4004 → 127.0.0.1:4002` for clients on a
network. That path is unnecessary here and unused.

Leave everything else alone — `TRADING_MODE=mock`, `ALLOW_ORDER_TRANSMIT=false`,
`KILL_SWITCH=true`, all six risk limits `0`.

### 3. Deploy

```bash
bash scripts/deploy.sh main
```

### 4. Verify

```bash
docker compose ps
docker compose logs ib-gateway | tail -40
```

The gateway takes 30–60 seconds to log in. Look for the API server reporting
ready.

**The API port must not be on the host:**

```bash
ss -tlpn | grep -E ':400[1-4]' || echo "no gateway API port on the host - correct"
```

Anything found here means a `ports:` mapping crept into the compose file.

**Connectivity, from the bot's own network namespace:**

```bash
docker compose exec -T sol-trading-bot python -c \
  "import socket; s=socket.create_connection(('ib-gateway',4004),5); print('reachable'); s.close()"
```

**The checkout** — the real proof:

```bash
docker compose exec -T -e TRADING_MODE=paper sol-trading-bot \
  python -m app.cli ibkr-checkout --contract-month 20260828
```

`TRADING_MODE=paper` is passed inline for that one command and is **not** written
to `.env`. The deployed configuration stays `mock` and halted.

### 5. Manual intervention, when needed

VNC is published to `127.0.0.1:5900` for the occasional stuck login or dialog:

```bash
ssh -N -o ServerAliveInterval=30 -L 5900:127.0.0.1:5900 root@srv1792440.hstgr.cloud
```

Then point a viewer at `localhost:5900` — on macOS, Finder → ⌘K →
`vnc://localhost:5900`. Check the tunnel before blaming the viewer:

```bash
lsof -nP -iTCP:5900 -sTCP:LISTEN
```

With no tunnel, macOS reports *"Connection failed to localhost — make sure Screen
Sharing is enabled on the remote computer"*, which points you at the wrong
machine entirely.

---

## After a reboot

Nothing to do. Both services carry `restart: unless-stopped`, the gateway
authenticates itself, and the bot comes back halted in `mock`.

Confirm rather than assume:

```bash
docker compose ps
docker compose exec -T sol-trading-bot python -m app.cli verify
```

Want `APPROVED_POSTURE` 12/12 and both containers healthy.

## Two settings whose absence is silent

IBC's `config.ini` is generated from a template with `envsubst`, so only the keys
written as `${VAR}` can be set from the environment. Two of those decide whether
an unattended gateway works at all, and leaving them empty fails in ways that
look like anything but a missing setting.

**`TWS_ACCEPT_INCOMING`** → `AcceptIncomingConnectionAction`. IB Gateway prompts
before admitting an API client it does not recognise. IBC only watches for that
dialog when this is set, so with it empty: the TCP connection is accepted, the
API handshake never completes, the client times out waiting for managed
accounts, and **nothing appears in the gateway log** — IBC never looked for a
dialog it was not told to handle. Hours can go into that one, because every
observation points at the network and the network is fine.

`accept` is safe here specifically because the API port is never published: it
exists only on the private Docker network and the bot container is the only thing
that can reach it. Publishing 4001–4004 to the host would make this a different
question.

**`EXISTING_SESSION_DETECTED_ACTION`** → `primary`. IBKR permits one session per
login; when another holds it, the gateway asks and IBC answers with this. Empty
means wait for a human, which for a container means hang.

Three related keys are **hardcoded empty in the template** and cannot be set from
the environment at all — `OverrideTwsApiPort`, `TrustedTwsApiClientIPs` and
`BindAddress`. Changing those needs `CUSTOM_CONFIG=yes` and a bind-mounted
`config.ini`. Worth knowing before spending time trying to set them.

Note also that `jts.ini` is written **only if it does not already exist**, so
gateway-side settings persist in the volume across recreates and are not
refreshed from the template.

## The host-installed gateway

Stopped in step 0, before the container was ever started — see there for why the
usual "confirm the replacement first" ordering does not apply.

Leave the files and the systemd units on disk. `systemctl enable --now ibgateway
vncserver@1` restores the manual-login path, which is the only credential-free
way to run this. Keep the ufw denies on 4001/4002 either way: they cost nothing
and they matter the moment anything binds those ports on the host again.

## Pinning the image

`:stable` is a floating tag on the one container holding an IBKR password, which
means an unreviewed change can arrive on any recreate. Pin it once you have a
working version:

```bash
docker inspect --format='{{index .RepoDigests 0}}' ghcr.io/gnzsnz/ib-gateway:stable
```

Put the resulting `image@sha256:...` in `docker-compose.yml` and update it
deliberately, the same way the `ibapi` distribution was chosen deliberately in
`docs/IBKR_API_NOTES.md`.
