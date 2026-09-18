#!/usr/bin/env bash
#
# Does ANY target size clear its own break-even line?
#
# Win rate on its own is not evidence. A $2 stop against a $1 target wins more
# than half its trades and loses money; a $1 stop against a $6 target wins
# rarely and can still make money. The only question that matters is whether
# the hit rate at a given target beats the hit rate that target REQUIRES:
#
#     required = (stop + cost) / (target + stop)          [per SOL]
#
# Widening the target lowers the requirement -- and also lowers the hit rate,
# because a further target is reached less often. Which falls faster is an
# empirical question about this instrument, and this script answers it in one
# pass by measuring the hit rate at each target with costs switched OFF (so it
# is the entry's own accuracy, uncontaminated by the fill model) and comparing
# it against the line.
#
# THIS IS A DIAGNOSTIC, NOT A TUNER. Read it as pass/fail on the whole family:
#   * no target clears its line -> the entry has no edge at any geometry, and
#     the strategy is finished. That is a real, useful answer.
#   * some target clears -> that is a HYPOTHESIS, not a setting. Picking the
#     best row and trading it is fitting to one year of noise; it has to be
#     confirmed on data that was not examined. See STRATEGY_ANALYSIS.md §7.3.
#
# Read-only: `backtest` reads bars and contract metadata, writes nothing,
# cannot reach a broker. Safe while the bot is armed.
#
# Usage:
#   scripts/target_sweep.sh --fresh              # build + throwaway container
#   scripts/target_sweep.sh --out /tmp/sweep
set -euo pipefail

OUT="./target-sweep"
FRESH=0
EXTRA=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --out) OUT="$2"; shift 2 ;;
    --fresh) FRESH=1; shift ;;
    --) shift; EXTRA=("$@"); break ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

mkdir -p "$OUT"

# --entrypoint python is load-bearing; see strategy_compare.sh for why.
if [[ $FRESH -eq 1 ]]; then
  echo "building sol-trading-bot from the working tree..." >&2
  docker compose build sol-trading-bot >&2
  RUNNER=(docker compose run --rm --no-deps -T --entrypoint python sol-trading-bot)
else
  RUNNER=(docker compose exec -T sol-trading-bot python)
fi

COMMON=(
  --source coinbase --symbol SOL-USD --session all
  --max-order-size 40 --max-position 40 --max-orders-per-hour 20
  --max-open-orders 2 --max-daily-loss 30000 --max-notional 500000
  --strategy sol-orb --sessions 09:30@America/New_York
)
STOP="1.00"
TARGETS=(1.50 2.00 2.50 3.00 4.00 5.00 6.00)

# 3 ticks/side is the pessimistic bracket on the measured 5-tick spread
# (crossing costs half the spread each way = 2.5 ticks, which the integer flag
# cannot express). The optimistic bracket is 2. Both are reported.
run() { # name, extra flags...
  local name="$1"; shift
  if "${RUNNER[@]}" -m app.cli backtest "${COMMON[@]}" "$@" \
       ${EXTRA[@]+"${EXTRA[@]}"} >"$OUT/$name.json" 2>"$OUT/$name.stderr"; then
    return 0
  fi
  echo "    FAILED -- see $OUT/$name.stderr" >&2
  rm -f "$OUT/$name.json"
}

echo "writing to $OUT" >&2
for t in "${TARGETS[@]}"; do
  params="position_contracts=40,stop_distance=$STOP,target_distance=$t"
  params="$params,max_trades_per_session=1,breakeven_enabled=false,trail_enabled=false"
  echo "--- target \$$t" >&2
  run "t${t}-hit"  --strategy-params "$params" --commission 0 --slippage-ticks 0 --spread-ticks 0
  run "t${t}-net2" --strategy-params "$params" --slippage-ticks 2
  run "t${t}-net3" --strategy-params "$params" --slippage-ticks 3
done

echo >&2
python3 "$(dirname "$0")/target_digest.py" "$OUT" --stop "$STOP"
