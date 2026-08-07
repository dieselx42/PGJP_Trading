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
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Sequence
from typing import Any

from app.config import Config, ConfigError
from app.enums import TradingMode
from app.monitoring.status import build_info, safety_summary
from app.safety.killswitch import KillSwitch
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
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="solbot-admin",
        description=(
            "Operator commands for sol-futures-trading-bot. "
            "No command in this tool can enable trading or clear the kill switch."
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
