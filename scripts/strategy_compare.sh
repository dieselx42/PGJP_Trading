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
#   scripts/strategy_compare.sh                     # every run
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

# Candidate C is the strategy designed against the diagnosed failures: size by
# risk rather than by decree, refuse entries whose stop does not dwarf the
# toll, and skip breakouts against the long-term trend. c1 and c2 turn its two
# opinionated rules OFF one at a time -- if a rule is not earning its place,
# that pair says so, and the ablation is run every time rather than being
# something a reader has to request.
#
# Candidate A re-derives the ORB's numbers against the $0.373/SOL cost floor:
# only wide-range days ($3+ opening range), a $6 target (toll 6% instead of
# 25%), a breakeven lock above the floor, one entry per session.
BIG_RANGE="position_contracts=40,min_orb_range=3.00,stop_distance=2.00,target_distance=6.00,breakeven_trigger=2.00,breakeven_lock=0.50,trail_trigger=3.00,trail_width=1.50,max_trades_per_session=1"

# Candidate D is the operator's specification: the document's ORB trigger
# unchanged, but NY only, one entry per day, and a PLAIN bracket -- a $1 stop
# and a $2 target, neither of which ever moves. It keeps the one ORB exit rule
# that worked in attribution (the target, 7/7 wins) and removes the two that
# did not: the break-even lock, whose $0.05 sits below the cost floor and so
# lost on all 64 of its exits, and the trail, which cancels the target before
# it can be reached.
#
# The 2:1 reward-to-risk is what makes it worth measuring. At 1 contract a $2
# win is +$50 gross and a $1 loss -$25, against ~$13 of round-trip cost: it
# needs a 50.8% win rate to break even, where a costless 2:1 needs 33.3%. The
# zero-cost twin says which of those two numbers the entry is actually near --
# and the ORB trigger measured 49.8% before, so this is a genuine test of
# whether the exit geometry alone can rescue it.
#
# The session is spelled with its zone on purpose. "--sessions 13:30" is 9:30
# Eastern only during DST, so running NY alone on a UTC clock would put it an
# hour early every winter.
BRACKET="position_contracts=40,stop_distance=1.00,target_distance=2.00,max_trades_per_session=1,breakeven_enabled=false,trail_enabled=false"
NY_ONLY="--sessions 09:30@America/New_York"

# name|description|strategy flags
RUNS=(
  "z-hold-benchmark|BENCHMARK: passive long, rolled quarterly|--strategy sol-hold --strategy-params position_contracts=40"
  "e1-baseline|ORB, document verbatim, real costs|--strategy sol-orb --strategy-params position_contracts=40"
  "e0-orb-zerocost|ORB raw edge (costs removed)|--strategy sol-orb --strategy-params position_contracts=40 ${ZEROCOST[*]}"
  "a-big-range|Candidate A: big-range ORB, real costs|--strategy sol-orb --strategy-params $BIG_RANGE"
  "a0-big-range-zerocost|Candidate A raw edge|--strategy sol-orb --strategy-params $BIG_RANGE ${ZEROCOST[*]}"
  "b-trend|Candidate B: daily Donchian trend, real costs|--strategy sol-trend --strategy-params position_contracts=40"
  "b0-trend-zerocost|Candidate B raw edge|--strategy sol-trend --strategy-params position_contracts=40 ${ZEROCOST[*]}"
  "c-momentum|Candidate C: regime-filtered, cost-gated, vol-sized|--strategy sol-momentum --strategy-params position_contracts=40"
  "c0-momentum-zerocost|Candidate C raw edge|--strategy sol-momentum --strategy-params position_contracts=40 ${ZEROCOST[*]}"
  "c1-momentum-no-regime|Candidate C with the regime filter OFF|--strategy sol-momentum --strategy-params position_contracts=40,regime_days=0"
  "c2-momentum-no-costgate|Candidate C with the cost gate OFF|--strategy sol-momentum --strategy-params position_contracts=40,min_stop_cost_multiple=0.001"
  "d-bracket|Candidate D: NY only, 1/day, plain \$1/\$2 bracket|--strategy sol-orb $NY_ONLY --strategy-params $BRACKET"
  "d0-bracket-zerocost|Candidate D raw edge|--strategy sol-orb $NY_ONLY --strategy-params $BRACKET ${ZEROCOST[*]}"
  "d1-bracket-both-sessions|Candidate D with London back on|--strategy sol-orb --strategy-params $BRACKET"
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
