# Operations

## The failure this system has actually had, twice

The IB Gateway stops accepting API connections while its container stays up.
The bot reconnects on a loop forever, `docker compose ps` reports both
containers fine, and nothing trades. It has happened twice:

- **August 2026** — 26 days, discovered only by asking the bot directly.
  937 failed reconnects. The container reported healthy throughout.
- **2026-09-21** — the feed died at ~00:00 UTC, which is exactly when
  `sol-sma` makes its daily decision. The paper trial's first trade was
  missed and filled 14 hours late after a manual restart.

**The cause is still unknown.** The August incident showed
`autorestart file not found: full authentication will be required`, which
points at the gateway's nightly restart and re-login. The September one left
no such line in twelve hours of gateway logs. Do not assume; the watchdog
below captures evidence before it restarts anything, precisely so the third
occurrence answers the question.

## The watchdog

`scripts/broker_watchdog.sh` asks the bot what it thinks of the broker and,
after enough consecutive failures, restarts the gateway and then the bot.

Install it:

```
crontab -e
# every 5 minutes; 3 failures (~15 min) before it acts
*/5 * * * * /opt/sol-futures-trading-bot/scripts/broker_watchdog.sh >/dev/null 2>&1
```

Check on it:

```
scripts/broker_watchdog.sh --status     # counters, last restart, recent log
scripts/broker_watchdog.sh --dry-run    # check and log, never restart
```

It writes to `logs/watchdog/watchdog.log`, and before every restart it dumps
`compose ps`, three hours of both containers' logs and the bot's status into
`logs/watchdog/incident-<timestamp>/`. **Read that directory after any
restart** — it is the only record of why the gateway was stuck.

Deliberate limits, so it cannot make things worse:

- Three consecutive failures before acting. IBKR's daily maintenance window
  produces brief disconnects the bot recovers from on its own.
- One restart per 30 minutes. A genuine IBKR-side outage produces a single
  attempt and a log line, not a restart loop that also destroys the evidence.
- A status call that cannot be parsed counts as a failure. A bot that cannot
  answer is itself a symptom.

## Why there is no healthcheck-based fix

The gateway now has a healthcheck (a TCP probe of the API port), and it is
worth having because it makes the broken state visible in
`docker compose ps`. It is **not** the fix:

- Docker Compose marks an unhealthy container unhealthy and leaves it
  running. Restarting on health is Swarm and Kubernetes behaviour.
- The bot's own healthcheck deliberately ignores the broker, and should keep
  doing so: a kill-switched bot is healthy, and tying health to the broker
  would restart-loop a system whose safe state is "not trading".
- Restarting the *bot* would not help anyway. It shares the gateway's network
  namespace, so its socket stays dead until the *gateway* comes back.

## Manual recovery

```
cd /opt/sol-futures-trading-bot
docker compose restart ib-gateway && sleep 90
docker compose restart sol-trading-bot && sleep 30
docker compose exec -T sol-trading-bot python -m app.cli status | grep -E '"state"|"connection_state"'
```

Expect `READY` and `connected`. `sol-sma` re-seeds its 50-day window and
re-adopts the broker position on start, so a restart costs the day in
progress, not the warm-up.

## Contract roll — manual, no auto-rollover

`MSLV6` expires **2026-10-30**. Before then:

```
docker compose exec -T sol-trading-bot python -m app.cli ibkr-checkout --contract-month 202612
# then set DEFAULT_CONTRACT_MONTH=202612 in .env and:
docker compose up -d --force-recreate sol-trading-bot
```

An open position must be flattened and re-entered by hand; the strategy knows
nothing about contracts and will not roll itself.
