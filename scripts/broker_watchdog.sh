#!/usr/bin/env bash
#
# Restart the IB Gateway when the bot has been unable to reach it for long
# enough that it is not a blip.
#
# WHY THIS EXISTS, and why it is not a Docker healthcheck. Twice now the
# gateway has stopped accepting API connections while both containers looked
# fine: a 26-day silent outage in August, and 2026-09-21 when the feed died at
# ~00:00 UTC and the strategy missed its first decision. Neither was visible
# without asking the bot directly. A healthcheck would not have fixed either:
# Docker marks an unhealthy container unhealthy and leaves it running (only
# Swarm/Kubernetes restart on it), and restarting the BOT does not revive a
# stuck GATEWAY -- the bot shares the gateway's network namespace, so its
# socket is dead until the gateway itself comes back.
#
# WHAT IT DOES NOT DO. It does not restart on a brief disconnect: IBKR has a
# daily maintenance window and the bot reconnects from those by itself. Only
# FAIL_THRESHOLD consecutive failures act, and never more often than
# COOLDOWN_SECONDS, so a genuine IBKR outage produces one restart attempt and
# a log line rather than a restart loop.
#
# It also captures diagnostics BEFORE restarting, because a restart destroys
# the evidence of why the gateway was stuck -- which is the reason the cause
# is still unknown after two occurrences.
#
# Install (runs every 5 minutes; 3 failures = ~15 minutes before it acts):
#   crontab -e
#   */5 * * * * /opt/sol-futures-trading-bot/scripts/broker_watchdog.sh >/dev/null 2>&1
#
# Usage:
#   scripts/broker_watchdog.sh              # one check, act if warranted
#   scripts/broker_watchdog.sh --dry-run    # check and log, never restart
#   scripts/broker_watchdog.sh --status     # print what the watchdog knows
set -uo pipefail

PROJECT_DIR="${PROJECT_DIR:-/opt/sol-futures-trading-bot}"
STATE_DIR="${STATE_DIR:-$PROJECT_DIR/logs/watchdog}"
LOG="$STATE_DIR/watchdog.log"
FAILS_FILE="$STATE_DIR/consecutive_failures"
LAST_RESTART_FILE="$STATE_DIR/last_restart_epoch"

#: Consecutive failed checks before acting. At a 5-minute cron that is ~15
#: minutes of no broker -- longer than any reconnect the bot wins on its own.
FAIL_THRESHOLD="${FAIL_THRESHOLD:-3}"
#: Never restart more often than this, so a broker-side outage cannot become a
#: restart loop that also destroys the evidence.
COOLDOWN_SECONDS="${COOLDOWN_SECONDS:-1800}"
#: How long to give the gateway before the bot is restarted onto it.
GATEWAY_SETTLE_SECONDS="${GATEWAY_SETTLE_SECONDS:-90}"

DRY_RUN=0
case "${1:-}" in
  --dry-run) DRY_RUN=1 ;;
  --status)
    echo "state dir:          $STATE_DIR"
    echo "consecutive fails:  $(cat "$FAILS_FILE" 2>/dev/null || echo 0)"
    last=$(cat "$LAST_RESTART_FILE" 2>/dev/null || echo "")
    if [[ -n "$last" ]]; then
      echo "last restart:       $(date -u -d "@$last" '+%Y-%m-%dT%H:%M:%SZ') ($(( ($(date +%s) - last) / 60 )) min ago)"
    else
      echo "last restart:       never"
    fi
    echo "recent log:"; tail -n 15 "$LOG" 2>/dev/null || echo "  (none)"
    exit 0 ;;
  "") ;;
  *) echo "unknown argument: $1" >&2; exit 2 ;;
esac

mkdir -p "$STATE_DIR"
cd "$PROJECT_DIR" || { echo "cannot cd to $PROJECT_DIR" >&2; exit 2; }

log() { printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >>"$LOG"; }

# The bot's own view of the broker. Anything unparseable counts as a failure:
# a status call that cannot answer is itself a symptom.
state=$(docker compose exec -T sol-trading-bot python -m app.cli status 2>/dev/null \
        | python3 -c 'import json,sys
try:
    d = json.load(sys.stdin)
    print(d["broker"]["connection_state"])
except Exception:
    print("unknown")' 2>/dev/null || echo "unknown")

if [[ "$state" == "connected" ]]; then
  prev=$(cat "$FAILS_FILE" 2>/dev/null || echo 0)
  if [[ "$prev" -gt 0 ]]; then
    log "RECOVERED broker=connected after $prev failed check(s)"
  fi
  echo 0 >"$FAILS_FILE"
  exit 0
fi

fails=$(( $(cat "$FAILS_FILE" 2>/dev/null || echo 0) + 1 ))
echo "$fails" >"$FAILS_FILE"
log "broker=$state consecutive_failures=$fails/$FAIL_THRESHOLD"

if [[ "$fails" -lt "$FAIL_THRESHOLD" ]]; then
  exit 0
fi

now=$(date +%s)
last=$(cat "$LAST_RESTART_FILE" 2>/dev/null || echo 0)
if [[ $(( now - last )) -lt "$COOLDOWN_SECONDS" ]]; then
  log "threshold reached but within cooldown ($(( (COOLDOWN_SECONDS - (now - last)) / 60 )) min left); not restarting"
  exit 0
fi

# Evidence first. A restart is what has destroyed it twice.
snap="$STATE_DIR/incident-$(date -u '+%Y%m%dT%H%M%SZ')"
mkdir -p "$snap"
docker compose ps >"$snap/compose-ps.txt" 2>&1
docker compose logs --since 3h ib-gateway >"$snap/ib-gateway.log" 2>&1
docker compose logs --since 3h sol-trading-bot >"$snap/sol-trading-bot.log" 2>&1
docker compose exec -T sol-trading-bot python -m app.cli status >"$snap/status.json" 2>&1
log "captured diagnostics to $snap"

if [[ "$DRY_RUN" -eq 1 ]]; then
  log "DRY RUN: would restart ib-gateway then sol-trading-bot"
  exit 0
fi

log "RESTARTING ib-gateway (broker=$state for $fails checks)"
docker compose restart ib-gateway >>"$LOG" 2>&1
sleep "$GATEWAY_SETTLE_SECONDS"
log "RESTARTING sol-trading-bot"
docker compose restart sol-trading-bot >>"$LOG" 2>&1
echo "$now" >"$LAST_RESTART_FILE"
echo 0 >"$FAILS_FILE"

sleep 30
after=$(docker compose exec -T sol-trading-bot python -m app.cli status 2>/dev/null \
        | python3 -c 'import json,sys
try:
    print(json.load(sys.stdin)["broker"]["connection_state"])
except Exception:
    print("unknown")' 2>/dev/null || echo "unknown")
log "after restart broker=$after"
