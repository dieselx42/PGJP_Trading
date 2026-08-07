"""Local-only health and status reporting.

Bound to loopback. Never published, never routed through Traefik, never
reachable from the internet. Reach it with ``make status`` or
``docker compose exec``.
"""

from __future__ import annotations
