"""Operator command line.

Run inside the container:

    docker compose exec sol-trading-bot python -m app.cli status

Every command respects the same environment as the trading process and none of
them can loosen a safety setting. There is deliberately **no**
``kill-switch-off`` and **no** ``enable-live``. Resuming trading is a
configuration change a human makes to the server ``.env`` followed by a
restart, and the friction is the feature.

Read-only commands work against the database, so they are safe to run while the
bot is trading and do not need a broker connection.

Two commands are not read-only, and both are deliberate:

* ``cancel-all-orders`` reaches the broker and cancels working orders. It only
  ever removes exposure.
* ``place-order`` sends **one** order, and is the only command that can create
  exposure. It cannot enable trading -- it requires a configuration that already
  permits it, and every interlock applies exactly as it does to a strategy.
  Without ``--confirm`` it previews and sends nothing. See
  ``app/execution/operator_order.py``.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import Config, ConfigError
from app.enums import TradingMode
from app.monitoring.status import build_info, safety_summary
from app.safety.killswitch import KillSwitch
from app.safety.posture import evaluate_posture, failing_checks, posture_is_approved
from app.state.database import Database, DatabaseError
from app.state.repositories import Repositories
from app.utilities.timeutils import utc_now

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2


def _emit(payload: object) -> None:
    print(json.dumps(payload, default=str, indent=2))


def _open_database(config: Config) -> tuple[Database, Repositories]:
    database = Database(config.database_path)
    database.connect()
    return database, Repositories(database)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_status(config: Config, args: argparse.Namespace) -> int:
    """Live status from the running process, falling back to durable state."""
    del args
    url = f"http://{config.health_host}:{config.health_port}/status"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            _emit(json.loads(response.read().decode("utf-8")))
        return EXIT_OK
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        # The bot may simply not be running. Report what durable state we have
        # rather than failing with a connection error.
        payload: dict[str, Any] = {
            "live_status": "unavailable",
            "reason": str(exc),
            "note": "the trading process is not answering on loopback; showing durable state",
            **build_info(config),
        }
        try:
            database, repos = _open_database(config)
            payload["application_state"] = repos.state.all()
            payload["database"] = database.info()
            payload["open_orders"] = [o.describe() for o in repos.orders.open_orders()]
            database.close()
        except DatabaseError as db_exc:
            payload["database_error"] = str(db_exc)
        _emit(payload)
        return EXIT_ERROR


def cmd_broker_status(config: Config, args: argparse.Namespace) -> int:
    del args
    database, repos = _open_database(config)
    try:
        _emit(
            {
                "trading_mode": config.trading_mode.value,
                "broker_implementation": (
                    "mock"
                    if config.trading_mode is TradingMode.MOCK
                    else "none"
                    if config.trading_mode is TradingMode.DISABLED
                    else "ibkr"
                ),
                "ib_host": config.ibkr.host,
                "ib_port_in_use": config.ib_port,
                "ib_paper_port": config.ibkr.paper_port,
                "ib_live_port": config.ibkr.live_port,
                "client_id": config.ibkr.client_id,
                "durable_state": repos.state.all(),
                "recent_events": repos.events.recent_bot_events(limit=10),
            }
        )
    finally:
        database.close()
    return EXIT_OK


def cmd_positions(config: Config, args: argparse.Namespace) -> int:
    del args
    database, repos = _open_database(config)
    try:
        positions = repos.positions.all()
        _emit({"count": len(positions), "positions": [p.describe() for p in positions]})
    finally:
        database.close()
    return EXIT_OK


def cmd_open_orders(config: Config, args: argparse.Namespace) -> int:
    del args
    database, repos = _open_database(config)
    try:
        orders = repos.orders.open_orders()
        _emit({"count": len(orders), "orders": [o.describe() for o in orders]})
    finally:
        database.close()
    return EXIT_OK


def cmd_contract_info(config: Config, args: argparse.Namespace) -> int:
    del args
    database, repos = _open_database(config)
    try:
        contracts = repos.contracts.all()
        _emit(
            {
                "configured_symbol": config.default_futures_symbol,
                "configured_exchange": config.default_exchange,
                "configured_contract_month": config.default_contract_month,
                "note": (
                    "an order can only be built against a broker-qualified dated contract; "
                    "continuous futures are never orderable"
                ),
                "qualified_contracts": [dict(row) for row in contracts],
            }
        )
    finally:
        database.close()
    return EXIT_OK


def cmd_kill_switch_status(config: Config, args: argparse.Namespace) -> int:
    del args
    database, repos = _open_database(config)
    try:
        kill_switch = KillSwitch(config_engaged=config.kill_switch, store=repos.state)
        _emit(
            {
                **kill_switch.describe(),
                "safety": safety_summary(config, kill_switch_engaged=kill_switch.engaged()),
            }
        )
    finally:
        database.close()
    return EXIT_OK


def cmd_kill_switch_on(config: Config, args: argparse.Namespace) -> int:
    """Latch the kill switch durably.

    Takes effect for the running process on its next tick. There is no command
    to clear it: edit ``KILL_SWITCH`` in the server ``.env`` and restart.
    """
    database, repos = _open_database(config)
    try:
        kill_switch = KillSwitch(config_engaged=config.kill_switch, store=repos.state)
        kill_switch.engage(args.reason, engaged_at=utc_now().isoformat())
        _emit(
            {
                "result": "KILL_SWITCH_ENGAGED",
                "reason": args.reason,
                **kill_switch.describe(),
                "next_steps": [
                    "The running process will stop producing actionable trades on its next tick.",
                    "Set KILL_SWITCH=true in the server .env so the latch survives a redeploy.",
                    "There is no kill-switch-off command; clearing it is a deliberate "
                    "configuration change followed by a restart.",
                ],
            }
        )
    finally:
        database.close()
    return EXIT_OK


async def _cancel_all(config: Config, reason: str) -> dict[str, object]:
    """Connect briefly on the admin client id and cancel every working order."""
    from app.broker.mock_broker import MockBroker  # noqa: PLC0415

    if config.trading_mode is TradingMode.DISABLED:
        return {
            "result": "NO_BROKER",
            "detail": "TRADING_MODE=disabled; there is no broker and no orders can exist",
        }

    broker: Any
    if config.trading_mode is TradingMode.MOCK:
        broker = MockBroker()
    else:
        from app.broker.ibkr_broker import IBKRBroker  # noqa: PLC0415
        from app.safety.gate import expected_account_type  # noqa: PLC0415

        port = config.ib_port
        assert port is not None
        broker = IBKRBroker(
            host=config.ibkr.host,
            port=port,
            # A distinct client id so this never disconnects the trading process.
            client_id=config.ibkr.admin_client_id,
            connect_timeout_seconds=config.ibkr.connect_timeout_seconds,
            expected_account_type=expected_account_type(config.trading_mode),
        )

    await broker.connect()
    try:
        open_before = await broker.get_open_orders()
        cancelled = await broker.cancel_all_orders()
        return {
            "result": "CANCEL_ALL_REQUESTED",
            "reason": reason,
            "open_orders_before": len(open_before),
            "cancels_issued": cancelled,
            "note": "positions are NOT affected; cancelling orders and flattening are separate",
        }
    finally:
        await broker.disconnect()


def cmd_cancel_all_orders(config: Config, args: argparse.Namespace) -> int:
    import asyncio  # noqa: PLC0415

    if not args.confirm:
        _emit(
            {
                "result": "CONFIRMATION_REQUIRED",
                "detail": "re-run with --confirm to cancel every working order",
            }
        )
        return EXIT_ERROR
    try:
        _emit(asyncio.run(_cancel_all(config, args.reason)))
    except Exception as exc:  # noqa: BLE001 -- report, do not traceback at an operator
        _emit({"result": "ERROR", "error": str(exc)})
        return EXIT_ERROR
    return EXIT_OK


def cmd_db_info(config: Config, args: argparse.Namespace) -> int:
    del args
    database = Database(config.database_path)
    try:
        _emit(database.info())
    finally:
        database.close()
    return EXIT_OK


def cmd_verify(config: Config, args: argparse.Namespace) -> int:
    """Verify the configuration this process actually parsed.

    `scripts/verify_safety.sh` checks what the .env file says. This checks what
    the running process holds, which differs whenever a hosting control panel,
    a compose override, or a stale container injects values the file never saw.

    Exits non-zero if the posture is not the approved one, and also if the
    checks themselves come back incomplete -- an unreadable result is a failure,
    never a pass.
    """
    posture = getattr(args, "posture", None) or "halted"
    database, repos = _open_database(config)
    try:
        kill_switch = KillSwitch(config_engaged=config.kill_switch, store=repos.state)
        checks = evaluate_posture(config, kill_switch_engaged=kill_switch.engaged())
    finally:
        database.close()

    failures = failing_checks(checks)
    if posture == "halted":
        approved = posture_is_approved(checks)
        result = "APPROVED_POSTURE" if approved else "POSTURE_NOT_APPROVED"
    else:
        # An armed system is EXPECTED to differ from the halted posture -- that
        # is what arming means, and reporting those differences as failures says
        # nothing. What must still hold is that nothing about it is live. Those
        # two checks are asserted here exactly as they are under `halted`; the
        # armed configuration's own invariants (limits set, freshness set) are
        # verified against the .env by scripts/verify_safety.sh before the
        # container starts.
        must_hold = {"LIVE_TRADING_ENABLED", "CAN_TRANSMIT_LIVE_ORDERS"}
        live_failures = tuple(c for c in failures if c.name in must_hold)
        approved = not live_failures and config.trading_mode is not TradingMode.LIVE
        failures = live_failures
        result = "ARMED_AND_NOT_LIVE" if approved else "ARMED_BUT_LIVE_CHECKS_FAILED"

    _emit(
        {
            "result": result,
            "posture": posture,
            "source": "the configuration this running process parsed, not the .env file",
            "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in checks],
            "failures": [c.name for c in failures],
            **build_info(config),
        }
    )
    return EXIT_OK if approved else EXIT_ERROR


async def _ibkr_checkout(config: Config, contract_month: str | None) -> dict[str, object]:
    """Connect read-only on the admin client id and report what was observed."""
    from app.broker.checkout import run_checkout  # noqa: PLC0415
    from app.broker.ibkr_broker import IBKRBroker  # noqa: PLC0415
    from app.safety.gate import expected_account_type  # noqa: PLC0415

    port = config.ib_port
    assert port is not None  # guaranteed by the paper-mode precondition
    broker = IBKRBroker(
        host=config.ibkr.host,
        port=port,
        # The admin client id, so a checkout can never disconnect a running bot.
        client_id=config.ibkr.admin_client_id,
        connect_timeout_seconds=config.ibkr.connect_timeout_seconds,
        expected_account_type=expected_account_type(config.trading_mode),
    )
    report = await run_checkout(config, broker, contract_month=contract_month)
    return report.describe()


def cmd_ibkr_checkout(config: Config, args: argparse.Namespace) -> int:
    """Verify the IBKR adapter against a real gateway, reading only.

    Every socket path in `app/broker/ibkr_broker.py` is unverified until this
    passes; the unit tests around it drive fakes. This command cannot place an
    order -- see the module docstring in `app/broker/checkout.py` for how that
    is enforced -- and it refuses to run unless the interlocks are engaged.
    """
    import asyncio  # noqa: PLC0415

    from app.broker.checkout import ProbeStatus, preconditions  # noqa: PLC0415
    from app.logging_config import configure_logging  # noqa: PLC0415

    # Without this, the adapter's log records fall through to logging's
    # last-resort handler, which prints the bare message ("ibkr error") and
    # discards every structured field -- the IB error code, the message, how it
    # was classified, whether it is retryable. That is precisely the information
    # a diagnostic exists to surface. Logs go to stderr so the report on stdout
    # stays parseable; no file sink, because this is not the trading process and
    # must not touch its log directory.
    configure_logging(config, run_id="ibkr-checkout", log_to_file=False, stream=sys.stderr)

    blocking = [p for p in preconditions(config) if p.status is ProbeStatus.FAIL]
    if blocking:
        _emit(
            {
                "result": "CHECKOUT_REFUSED",
                "detail": "the configuration does not permit a read-only checkout",
                "failures": [{"name": p.name, "detail": p.detail} for p in blocking],
            }
        )
        return EXIT_ERROR

    try:
        payload = asyncio.run(_ibkr_checkout(config, args.contract_month))
    except Exception as exc:  # noqa: BLE001 -- report, do not traceback at an operator
        _emit({"result": "CHECKOUT_ERROR", "error": str(exc), "error_class": type(exc).__name__})
        return EXIT_ERROR

    _emit({**payload, **build_info(config)})
    return EXIT_OK if payload.get("result") == "CHECKOUT_PASSED" else EXIT_ERROR


async def _check_permission(config: Config, contract_month: str | None) -> dict[str, object]:
    """Observe futures permission with a whatIf preview. Places no order."""
    from app.broker.ibkr_broker import IBKRBroker  # noqa: PLC0415
    from app.contracts.resolver import ContractResolver  # noqa: PLC0415
    from app.safety.gate import expected_account_type  # noqa: PLC0415

    port = config.ib_port
    if port is None:
        return {"result": "NO_BROKER", "detail": f"no IB port for mode {config.trading_mode.value}"}

    broker = IBKRBroker(
        host=config.ibkr.host,
        port=port,
        # The admin id, so this can never disconnect a running bot.
        client_id=config.ibkr.admin_client_id,
        connect_timeout_seconds=config.ibkr.connect_timeout_seconds,
        expected_account_type=expected_account_type(config.trading_mode),
    )
    await broker.connect()
    try:
        month = contract_month or config.default_contract_month
        if not month:
            return {
                "result": "NO_CONTRACT",
                "detail": "pass --contract-month or set DEFAULT_CONTRACT_MONTH; "
                "an expiration is never chosen implicitly",
            }
        contract = await ContractResolver(broker).resolve(
            symbol=config.default_futures_symbol, contract_month=month
        )
        probe = await broker.probe_futures_permission(contract)
        return {
            "result": "PERMITTED" if probe.permitted else "NOT_PERMITTED",
            "contract": {"local_symbol": contract.local_symbol, "con_id": contract.con_id},
            "probe": probe.describe(),
            "note": (
                "observed with a whatIf preview: IBKR prices an order it would accept and "
                "refuses one it would not. No order was placed."
            ),
        }
    finally:
        await broker.disconnect()


def cmd_check_permission(config: Config, args: argparse.Namespace) -> int:
    """Ask IBKR whether this account may trade the contract, by observation.

    The TWS API exposes no permission flag, so `SOL_FUTURES_PERMISSION_READY` is
    an operator declaration. This is how that declaration gets backed by
    evidence rather than belief -- and it is the same check that would catch
    permission being revoked, which no configuration variable ever would.

    Places no order: see `IBKRBroker.probe_futures_permission`.
    """
    import asyncio  # noqa: PLC0415

    from app.logging_config import configure_logging  # noqa: PLC0415

    configure_logging(config, run_id="check-permission", log_to_file=False, stream=sys.stderr)

    if config.trading_mode is TradingMode.LIVE:
        _emit(
            {
                "result": "REFUSED_LIVE_MODE",
                "detail": "this probe is not available in live mode",
            }
        )
        return EXIT_ERROR

    try:
        payload = asyncio.run(_check_permission(config, args.contract_month))
    except Exception as exc:  # noqa: BLE001 -- report, do not traceback at an operator
        _emit({"result": "PROBE_ERROR", "error": str(exc), "error_class": type(exc).__name__})
        return EXIT_ERROR

    _emit({**payload, **build_info(config)})
    return EXIT_OK if payload.get("result") == "PERMITTED" else EXIT_ERROR


def cmd_place_order(config: Config, args: argparse.Namespace) -> int:
    """Send ONE operator order through the real pipeline.

    The only command in this tool that can cause an order. It loosens nothing:
    the kill switch, the transmit flag and every risk limit apply exactly as
    they do to a strategy, and a refusal from either approver is reported
    rather than worked around.

    Without ``--confirm`` it previews and sends nothing. See
    ``app/execution/operator_order.py`` for why the confirmation token is the
    broker-reported local symbol rather than a bare flag.
    """
    import asyncio  # noqa: PLC0415
    from decimal import Decimal, InvalidOperation  # noqa: PLC0415

    from app.enums import OrderType  # noqa: PLC0415
    from app.execution.operator_order import (  # noqa: PLC0415
        OperatorOrderRequest,
        place_operator_order,
    )
    from app.logging_config import configure_logging  # noqa: PLC0415

    # Same reasoning as ibkr-checkout: the adapter's structured fields are the
    # diagnosis, and stdout stays parseable.
    configure_logging(config, run_id="operator-order", log_to_file=False, stream=sys.stderr)

    limit_price: Decimal | None = None
    if args.limit_price is not None:
        try:
            limit_price = Decimal(args.limit_price)
        except (InvalidOperation, ValueError):
            _emit({"result": "INVALID_LIMIT_PRICE", "value": args.limit_price})
            return EXIT_ERROR

    request = OperatorOrderRequest(
        target_position=args.target_position,
        order_type=OrderType.MARKET if args.order_type == "market" else OrderType.LIMIT,
        limit_price=limit_price,
        confirm=args.confirm,
    )

    try:
        payload = asyncio.run(place_operator_order(config, request))
    except Exception as exc:  # noqa: BLE001 -- report, do not traceback at an operator
        _emit(
            {
                "result": "OPERATOR_ORDER_ERROR",
                "error": str(exc),
                "error_class": type(exc).__name__,
            }
        )
        return EXIT_ERROR

    _emit({**payload, **build_info(config)})
    return EXIT_OK if payload.get("result") in {"SUBMITTED", "PREVIEW_ONLY"} else EXIT_ERROR


def cmd_bars_import(config: Config, args: argparse.Namespace) -> int:
    """Fetch historical bars into the local store.

    Reads and writes the `bars` table only. It cannot reach a broker and cannot
    affect a running bot -- see `app/backtest/__init__.py`.

    Use `--limit` for the first run against a new source. The HTTP path is the
    one thing here that could not be tested before deployment, so fetch ten
    bars, look at them, and only then fetch a year.
    """
    from datetime import timedelta  # noqa: PLC0415

    from app.backtest.sources import (  # noqa: PLC0415
        BinanceBarSource,
        CoinbaseBarSource,
        CsvBarSource,
        HistoricalSourceError,
    )
    from app.backtest.store import BarRepository  # noqa: PLC0415

    end = datetime.now(UTC) if args.end is None else _parse_day(args.end)
    start = end - timedelta(days=args.days) if args.start is None else _parse_day(args.start)
    if start >= end:
        _emit({"result": "INVALID_RANGE", "start": start.isoformat(), "end": end.isoformat()})
        return EXIT_ERROR

    source: object
    if args.source == "csv":
        if not args.csv_path:
            _emit({"result": "CSV_PATH_REQUIRED", "detail": "--csv-path is required for csv"})
            return EXIT_ERROR
        try:
            columns = json.loads(args.csv_columns) if args.csv_columns else {}
        except json.JSONDecodeError as exc:
            _emit({"result": "INVALID_CSV_COLUMNS", "error": str(exc)})
            return EXIT_ERROR
        try:
            source = CsvBarSource(
                Path(args.csv_path), source_name=args.csv_source_name, columns=columns
            )
        except HistoricalSourceError as exc:
            _emit({"result": "INVALID_CSV_MAPPING", "error": str(exc)})
            return EXIT_ERROR
    elif args.source == "coinbase":
        source = CoinbaseBarSource()
    elif args.source == "binance-us":
        source = BinanceBarSource.united_states()
    else:
        source = BinanceBarSource()

    database, _ = _open_database(config)
    try:
        database.migrate()
        repo = BarRepository(database)
        fetched: list[object] = []
        try:
            for bar in source.fetch(
                symbol=args.symbol, interval=args.interval, start=start, end=end
            ):
                fetched.append(bar)
                if args.limit and len(fetched) >= args.limit:
                    break
        except HistoricalSourceError as exc:
            _emit(
                {
                    "result": "FETCH_FAILED",
                    "error": str(exc),
                    "fetched_before_failure": len(fetched),
                }
            )
            return EXIT_ERROR

        added = repo.insert_many(fetched)  # type: ignore[arg-type]
        info = repo.info(source=_source_name(source), symbol=args.symbol, interval=args.interval)
        _emit(
            {
                "result": "IMPORTED",
                "requested": {
                    "symbol": args.symbol,
                    "interval": args.interval,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "limit": args.limit,
                },
                "fetched": len(fetched),
                "added": added,
                "already_present": len(fetched) - added,
                "sample": [b.describe() for b in fetched[:3]],  # type: ignore[attr-defined]
                "stored": info.describe(),
                "note": (
                    "gaps are reported, never filled. A missing bar is a fact about the "
                    "data; inventing a price to cover it is how a backtest starts lying."
                ),
            }
        )
    finally:
        database.close()
    return EXIT_OK


def cmd_bars_info(config: Config, args: argparse.Namespace) -> int:
    """What history is stored, over what range, with gaps named."""
    del args
    from app.backtest.store import BarRepository  # noqa: PLC0415

    database, _ = _open_database(config)
    try:
        database.migrate()
        repo = BarRepository(database)
        series = repo.series()
        _emit(
            {
                "series_count": len(series),
                "series": [
                    repo.info(source=src, symbol=sym, interval=iv).describe()
                    for src, sym, iv in series
                ],
            }
        )
    finally:
        database.close()
    return EXIT_OK


def cmd_backtest(config: Config, args: argparse.Namespace) -> int:
    """Replay stored bars through the real interlocks.

    Reads the `bars` and `contract_metadata` tables. It writes nothing, reaches
    no broker, and cannot affect a running bot.

    Two things it deliberately refuses to do rather than guess:

    * **Invent a contract.** The multiplier, tick size and conId come from a
      qualification IBKR actually returned, loaded from the database. With none
      stored it stops and says to run `ibkr-checkout` -- a fabricated conId
      would defeat the check that makes contract identity unambiguous, and a
      guessed multiplier silently scales every P&L figure in the result.
    * **Loosen a limit.** The risk limits come from the deployed configuration
      unless explicitly overridden on the command line, and the result records
      which. A replay run under limits nobody uses answers a question nobody
      asked.
    """
    from decimal import Decimal, InvalidOperation  # noqa: PLC0415

    from app.backtest.broker import FillModel  # noqa: PLC0415
    from app.backtest.engine import (  # noqa: PLC0415
        BacktestEngine,
        always_tradeable,
        backtest_config,
        cme_liquid_hours,
    )
    from app.backtest.results import build_report  # noqa: PLC0415
    from app.backtest.store import BarRepository  # noqa: PLC0415
    from app.strategy.noop import build_strategy  # noqa: PLC0415

    try:
        commission = Decimal(args.commission)
    except InvalidOperation:
        _emit({"result": "INVALID_COMMISSION", "value": args.commission})
        return EXIT_ERROR
    if commission < 0 or args.slippage_ticks < 0 or args.spread_ticks < 0:
        # A negative cost is a fill model that pays the strategy to trade.
        _emit({"result": "INVALID_FILL_MODEL", "detail": "costs cannot be negative"})
        return EXIT_ERROR

    try:
        strategy = build_strategy(args.strategy or config.strategy_name)
    except KeyError as exc:
        _emit({"result": "UNKNOWN_STRATEGY", "error": str(exc)})
        return EXIT_ERROR

    limits = config.risk
    overrides: dict[str, str] = {}
    limit_args = {
        "MAX_ORDER_SIZE": (args.max_order_size, limits.max_order_size),
        "MAX_POSITION_CONTRACTS": (args.max_position, limits.max_position_contracts),
        "MAX_ORDERS_PER_HOUR": (args.max_orders_per_hour, limits.max_orders_per_hour),
        "MAX_OPEN_ORDERS": (args.max_open_orders, limits.max_open_orders),
        "MAX_DAILY_LOSS_USD": (args.max_daily_loss, limits.max_daily_loss_usd),
        "MAX_NOTIONAL_EXPOSURE_USD": (args.max_notional, limits.max_notional_exposure_usd),
    }
    limit_source = {
        name: ("command line" if supplied is not None else "deployed configuration")
        for name, (supplied, _) in limit_args.items()
    }
    for name, (supplied, deployed) in limit_args.items():
        overrides[name] = str(deployed if supplied is None else supplied)

    if args.inherit_switches:
        overrides["KILL_SWITCH"] = "true" if config.kill_switch else "false"
        overrides["ALLOW_ORDER_TRANSMIT"] = "true" if config.allow_order_transmit else "false"

    database, repositories = _open_database(config)
    try:
        database.migrate()

        contract = repositories.contracts.find(args.contract_symbol, args.contract_month)
        if contract is None:
            _emit(
                {
                    "result": "NO_QUALIFIED_CONTRACT",
                    "requested": {
                        "symbol": args.contract_symbol,
                        "expiration": args.contract_month,
                    },
                    "detail": (
                        "no stored qualification for this contract. Run `ibkr-checkout "
                        "--contract-month YYYYMM` first. A contract is never invented here: "
                        "a guessed multiplier would silently scale every P&L figure below."
                    ),
                }
            )
            return EXIT_ERROR

        try:
            replay_config = backtest_config(
                symbol=contract.symbol,
                max_position_contracts=int(overrides["MAX_POSITION_CONTRACTS"]),
                max_order_size=int(overrides["MAX_ORDER_SIZE"]),
                max_daily_loss_usd=overrides["MAX_DAILY_LOSS_USD"],
                max_orders_per_hour=int(overrides["MAX_ORDERS_PER_HOUR"]),
                max_open_orders=int(overrides["MAX_OPEN_ORDERS"]),
                max_notional_exposure_usd=overrides["MAX_NOTIONAL_EXPOSURE_USD"],
                overrides=overrides,
            )
        except ConfigError as exc:
            _emit({"result": "INVALID_BACKTEST_CONFIG", "error": str(exc)})
            return EXIT_ERROR

        bars = BarRepository(database).load(
            source=args.source,
            symbol=args.symbol,
            interval=args.interval,
            start=None if args.start is None else _parse_day(args.start),
            end=None if args.end is None else _parse_day(args.end),
        )
        if not bars:
            _emit(
                {
                    "result": "NO_BARS",
                    "requested": {
                        "source": args.source,
                        "symbol": args.symbol,
                        "interval": args.interval,
                    },
                    "detail": (
                        "import history first: `bars-import --limit 10` to verify the "
                        "source, then a full year."
                    ),
                }
            )
            return EXIT_ERROR

        engine = BacktestEngine(
            config=replay_config,
            contract=contract,
            strategy=strategy,
            fill_model=FillModel(
                slippage_ticks=args.slippage_ticks,
                commission_per_contract=commission,
                spread_ticks=args.spread_ticks,
            ),
            session=cme_liquid_hours if args.session == "cme" else always_tradeable,
        )
        report = build_report(engine.run(bars))
    finally:
        database.close()

    payload: dict[str, object] = {
        "result": "REPLAYED",
        "strategy": strategy.describe(),
        "contract": {
            "symbol": contract.symbol,
            "local_symbol": contract.local_symbol,
            "con_id": contract.con_id,
            "expiration": contract.expiration,
            "multiplier": contract.multiplier,
            "min_tick": str(contract.min_tick),
            "source": "stored IBKR qualification, not invented",
        },
        "limits": {
            "values": replay_config.risk.as_dict(),
            "source": limit_source,
        },
        "switches": {
            "inherited_from_deployed_config": bool(args.inherit_switches),
            "kill_switch": replay_config.kill_switch,
            "allow_order_transmit": replay_config.allow_order_transmit,
            "note": (
                "without --inherit-switches a replay runs with the master switches "
                "released, so a halted server can still answer 'what would this have "
                "done'. It changes nothing on the server either way."
            ),
        },
        **report.describe(),
    }
    if not report.run.fills:
        payload["why_no_trades"] = _why_no_trades(strategy.name, report.refusals_by_reason)
    _emit({**payload, **build_info(config)})
    return EXIT_OK


def _why_no_trades(strategy_name: str, refusals_by_reason: dict[str, int]) -> str:
    """An empty result must say which kind of empty it is.

    "The strategy produced nothing" and "the strategy was refused" look
    identical in the numbers and mean opposite things. Distinguishing them
    matters most for the deployed halted configuration, where every limit is
    zero -- which means NOT CONFIGURED and therefore prohibited, never
    unlimited. An operator seeing a flat result there should be told the
    replay never got to trade rather than concluding the strategy is flat.
    """
    unconfigured = sorted(r for r in refusals_by_reason if r.endswith("_NOT_CONFIGURED"))
    if unconfigured:
        return (
            "every order was refused because the deployed configuration does not "
            f"authorise trading: {', '.join(unconfigured)}. Zero means NOT CONFIGURED, "
            "never unlimited. Pass explicit limits (--max-order-size, --max-position, "
            "...) to replay under limits you want to evaluate."
        )
    if refusals_by_reason:
        top = max(refusals_by_reason.items(), key=lambda kv: (kv[1], kv[0]))
        return (
            f"no order was transmitted; the most frequent refusal was {top[0]} "
            f"({top[1]} times). This is an interlock result, not a strategy result."
        )
    if strategy_name == "noop":
        return (
            "the registered strategy is `noop`, which never produces an intent. This "
            "result is a plumbing check, not a statement about any trading edge."
        )
    return "the strategy produced no intent that required an order over this history."


def _source_name(source: object) -> str:
    return str(getattr(source, "source_name", None) or getattr(source, "name", "unknown"))


def _parse_day(raw: str) -> datetime:
    return datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=UTC)


def cmd_config(config: Config, args: argparse.Namespace) -> int:
    del args
    _emit({"config": config.redacted(), **build_info(config)})
    return EXIT_OK


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

COMMANDS = {
    "status": cmd_status,
    "broker-status": cmd_broker_status,
    "positions": cmd_positions,
    "open-orders": cmd_open_orders,
    "contract-info": cmd_contract_info,
    "kill-switch-status": cmd_kill_switch_status,
    "kill-switch-on": cmd_kill_switch_on,
    "cancel-all-orders": cmd_cancel_all_orders,
    "db-info": cmd_db_info,
    "config": cmd_config,
    "verify": cmd_verify,
    "ibkr-checkout": cmd_ibkr_checkout,
    "place-order": cmd_place_order,
    "check-permission": cmd_check_permission,
    "bars-import": cmd_bars_import,
    "bars-info": cmd_bars_info,
    "backtest": cmd_backtest,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="solbot-admin",
        description=(
            "Operator commands for sol-futures-trading-bot. "
            "No command in this tool can enable trading or clear the kill switch. "
            "`place-order` can SEND one order, but only where the configuration "
            "already permits it -- it loosens nothing and every interlock still applies."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name in COMMANDS:
        sub = subparsers.add_parser(name)
        if name == "kill-switch-on":
            sub.add_argument("--reason", required=True, help="why the kill switch is being engaged")
        if name == "cancel-all-orders":
            sub.add_argument("--reason", default="operator request")
            sub.add_argument(
                "--confirm",
                action="store_true",
                help="required; cancels every working order at the broker",
            )
        if name == "place-order":
            sub.add_argument(
                "--target-position",
                type=int,
                required=True,
                metavar="N",
                help=(
                    "ABSOLUTE target position in contracts, signed. Not a delta: 1 means "
                    "'be long 1', and running it twice leaves you long 1, not 2. Use 0 to flatten."
                ),
            )
            sub.add_argument(
                "--order-type",
                choices=("limit", "market"),
                default="limit",
                help="limit (default) -- a first order should not fill at a surprising price",
            )
            sub.add_argument("--limit-price", default=None, help="required for a limit order")
            sub.add_argument(
                "--confirm",
                default=None,
                metavar="LOCAL_SYMBOL",
                help=(
                    "the contract's broker-reported local symbol. Omit for a preview that "
                    "sends nothing; the preview prints the value to pass here."
                ),
            )
        if name == "bars-import":
            sub.add_argument(
                "--symbol",
                default="SOLUSDT",
                help=(
                    "the venue's own symbol: SOLUSDT on binance, SOLUSD on binance-us, "
                    "SOL-USD on coinbase. Stored as given, and part of the series key."
                ),
            )
            sub.add_argument("--interval", default="1m", choices=("1m", "5m", "15m", "1h", "1d"))
            sub.add_argument(
                "--source",
                default="binance",
                choices=("binance", "binance-us", "coinbase", "csv"),
                help=(
                    "binance is geo-blocked (HTTP 451) from a US-hosted server; use "
                    "coinbase or binance-us there. kraken is absent on purpose: it serves "
                    "only the most recent 720 bars, which is 12 hours of 1-minute data."
                ),
            )
            sub.add_argument(
                "--days", type=int, default=365, help="lookback when --start is absent"
            )
            sub.add_argument("--start", default=None, metavar="YYYY-MM-DD")
            sub.add_argument("--end", default=None, metavar="YYYY-MM-DD")
            sub.add_argument(
                "--limit",
                type=int,
                default=0,
                help=(
                    "stop after N bars. Use a small value on the FIRST run against a new "
                    "source: the HTTP path could not be tested before deployment, so the "
                    "first real fetch is the verification."
                ),
            )
            sub.add_argument("--csv-path", default=None)
            sub.add_argument("--csv-source-name", default="csv")
            sub.add_argument(
                "--csv-columns",
                default=None,
                metavar="JSON",
                help='e.g. \'{"opened_at":"time","open":"o","high":"h","low":"l","close":"c"}\'',
            )
        if name == "backtest":
            sub.add_argument("--symbol", default="SOLUSDT", help="the BAR symbol to replay")
            sub.add_argument("--interval", default="1m", choices=("1m", "5m", "15m", "1h", "1d"))
            sub.add_argument("--source", default="binance", help="bar provenance, e.g. binance")
            sub.add_argument("--start", default=None, metavar="YYYY-MM-DD")
            sub.add_argument("--end", default=None, metavar="YYYY-MM-DD")
            sub.add_argument(
                "--contract-symbol",
                default="MSL",
                help=(
                    "the FUTURES contract to price against. Loaded from a stored IBKR "
                    "qualification; never invented, because a guessed multiplier silently "
                    "scales every P&L figure in the result."
                ),
            )
            sub.add_argument("--contract-month", default=None, metavar="YYYYMM")
            sub.add_argument(
                "--strategy",
                default=None,
                help="registered strategy name; defaults to STRATEGY_NAME",
            )
            sub.add_argument(
                "--session",
                choices=("cme", "all"),
                default="cme",
                help=(
                    "cme (default) trades only CME liquid hours. `all` is honest for spot "
                    "data and wrong for futures -- it trades bars the contract does not."
                ),
            )
            sub.add_argument("--slippage-ticks", type=int, default=1)
            sub.add_argument("--spread-ticks", type=int, default=2)
            sub.add_argument(
                "--commission",
                default="3.41",
                help="per contract per side; the default is what IBKR quoted for MSLQ6",
            )
            sub.add_argument(
                "--inherit-switches",
                action="store_true",
                help=(
                    "run the replay under the deployed kill switch and transmit setting. "
                    "Off by default so a halted server can still answer 'what would this "
                    "have done'. Changes nothing on the server either way."
                ),
            )
            for flag, help_text in (
                ("--max-order-size", "contracts per order"),
                ("--max-position", "contracts held"),
                ("--max-orders-per-hour", "order rate"),
                ("--max-open-orders", "working orders"),
            ):
                sub.add_argument(flag, type=int, default=None, help=f"override {help_text}")
            sub.add_argument("--max-daily-loss", default=None, help="override, USD")
            sub.add_argument("--max-notional", default=None, help="override, USD")
        if name == "verify":
            sub.add_argument(
                "--posture",
                choices=("halted", "paper-armed"),
                default="halted",
                help=(
                    "which configuration to consider correct. Defaults to halted, so a "
                    "check that says nothing gets the refusing answer. Under paper-armed, "
                    "differences from the halted posture are expected; the live checks "
                    "still must pass."
                ),
            )
        if name in {"ibkr-checkout", "check-permission"}:
            sub.add_argument(
                "--contract-month",
                default=None,
                metavar="YYYYMM",
                help=(
                    "expiration to qualify for this run only; never written back to .env. "
                    "Omitted means the contract probe is skipped rather than guessed."
                ),
            )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config = Config.from_env()
    except ConfigError as exc:
        sys.stderr.write(f"FATAL: invalid configuration: {exc}\n")
        return EXIT_CONFIG

    handler = COMMANDS[args.command]
    try:
        return handler(config, args)
    except DatabaseError as exc:
        _emit({"result": "DATABASE_ERROR", "error": str(exc)})
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
