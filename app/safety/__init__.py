"""Trading safety interlocks.

This package contains the single choke point through which every order must
pass immediately before transmission. It is deliberately small, pure, and
dependency-free so that it can be read end-to-end and fully unit tested.

Deviation from the suggested layout: the specification placed interlocks under
``app/risk``. They live here instead because they are categorically different
from risk limits. Risk limits are *tunable numbers*; interlocks are *invariants*
that no configuration may relax. Keeping them apart makes it obvious which file
must never be softened.
"""

from __future__ import annotations
