from __future__ import annotations

from pathlib import Path
import csv
import tempfile
import unittest

import _bootstrap  # noqa: F401
from bar_features import (
    BarFeatureConfig,
    build_bar_features,
    compute_bar_features,
    ema,
    rsi,
    vwap_by_session,
)
from projectx import BarUnit


def make_bars() -> list[dict[str, str]]:
    # Closes 10..14, high = close+1, low = close-1, constant volume.
    bars = []
    for i, close in enumerate([10.0, 11.0, 12.0, 13.0, 14.0]):
        bars.append(
            {
                "t": f"2026-06-04T20:0{i}:00Z",
                "o": str(close),
                "h": str(close + 1),
                "l": str(close - 1),
                "c": str(close),
                "v": "100",
            }
        )
    return bars


class ComputeBarFeaturesTests(unittest.TestCase):
    def test_trailing_indicators(self) -> None:
        rows = compute_bar_features(make_bars(), windows=[2])

        self.assertEqual(len(rows), 5)
        # Window features are blank until there is enough history.
        self.assertEqual(rows[0]["return_2bar"], "")

        third = rows[2]
        self.assertAlmostEqual(third["return_1"], 12 / 11 - 1)
        self.assertAlmostEqual(third["return_2bar"], 12 / 10 - 1)  # 0.2 momentum
        # close 12 sits 2/3 up the [10, 13] high/low range of the last 2 bars.
        self.assertAlmostEqual(third["range_pos_2bar"], 2 / 3)
        # Constant volume -> ratio to its own average is 1.
        self.assertAlmostEqual(third["vol_ratio_2bar"], 1.0)
        # Bar range = (high - low) / close = 2 / 12.
        self.assertAlmostEqual(third["bar_range"], 2 / 12)
        # RSI / EMA columns exist (blank here — only 5 bars, periods 9/9/21).
        self.assertIn("rsi_9", third)
        self.assertIn("ema_9", third)
        self.assertIn("ema_21", third)
        # VWAP: typical price == close here, constant volume, all one session,
        # so VWAP is the running mean of closes: (10+11+12)/3 = 11.
        self.assertAlmostEqual(third["vwap"], 11.0)
        self.assertAlmostEqual(third["dist_vwap"], 12 / 11 - 1)

    def test_no_lookahead_leaves_early_windows_blank(self) -> None:
        rows = compute_bar_features(make_bars(), windows=[60])
        for row in rows:
            self.assertEqual(row["return_60bar"], "")
            self.assertEqual(row["vol_60bar"], "")


class EmaRsiTests(unittest.TestCase):
    def test_ema_seeds_with_sma_then_smooths(self) -> None:
        out = ema([10.0, 11.0, 12.0, 13.0, 14.0], period=2)
        self.assertIsNone(out[0])
        self.assertAlmostEqual(out[1], 10.5)  # SMA of first 2 = seed
        self.assertAlmostEqual(out[2], 11.5)  # 2/3*12 + 1/3*10.5

    def test_rsi_extremes(self) -> None:
        rising = rsi([1.0, 2.0, 3.0, 4.0, 5.0], period=2)
        self.assertIsNone(rising[1])  # not enough history yet
        self.assertEqual(rising[4], 100.0)  # only gains -> 100
        falling = rsi([5.0, 4.0, 3.0, 2.0, 1.0], period=2)
        self.assertEqual(falling[4], 0.0)  # only losses -> 0


class VwapTests(unittest.TestCase):
    def test_resets_each_session(self) -> None:
        # h == l == c, so typical price equals close.
        highs = lows = closes = [10.0, 20.0, 30.0]
        volumes = [1.0, 3.0, 2.0]
        sessions = ["2026-06-04", "2026-06-04", "2026-06-05"]
        out = vwap_by_session(highs, lows, closes, volumes, sessions)
        self.assertAlmostEqual(out[0], 10.0)
        self.assertAlmostEqual(out[1], (10 * 1 + 20 * 3) / 4)  # 17.5, same session
        self.assertAlmostEqual(out[2], 30.0)  # new session resets the accumulation


class BuildBarFeaturesTests(unittest.TestCase):
    def test_builds_feature_table_from_partition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            partition = (
                data_dir / "bronze" / "projectx" / "bars"
                / "contract=CON_F_US_MNQ_M26" / "unit=minute_1"
            )
            partition.mkdir(parents=True)
            lines = ["t,o,h,l,c,v"]
            for i, close in enumerate([10.0, 11.0, 12.0, 13.0, 14.0]):
                lines.append(f"2026-06-04T20:0{i}:00+00:00,{close},{close + 1},{close - 1},{close},100")
            (partition / "window.csv").write_text("\n".join(lines), encoding="utf-8")

            result = build_bar_features(
                BarFeatureConfig(
                    data_dir=data_dir,
                    contract_part="contract=CON_F_US_MNQ_M26",
                    unit=BarUnit.MINUTE,
                    unit_number=1,
                    windows=[2],
                )
            )

            self.assertEqual(result.bars, 5)
            self.assertEqual(result.rows, 5)
            self.assertTrue(result.path.exists())
            with result.path.open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 5)
            self.assertIn("return_2bar", rows[0])


if __name__ == "__main__":
    unittest.main()
