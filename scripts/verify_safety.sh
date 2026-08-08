#!/usr/bin/env bash
# =============================================================================
# Safety verification for a server .env.
#
#     bash scripts/verify_safety.sh /opt/sol-futures-trading-bot/.env
#
# Exits non-zero if the file is configured in a way this phase does not permit.
# It is run by `make run` and by the deploy script BEFORE the container starts,
# so a bad configuration is caught before it can do anything.
#
# This script only READS the file. It never writes one, which is the whole point
# of keeping .env off CI/CD entirely.
# =============================================================================

set -uo pipefail

ENV_FILE="${1:-.env}"
FAILURES=0
WARNINGS=0

fail() { printf '  [FAIL] %s\n' "$1"; FAILURES=$((FAILURES + 1)); }
warn() { printf '  [WARN] %s\n' "$1"; WARNINGS=$((WARNINGS + 1)); }
pass() { printf '  [ ok ] %s\n' "$1"; }

# Read a variable without sourcing the file (sourcing an untrusted .env would
# execute whatever is in it).
value_of() {
    grep -E "^[[:space:]]*$1[[:space:]]*=" "$ENV_FILE" 2>/dev/null \
        | tail -1 \
        | sed -E "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//" \
        | sed -E 's/[[:space:]]*(#.*)?$//' \
        | tr -d '"'"'"
}

expect() {
    local name="$1" want="$2" got
    got="$(value_of "$name")"
    if [ -z "$got" ]; then
        fail "$name is not set (expected '$want')"
    elif [ "$got" != "$want" ]; then
        fail "$name='$got' (expected '$want')"
    else
        pass "$name=$got"
    fi
}

echo "Verifying trading safety configuration in: $ENV_FILE"
echo

if [ ! -f "$ENV_FILE" ]; then
    echo "  [FAIL] $ENV_FILE does not exist."
    echo
    echo "  Create it on the server from .env.example and chmod 600 it."
    echo "  CI/CD must never create this file."
    exit 1
fi

# An emptied or truncated .env is reported explicitly rather than as twelve
# separate "not set" failures, because the cause and the fix are different.
#
# This is not hypothetical. Hostinger's Docker Manager edits .env DIRECTLY
# rather than overlaying it: clearing its Environment panel wrote an empty file
# back to disk, the container came up with no configuration at all, and every
# value fell through to its default. The application landed in its most
# restrictive state, which is the design working -- but the file had been
# silently destroyed.
# `grep -c` prints 0 AND exits non-zero when there are no matches, so a
# `|| echo 0` fallback yields the string "0\n0". Piping through wc -l always
# gives exactly one number and exit status 0.
VARIABLE_COUNT="$(grep -E '^[[:space:]]*[A-Za-z_][A-Za-z0-9_]*[[:space:]]*=' "$ENV_FILE" 2>/dev/null | wc -l)"
MINIMUM_VARIABLES=10

if [ "$VARIABLE_COUNT" -eq 0 ]; then
    echo "  [FAIL] $ENV_FILE contains no variables at all ($(wc -c < "$ENV_FILE") bytes)."
    echo
    echo "  The file has been emptied. Every setting would fall back to its"
    echo "  default, which is safe but is NOT the deployed configuration."
    echo
    echo "  Most likely cause: a hosting control panel rewrote it. Hostinger's"
    echo "  Docker Manager edits .env directly -- do not manage this stack from"
    echo "  that UI."
    echo
    echo "  Restore with:"
    echo "    cp .env.example .env && chmod 600 .env"
    echo "    # then re-apply DEFAULT_CONTRACT_MONTH and any local settings"
    exit 1
fi

if [ "$VARIABLE_COUNT" -lt "$MINIMUM_VARIABLES" ]; then
    echo "  [FAIL] $ENV_FILE has only $VARIABLE_COUNT variable(s); expected at least $MINIMUM_VARIABLES."
    echo
    echo "  The file looks truncated. Compare it against .env.example before"
    echo "  starting anything."
    exit 1
fi

echo "  [ ok ] $ENV_FILE defines $VARIABLE_COUNT variables"
echo

# -----------------------------------------------------------------------------
echo "Interlocks:"
expect TRADING_MODE                 mock
expect ALLOW_ORDER_TRANSMIT         false
expect LIVE_TRADING_ENABLED         false
expect KILL_SWITCH                  true
expect SOL_FUTURES_PERMISSION_READY false

echo
echo "Risk limits (0 = not configured = trading not authorised):"
for var in MAX_POSITION_CONTRACTS MAX_ORDER_SIZE MAX_DAILY_LOSS_USD \
           MAX_ORDERS_PER_HOUR MAX_OPEN_ORDERS MAX_NOTIONAL_EXPOSURE_USD; do
    expect "$var" 0
done

echo
echo "File permissions:"
if [ "$(uname)" = "Darwin" ]; then
    perms=$(stat -f '%Lp' "$ENV_FILE" 2>/dev/null)
else
    perms=$(stat -c '%a' "$ENV_FILE" 2>/dev/null)
fi
if [ "$perms" = "600" ]; then
    pass "$ENV_FILE is 0600"
else
    warn "$ENV_FILE is $perms; run: chmod 600 $ENV_FILE"
fi

echo
echo "Credentials that must NOT be in the BOT's environment:"
# The gateway container needs IBKR credentials (see SECURITY.md, "Decision:
# IBC"). The bot still does not, and this check is what keeps that true: the
# bot has no field to receive them and no code path that would use one, so a
# credential here is either a mistake or a misunderstanding of the split.
if grep -qiE '^[[:space:]]*(IB_USERNAME|IB_PASSWORD|IBKR_USER|IBKR_PASSWORD|TWS_USERID|TWS_PASSWORD)[[:space:]]*=' "$ENV_FILE" 2>/dev/null; then
    fail "IBKR credentials found in $ENV_FILE -- they belong in .env.ibgateway, which only the gateway service reads"
else
    pass "no IBKR credentials in the bot's environment"
fi

echo
echo "IB Gateway credential file:"
GATEWAY_ENV="$(dirname "$ENV_FILE")/.env.ibgateway"
if [ ! -f "$GATEWAY_ENV" ]; then
    warn "$GATEWAY_ENV does not exist; the gateway service will not start"
else
    if [ "$(uname)" = "Darwin" ]; then
        gw_perms=$(stat -f '%Lp' "$GATEWAY_ENV" 2>/dev/null)
    else
        gw_perms=$(stat -c '%a' "$GATEWAY_ENV" 2>/dev/null)
    fi
    if [ "$gw_perms" = "600" ]; then
        pass "$GATEWAY_ENV is 0600"
    else
        # This file holds a brokerage password. Loose permissions are a
        # failure, not a warning -- unlike the bot's .env, which holds none.
        fail "$GATEWAY_ENV is $gw_perms and holds an IBKR password; run: chmod 600 $GATEWAY_ENV"
    fi

    if grep -qE '^[[:space:]]*TWS_PASSWORD[[:space:]]*=[[:space:]]*$' "$GATEWAY_ENV" 2>/dev/null; then
        warn "TWS_PASSWORD is empty in $GATEWAY_ENV; the gateway cannot log in"
    fi

    if git -C "$(dirname "$0")/.." ls-files --error-unmatch .env.ibgateway >/dev/null 2>&1; then
        fail ".env.ibgateway is tracked by git -- it holds an IBKR password; remove it from the index and rotate the credential at IBKR"
    else
        pass ".env.ibgateway is not tracked by git"
    fi
fi

echo
echo "Repository hygiene:"
if git -C "$(dirname "$0")/.." ls-files --error-unmatch .env >/dev/null 2>&1; then
    fail ".env is tracked by git -- remove it from the index immediately"
else
    pass ".env is not tracked by git"
fi

# -----------------------------------------------------------------------------
echo
echo "----------------------------------------------------------------"
if [ "$FAILURES" -gt 0 ]; then
    echo "RESULT: $FAILURES failure(s), $WARNINGS warning(s)."
    echo "This configuration is NOT the approved state for this phase."
    exit 1
fi
echo "RESULT: safe. $WARNINGS warning(s)."
echo "Live orders cannot be transmitted with this configuration."
exit 0
