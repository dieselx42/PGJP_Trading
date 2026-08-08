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

**The trading bot holds no broker credentials.** There is no IBKR username,
password, or 2FA material anywhere in this repository, in the bot's image, in the
database, or in the bot's configuration schema. The bot reaches an
already-authenticated gateway with a host, a port and a client id — none of which
are secret.

`scripts/verify_safety.sh` fails if the bot's `.env` contains `IB_USERNAME`,
`IB_PASSWORD`, `IBKR_PASSWORD`, `TWS_USERID` or `TWS_PASSWORD`, and CI greps the
whole tree for the same.

### Decision: IBC, and where the credential now lives

The original brief forbade storing IBKR credentials anywhere and forbade
installing IBC or equivalent login automation without explicit approval. That was
implemented as a host-installed IB Gateway with a manual login over an
SSH-tunnelled VNC session — no credential stored anywhere, at the cost of a human
login after every reboot and every IBKR re-authentication.

**On 2026-08-08 the operator reversed that decision** in favour of the
containerised `ghcr.io/gnzsnz/ib-gateway` image, which embeds IBC and requires
`TWS_USERID` and `TWS_PASSWORD` in its environment. Recorded here rather than
left as a contradiction between the documentation and the running system.

What that buys, and it is not nothing:

- **Unattended operation.** The gateway authenticates itself after a reboot.
- **A stronger port guarantee.** The API port is never published to the host at
  all; the bot reaches the gateway by service name on a private Docker network.
  The host-installed gateway listened on `*:4002` and depended on ufw.
- **Persistent settings** in a named volume rather than a home directory.

What it costs, stated plainly:

- The IBKR password sits in a file on the server, readable by root.
- A third-party image sits in the authentication path of a brokerage account.
- Anyone with root on the VPS can obtain the credential.

**What did not change:** the *trading bot* still holds no credential. The gateway
gets its own `.env.ibgateway`, referenced only by the gateway service; the bot's
`.env` is still checked for credential-shaped variables and still fails the
deployment if any appear. The bot has no field to receive them and no code path
that would use one.

Consequences to accept deliberately:

- Treat the VPS as holding a brokerage credential. Root on that box is now
  equivalent to the IBKR password.
- Use a **paper** credential until live trading is a separate, explicit decision.
- Rotate at IBKR if the server is ever suspected compromised — the credential is
  no longer only in your head.

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

### Branch protection on `main`

`main` requires a pull request and all four CI jobs, requires branches to be up
to date before merging, and blocks force pushes and deletion. **"Do not allow
bypassing the above settings" is enabled.**

That last setting is the one that makes the rest real. Without it, repository
administrators are exempt — and since every push to this repository is made by
an admin, the rule merely *logs* violations and permits them:

```
remote: Bypassed rule violations for refs/heads/main:
remote: - Changes must be made through a pull request.
remote: - 4 of 4 required status checks are expected.
   ea6c25f..f16898f  HEAD -> main
```

That output is from this repository, before the setting was corrected. The push
succeeded. A protection rule nobody has watched refuse something is a settings
page, not a control — the same reason `verify_safety.sh` exists rather than a
comment claiming the configuration is safe.

**Plan caveat:** on GitHub Free, branch protection is enforced on **public**
repositories only. Making this repository private on the Free plan silently
stops enforcement while continuing to display the rule. If visibility changes,
re-verify by attempting a direct push to `main` and confirming it is rejected.

### The hosting control panel edits `.env` — it does not overlay it

> **Do not save changes in Hostinger's Docker Manager "Environment" panel.**
> Close it without saving. Manage this stack only from
> `/opt/sol-futures-trading-bot` with `docker compose`.

This is the single most important operational warning in this document, because
getting it wrong destroyed the production configuration once already.

**What the panel is.** It reads `.env`, displays each variable as a row, and
**writes the rows back to `.env` when saved**. It is an editor of the single
source of truth, not a separate layer on top of it. Emptying the panel empties
the file.

**What went wrong.** The panel displayed all 38 variables correctly, plus two
invalid rows its parser had produced from comment lines (see below). Acting on
advice to "leave the panel empty", every row was deleted and saved. `.env`
became one byte. The container was recreated with no configuration at all.

**What the application did about it.** Every value fell through to its default,
and the system landed in its most restrictive state:

```
state                     SAFE
trading_mode              disabled     ← no broker object is constructed at all
kill_switch               true
allow_order_transmit      false
can_transmit_live_orders  false
all six risk limits       0
```

Total configuration loss produced maximum restriction. That is the fail-closed
design working: a system reading absent configuration as "no limits" would have
come up trading-capable with an empty file.

**How it was caught.** `python -m app.cli verify` reported
`TRADING_MODE: disabled (approved: mock)` on its first real use. The file-based
`verify_safety.sh` had not been run, because the redeploy went through
`docker compose` directly — which is why that check is now part of the
documented redeploy path rather than an optional extra.

### Defences now in place

* **`.env.example` contains no `=` inside comments.** The panel's importer
  splits every line on the first `=`, including comments. It turned
  `# ... if LIVE_TRADING_ENABLED=true while` into a variable named
  `# ... if LIVE_TRADING_ENABL…` valued `true while`, and `# =====` banner lines
  into a variable named `#`. Comments now use `-`, so the file imports cleanly.
  Saving the panel would still strip every comment from `.env`; the values
  would survive, the documentation would not.

* **`scripts/verify_safety.sh` detects an emptied or truncated file** and says
  so explicitly, naming the likely cause, rather than emitting twelve separate
  "not set" failures that obscure it.

* **`python -m app.cli verify` checks the running process, not the file.** It
  reports the configuration the container actually parsed and exits non-zero if
  it is not the approved posture. `scripts/deploy.sh` runs it after every
  deploy, and CI proves it fails closed by injecting `ALLOW_ORDER_TRANSMIT=true`
  into a live container and asserting rejection.

Run both any time the deployment is touched:

```bash
bash scripts/verify_safety.sh .env                                  # the file
docker compose exec -T sol-trading-bot python -m app.cli verify      # the process
```

### Repository visibility

**This repository is deliberately public.** That is a decision, not an
oversight, and it is coupled to the section above: on the GitHub Free plan,
branch protection is enforced on public repositories only. Making this
repository private without also upgrading would silently switch off the
protection on `main` while continuing to display the rule.

It is safe to be public today because it contains no secrets. `.env` is
gitignored, has never been committed, and CI fails the build if any environment
file, credential, or trading-enabling default appears in the tree. What is
exposed is infrastructure, and the infrastructure's safety does not depend on
anybody not reading it.

**Revisit when strategy code lands.** A trading strategy is the first thing here
that would be genuinely worth concealing, and by then the *history* matters too,
not just the tip. At that point go private **and** move to a plan or a Ruleset
that keeps protection enforced — then confirm it survived by attempting a direct
push to `main` and checking that it is rejected rather than bypassed.

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

## Verified posture: srv1792440.hstgr.cloud

Audited and hardened 2026-08-07. Ubuntu 24.04.4 LTS, 2 vCPU, 7.8 GiB RAM,
Docker 29.5.3, Compose v5.1.4.

### SSH

```
permitrootlogin              without-password   (alias of prohibit-password)
passwordauthentication       no
kbdinteractiveauthentication no
```

Root is key-only, password login is off, and the PAM keyboard-interactive path
is closed — without that last one some configurations still accept an
interactive password prompt after `PasswordAuthentication no`.

**These live in `/etc/ssh/sshd_config.d/00-hardening.conf`, not in the main
config, and that placement is the point.** Ubuntu 24.04 puts
`Include /etc/ssh/sshd_config.d/*.conf` at the *top* of `sshd_config`, and sshd
keeps the **first** value it reads for any directive. The image ships
`50-cloud-init.conf` containing `PasswordAuthentication yes`, which overrides
both `60-cloudimg-settings.conf` and the main file. Editing `sshd_config`
therefore appears to work and silently does nothing.

A file named `00-` sorts ahead of cloud-init's, so it wins — and it keeps
winning if cloud-init regenerates `50-` on a later boot. Verify after any
reboot with:

```bash
sshd -T | grep -iE '^(permitrootlogin|passwordauthentication|kbdinteractiveauthentication) '
```

Reverting is `rm /etc/ssh/sshd_config.d/00-hardening.conf && systemctl reload ssh`.

### Firewall

```
Status: active
Default: deny (incoming), allow (outgoing), deny (routed)

22/tcp    ALLOW IN    Anywhere        (+ v6)
80/tcp    ALLOW IN    Anywhere        (+ v6)
443/tcp   ALLOW IN    Anywhere        (+ v6)
```

80 and 443 are **required**: Hostinger runs Traefik in host network mode, so it
binds the host's interfaces directly and its traffic passes through the INPUT
chain that ufw controls. Removing those rules takes your sites down.

Note what ufw does *not* protect: Docker inserts its own rules ahead of ufw's,
so a container that publishes a port is reachable regardless of ufw. That is
not a gap here, because `docker-compose.yml` publishes nothing at all and CI
fails the build if that changes — but it does mean ufw is not what is keeping
the trading bot private. The absence of a port mapping is.

### Trading application

```
sol-trading-bot     published ports: none
4001 / 4002 / 8787  not listening
.env                0600, owned by soldeploy
```

### Known remaining items

- A reboot is pending from unattended-upgrades. Both Traefik and the bot use
  `restart: unless-stopped`; the bot returns in `HALTED` because `KILL_SWITCH`
  lives in `.env`. Re-verify the sshd settings afterwards.
- Two Traefik volumes exist (`traefik-letsencrypt` and
  `traefik_traefik-letsencrypt`), one likely orphaned from a compose project
  rename. Left alone deliberately — deleting the wrong one destroys the TLS
  certificates.

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
