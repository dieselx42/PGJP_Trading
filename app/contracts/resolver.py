"""Contract resolution and qualification.

The resolver turns "MSL" plus an explicitly chosen expiration into a
broker-confirmed :class:`~app.contracts.models.QualifiedContract`, and persists
the metadata so an operator can see exactly which instrument the process is
pointed at.

Three rules are enforced here and nowhere else is allowed to bypass them:

* An expiration must be chosen explicitly. There is no front-month heuristic
  and no automatic rollover in this phase.
* A continuous future is never qualified for trading.
* An ambiguous lookup is an error. If IBKR returns more than one match we stop;
  we do not pick one.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.broker.base import Broker
from app.broker.models import BrokerContractError, BrokerError, BrokerPermissionError
from app.contracts.models import ContractError, ContractSpec, QualifiedContract
from app.contracts.solana import get_product
from app.logging_config import get_logger

_LOG = get_logger("contracts.resolver")


class ContractResolutionError(Exception):
    """The requested contract could not be resolved to a tradeable instrument."""


class ContractResolver:
    """Resolves and qualifies futures contracts through a broker."""

    def __init__(self, broker: Broker) -> None:
        self._broker = broker
        self._cache: dict[str, QualifiedContract] = {}

    @property
    def cached(self) -> dict[str, QualifiedContract]:
        return dict(self._cache)

    def build_spec(self, symbol: str, contract_month: str | None) -> ContractSpec:
        """Build a lookup request, refusing anything that could not be traded."""
        try:
            product = get_product(symbol)
        except KeyError as exc:
            raise ContractResolutionError(str(exc)) from exc
        if not contract_month:
            raise ContractResolutionError(
                f"{symbol}: DEFAULT_CONTRACT_MONTH is not set. An explicit expiration is "
                "required; this system never selects a front month implicitly and does not "
                "roll contracts automatically."
            )
        spec = product.spec(contract_month)
        spec.require_orderable()
        return spec

    async def resolve(self, symbol: str, contract_month: str | None) -> QualifiedContract:
        """Qualify a contract, caching the result for the process lifetime."""
        spec = self.build_spec(symbol, contract_month)
        cache_key = spec.key()
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            contract = await self._broker.qualify_contract(spec)
        except BrokerPermissionError as exc:
            # Permission failures are configuration problems, not transient
            # ones. Surface them clearly and do not retry.
            _LOG.error(
                "contract qualification refused for permission reasons",
                extra={
                    "event": "contract.permission_denied",
                    "symbol": symbol,
                    "contract_month": contract_month,
                    "error": str(exc),
                    "retryable": False,
                },
            )
            raise ContractResolutionError(
                f"{symbol} {contract_month}: broker refused for permission reasons - {exc}"
            ) from exc
        except (BrokerContractError, ContractError) as exc:
            _LOG.error(
                "contract qualification failed",
                extra={
                    "event": "contract.qualification_failed",
                    "symbol": symbol,
                    "contract_month": contract_month,
                    "error": str(exc),
                },
            )
            raise ContractResolutionError(f"{symbol} {contract_month}: {exc}") from exc
        except BrokerError as exc:
            raise ContractResolutionError(
                f"{symbol} {contract_month}: broker error during qualification - {exc}"
            ) from exc

        self._validate_qualified(contract, spec)
        self._cache[cache_key] = contract
        self._cache[contract.key()] = contract

        _LOG.info(
            "contract qualified",
            extra={"event": "contract.qualified", **contract.describe()},
        )
        return contract

    async def list_candidates(
        self, symbol: str, contract_month: str | None = None
    ) -> Sequence[QualifiedContract]:
        """Discover the contracts a broker knows about for a symbol.

        Used by the ``contract-info`` operator command to choose an expiration
        deliberately. Never used to pick one automatically.
        """
        try:
            product = get_product(symbol)
        except KeyError as exc:
            raise ContractResolutionError(str(exc)) from exc
        spec = product.spec(contract_month)
        try:
            return await self._broker.get_contract_details(spec)
        except BrokerError as exc:
            raise ContractResolutionError(f"{symbol}: {exc}") from exc

    @staticmethod
    def _validate_qualified(contract: QualifiedContract, spec: ContractSpec) -> None:
        """Post-conditions the broker's answer must satisfy before we trade it."""
        if contract.symbol.upper() != spec.symbol.upper():
            raise ContractResolutionError(
                f"broker returned symbol {contract.symbol!r} for a {spec.symbol!r} request"
            )
        if contract.is_continuous:
            raise ContractResolutionError("broker returned a continuous future; refusing")
        if contract.con_id <= 0:
            raise ContractResolutionError("broker returned a contract without a conId")
        requested = spec.last_trade_date_or_contract_month or ""
        if requested and not contract.expiration.startswith(requested[:6]):
            raise ContractResolutionError(
                f"broker returned expiration {contract.expiration!r} for a "
                f"{requested!r} request; refusing a contract we did not ask for"
            )


__all__ = ["ContractResolutionError", "ContractResolver"]
