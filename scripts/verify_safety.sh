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

# -----------------------------------------------------------------------------
# POSTURE -- which configuration this script should consider correct.
#
#   halted       (default) Every interlock engaged, every limit 0. The shipped
#                state, and the one CI and an unattended deploy expect.
#
#   paper-armed  Deliberately armed for PAPER trading. Chosen explicitly by an
#                operator at the shell:
#
#                    DEPLOY_POSTURE=paper-armed bash scripts/deploy.sh main
#
# This is not a bypass, and `paper-armed` is not "skip the checks". It is a
# DIFFERENT set of checks for a different intended state, and several of them
# are stricter than the halted ones: limits that are zero mean NOT CONFIGURED,
# which is a refusal, so an armed system with a zero limit is misconfigured
# rather than safe. Sanity ceilings catch the fat-fingered extra zero.
#
# There is deliberately NO `live-armed` posture. LIVE_TRADING_ENABLED=false and
# TRADING_MODE != live are asserted under BOTH postures, so no value of this
# variable can approve a live configuration.
#
# Defaulting to `halted` matters: a deploy that forgets to say what it wants
# gets the refusing answer, and `.github/workflows/deploy.yml` never sets it.
# -----------------------------------------------------------------------------
POSTURE="${2:-${DEPLOY_POSTURE:-halted}}"
case "$POSTURE" in
    halted|paper-armed) ;;
    *)
        echo "  [FAIL] unknown posture '$POSTURE' (expected 'halted' or 'paper-armed')" >&2
        exit 2
        ;;
esac

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

# A limit that is set, positive, and not absurd. Zero is NOT acceptable in an
# armed posture: zero means NOT CONFIGURED, which the risk manager treats as
# "trading not authorised", so an armed system carrying one would refuse every
# order while looking configured. The ceiling exists to catch an extra zero,
# not to express a view on position sizing -- raise it here, deliberately, when
# the phase moves on.
expect_within() {
    local name="$1" ceiling="$2" got
    got="$(value_of "$name")"
    if [ -z "$got" ]; then
        fail "$name is not set (armed posture requires 1..$ceiling)"
    elif ! printf '%s' "$got" | grep -qE '^[0-9]+$'; then
        fail "$name='$got' is not a whole number"
    elif [ "$got" -eq 0 ]; then
        fail "$name=0 means NOT CONFIGURED, which refuses every order; an armed system needs a real limit"
    elif [ "$got" -gt "$ceiling" ]; then
        fail "$name='$got' exceeds the $ceiling ceiling for a paper-armed posture -- an extra digit?"
    else
        pass "$name=$got (within $ceiling)"
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
echo "Posture: $POSTURE"
echo
echo "Interlocks:"

# Asserted under EVERY posture. Nothing this script can be asked for approves a
# live configuration.
expect LIVE_TRADING_ENABLED false
if [ "$(value_of TRADING_MODE)" = "live" ]; then
    fail "TRADING_MODE=live is never approved by this script, under any posture"
fi

if [ "$POSTURE" = "halted" ]; then
    expect TRADING_MODE                 mock
    expect ALLOW_ORDER_TRANSMIT         false
    expect KILL_SWITCH                  true
    expect SOL_FUTURES_PERMISSION_READY false

    echo
    echo "Risk limits (0 = not configured = trading not authorised):"
    for var in MAX_POSITION_CONTRACTS MAX_ORDER_SIZE MAX_DAILY_LOSS_USD \
               MAX_ORDERS_PER_HOUR MAX_OPEN_ORDERS MAX_NOTIONAL_EXPOSURE_USD; do
        expect "$var" 0
    done
else
    expect TRADING_MODE                 paper
    expect ALLOW_ORDER_TRANSMIT         true
    expect SOL_FUTURES_PERMISSION_READY true

    # The kill switch may legitimately be either way here. Engaged means armed
    # but halted, which is a real and useful state -- and the database latch can
    # engage it independently of this file, so demanding a value would report a
    # failure for a system behaving correctly.
    ks="$(value_of KILL_SWITCH)"
    case "$ks" in
        false) pass "KILL_SWITCH=false (armed)" ;;
        true)  warn "KILL_SWITCH=true: armed but halted, so no order will be sent" ;;
        *)     fail "KILL_SWITCH='$ks' (expected 'true' or 'false')" ;;
    esac

    echo
    echo "Risk limits (must be configured, and sane, in an armed posture):"
    expect_within MAX_ORDER_SIZE              5
    expect_within MAX_POSITION_CONTRACTS      5
    expect_within MAX_OPEN_ORDERS             5
    expect_within MAX_ORDERS_PER_HOUR        20
    expect_within MAX_DAILY_LOSS_USD       5000
    expect_within MAX_NOTIONAL_EXPOSURE_USD 100000

    # Freshness is an interlock, not a limit: zero means the gate refuses every
    # order for MARKET_DATA_MAX_AGE_NOT_CONFIGURED. An armed system with it
    # unset is armed and inert.
    age="$(value_of MARKET_DATA_MAX_AGE_SECONDS)"
    if [ -z "$age" ] || [ "$age" = "0" ]; then
        fail "MARKET_DATA_MAX_AGE_SECONDS is 0 or unset; the gate refuses every order without it"
    else
        pass "MARKET_DATA_MAX_AGE_SECONDS=$age"
    fi

    contract_month="$(value_of DEFAULT_CONTRACT_MONTH)"
    if [ -z "$contract_month" ]; then
        fail "DEFAULT_CONTRACT_MONTH is not set; an expiration is never chosen implicitly"
    else
        pass "DEFAULT_CONTRACT_MONTH=$contract_month"
        # An armed system whose contract lapses is a silent halt: both approvers
        # start refusing every order, and the only sign is a rejection reason
        # nobody is reading. Warned here because a deploy is where somebody is
        # actually looking. Rolling stays a deliberate decision -- this only
        # ever tells you; it never edits .env.
        if printf '%s' "$contract_month" | grep -qE '^[0-9]{8}$'; then
            today_epoch=$(date -u +%s)
            expiry_epoch=$(date -u -d "$contract_month" +%s 2>/dev/null \
                || date -u -j -f '%Y%m%d' "$contract_month" +%s 2>/dev/null || echo "")
            if [ -n "$expiry_epoch" ]; then
                days_left=$(( (expiry_epoch - today_epoch) / 86400 ))
                if [ "$days_left" -lt 0 ]; then
                    fail "contract $contract_month EXPIRED $(( -days_left )) day(s) ago; every order will be refused. Qualify the next month, flatten any position, then update DEFAULT_CONTRACT_MONTH."
                elif [ "$days_left" -le 14 ]; then
                    warn "contract $contract_month expires in $days_left day(s); qualify the next month and update DEFAULT_CONTRACT_MONTH before then"
                else
                    pass "contract $contract_month has $days_left day(s) to expiry"
                fi
            fi
        fi
    fi
fi

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
    # Not a warning. docker-compose.yml references this file, so compose
    # refuses to start ANY service without it -- including the bot, which
    # would otherwise be running happily. Failing here stops a deployment
    # from taking the trading process down over a missing config file.
    fail "$GATEWAY_ENV does not exist; compose will refuse to start any service"
    echo
    echo "  Create it before deploying:"
    echo "    cp .env.ibgateway.example .env.ibgateway && chmod 600 .env.ibgateway"
    echo "    # then set TWS_USERID and TWS_PASSWORD (paper credentials)"
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
echo "RESULT: safe for posture '$POSTURE'. $WARNINGS warning(s)."
if [ "$POSTURE" = "halted" ]; then
    echo "No orders can be transmitted with this configuration."
else
    echo "ARMED FOR PAPER TRADING. This configuration CAN send orders to IBKR's"
    echo "paper account. Live trading remains blocked: LIVE_TRADING_ENABLED=false"
    echo "and TRADING_MODE is not live."
fi
exit 0
