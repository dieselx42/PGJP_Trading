"""Broker abstraction.

Nothing outside this package knows that Interactive Brokers exists. Strategies,
risk, and execution deal only with :class:`~app.broker.base.Broker` and the
models in :mod:`app.broker.models`.

That isolation is what makes the rest of the system testable without IBKR, and
what will let historical data be fed through the same strategy code later.
"""

from __future__ import annotations
