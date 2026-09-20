"""The sign-flip null, checked by hand where it can be and against brute
force where it cannot.

The script is pre-registered inference: a wrong p-value here would be quoted
as the verdict on a candidate. So the exact branch is checked against a full
enumeration written independently (itertools.product), the percentile
selection is checked at every rank, and the Monte Carlo branch is checked for
determinism and for the one case whose answer is known.
"""

from __future__ import annotations

import json
import random
from decimal import Decimal
from itertools import product
from pathlib import Path

import pytest

from scripts.sign_flip import (
    _half_sums,
    _kth_smallest,
    main,
    reading,
    segments_from_report,
    sign_flip,
)


class TestExactByHand:
    def test_three_segments_all_eight_configurations(self) -> None:
        """Segments +3, -1, +2, so G = 4. The eight sign vectors give
        4, 0, 6, 2, -2, -6, 0, -4 -> sorted -6 -4 -2 0 0 2 4 6.
        P(G* >= 4) = 2/8, P(G* <= 4) = 7/8, 5th pct = rank ceil(0.4) = 1 ->
        -6, 95th pct = rank ceil(7.6) = 8 -> 6."""
        result = sign_flip([Decimal(3), Decimal(-1), Decimal(2)])
        assert result.method == "exact"
        assert result.configurations == 8
        assert result.n == 3
        assert result.total == Decimal(4)
        assert result.mean == Decimal(4) / 3
        assert result.p_high == Decimal("0.25")
        assert result.p_low == Decimal("0.875")
        assert result.pct5 == Decimal(-6)
        assert result.pct95 == Decimal(6)

    def test_one_segment(self) -> None:
        """n=1 has two configurations: +g and -g. The empty half must count
        as one configuration, not zero, or 2^1 would come out as 0."""
        result = sign_flip([Decimal("1.5")])
        assert result.configurations == 2
        assert result.p_high == Decimal("0.5")
        assert result.p_low == Decimal(1)
        assert result.pct5 == Decimal("-1.5")
        assert result.pct95 == Decimal("1.5")

    def test_no_segments_is_refused(self) -> None:
        with pytest.raises(ValueError, match="no segments"):
            sign_flip([])


class TestExactAgainstBruteForce:
    """Meet-in-the-middle must agree with enumerating every sign vector."""

    @pytest.mark.parametrize("seed", [1, 2, 3])
    def test_p_values_and_every_percentile_rank(self, seed: int) -> None:
        rng = random.Random(seed)
        # Two decimals, a few exact zeros and repeats, so ties are exercised.
        pool = [Decimal(rng.randint(-500, 500)) / 100 for _ in range(6)] + [Decimal(0)]
        segments = [rng.choice(pool) for _ in range(9)]
        total = sum(segments, Decimal(0))
        every = sorted(
            sum((s * g for s, g in zip(signs, segments, strict=True)), Decimal(0))
            for signs in product((1, -1), repeat=len(segments))
        )
        assert len(every) == 512

        result = sign_flip(segments)
        assert result.configurations == 512
        assert result.p_high == Decimal(sum(1 for v in every if v >= total)) / 512
        assert result.p_low == Decimal(sum(1 for v in every if v <= total)) / 512
        assert result.pct5 == every[max(1, -(-5 * 512 // 100)) - 1]
        assert result.pct95 == every[max(1, -(-95 * 512 // 100)) - 1]

        a = _half_sums(segments[:4])
        b = _half_sums(segments[4:])
        for k in range(1, 513):
            assert _kth_smallest(a, b, k) == every[k - 1], k


class TestMonteCarlo:
    def test_above_thirty_segments_it_draws_and_is_reproducible(self) -> None:
        """Thirty-one segments of +1: G = 31 is the single best configuration,
        so p_high is essentially zero and p_low is exactly one. Two runs with
        the fixed seed must agree to the digit."""
        segments = [Decimal(1)] * 31
        first = sign_flip(segments, draws=2000)
        second = sign_flip(segments, draws=2000)
        assert first.method == "monte-carlo"
        assert first.configurations == 2000
        assert first.p_high < Decimal("0.01")
        assert first.p_low == Decimal(1)
        assert first.pct5 < 0 < first.pct95
        assert first == second

    def test_a_different_seed_is_a_different_draw(self) -> None:
        segments = [Decimal(i % 5 - 2) for i in range(31)]
        assert sign_flip(segments, draws=500, seed=1) != sign_flip(segments, draws=500, seed=2)


class TestSegments:
    def _doc(self, **overrides: object) -> dict[str, object]:
        doc: dict[str, object] = {
            "performance": {"final_unrealized": "-500"},
            "trades": {
                "detail": [
                    {"gross_pnl": "250", "quantity": 40},
                    {"gross_pnl": "-100", "quantity": 40},
                ],
                "detail_truncated": 0,
                "final_position": -40,
            },
        }
        doc.update(overrides)
        return doc

    def test_per_sol_closed_trades_then_the_open_tail(self) -> None:
        """250 / (25 x 40) = 0.25, -100 / 1000 = -0.1, and the open short's
        -500 / 1000 = -0.5 last."""
        assert segments_from_report(self._doc()) == [
            Decimal("0.25"),
            Decimal("-0.1"),
            Decimal("-0.5"),
        ]

    def test_a_flat_ending_has_no_tail(self) -> None:
        doc = self._doc()
        doc["trades"]["final_position"] = 0  # type: ignore[index]
        assert len(segments_from_report(doc)) == 2

    def test_a_truncated_trade_list_is_refused(self) -> None:
        doc = self._doc()
        doc["trades"]["detail_truncated"] = 3  # type: ignore[index]
        with pytest.raises(ValueError, match="truncated"):
            segments_from_report(doc)

    def test_an_open_tail_without_the_figure_is_refused(self) -> None:
        doc = self._doc(performance={})
        with pytest.raises(ValueError, match="final_unrealized"):
            segments_from_report(doc)


class TestReading:
    def test_below_fifteen_segments_nothing_may_be_read(self) -> None:
        result = sign_flip([Decimal(1)] * 3)
        assert any("neither S3 nor K1" in line for line in reading(result))

    def test_the_thresholds_are_the_pre_registered_ones(self) -> None:
        strong = sign_flip([Decimal(1)] * 15)  # p_high = 1 / 32768
        lines = reading(strong)
        assert "S3 (p_high < 0.05): holds" in lines
        assert "K1 (p_high >= 0.95, wrong side): does not fire" in lines
        wrong = sign_flip([Decimal(-1)] * 15)  # p_high = 1
        lines = reading(wrong)
        assert "S3 (p_high < 0.05): does not hold" in lines
        assert "K1 (p_high >= 0.95, wrong side): FIRES" in lines


class TestCommand:
    def test_prints_the_null_for_a_result_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "f0-sma-zerocost.json"
        path.write_text(
            json.dumps(
                {
                    # The hand-checked +3, -1, +2 of TestExactByHand, in
                    # 40-contract dollars: G = 4, p_high = 2/8.
                    "performance": {"final_unrealized": "2000"},
                    "trades": {
                        "detail": [
                            {"gross_pnl": "3000", "quantity": 40},
                            {"gross_pnl": "-1000", "quantity": 40},
                        ],
                        "detail_truncated": 0,
                        "final_position": 40,
                    },
                }
            )
        )
        assert main(["sign_flip.py", str(path)]) == 0
        out = capsys.readouterr().out
        assert "sign-flip null :: f0-sma-zerocost.json" in out
        assert "segments (n)          3" in out
        assert "exact, 8 sign vectors" in out
        assert "p_high = P(G* >= G)   0.2500" in out

    def test_usage_and_unreadable_input(self, tmp_path: Path) -> None:
        assert main(["sign_flip.py"]) == 2
        assert main(["sign_flip.py", str(tmp_path / "missing.json")]) == 2
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({"trades": {"detail": [], "detail_truncated": 0}}))
        assert main(["sign_flip.py", str(bad)]) == 1
