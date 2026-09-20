"""The close-versus-average strategy end to end through `app.cli backtest`.

The unit tests prove the rule in isolation. This file proves the two things
they cannot, both of which would fail silently otherwise:

* a change of side is ONE order of twice the size, so it is refused by the
  order-size limit every other row runs under and passes only when that
  limit is raised -- the reason every sol-sma row carries
  ``--max-order-size`` at 2 x size;
* the flip intent's metadata threads through order -> fill -> book, so the
  closed long carries ``session="long"`` from its entry and
  ``exit_reason="flip"`` from the order that both closed it and opened the
  short -- and the open short's mark lands in ``performance.final_unrealized``.

The series is five UTC days at a 3-day window, checked by hand:

    days 0-2 close 100, 100, 103 -> SMA 101 < 103 -> long on day 3's first bar
    day 3 closes 95              -> SMA(100, 103, 95) = 99.33 > 95 -> short
    the flip fires on day 4's first bar and fills at the second bar's open
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.backtest.models import Bar
from app.backtest.store import BarRepository
from app.cli import EXIT_OK, main
from app.contracts.models import QualifiedContract
from app.enums import SecurityType
from app.state.database import Database
from app.state.repositories import Repositories
from tests.conftest import CONTRACT_MONTH, default_env

pytestmark = pytest.mark.integration

DAY0 = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)


def _bar(at: datetime, open_: str, close: str) -> Bar:
    o, c = Decimal(open_), Decimal(close)
    return Bar(
        source="binance",
        symbol="SOLUSDT",
        interval="1m",
        opened_at=at,
        open=o,
        high=max(o, c),
        low=min(o, c),
        close=c,
    )


def _series() -> list[Bar]:
    bars = [
        _bar(DAY0, "100", "100"),
        _bar(DAY0 + timedelta(days=1), "100", "100"),
        _bar(DAY0 + timedelta(days=2), "103", "103"),
    ]
    # Day 3: the first bar completes day 2 and carries the long intent; the
    # second bar's open is the fill (103.50 + one tick of slippage = 103.55);
    # the last bar closes the day at 95, below its average.
    d3 = DAY0 + timedelta(days=3)
    bars.append(_bar(d3, "103", "103"))
    bars.append(_bar(d3 + timedelta(minutes=1), "103.5", "103.5"))
    bars.append(_bar(d3 + timedelta(minutes=2), "103.5", "95"))
    # Day 4: the first bar completes day 3 and carries the flip; the second
    # bar's open is the fill (94 - one tick = 93.95); the third bar is where
    # the replay ends, holding the short marked at 94.
    d4 = DAY0 + timedelta(days=4)
    bars.append(_bar(d4, "95", "95"))
    bars.append(_bar(d4 + timedelta(minutes=1), "94", "94"))
    bars.append(_bar(d4 + timedelta(minutes=2), "94", "94"))
    return bars


@pytest.fixture
def sma_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "trading.db"
    database = Database(db_path)
    database.connect()
    database.migrate()
    Repositories(database).contracts.upsert(
        QualifiedContract(
            con_id=812345678,
            symbol="MSL",
            local_symbol="MSLZ6",
            sec_type=SecurityType.FUTURE,
            exchange="CME",
            currency="USD",
            expiration=CONTRACT_MONTH,
            last_trade_date="20261218",
            multiplier="25",
            min_tick=Decimal("0.05"),
            trading_class="MSL",
        )
    )
    BarRepository(database).insert_many(_series())
    database.close()
    for key, value in default_env(
        DATABASE_PATH=str(db_path),
        LOG_DIR=str(tmp_path / "logs"),
        DEFAULT_CONTRACT_MONTH=CONTRACT_MONTH,
        HEALTH_PORT="0",
    ).items():
        monkeypatch.setenv(key, value)
    return db_path


def _replay(max_order_size: str) -> list[str]:
    return [
        "backtest",
        "--source",
        "binance",
        "--symbol",
        "SOLUSDT",
        "--strategy",
        "sol-sma",
        # UTC-midnight bars; the CME filter would drop them all, and
        # `--session all` is what the compare script runs anyway.
        "--session",
        "all",
        "--strategy-params",
        "position_contracts=1,sma_days=3",
        "--max-order-size",
        max_order_size,
        "--max-position",
        "1",
        "--max-orders-per-hour",
        "60",
        "--max-open-orders",
        "2",
        "--max-daily-loss",
        "5000",
        "--max-notional",
        "100000",
    ]


class TestSmaThroughTheCommand:
    def test_the_flip_is_one_order_fully_attributed(
        self, sma_env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(_replay(max_order_size="2"))
        report = json.loads(capsys.readouterr().out)
        assert code == EXIT_OK
        assert report["refusals"]["count"] == 0

        # One closed round trip -- the long -- and the short still open.
        assert report["trades"]["count"] == 1
        assert report["trades"]["final_position"] == -1
        (trade,) = report["trades"]["detail"]
        assert trade["side"] == "LONG"
        assert trade["quantity"] == 1
        assert trade["session"] == "long"
        assert trade["trade_n"] == 1
        assert trade["exit_reason"] == "flip"
        by_exit = {row["bucket"]: row for row in report["attribution"]["by_exit_reason"]}
        assert set(by_exit) == {"flip"}

        # By hand: in at 103.55, out at 93.95 -> gross (93.95 - 103.55) x 25 =
        # -240.00. Commission is three contract-sides (1 in, 2 on the flip) =
        # 10.23, of which the closed trade carries its own entry (3.41) plus
        # half the flip's 6.82 -- a full 6.82 round trip -- and the other
        # half is the open short's entry. That short, in at 93.95 and marked
        # at 94, is the -1.25 in final_unrealized.
        perf = report["performance"]
        assert Decimal(trade["entry_price"]) == Decimal("103.55")
        assert Decimal(trade["exit_price"]) == Decimal("93.95")
        assert Decimal(trade["gross_pnl"]) == Decimal("-240.00")
        assert Decimal(trade["commission"]) == Decimal("6.82")
        assert Decimal(trade["net_pnl"]) == Decimal("-246.82")
        assert Decimal(perf["commission_paid"]) == Decimal("10.23")
        assert Decimal(perf["net_pnl"]) == Decimal("-250.23")
        assert Decimal(perf["final_unrealized"]) == Decimal("-1.25")
        assert any("final_unrealized" in note for note in report["limitations"])

        counters = report["strategy"]["counters"]
        assert counters["days_completed"] == 4
        assert counters["days_in_warmup"] == 2
        assert counters["targets_long"] == 1
        assert counters["targets_short"] == 1
        assert counters["flips"] == 1
        assert counters["orders_emitted"] == 2
        assert counters["orders_reemitted"] == 0
        assert counters["fills_off_target"] == 0
        assert report["strategy"]["target_position"] == -1
        assert report["strategy"]["signal_day"] == "2026-01-08"

    def test_the_flip_is_refused_at_the_single_contract_order_limit(
        self, sma_env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Same series, MAX_ORDER_SIZE 1: the entry passes, the 2-lot flip is
        refused on every bar it is re-emitted, and the long is never closed.
        This is why the harness rows raise the limit to 2 x size -- and why
        a live MAX_ORDER_SIZE of 1 would leave the rule stuck on one side."""
        code = main(_replay(max_order_size="1"))
        report = json.loads(capsys.readouterr().out)
        assert code == EXIT_OK
        assert report["trades"]["count"] == 0
        assert report["trades"]["final_position"] == 1
        assert report["refusals"]["count"] == 3, "re-emitted on each of day 4's bars"
        assert set(report["refusals"]["by_reason"]) == {"MAX_ORDER_SIZE_EXCEEDED"}
        assert report["strategy"]["counters"]["orders_reemitted"] == 2
