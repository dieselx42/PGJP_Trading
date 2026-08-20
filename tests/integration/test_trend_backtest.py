"""The trend strategy end to end through `app.cli backtest`.

The unit tests prove the strategy's rules in isolation. This file proves the
one thing they cannot: that a `sol-trend` replay through the real command
threads signal -> risk -> gate -> fill -> `on_fill` -> attribution, so the
report's trade carries the direction and exit reason the strategy declared.
A wiring mistake anywhere in that chain -- a strategy that never sees its
fill, metadata dropped between order and trade -- produces a report that is
plausible and wrong, which is the failure mode worth an integration test.

The bar series is 25 UTC days, a few 1-minute bars each: 21 flat days to
warm up the 20-day channel and ATR, one breakout day, a run-up, then a
reversal deep enough to spring the trail. One trade, fully attributable,
checked by hand:

    warmup TR = 2/day, breakout TR = 5  ->  ATR(20) = (19*2 + 5)/20 = 2.15
    trail = 3 * 2.15 = 6.45 behind the peak of 110  ->  stop 103.55
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


def _bar(at: datetime, open_: str, high: str, low: str, close: str) -> Bar:
    return Bar(
        source="binance",
        symbol="SOLUSDT",
        interval="1m",
        opened_at=at,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
    )


def _series() -> list[Bar]:
    bars: list[Bar] = []
    # Days 0-20: flat. High 101, low 99, close 100 -> TR 2 against the
    # previous close of 100, every day.
    for day in range(21):
        at = DAY0 + timedelta(days=day)
        bars.append(_bar(at, "100", "101", "99", "100"))
    # Day 21: closes at 103, above the 101 channel. TR 5 (high 105, low 100).
    bars.append(_bar(DAY0 + timedelta(days=21), "100", "105", "100", "103"))
    # Day 22: first bar completes day 21 -> the entry intent fires here; the
    # second bar's open is the fill. Then the run-up to a 110 peak.
    d22 = DAY0 + timedelta(days=22)
    bars.append(_bar(d22, "103.5", "104", "103.4", "103.8"))
    bars.append(_bar(d22 + timedelta(minutes=1), "103.5", "104", "103.4", "103.9"))
    bars.append(_bar(d22 + timedelta(minutes=2), "104", "106", "104", "106"))
    d23 = DAY0 + timedelta(days=23)
    bars.append(_bar(d23, "106", "110", "106", "109"))
    # Day 24: reversal through the trailed stop at 103.55 (peak 110 minus
    # 3 x ATR 2.15). The exit intent fires on the breach bar; the next bar's
    # open is the exit fill.
    d24 = DAY0 + timedelta(days=24)
    bars.append(_bar(d24, "108", "108", "103", "103.2"))
    bars.append(_bar(d24 + timedelta(minutes=1), "103.2", "103.5", "102.8", "103"))
    bars.append(_bar(d24 + timedelta(minutes=2), "103", "103.2", "102.5", "102.6"))
    return bars


@pytest.fixture
def trend_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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


class TestTrendThroughTheCommand:
    def test_one_trade_fully_attributed(
        self, trend_env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(
            [
                "backtest",
                "--source",
                "binance",
                "--symbol",
                "SOLUSDT",
                "--strategy",
                "sol-trend",
                # The series is UTC-midnight bars; the CME liquid-hours filter
                # would drop them all. `--session all` is also what the compare
                # script runs, so the test exercises the same path.
                "--session",
                "all",
                # One contract, against one-contract limits -- and the same
                # `--strategy-params` path the compare script drives.
                "--strategy-params",
                "position_contracts=1",
                "--max-order-size",
                "1",
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
        )
        report = json.loads(capsys.readouterr().out)
        assert code == EXIT_OK

        # One round trip, and the metadata the strategy declared survived the
        # whole order -> fill -> trade chain.
        assert report["trades"]["count"] == 1
        by_session = {row["bucket"]: row for row in report["attribution"]["by_session"]}
        assert "long" in by_session
        by_exit = {row["bucket"]: row for row in report["attribution"]["by_exit_reason"]}
        assert "trail" in by_exit

        counters = report["strategy"]["counters"]
        assert counters["entries_long"] == 1
        assert counters["exits_trail"] == 1
        assert counters["exits_stop"] == 0
        assert counters["days_completed"] == 24

        # What matters here is not the trade's sign but the accounting: a
        # 1-contract round trip pays exactly the modelled costs, and net
        # reconciles with the attribution's gross. Slippage lives INSIDE the
        # fill prices (bar open +/- one tick), so gross already carries it and
        # only commission is subtracted separately: net = gross - commission.
        # Checked by hand: in at 103.50+0.05, out at 103.20-0.05, so gross =
        # (103.15 - 103.55) * 25 = -10.00 and net = -16.82.
        perf = report["performance"]
        net = Decimal(perf["net_pnl"])
        assert Decimal(perf["commission_paid"]) == Decimal("6.82")
        assert Decimal(perf["slippage_paid"]) == Decimal("2.50")
        assert net + Decimal(perf["commission_paid"]) == Decimal(by_exit["trail"]["gross"])
        assert Decimal(by_exit["trail"]["gross"]) == Decimal("-10.00")
