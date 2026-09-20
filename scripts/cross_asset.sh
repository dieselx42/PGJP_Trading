#!/usr/bin/env bash
#
# Section 11: the IDENTICAL sol-sma rule on BTC and ETH, sign-flip only.
#
# Nothing about "close above its 50-day average" is specific to SOL. Running
# the unchanged rule on two more assets over the same window is the cheapest
# independent-ish evidence about the RULE -- with the caveat, written before
# any of it runs, that the three share the 2022 collapse and the 2024 run, so
# three p-values here are far from three independent tests.
#
# Costs are switched off and the contract is the qualified MSL metadata
# (multiplier 25, tick $0.05) whatever the symbol, because the replay never
# invents a contract. So every dollar figure for BTC and ETH is "per 25
# units" and meaningless; only the sign-flip p and the split are read. The
# sign-flip statistic is invariant to a common scale, which is why that is
# enough. Risk limits are raised so nothing is refused at BTC prices.
#
# Bars must be imported first (three chunks each, see section 11).
#
# Usage:
#   scripts/cross_asset.sh --fresh --out /tmp/xa
set -euo pipefail

OUT="./cross-asset"
FRESH=0
START="2021-07-01"
END="2026-09-20"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --out) OUT="$2"; shift 2 ;;
    --fresh) FRESH=1; shift ;;
    --start) START="$2"; shift 2 ;;
    --end) END="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
mkdir -p "$OUT"

# --entrypoint python is load-bearing; see strategy_compare.sh.
if [[ $FRESH -eq 1 ]]; then
  docker compose build sol-trading-bot >&2
  RUNNER=(docker compose run --rm --no-deps -T --entrypoint python sol-trading-bot)
else
  RUNNER=(docker compose exec -T sol-trading-bot python)
fi

for sym in SOL-USD BTC-USD ETH-USD; do
  name=$(echo "$sym" | tr '[:upper:]-' '[:lower:]_')
  echo "--- $sym" >&2
  if "${RUNNER[@]}" -m app.cli backtest \
       --source coinbase --symbol "$sym" --session all \
       --max-order-size 2 --max-position 1 --max-orders-per-hour 20 --max-open-orders 2 \
       --max-daily-loss 1000000000 --max-notional 100000000000 \
       --strategy sol-sma --strategy-params position_contracts=1 \
       --commission 0 --slippage-ticks 0 --spread-ticks 0 \
       --start "$START" --end "$END" >"$OUT/$name.json" 2>"$OUT/$name.stderr"; then
    python3 "$(dirname "$0")/sign_flip.py" "$OUT/$name.json" --split 2024-01-01
  else
    echo "    FAILED -- see $OUT/$name.stderr (bars imported for $sym?)" >&2
  fi
  echo >&2
done
