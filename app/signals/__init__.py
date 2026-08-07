"""Strategy output and its validation.

Strategies emit :class:`~app.signals.models.TradeIntent` objects. They cannot
emit broker orders, because they have no access to a broker and the intent type
carries no broker concepts.
"""

from __future__ import annotations
