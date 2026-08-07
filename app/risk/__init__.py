"""Risk management.

The risk manager is an independent approver. It does not trust the strategy,
and the transmit gate does not trust the risk manager: both must approve, and
they check different things. Risk answers "is this trade within our limits?";
the gate answers "is this system in a state where any trade may be sent at
all?".
"""

from __future__ import annotations
