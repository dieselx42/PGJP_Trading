"""Market data abstraction.

Strategies receive :class:`~app.market_data.models.Quote` objects and nothing
else. That is what will let historical bars be replayed through the same
strategy code during backtesting without touching broker or risk code.
"""

from __future__ import annotations
