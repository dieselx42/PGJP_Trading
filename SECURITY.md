# Security

## Threat model

This system can move money. The failures that matter are:

1. An unintended order reaching a live account.
2. Credentials leaking — into git, into the image, into logs, or into a status
   endpoint.
3. IB Gateway's API port becoming reachable from the internet. It is an
   unauthenticated control channel for a brokerage account.
4. A deployment silently changing trading behaviour.

Everything below exists to address one of those four.

---

## 1. Secrets

### What this system holds

**No broker credentials, ever.** There is no IBKR username, password, or 2FA
material anywhere in this repository, in the image, in the database, or in the
configuration schema. IB Gateway owns authentication, and nothing here attempts
to bypass, automate, or work around it.

`scripts/verify_safety.sh` fails if a server `.env` contains `IB_USERNAME`,
`IB_PASSWORD`, `IBKR_PASSWORD`, or `TWS_PASSWORD`, and CI greps the whole tree
for the same.

### Where configuration lives

| Location | Contents |
|---|---|
| `/opt/sol-futures-trading-bot/.env` on the VPS, mode `0600` | the only production configuration |
| `.env.example` in git | documentation; every value is the safe one |
| GitHub Actions secrets | the deploy SSH key and host details, nothing else |
| The Docker image | nothing |

`.gitignore` excludes `.env` and every `.env.*` except `.env.example`, plus
`*.pem`, `*.key`, `id_rsa*`, `id_ed25519*`, and `secrets/`. `.dockerignore`
excludes the same from the build context, so a secret cannot reach a layer even
by accident.

CI fails the build if any environment file is tracked.

### Log redaction

A `RedactionFilter` is installed on **every** handler, so it also catches
third-party libraries logging through the standard `logging` module.

- Keys containing `password`, `secret`, `token`, `api_key`, `authorization`,
  `cookie`, `credential`, `private_key`, `ssh_key`, `access_key`, or
  `client_secret` are replaced, recursively, in nested structures.
- Free text matching `key=value` credential patterns, PEM private key blocks,
  and GitHub token formats is rewritten.
- Account identifiers are masked: `DU1234567` → `DU***567`.

Redaction is a **backstop, not a licence**. The application does not hold
secrets in the first place; this catches the case where something unexpected
flows through.

### Status endpoint

`/status` reports operational facts only. It has no code path that reads a
credential, and an integration test asserts that the serialised payload contains
no unmasked account id and no credential-shaped key.

---

## 2. Network isolation

### What is exposed

**Nothing.**

| Component | Binding | Published? | Behind Traefik? |
|---|---|---|---|
| Trading bot | no listening socket except health | no | no |
| Health / status | `127.0.0.1:8787` *inside the container* | no | no |
| SQLite | a file on disk | no | no |
| IB Gateway (future) | `127.0.0.1:4002` / `4001` | **must never be** | **must never be** |

`docker-compose.yml` has no `ports:` mapping and no Traefik labels. CI fails the
build if either appears.

The Docker healthcheck runs **inside** the container and reaches the health
service over loopback, so nothing needs publishing for orchestration to work.
Operators reach it through `docker compose exec` (`make status`).

### IB Gateway ports — the rule

> Ports 4001 and 4002 are an **unauthenticated control channel for a brokerage
> account**. Anyone who can reach them can place orders. There is no password on
> the API socket.

Therefore:

- **Never** add them to a `ports:` mapping.
- **Never** route them through Traefik.
- **Never** bind IB Gateway to `0.0.0.0`.
- Use IB Gateway's own **trusted IP allowlist** in addition to network controls.

When IB Gateway is introduced, exactly one of these two topologies:

**A — Gateway on the host (simplest):** bind it to `127.0.0.1` only, and give
the container `network_mode: host` or an `extra_hosts: host-gateway` entry.
Nothing is published either way.

**B — Gateway in a container (better isolation):** put the gateway and the bot on
a dedicated network declared `internal: true`. Docker then provides no route
from that network to the outside world at all, so a misconfiguration cannot
expose the port.

Verify after any change:

```bash
ss -tlpn | grep -E ':(4001|4002)'    # must be empty or 127.0.0.1 only
docker ps --format '{{.Names}}: {{.Ports}}'
sudo iptables -t nat -S | grep -E '4001|4002'   # must be empty
```

### Traefik

The Hostinger Traefik installation is **left entirely alone**. It is not
modified, not restarted, not reconfigured, and this service does not register
with it. `sol-trading-bot` runs on its own bridge network, `sol-trading-internal`,
named so its ownership is obvious in `docker network ls`.

CI fails the build if `docker-compose.yml` gains a Traefik reference.

---

## 3. Live trading safeguards

### The four interlocks

```ini
TRADING_MODE=mock
ALLOW_ORDER_TRANSMIT=false
LIVE_TRADING_ENABLED=false
KILL_SWITCH=true
```

All four must be in the trading position for any order to transmit, plus every
runtime condition in the gate. Changing one is never sufficient.

### No half-armed configurations

The application **refuses to start** if:

- `LIVE_TRADING_ENABLED=true` while `TRADING_MODE != live`, or
- `TRADING_MODE=live` while `LIVE_TRADING_ENABLED=false`.

Going live therefore always requires two coordinated, deliberate edits, and the
intermediate state is an error rather than a quiet near-miss.

### Independent account verification

Account type comes from the **broker-reported account id**, never from
configuration and never from which port was dialled:

- `DU` / `DF` / `DI` prefix → paper
- `U` + digit → live
- anything else, or a mixed managed-account list → **unknown**, which never trades

If IBKR reports an account type that does not match the configured mode, the
adapter **disconnects the session** rather than staying attached to the wrong
account. The gate would refuse every order anyway; holding the session open just
invites a later mistake.

### Port separation

`Config.ib_port` returns the paper port in paper mode and the live port in live
mode. There is no other code path that chooses a port. Configuration also
refuses `IB_PAPER_PORT == IB_LIVE_PORT`, because that ambiguity would make the
mode/account cross-check the only thing standing between us and the wrong
account.

### Zero means prohibited

Every risk limit at `0` means *not configured*, which means *trading not
authorised*. Each produces its own rejection reason. This is asserted for every
limit individually in `test_10_zero_limit_disables_rather_than_unlimits`.

### Kill switch

Two sources, OR'd together:

- `KILL_SWITCH=true` in the environment — survives restarts, the configuration
  of record.
- A durable latch in the database — lets an operator stop a *running* process
  without a redeploy.

If the durable store cannot be read, the switch reads as **engaged**. Being
unable to determine the safety state is itself an unsafe state.

The latch is **one-way**. There is no `kill-switch-off` command, no API that
clears it, and `KillSwitch` has no `disengage` method — asserted by a test.
Resuming trading is a human editing `.env` and restarting.

Engaging the kill switch stops new orders and stops strategies producing
actionable output. It does **not** liquidate positions. Cancelling working
orders and flattening a book are different decisions with different risk, and
only the first is automated.

---

## 4. Deployment security

### CI/CD cannot change trading safety

- `deploy.yml` is **`workflow_dispatch` only**. There is no `push` trigger; a
  git push cannot reach the trading server.
- It requires typing `deploy` to confirm, and targets a GitHub `production`
  environment (add a required reviewer there for four-eyes).
- `scripts/deploy.sh` **never writes `.env`** — it has no such code path, and CI
  greps it to make sure one is not added.
- `deploy.sh` runs `verify_safety.sh` **before** starting the container and
  refuses to deploy if the configuration is not the approved one.
- The workflow re-runs `verify_safety.sh` over SSH after deploying.
- If the container does not become healthy, the previous image is restored.

A deployment updates application code and the container. That is all it can do.

### SSH

- Authentication by **deploy key only**. No password is stored in GitHub.
- The host key is **pinned** via `DEPLOY_KNOWN_HOSTS`.
  `StrictHostKeyChecking=no` would make the deploy trivially interceptable.
- `IdentitiesOnly=yes`, so no other agent key is offered.
- The private key is written to a `0600` file and deleted in an `always()` step.
- Use a **restricted deployment user** with docker-group membership rather than
  root. See RUNBOOK.md.

### Container hardening

| Control | Setting |
|---|---|
| User | non-root `trader`, uid/gid 10001 |
| Root filesystem | `read_only: true` |
| Writable paths | `/app/data`, `/app/logs` (mounts), `/tmp` (16 MB tmpfs) |
| Privilege escalation | `no-new-privileges:true` |
| Capabilities | `cap_drop: ALL` |
| Published ports | none |
| Restart policy | `unless-stopped` |
| Log rotation | 10 MB × 5 files |
| Installed packages | none beyond the base image |

CI asserts the image runs as `trader` and contains no `.env`.

### Dependency surface

There are **no runtime dependencies**. `disabled` and `mock` modes run on the
Python standard library alone. The only third-party package in the project is
the optional `ibkr` extra, installed deliberately in a later phase — see
`docs/IBKR_API_NOTES.md`, which covers the supply-chain concern with that
package specifically.

---

## Server security checklist

Run `scripts/vps_audit.sh` on the VPS. It is read-only and changes nothing.

- [ ] Only intended ports are publicly listening (`ss -tlpn`)
- [ ] Ports 4001/4002 are absent or loopback-only
- [ ] `docker ps` shows no unexpected published ports
- [ ] The firewall permits SSH and Traefik's ports, nothing else the bot needs
- [ ] SSH: `PasswordAuthentication no`, `PermitRootLogin` restricted
- [ ] `.env` is `0600` and owned by the deployment user
- [ ] `.env` is not tracked by git
- [ ] Traefik is running and untouched

**Do not change the SSH port or firewall rules without confirming you retain a
working session first.** A lockout on a trading server is worse than an open
port.

---

## Incident response

**Suspected unauthorised order or unexpected position**

1. `make kill-switch-on REASON="incident <id>"` — stops new orders immediately.
2. `make open-orders` and `make positions` — see what exists.
3. `make cancel-all-orders` if working orders must go. This does **not** close
   positions; closing them is a human decision made in IBKR.
4. `make logs` and query the database by `correlation_id` for the full lifecycle.
5. Set `KILL_SWITCH=true` in `.env` so the state survives a redeploy.

**Suspected credential exposure**

1. Rotate the GitHub deploy key: remove it from the server's
   `authorized_keys`, generate a new pair, update `DEPLOY_SSH_KEY`.
2. Rotate IBKR credentials **at IBKR**. They are not stored here, so there is
   nothing in this system to rotate.
3. Review `docker compose logs` and `logs/trading.log` for what was reachable.
4. Check `git log -p` for anything that should not have been committed.

**Reporting**

This is a private repository. Report anything security-relevant directly to the
repository owner rather than opening a public issue.
