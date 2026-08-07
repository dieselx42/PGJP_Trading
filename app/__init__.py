"""sol-futures-trading-bot.

Automated CME Solana futures trading system.

Safety posture: this package FAILS CLOSED. Absent configuration, unparseable
configuration, missing broker state, or an unverified account identity all
result in orders being REJECTED, never transmitted.
"""

from __future__ import annotations

# Kept in sync with `project.version` in pyproject.toml.
# tests/unit/test_version.py asserts the two match.
__version__ = "0.1.0"

__all__ = ["__version__"]
