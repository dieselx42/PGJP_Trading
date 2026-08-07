"""Durable state.

SQLite for V1, behind a thin abstraction so PostgreSQL can replace it without
touching callers. See :mod:`app.state.database` for what portability actually
required.
"""

from __future__ import annotations
