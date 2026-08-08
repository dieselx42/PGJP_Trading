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
IB_HOST=ib-gateway
IB_PAPER_PORT=4004
IB_LIVE_PORT=4003
```

`4003`/`4004` rather than `4001`/`4002`: the image binds IB Gateway to its own
loopback and bridges it with socat, so those are the ports another container
sees. `4004` is paper.

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

## Decommissioning the host-installed gateway

Once the container works, the host install is redundant and is a second thing
that can hold port 4002:

```bash
systemctl disable --now ibgateway vncserver@1
```

Leave the files in place until you are satisfied — nothing costs anything by
sitting on disk, and the ufw denies on 4001/4002 stay useful either way.

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
