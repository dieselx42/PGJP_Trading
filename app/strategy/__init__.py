"""Strategy layer.

Strategies see standardised :class:`~app.market_data.models.Quote` objects and
return :class:`~app.signals.models.TradeIntent` objects. They have no broker
handle, no order types, and no knowledge of IBKR.

That boundary is what makes backtesting possible later: replaying historical
quotes through the same ``on_quote`` method needs no change to strategy code.
"""

from __future__ import annotations
