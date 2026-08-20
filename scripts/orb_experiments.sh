#!/usr/bin/env bash
#
# Run the ORB loss-attribution experiment matrix in one pass.
#
# Read-only: `backtest` reads `bars` and `contract_metadata`, writes nothing,
# cannot reach a broker, and cannot affect the running bot. Safe to run while
# the live process is armed.
#
# Each run's FULL json is saved under --out (nothing is lost); the digest
# printed at the end is the ~90%-smaller comparison table meant for pasting.
#
# Usage:
#   scripts/orb_experiments.sh                     # all experiments
#   scripts/orb_experiments.sh --out /tmp/orb      # choose output dir
#   scripts/orb_experiments.sh -- --start 2025-08-01 --end 2026-08-01
#                                                  # extra flags -> every run
set -euo pipefail

OUT="./orb-experiments"
EXTRA=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --out) OUT="$2"; shift 2 ;;
    --) shift; EXTRA=("$@"); break ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

mkdir -p "$OUT"

# Common to every run, so the only difference between two results is the
# experiment's own flags. --session all is the honest choice for spot bars
# (see --session help); coinbase SOL-USD is the stored proxy series.
COMMON=(
  --source coinbase --symbol SOL-USD --strategy sol-orb --session all
  --max-order-size 40 --max-position 40 --max-orders-per-hour 20
  --max-open-orders 2 --max-daily-loss 30000 --max-notional 500000
)

# name|description|experiment-specific flags
#
# E0 is a control, not a proposal: zero costs measures the RAW edge. If the
# strategy is not clearly profitable here, no parameter in E2-E6 can save it
# and the answer is "replace", not "tune".
#
# E3's 0.40 is not a guess. Cost per contract per round trip is
# 2*$3.41 commission + 2*$1.25 slippage = $9.32, which over 25 SOL/contract
# is $0.373/SOL. The document's breakeven_lock of $0.05 is BELOW that floor,
# so every breakeven exit loses by arithmetic. 0.40 is the first round number
# above it.
EXPERIMENTS=(
  "e0-zero-cost|control: raw edge with costs removed|--commission 0 --slippage-ticks 0 --spread-ticks 0"
  "e1-baseline|the document verbatim, with loss attribution|"
  "e2-breakeven-off|breakeven rule never fires (isolates its bleed)|--strategy-params breakeven_trigger=20"
  "e3-breakeven-above-cost|breakeven locks \$0.40 > the \$0.373 cost floor|--strategy-params breakeven_lock=0.40"
  "e4-wider-stop|stop \$1.30 vs \$0.65: is the tight stop churning?|--strategy-params stop_distance=1.30"
  "e5-single-entry|one entry per session: is re-entry throwing good after bad?|--strategy-params max_trades_per_session=1"
  "e6-ny-0930-et|NY open at 13:30 UTC = 9:30 ET during DST|--sessions 08:00,13:30"
)

echo "writing to $OUT" >&2
for row in "${EXPERIMENTS[@]}"; do
  IFS='|' read -r name desc flags <<<"$row"
  echo "--- $name: $desc" >&2
  # shellcheck disable=SC2086
  if docker compose exec -T sol-trading-bot python -m app.cli backtest \
       "${COMMON[@]}" $flags "${EXTRA[@]+"${EXTRA[@]}"}" \
       >"$OUT/$name.json" 2>"$OUT/$name.stderr"; then
    printf '%s\n' "$desc" >"$OUT/$name.desc"
  else
    echo "    FAILED (exit $?) -- see $OUT/$name.stderr" >&2
    # Keep going: one failed experiment should not cost the other six.
    rm -f "$OUT/$name.json"
  fi
done

echo >&2
python3 "$(dirname "$0")/orb_digest.py" "$OUT"
