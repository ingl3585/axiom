from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import csv
import tempfile
import unittest

import _bootstrap  # noqa: F401
from bar_features import (
    BarFeatureConfig,
    build_bar_features,
    compute_bar_features,
    compute_order_flow,
    compute_session_reference,
    ema,
    model_feature_columns,
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


class SessionReferenceTests(unittest.TestCase):
    def test_opening_range_prior_day_overnight_gap_rvol(self) -> None:
        # All July 2026 (EDT, UTC-4): 13:30 UTC = 09:30 ET cash open.
        times = [
            datetime(2026, 7, 1, 13, 30, tzinfo=UTC),  # day1 09:30 (OR window)
            datetime(2026, 7, 1, 13, 45, tzinfo=UTC),  # day1 09:45 (OR window)
            datetime(2026, 7, 1, 14, 15, tzinfo=UTC),  # day1 10:15 (post-OR)
            datetime(2026, 7, 1, 22, 0, tzinfo=UTC),   # day1 18:00 overnight
            datetime(2026, 7, 2, 13, 30, tzinfo=UTC),  # day2 09:30 open
        ]
        opens = [100.0, 100.0, 108.0, 111.0, 100.0]
        highs = [105.0, 110.0, 112.0, 120.0, 101.0]
        lows = [95.0, 98.0, 107.0, 90.0, 99.0]
        closes = [100.0, 108.0, 111.0, 95.0, 100.0]
        volumes = [10.0, 20.0, 15.0, 5.0, 8.0]

        ref = compute_session_reference(times, opens, highs, lows, closes, volumes)

        # Post opening-range bar: OR = high/low of the first 30 min (110 / 95),
        # close 111 breaks out above it.
        self.assertEqual(ref[2]["or_high"], 110.0)
        self.assertEqual(ref[2]["or_low"], 95.0)
        self.assertEqual(ref[2]["or_breakout"], 1)
        self.assertEqual(ref[2]["prior_rth_high"], "")  # no prior day yet
        self.assertAlmostEqual(ref[2]["dist_round_100"], 11 / 111)
        self.assertAlmostEqual(ref[2]["dist_or_high"], 111 / 110 - 1)
        self.assertAlmostEqual(ref[2]["dist_or_low"], 111 / 95 - 1)

        # Overnight bar: reference levels blank, but rvol/round still computed.
        self.assertEqual(ref[3]["prior_rth_high"], "")
        self.assertEqual(ref[3]["or_breakout"], "")

        # Day 2 open: prior-day levels, overnight high/low, and the gap are known.
        self.assertEqual(ref[4]["prior_rth_high"], 112.0)
        self.assertEqual(ref[4]["prior_rth_low"], 95.0)
        self.assertEqual(ref[4]["prior_rth_close"], 111.0)
        self.assertEqual(ref[4]["overnight_high"], 120.0)
        self.assertEqual(ref[4]["overnight_low"], 90.0)
        self.assertAlmostEqual(ref[4]["gap"], 100 / 111 - 1)
        self.assertAlmostEqual(ref[4]["dist_prior_high"], 100 / 112 - 1)
        self.assertAlmostEqual(ref[4]["dist_prior_close"], 100 / 111 - 1)
        self.assertAlmostEqual(ref[4]["dist_overnight_high"], 100 / 120 - 1)
        self.assertAlmostEqual(ref[4]["dist_overnight_low"], 100 / 90 - 1)
        self.assertEqual(ref[4]["or_breakout"], "")  # OR still forming at the open
        # rvol: 09:30 volume 8 vs the only prior 09:30 bar (volume 10) = 0.8.
        self.assertAlmostEqual(ref[4]["rvol"], 0.8)


class OrderFlowTests(unittest.TestCase):
    def test_delta_ratio_and_session_cumulative(self) -> None:
        buy = [5.0, 1.0, None, 4.0]
        sell = [2.0, 3.0, None, 1.0]
        volumes = [7.0, 4.0, 10.0, 5.0]
        sessions = ["2026-06-04", "2026-06-04", "2026-06-04", "2026-06-05"]

        out = compute_order_flow(buy, sell, volumes, sessions)

        self.assertEqual(out[0]["delta"], 3.0)  # 5 - 2
        self.assertAlmostEqual(out[0]["delta_ratio"], 3.0 / 7.0)
        self.assertEqual(out[0]["cum_delta"], 3.0)
        self.assertEqual(out[1]["delta"], -2.0)  # 1 - 3
        self.assertEqual(out[1]["cum_delta"], 1.0)  # 3 + (-2)
        # No aggressor data (e.g. an API history bar) -> blank, cumulative held.
        self.assertEqual(out[2]["delta"], "")
        self.assertEqual(out[2]["cum_delta"], "")
        # New session resets the running delta.
        self.assertEqual(out[3]["delta"], 3.0)  # 4 - 1
        self.assertEqual(out[3]["cum_delta"], 3.0)
        # cum_delta_ratio normalizes the running delta by session volume.
        self.assertAlmostEqual(out[0]["cum_delta_ratio"], 3.0 / 7.0)
        self.assertAlmostEqual(out[1]["cum_delta_ratio"], 1.0 / 11.0)  # 1 / (7+4)
        self.assertEqual(out[2]["cum_delta_ratio"], "")
        self.assertAlmostEqual(out[3]["cum_delta_ratio"], 3.0 / 5.0)  # session reset


class ModelColumnsTests(unittest.TestCase):
    def test_excludes_raw_levels_keeps_stationary(self) -> None:
        cols = set(model_feature_columns([5, 20, 60]))
        # Stationary distances / ratios / oscillators are kept.
        for name in (
            "dist_vwap", "dist_ema_9", "dist_prior_close", "dist_or_high",
            "delta_ratio", "cum_delta_ratio", "return_5bar", "rsi_9", "is_rth",
        ):
            self.assertIn(name, cols)
        # Identifiers and raw price/volume levels are excluded.
        for name in (
            "t", "o", "c", "v", "vwap", "ema_9", "prior_rth_high",
            "overnight_low", "or_high", "delta", "cum_delta",
        ):
            self.assertNotIn(name, cols)


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
