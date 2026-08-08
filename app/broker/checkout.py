"""Read-only checkout of a live IB Gateway session.

``app/broker/ibkr_broker.py`` is the largest unverified surface in this project:
every unit test around it drives fakes, because there has never been a gateway
to point it at. Its socket connect, its handshake ordering, its account-type
detection, its error classification and its contract qualification have all been
reasoned about and none of them have been *observed*.

This module observes them. It connects once on the admin client id, runs a fixed
sequence of read-only probes, and reports PASS / FAIL / SKIP for each with the
evidence it saw. A wrong answer becomes visible instead of being something you
have to notice.

**It cannot place an order.** That is enforced three ways, deliberately
overlapping:

1. The broker is typed as :class:`ReadOnlyBroker`, a Protocol with no write
   methods at all, so ``mypy --strict`` rejects ``broker.place_order(...)`` here
   as an attribute error rather than a policy violation.
2. :func:`preconditions` refuses to connect unless the process-wide interlocks
   are in their refusing position, so the checkout cannot even run in a
   configuration that could transmit.
3. ``GATE_REFUSES_WHEN_EVERYTHING_ELSE_IS_GREEN`` re-evaluates the transmit gate
   against a deliberately optimistic session and requires it to still refuse.

The third is the one that matters most. The first two say the checkout is safe;
the third says the *system* is, at the moment a real broker session exists,
which is the first moment that claim has ever been testable.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from app.broker.models import (
    AccountSummary,
    BrokerError,
    BrokerFill,
    BrokerOrderSnapshot,
    BrokerPosition,
    ConnectionInfo,
    MarketDataTick,
)
from app.config import Config
from app.contracts.models import ContractError, ContractSpec, QualifiedContract
from app.enums import AccountType, ApplicationState, ConnectionState, SecurityType, TradingMode
from app.safety.gate import GateContext, TransmitGate

__all__ = [
    "CheckoutReport",
    "Probe",
    "ProbeStatus",
    "ReadOnlyBroker",
    "preconditions",
    "run_checkout",
]


class ProbeStatus(StrEnum):
    PASS = "PASS"  # noqa: S105 -- a probe outcome, not a credential
    FAIL = "FAIL"
    SKIP = "SKIP"


@dataclass(frozen=True, slots=True)
class Probe:
    """One observation, with whatever the broker actually returned."""

    name: str
    status: ProbeStatus
    detail: str
    evidence: dict[str, object] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status is ProbeStatus.PASS

    def describe(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": self.status.value,
            "detail": self.detail,
            "evidence": self.evidence,
        }


@dataclass(frozen=True, slots=True)
class CheckoutReport:
    probes: tuple[Probe, ...]

    @property
    def failed(self) -> tuple[Probe, ...]:
        return tuple(p for p in self.probes if p.status is ProbeStatus.FAIL)

    @property
    def skipped(self) -> tuple[Probe, ...]:
        return tuple(p for p in self.probes if p.status is ProbeStatus.SKIP)

    @property
    def ok(self) -> bool:
        """True only when at least one probe ran and none failed.

        An empty report is not a pass. A checkout that produced no observations
        has verified nothing, and the one shell script in this project that
        treated "no output" as "no problems" is the reason that distinction is
        spelled out here.
        """
        return bool(self.probes) and not self.failed

    def describe(self) -> dict[str, object]:
        return {
            "result": "CHECKOUT_PASSED" if self.ok else "CHECKOUT_FAILED",
            "counts": {
                "passed": sum(1 for p in self.probes if p.status is ProbeStatus.PASS),
                "failed": len(self.failed),
                "skipped": len(self.skipped),
            },
            "probes": [p.describe() for p in self.probes],
            "failures": [p.name for p in self.failed],
        }


class ReadOnlyBroker(Protocol):
    """The subset of :class:`~app.broker.base.Broker` this module may touch.

    Every method here reads. There is intentionally no ``place_order``,
    ``modify_order``, ``cancel_order`` or ``cancel_all_orders``, so an attempt
    to add one to the checkout fails type checking rather than review.
    """

    async def connect(self) -> ConnectionInfo: ...
    async def disconnect(self) -> None: ...

    @property
    def account_type(self) -> AccountType: ...

    async def get_account_summary(self) -> AccountSummary: ...
    async def get_positions(self) -> Sequence[BrokerPosition]: ...
    async def get_open_orders(self) -> Sequence[BrokerOrderSnapshot]: ...
    async def get_fills(self, *, since_seconds: int = ...) -> Sequence[BrokerFill]: ...
    async def get_contract_details(self, spec: ContractSpec) -> Sequence[QualifiedContract]: ...
    async def qualify_contract(self, spec: ContractSpec) -> QualifiedContract: ...
    async def request_market_data(self, contract: QualifiedContract) -> MarketDataTick: ...
    async def cancel_market_data(self, contract: QualifiedContract) -> None: ...


# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------


def preconditions(config: Config) -> tuple[Probe, ...]:
    """Check the configuration is one in which a read-only checkout is allowed.

    These are evaluated before anything opens a socket. If any of them fails,
    :func:`run_checkout` returns without connecting.
    """
    probes: list[Probe] = []

    probes.append(
        _boolean_probe(
            name="PRECONDITION_TRANSMIT_DISABLED",
            ok=config.allow_order_transmit is False,
            when_ok="ALLOW_ORDER_TRANSMIT=false",
            when_not=(
                "ALLOW_ORDER_TRANSMIT is true; a read-only checkout will not run in a "
                "configuration that could transmit. Set it to false and restart."
            ),
        )
    )
    probes.append(
        _boolean_probe(
            name="PRECONDITION_NOT_LIVE",
            ok=config.trading_mode is not TradingMode.LIVE and not config.live_trading_enabled,
            when_ok=f"TRADING_MODE={config.trading_mode.value}, LIVE_TRADING_ENABLED=false",
            when_not="this checkout is never run against a live account",
        )
    )
    probes.append(
        _boolean_probe(
            name="PRECONDITION_MODE_IS_PAPER",
            ok=config.trading_mode is TradingMode.PAPER,
            when_ok="TRADING_MODE=paper; the IBKR adapter is the broker in use",
            when_not=(
                f"TRADING_MODE={config.trading_mode.value}; there is no IBKR session to check "
                "out. Set TRADING_MODE=paper against a DU paper account first."
            ),
        )
    )
    probes.append(
        _boolean_probe(
            name="PRECONDITION_KILL_SWITCH_ENGAGED",
            ok=config.kill_switch is True,
            when_ok="KILL_SWITCH=true",
            when_not="KILL_SWITCH is false; leave it engaged for the read-only checkout",
        )
    )
    return tuple(probes)


def _boolean_probe(*, name: str, ok: bool, when_ok: str, when_not: str) -> Probe:
    return Probe(
        name=name,
        status=ProbeStatus.PASS if ok else ProbeStatus.FAIL,
        detail=when_ok if ok else when_not,
    )


# ---------------------------------------------------------------------------
# The checkout
# ---------------------------------------------------------------------------


async def run_checkout(
    config: Config,
    broker: ReadOnlyBroker,
    *,
    contract_month: str | None = None,
) -> CheckoutReport:
    """Connect, observe, disconnect. Never writes anything anywhere.

    ``contract_month`` overrides ``DEFAULT_CONTRACT_MONTH`` for this run only.
    Neither this function nor the CLI writes the value back to ``.env``:
    choosing an expiration is a decision an operator records deliberately, not
    something a diagnostic tool infers.
    """
    probes: list[Probe] = list(preconditions(config))
    if any(p.status is ProbeStatus.FAIL for p in probes):
        probes.append(
            Probe(
                name="SESSION",
                status=ProbeStatus.SKIP,
                detail="preconditions failed; no connection was attempted",
            )
        )
        return CheckoutReport(tuple(probes))

    connected = False
    try:
        info = await broker.connect()
        connected = info.connected
        probes.append(
            Probe(
                name="SOCKET_CONNECT",
                status=ProbeStatus.PASS if connected else ProbeStatus.FAIL,
                detail=(
                    f"connected to {info.host}:{info.port} as client {info.client_id}"
                    if connected
                    else "connect() returned without an established session"
                ),
                evidence=info.describe(),
            )
        )
    except BrokerError as exc:
        probes.append(_error_probe("SOCKET_CONNECT", exc))
    except OSError as exc:  # socket-level failure before the adapter classifies it
        probes.append(
            Probe(
                name="SOCKET_CONNECT",
                status=ProbeStatus.FAIL,
                detail=f"{type(exc).__name__}: {exc}",
                evidence={
                    "hint": (
                        "IB Gateway is not listening where this process expects. Check "
                        "IB_HOST/IB_PAPER_PORT and that the gateway's API is enabled."
                    )
                },
            )
        )

    if not connected:
        for name in _POST_CONNECT_PROBES:
            probes.append(Probe(name=name, status=ProbeStatus.SKIP, detail="no broker session"))
        return CheckoutReport(tuple(probes))

    try:
        probes.extend(await _session_probes(config, broker, contract_month=contract_month))
    finally:
        await _disconnect_quietly(broker)

    return CheckoutReport(tuple(probes))


#: Probe names that only exist once a session is up. Listed so a failed connect
#: still produces a full report rather than a truncated one -- an operator
#: should see what was *not* checked, not just what was.
_POST_CONNECT_PROBES: tuple[str, ...] = (
    "ACCOUNT_TYPE_IS_PAPER",
    "ACCOUNT_SUMMARY",
    "POSITIONS_READABLE",
    "OPEN_ORDERS_EMPTY",
    "FILLS_READABLE",
    "CONTRACT_QUALIFIES",
    "MARKET_DATA",
    "GATE_REFUSES_WHEN_EVERYTHING_ELSE_IS_GREEN",
)


async def _session_probes(
    config: Config,
    broker: ReadOnlyBroker,
    *,
    contract_month: str | None,
) -> list[Probe]:
    probes: list[Probe] = []

    observed_type: AccountType = broker.account_type
    probes.append(
        Probe(
            name="ACCOUNT_TYPE_IS_PAPER",
            status=(ProbeStatus.PASS if observed_type is AccountType.PAPER else ProbeStatus.FAIL),
            detail=(
                "the account id the broker reported resolves to a paper account"
                if observed_type is AccountType.PAPER
                else (
                    f"broker-reported account type is {observed_type.value}; a paper checkout "
                    "requires a DU/DF/DI account. Do not continue."
                )
            ),
            evidence={"account_type": observed_type.value},
        )
    )

    summary = await _probe(probes, "ACCOUNT_SUMMARY", broker.get_account_summary)
    if summary is not None:
        probes[-1] = _replace_evidence(
            probes[-1],
            detail="account summary retrieved",
            evidence={
                **summary.describe(),
                "note": (
                    "futures_permission is None by design: the TWS API exposes no such flag, "
                    "so the gate treats it as not permitted"
                ),
            },
        )

    positions = await _probe(probes, "POSITIONS_READABLE", broker.get_positions)
    if positions is not None:
        probes[-1] = _replace_evidence(
            probes[-1],
            detail=f"{len(positions)} position(s) reported",
            evidence={"count": len(positions), "positions": [p.describe() for p in positions]},
        )

    open_orders = await _probe(probes, "OPEN_ORDERS_EMPTY", broker.get_open_orders)
    if open_orders is not None:
        empty = len(open_orders) == 0
        probes[-1] = Probe(
            name="OPEN_ORDERS_EMPTY",
            status=ProbeStatus.PASS if empty else ProbeStatus.FAIL,
            detail=(
                "no working orders at the broker, as expected"
                if empty
                else (
                    f"{len(open_orders)} working order(s) exist. This software has never "
                    "transmitted one, so they came from elsewhere -- investigate before "
                    "going further."
                )
            ),
            evidence={"count": len(open_orders), "orders": [o.describe() for o in open_orders]},
        )

    fills = await _probe(probes, "FILLS_READABLE", broker.get_fills)
    if fills is not None:
        probes[-1] = _replace_evidence(
            probes[-1],
            detail=f"{len(fills)} execution(s) in the last 24h",
            evidence={"count": len(fills)},
        )

    contract = await _contract_probe(config, broker, probes, contract_month=contract_month)
    await _market_data_probe(broker, probes, contract)
    probes.append(_gate_probe(config))

    return probes


async def _contract_probe(
    config: Config,
    broker: ReadOnlyBroker,
    probes: list[Probe],
    *,
    contract_month: str | None,
) -> QualifiedContract | None:
    month = contract_month or config.default_contract_month
    if not month:
        probes.append(
            Probe(
                name="CONTRACT_QUALIFIES",
                status=ProbeStatus.SKIP,
                detail=(
                    "no expiration given. Re-run with --contract-month YYYYMM. An expiration "
                    "is never chosen implicitly -- picking the front month automatically is "
                    "how an order lands on the wrong contract."
                ),
                evidence={"symbol": config.default_futures_symbol},
            )
        )
        return None

    try:
        spec = ContractSpec(
            symbol=config.default_futures_symbol,
            sec_type=SecurityType.FUTURE,
            exchange=config.default_exchange,
            currency=config.default_currency,
            last_trade_date_or_contract_month=month,
        )
    except ContractError as exc:
        probes.append(Probe(name="CONTRACT_QUALIFIES", status=ProbeStatus.FAIL, detail=str(exc)))
        return None

    try:
        qualified = await broker.qualify_contract(spec)
    except (BrokerError, ContractError) as exc:
        probes.append(
            _error_probe(
                "CONTRACT_QUALIFIES",
                exc,
                hint=(
                    "A permission error here is the expected result while US futures "
                    "permission is still pending, and confirms the error classification "
                    "works. A contract error means the symbol, exchange or expiration is "
                    "wrong -- check them against IBKR's contract search."
                ),
            )
        )
        return None

    probes.append(
        Probe(
            name="CONTRACT_QUALIFIES",
            status=ProbeStatus.PASS,
            detail=(
                f"{qualified.local_symbol} qualified (conId {qualified.con_id}). Compare every "
                "field below against IBKR's contract search before trusting it; the reference "
                "constants in app/contracts/solana.py have never been checked against a live "
                "session."
            ),
            evidence={
                "con_id": qualified.con_id,
                "local_symbol": qualified.local_symbol,
                "expiration": qualified.expiration,
                "last_trade_date": qualified.last_trade_date,
                "multiplier": qualified.multiplier,
                "min_tick": str(qualified.min_tick),
                "trading_class": qualified.trading_class,
                "exchange": qualified.exchange,
                "trading_hours": qualified.trading_hours,
                "liquid_hours": qualified.liquid_hours,
                "time_zone_id": qualified.time_zone_id,
            },
        )
    )
    return qualified


async def _market_data_probe(
    broker: ReadOnlyBroker,
    probes: list[Probe],
    contract: QualifiedContract | None,
) -> None:
    if contract is None:
        probes.append(
            Probe(
                name="MARKET_DATA",
                status=ProbeStatus.SKIP,
                detail="no qualified contract to subscribe to",
            )
        )
        return

    try:
        tick = await broker.request_market_data(contract)
    except BrokerError as exc:
        probes.append(
            _error_probe(
                "MARKET_DATA",
                exc,
                hint=(
                    "A permission error here means the account lacks the CME market data "
                    "subscription. That is an account change at IBKR, not a code change, "
                    "and it is why this error class is never retried."
                ),
            )
        )
        return
    finally:
        await _cancel_market_data_quietly(broker, contract)

    has_price = tick.bid is not None or tick.ask is not None or tick.last is not None
    probes.append(
        Probe(
            name="MARKET_DATA",
            status=ProbeStatus.PASS if has_price else ProbeStatus.FAIL,
            detail=(
                "a priced tick arrived"
                if has_price
                else (
                    "a tick arrived with no bid, ask or last. Outside CME trading hours this "
                    "is expected; during liquid hours it is not."
                )
            ),
            evidence=tick.describe(),
        )
    )


def _gate_probe(config: Config) -> Probe:
    """Require the transmit gate to refuse even with a perfect session.

    Every session-side field is set to the value that would *help* an order
    through, and ``risk_approved`` is forced true so risk cannot be the reason.
    Whatever refuses here refuses because of the deployed configuration, which
    is the thing being verified.
    """
    optimistic = GateContext(
        app_state=ApplicationState.READY,
        connection_state=ConnectionState.CONNECTED,
        broker_account_type=AccountType.PAPER,
        contract_qualified=True,
        contract_is_continuous=False,
        account_available=True,
        positions_reconciled=True,
        open_orders_reconciled=True,
        market_data_age_seconds=0.0,
        broker_permission_ready=True,
        strategy_enabled=True,
        risk_approved=True,
    )
    decision = TransmitGate(config).evaluate(optimistic)
    return Probe(
        name="GATE_REFUSES_WHEN_EVERYTHING_ELSE_IS_GREEN",
        status=ProbeStatus.PASS if not decision.allowed else ProbeStatus.FAIL,
        detail=(
            "the gate still refuses to transmit with a real broker session attached"
            if not decision.allowed
            else (
                "THE GATE WOULD ALLOW AN ORDER. Stop and engage the kill switch. The "
                "configuration this process parsed is not the approved one."
            )
        ),
        evidence={
            "allowed": decision.allowed,
            "reasons": list(decision.reasons),
            "context": "every session-side condition forced to its most permissive value",
        },
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _probe(probes: list[Probe], name: str, call: Any) -> Any:
    """Run one read call, appending a placeholder PASS or a classified FAIL.

    The caller replaces the placeholder with the real detail and evidence, which
    keeps the error handling in one place instead of repeated per probe.
    """
    try:
        result = await call()
    except BrokerError as exc:
        probes.append(_error_probe(name, exc))
        return None
    probes.append(Probe(name=name, status=ProbeStatus.PASS, detail="ok"))
    return result


def _replace_evidence(probe: Probe, *, detail: str, evidence: dict[str, object]) -> Probe:
    return Probe(name=probe.name, status=probe.status, detail=detail, evidence=evidence)


def _error_probe(name: str, exc: Exception, *, hint: str | None = None) -> Probe:
    evidence: dict[str, object] = {
        "error_class": type(exc).__name__,
        "retryable": getattr(exc, "retryable", False),
    }
    if hint:
        evidence["hint"] = hint
    return Probe(name=name, status=ProbeStatus.FAIL, detail=str(exc), evidence=evidence)


async def _disconnect_quietly(broker: ReadOnlyBroker) -> None:
    """A failure to hang up must not mask the findings that preceded it."""
    try:
        await broker.disconnect()
    except (BrokerError, OSError):
        return


async def _cancel_market_data_quietly(broker: ReadOnlyBroker, contract: QualifiedContract) -> None:
    try:
        await broker.cancel_market_data(contract)
    except (BrokerError, OSError):
        return
