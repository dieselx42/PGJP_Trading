"""Identifier generation.

Two distinct kinds of identifier are used and they must not be confused:

* Random identifiers (``run_id``, ``correlation_id``, ``internal_order_id``)
  make a single lifecycle traceable through logs and the database.

* The *idempotency key* is DETERMINISTIC. It is derived from the economic
  content of a trade intent, so that replaying the same intent after a process
  restart produces the same key and collides with the already-persisted order
  instead of creating a duplicate.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping


def new_run_id() -> str:
    """Unique id for one application start. Included on every log record."""
    return f"run_{uuid.uuid4().hex[:16]}"


def new_correlation_id() -> str:
    """Ties signal -> risk decision -> order -> broker response -> fill together."""
    return f"cor_{uuid.uuid4().hex[:16]}"


def new_internal_order_id() -> str:
    return f"ord_{uuid.uuid4().hex[:16]}"


def new_intent_id() -> str:
    return f"int_{uuid.uuid4().hex[:16]}"


def idempotency_key(parts: Mapping[str, object]) -> str:
    """Deterministic key derived from the economic content of an order.

    ``parts`` must contain everything that makes two orders economically
    distinct, and nothing that varies across a restart (no wall-clock ``now``,
    no random id). Keys are sorted so dictionary ordering cannot change the
    result.
    """
    if not parts:
        raise ValueError("idempotency key requires at least one component")
    canonical = "|".join(f"{key}={parts[key]!s}" for key in sorted(parts))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"idem_{digest[:40]}"


__all__ = [
    "idempotency_key",
    "new_correlation_id",
    "new_intent_id",
    "new_internal_order_id",
    "new_run_id",
]
