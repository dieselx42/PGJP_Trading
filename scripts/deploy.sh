#!/usr/bin/env bash
# =============================================================================
# Server-side deploy.
#
# Runs ON the VPS, invoked either by hand or over SSH by GitHub Actions.
#
#     bash scripts/deploy.sh [git-ref]
#
# WHAT THIS SCRIPT WILL NOT DO
# ----------------------------
#   * It never creates, writes, or modifies .env. Production configuration
#     lives only on the server and is edited only by a human. A deploy that
#     could rewrite .env could enable live trading from a git push.
#   * It never touches Traefik, other containers, other networks, or other
#     volumes.
#   * It refuses to start the application if the safety verification fails.
#
# It updates application code and the container. That is all.
# =============================================================================

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/sol-futures-trading-bot}"
SERVICE="sol-trading-bot"
GIT_REF="${1:-main}"

log()  { printf '\n[deploy] %s\n' "$1"; }
fail() { printf '\n[deploy] ERROR: %s\n' "$1" >&2; exit 1; }

cd "$APP_DIR" 2>/dev/null || fail "$APP_DIR does not exist. See RUNBOOK.md for first-time setup."

# -----------------------------------------------------------------------------
log "Pre-flight"
[ -f .env ] || fail ".env is missing. Create it from .env.example and chmod 600 it. This script will not create it."
command -v docker >/dev/null 2>&1 || fail "docker is not installed"
docker compose version >/dev/null 2>&1 || fail "the docker compose v2 plugin is not installed"

# Snapshot the current image so a failed deploy can be rolled back.
PREVIOUS_IMAGE="$(docker compose images -q "$SERVICE" 2>/dev/null || true)"
log "Current image: ${PREVIOUS_IMAGE:-none}"

# -----------------------------------------------------------------------------
log "Fetching $GIT_REF"
git fetch --prune origin
git checkout "$GIT_REF"
git reset --hard "origin/$GIT_REF"
GIT_COMMIT="$(git rev-parse --short HEAD)"
BUILD_TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
export GIT_COMMIT BUILD_TIMESTAMP
log "Deploying commit $GIT_COMMIT"

# Confirm .env survived the checkout untouched. It is gitignored, so it should
# have; checking costs nothing and the failure mode is severe.
[ -f .env ] || fail ".env disappeared during checkout. Restore it before continuing."

# -----------------------------------------------------------------------------
log "Verifying trading safety configuration"
bash scripts/verify_safety.sh .env || fail "safety verification failed; refusing to deploy"

# -----------------------------------------------------------------------------
log "Preparing data and log directories"
bash scripts/bootstrap_dirs.sh

# -----------------------------------------------------------------------------
log "Building image"
docker compose build --pull

log "Restarting service"
docker compose up -d --remove-orphans

# -----------------------------------------------------------------------------
log "Waiting for the container to report healthy"
DEADLINE=$((SECONDS + 120))
STATUS="unknown"
while [ "$SECONDS" -lt "$DEADLINE" ]; do
    STATUS="$(docker inspect --format '{{.State.Health.Status}}' "$SERVICE" 2>/dev/null || echo "missing")"
    case "$STATUS" in
        healthy) break ;;
        unhealthy) break ;;
    esac
    sleep 3
done

if [ "$STATUS" != "healthy" ]; then
    log "Container did not become healthy (status: $STATUS). Recent logs:"
    docker compose logs --tail=60 "$SERVICE" || true
    if [ -n "$PREVIOUS_IMAGE" ]; then
        log "Rolling back to $PREVIOUS_IMAGE"
        IMAGE_TAG="$PREVIOUS_IMAGE" docker compose up -d || true
    fi
    fail "deploy failed health check"
fi

# -----------------------------------------------------------------------------
log "Post-deploy verification"
docker compose exec -T "$SERVICE" python -m app.cli kill-switch-status

log "Deployed $GIT_COMMIT successfully. Trading remains disabled by configuration."
docker compose ps
