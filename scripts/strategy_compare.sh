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
# The application code is baked into the image -- only ./data and ./logs are
# bind-mounted -- so a `git checkout` on the host does NOT change what the
# running container executes. After pulling a branch that adds or renames a
# strategy parameter, either rebuild and recreate (which restarts the live
# bot) or use --fresh, which builds the image and runs the replays in a
# throwaway container while the bot keeps trading untouched. --fresh is the
# safer default choice and the reason this flag exists.
#
# Usage:
#   scripts/strategy_compare.sh                     # every run, live container
#   scripts/strategy_compare.sh --fresh             # build + throwaway container
#   scripts/strategy_compare.sh --out /tmp/compare  # choose output dir
#   scripts/strategy_compare.sh --only g             # just the rows named g*
#   scripts/strategy_compare.sh -- --start 2025-08-01 --end 2026-08-01
#                                                   # extra flags -> every run
set -euo pipefail

OUT="./strategy-compare"
FRESH=0
ONLY=""
EXTRA=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --out) OUT="$2"; shift 2 ;;
    --fresh) FRESH=1; shift ;;
    --only) ONLY="$2"; shift 2 ;;   # run only rows whose name starts with this
    --) shift; EXTRA=("$@"); break ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

# --entrypoint python is LOAD-BEARING, not tidiness. The image's ENTRYPOINT is
# ["python", "-m", "app.main"] -- the trading application. `docker compose run
# SERVICE cmd...` appends cmd as ARGUMENTS to that entrypoint rather than
# replacing it, so without the override every "replay" launched a second
# trading process instead, with the live .env and, because the service runs
# under network_mode: "service:ib-gateway", inside the running gateway's
# network namespace. They all died on the health port (which the live bot
# already holds) a few lines BEFORE the broker is constructed, so nothing
# connected and nothing traded -- but that was the startup order saving it,
# not any property of this script. `exec` never had the problem because it
# replaces the command outright.
#
# So RUNNER ends at the interpreter and the replay is invoked as `-m app.cli
# backtest`: whatever else changes, the entrypoint cannot come back.
if [[ $FRESH -eq 1 ]]; then
  echo "building sol-trading-bot from the working tree..." >&2
  docker compose build sol-trading-bot >&2
  # --no-deps so this never starts IB Gateway, and --rm so nothing is left
  # behind. The replay needs only the SQLite database under ./data, which is
  # bind-mounted into every container from this compose file; the live bot
  # holds it in WAL mode, where a concurrent reader is safe.
  RUNNER=(docker compose run --rm --no-deps -T --entrypoint python sol-trading-bot)
else
  # The container that is currently running -- i.e. whatever image it was
  # started from, which may predate the checked-out source.
  RUNNER=(docker compose exec -T sol-trading-bot python)
fi

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

# Candidate F is the simplest thing in the table and different in kind from
# every other row: not a trade with a lifecycle but a position TARGET. Once a
# day, the completed UTC close against its 50-day simple moving average --
# above, hold long; below, hold short; never flat after warm-up; no stop, no
# target, no trail. One parameter, fixed before any replay ran (the horizon
# and the ~25 flips a year a random walk produces are the reasons, not a
# result), and a non-quant can check the position by eye on any chart.
#
# A change of side is ONE order of 2 x size (long 40 -> short 40 is an
# 80-lot), so every F row carries --max-order-size 80 AFTER the COMMON limits
# -- argparse keeps the last value, so 80 wins over COMMON's 40 without
# loosening any other row. The notional limit is checked on the projected
# position (40 x 25 x price), so COMMON's 500000 refuses only above $500/SOL;
# refusals.count == 0 is the check, and if the store's prices ever exceed
# that, add --max-notional 2000000 to the F rows.
#
# The rows are read in a fixed order written down BEFORE they ran (the
# pre-registration in app/strategy/sma.py and docs/STRATEGY_ANALYSIS.md §9):
#   f3-sma-net3     the PRIMARY cost bracket (3 ticks/side) for every net
#                   criterion;
#   f0-sma-zerocost the gross-edge row and the input to scripts/sign_flip.py;
#   f-sma           the 1-tick row, reported for continuity with the other
#                   rows only;
#   f1/f2           the halved and doubled window at zero cost, read ONLY for
#                   whether the SIGN of their gross agrees with f0's -- they
#                   may never move sma_days, and disagreement can only
#                   downgrade a success to inconclusive;
#   f4/f5           the same two windows at 3 ticks, for the one kill that
#                   asks whether the whole window family lost after costs.
# An always-in rule's last segment is open when the replay ends, so every F
# row is read on net_pnl + performance.final_unrealized, never on net alone.
SMA="position_contracts=40"
SMA_ORDER="--max-order-size 80"

# Candidate G fades the level the Turtle system buys: a close beyond the
# 20-day channel of closes that closes back inside within two days, with the
# stop at the break's extreme close and the target at the channel midpoint.
# Connors & Raschke's published numbers, on a closing basis; pre-registered
# in docs/STRATEGY_ANALYSIS.md section 12 as a COMPLEMENT to sol-sma -- it is
# read for whether it earns in the months the trend rule does not.
FADE="position_contracts=40"

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
  "f-sma|Candidate F: close vs 50-day SMA, long/short always in, 1-tick costs|--strategy sol-sma --strategy-params $SMA $SMA_ORDER"
  "f0-sma-zerocost|Candidate F raw edge (sign-flip input)|--strategy sol-sma --strategy-params $SMA $SMA_ORDER ${ZEROCOST[*]}"
  "f3-sma-net3|Candidate F at the pessimistic 3-tick bracket (PRIMARY for S1/K3)|--strategy sol-sma --strategy-params $SMA $SMA_ORDER --slippage-ticks 3"
  "f1-sma25-signcheck|Candidate F halved window, SIGN AGREEMENT ONLY, may not change sma_days|--strategy sol-sma --strategy-params $SMA,sma_days=25 $SMA_ORDER ${ZEROCOST[*]}"
  "f2-sma100-signcheck|Candidate F doubled window, SIGN AGREEMENT ONLY, may not change sma_days|--strategy sol-sma --strategy-params $SMA,sma_days=100 $SMA_ORDER ${ZEROCOST[*]}"
  "f4-sma25-net3|Candidate F halved window at 3 ticks (K3 only)|--strategy sol-sma --strategy-params $SMA,sma_days=25 $SMA_ORDER --slippage-ticks 3"
  "f5-sma100-net3|Candidate F doubled window at 3 ticks (K3 only)|--strategy sol-sma --strategy-params $SMA,sma_days=100 $SMA_ORDER --slippage-ticks 3"
  "g-fade|Candidate G: failed 20-day channel break (Turtle Soup on closes), 1-tick costs|--strategy sol-fade --strategy-params $FADE"
  "g0-fade-zerocost|Candidate G raw edge (sign-flip input)|--strategy sol-fade --strategy-params $FADE ${ZEROCOST[*]}"
  "g3-fade-net3|Candidate G at the pessimistic 3-tick bracket (PRIMARY)|--strategy sol-fade --strategy-params $FADE --slippage-ticks 3"
  "g1-fade10-signcheck|Candidate G halved channel, SIGN AGREEMENT ONLY|--strategy sol-fade --strategy-params $FADE,channel_days=10 ${ZEROCOST[*]}"
  "g2-fade40-signcheck|Candidate G doubled channel, SIGN AGREEMENT ONLY|--strategy sol-fade --strategy-params $FADE,channel_days=40 ${ZEROCOST[*]}"
)

echo "writing to $OUT" >&2
for row in "${RUNS[@]}"; do
  IFS='|' read -r name desc flags <<<"$row"
  if [[ -n "$ONLY" && "$name" != "$ONLY"* ]]; then continue; fi
  echo "--- $name: $desc" >&2
  # --sessions is an ORB-only diagnostic (the CLI injects it as a strategy
  # param, and sol-trend and sol-sma -- 24/7 daily systems -- rightly refuse
  # it), so strip it from the extras for those runs instead of failing them.
  RUN_EXTRA=()
  skip_next=0
  for arg in ${EXTRA[@]+"${EXTRA[@]}"}; do
    if [[ $skip_next -eq 1 ]]; then skip_next=0; continue; fi
    if [[ ( "$flags" == *sol-trend* || "$flags" == *sol-sma* || "$flags" == *sol-fade* ) && "$arg" == --sessions ]]; then skip_next=1; continue; fi
    if [[ ( "$flags" == *sol-trend* || "$flags" == *sol-sma* || "$flags" == *sol-fade* ) && "$arg" == --sessions=* ]]; then continue; fi
    RUN_EXTRA+=("$arg")
  done
  # shellcheck disable=SC2086
  if "${RUNNER[@]}" -m app.cli backtest \
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
