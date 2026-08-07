#!/usr/bin/env python3
"""Docker healthcheck.

Runs *inside* the container, so it reaches the health service over loopback and
nothing has to be published. Exits 0 when the process reports healthy.

"Healthy" deliberately does not mean "trading". A kill-switched bot is doing
exactly what it was told and must not be restarted for it. See
``app/monitoring/health.py``.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

TIMEOUT_SECONDS = 5


def main() -> int:
    host = os.environ.get("HEALTH_HOST", "127.0.0.1").strip() or "127.0.0.1"
    if host == "0.0.0.0":  # noqa: S104 - probe loopback whatever we bound to
        host = "127.0.0.1"
    port = os.environ.get("HEALTH_PORT", "8787").strip() or "8787"
    url = f"http://{host}:{port}/health"

    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as response:
            if response.status != 200:
                print(f"unhealthy: HTTP {response.status}", file=sys.stderr)
                return 1
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        print(f"unhealthy: {exc}", file=sys.stderr)
        return 1

    if payload.get("status") != "healthy":
        print(f"unhealthy: {payload}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
