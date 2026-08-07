#!/usr/bin/env bash
# =============================================================================
# Create the host directories the container bind-mounts, owned by the
# container's user.
#
# The image runs as uid/gid 10001 (`trader`). Bind mounts keep host ownership,
# so a root-owned ./data would leave the container unable to write its database.
# Fixing that here is less surprising than running the container as root.
# =============================================================================

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONTAINER_UID=10001
CONTAINER_GID=10001

for dir in data logs; do
    target="$ROOT_DIR/$dir"
    mkdir -p "$target"
    if [ "$(id -u)" -eq 0 ]; then
        chown -R "$CONTAINER_UID:$CONTAINER_GID" "$target"
        chmod 750 "$target"
        echo "  $target -> owned by $CONTAINER_UID:$CONTAINER_GID"
    else
        current="$(stat -c '%u' "$target" 2>/dev/null || echo unknown)"
        if [ "$current" != "$CONTAINER_UID" ]; then
            echo "  WARNING: $target is owned by uid $current, not $CONTAINER_UID."
            echo "           The container runs as uid $CONTAINER_UID and will not be able to write."
            echo "           Fix with: sudo chown -R $CONTAINER_UID:$CONTAINER_GID $target"
        else
            echo "  $target -> ok"
        fi
    fi
done
