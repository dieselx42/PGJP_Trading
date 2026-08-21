#!/usr/bin/env bash
#
# Backtest the ORB baseline against both replacement candidates, in one pass.
#
# Each strategy is run twice -- once with real costs and once with costs
# zeroed -- so every row answers two questions at once: what would it have
# made, and how much of that answer is the cost model. All runs are at the
# document's 40 contracts (1,000 SOL), so the dollars compare like for like.
#
# z-hold-benchmark is the row that matters most and the one the earlier
# comparisons lacked: passive long exposure, rolled quarterly. An active
# strategy that does not beat it is destroying value relative to doing
# nothing, however good its own numbers look in isolation.
#
# Read-only, same as orb_experiments.sh: `backtest` reads `bars` and
# `contract_metadata`, writes nothing, cannot reach a broker, and cannot
# affect the running bot. Safe to run while the live process is armed.
#
# Usage:
#   scripts/strategy_compare.sh                     # all six runs
#   scripts/strategy_compare.sh --out /tmp/compare  # choose output dir
#   scripts/strategy_compare.sh -- --start 2025-08-01 --end 2026-08-01
#                                                   # extra flags -> every run
set -euo pipefail

OUT="./strategy-compare"
EXTRA=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --out) OUT="$2"; shift 2 ;;
    --) shift; EXTRA=("$@"); break ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

mkdir -p "$OUT"

COMMON=(
  --source coinbase --symbol SOL-USD --session all
  --max-order-size 40 --max-position 40 --max-orders-per-hour 20
  --max-open-orders 2 --max-daily-loss 30000 --max-notional 500000
)
ZEROCOST=(--commission 0 --slippage-ticks 0 --spread-ticks 0)

# Candidate A re-derives the ORB's numbers against the $0.373/SOL cost floor:
# only wide-range days ($3+ opening range), a $6 target (toll 6% instead of
# 25%), a breakeven lock above the floor, one entry per session.
BIG_RANGE="position_contracts=40,min_orb_range=3.00,stop_distance=2.00,target_distance=6.00,breakeven_trigger=2.00,breakeven_lock=0.50,trail_trigger=3.00,trail_width=1.50,max_trades_per_session=1"

# name|description|strategy flags
RUNS=(
  "z-hold-benchmark|BENCHMARK: passive long, rolled quarterly|--strategy sol-hold --strategy-params position_contracts=40"
  "e1-baseline|ORB, document verbatim, real costs|--strategy sol-orb --strategy-params position_contracts=40"
  "e0-orb-zerocost|ORB raw edge (costs removed)|--strategy sol-orb --strategy-params position_contracts=40 ${ZEROCOST[*]}"
  "a-big-range|Candidate A: big-range ORB, real costs|--strategy sol-orb --strategy-params $BIG_RANGE"
  "a0-big-range-zerocost|Candidate A raw edge|--strategy sol-orb --strategy-params $BIG_RANGE ${ZEROCOST[*]}"
  "b-trend|Candidate B: daily Donchian trend, real costs|--strategy sol-trend --strategy-params position_contracts=40"
  "b0-trend-zerocost|Candidate B raw edge|--strategy sol-trend --strategy-params position_contracts=40 ${ZEROCOST[*]}"
)

echo "writing to $OUT" >&2
for row in "${RUNS[@]}"; do
  IFS='|' read -r name desc flags <<<"$row"
  echo "--- $name: $desc" >&2
  # --sessions is an ORB-only diagnostic (the CLI injects it as a strategy
  # param, and sol-trend -- a 24/7 daily system -- rightly refuses it), so
  # strip it from the extras for trend runs instead of failing them.
  RUN_EXTRA=()
  skip_next=0
  for arg in ${EXTRA[@]+"${EXTRA[@]}"}; do
    if [[ $skip_next -eq 1 ]]; then skip_next=0; continue; fi
    if [[ "$flags" == *sol-trend* && "$arg" == --sessions ]]; then skip_next=1; continue; fi
    if [[ "$flags" == *sol-trend* && "$arg" == --sessions=* ]]; then continue; fi
    RUN_EXTRA+=("$arg")
  done
  # shellcheck disable=SC2086
  if docker compose exec -T sol-trading-bot python -m app.cli backtest \
       "${COMMON[@]}" $flags "${RUN_EXTRA[@]+"${RUN_EXTRA[@]}"}" \
       >"$OUT/$name.json" 2>"$OUT/$name.stderr"; then
    printf '%s\n' "$desc" >"$OUT/$name.desc"
  else
    echo "    FAILED (exit $?) -- see $OUT/$name.stderr" >&2
    # Keep going: one failed run should not cost the other five.
    rm -f "$OUT/$name.json"
  fi
done

echo >&2
python3 "$(dirname "$0")/orb_digest.py" "$OUT"
