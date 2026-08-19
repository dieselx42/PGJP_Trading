"""Historical replay.

Two structural guarantees, both asserted by tests rather than described here:

**It runs the real interlocks.** The replay drives the actual `RiskManager` and
the actual `TransmitGate`, not simplified copies. A backtest that bypasses them
is a statement about a system that does not exist. The consequence is worth
stating plainly: if the gate refuses an order during a replay, the replay is
*correct* to record no trade.

**It cannot reach a broker.** Nothing in this package constructs `IBKRBroker` or
reads the server `.env`. A backtester that can touch a live broker is a trading
system with extra steps.
"""
