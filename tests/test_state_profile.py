from __future__ import annotations

from pathlib import Path
import csv
import json
import tempfile
import unittest

import _bootstrap  # noqa: F401
from state_profile import (
    ProfileThresholds,
    StateProfileConfig,
    build_state_profile,
    classify_market_state,
    forward_outcome,
)


def feature_row(
    index: int,
    close: float,
    *,
    return_20bar: float = 0.01,
    dist_ema_9: float = 0.002,
    dist_ema_21: float = 0.001,
    vol_20bar: float = 0.002,
    vol_ratio_20bar: float = 1.2,
    dist_vwap: float = 0.001,
    vwap_sigma: float = 0.5,
    delta_ratio: float | str = "",
    rsi_9: float = 60.0,
    or_breakout: int | str = 0,
) -> dict[str, object]:
    return {
        "t": f"2026-06-04T20:{index:02d}:00Z",
        "o": close,
        "h": close + 1,
        "l": close - 1,
        "c": close,
        "v": 100,
        "session_bucket": "midday",
        "return_20bar": return_20bar,
        "dist_ema_9": dist_ema_9,
        "dist_ema_21": dist_ema_21,
        "vol_20bar": vol_20bar,
        "vol_ratio_20bar": vol_ratio_20bar,
        "dist_vwap": dist_vwap,
        "vwap_sigma": vwap_sigma,
        "delta_ratio": delta_ratio,
        "cum_delta_ratio": "",
        "rsi_9": rsi_9,
        "or_breakout": or_breakout,
    }


class StateClassificationTests(unittest.TestCase):
    def test_classifies_market_dimensions(self) -> None:
        row = feature_row(
            0,
            100.0,
            vol_20bar=0.005,
            vol_ratio_20bar=1.8,
            vwap_sigma=1.8,
            delta_ratio=0.35,
            rsi_9=76,
            or_breakout=1,
        )

        state = classify_market_state(
            {key: str(value) for key, value in row.items()},
            ProfileThresholds(volatility_low=0.001, volatility_high=0.003),
        )

        self.assertEqual(state.trend_state, "trend_up")
        self.assertEqual(state.volatility_state, "vol_high")
        self.assertEqual(state.activity_state, "activity_high")
        self.assertEqual(state.location_state, "extreme_above_vwap")
        self.assertEqual(state.structure_state, "or_breakout_up")
        self.assertEqual(state.flow_state, "buy_pressure")
        self.assertEqual(state.rsi_state, "overbought")

    def test_forward_outcome_uses_future_highs_lows_and_close(self) -> None:
        rows = [
            {key: str(value) for key, value in feature_row(0, 100.0).items()},
            {key: str(value) for key, value in feature_row(1, 101.0).items()},
            {key: str(value) for key, value in feature_row(2, 102.0).items()},
        ]

        outcome = forward_outcome(rows, index=0, horizon_bars=2, tick_size=0.25)

        self.assertIsNotNone(outcome)
        assert outcome is not None
        self.assertAlmostEqual(outcome.forward_ticks, 8.0)
        self.assertAlmostEqual(outcome.mfe_ticks, 12.0)
        self.assertAlmostEqual(outcome.mae_ticks, 0.0)


class BuildStateProfileTests(unittest.TestCase):
    def test_builds_state_rows_and_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory) / "data"
            feature_path = (
                data_dir
                / "silver"
                / "projectx"
                / "features"
                / "bars"
                / "contract=CON_F_US_MNQ_M26"
                / "unit=minute_1"
                / "features.csv"
            )
            feature_path.parent.mkdir(parents=True)
            rows = [feature_row(index, 100.0 + index) for index in range(6)]
            with feature_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)

            result = build_state_profile(
                StateProfileConfig(
                    data_dir=data_dir,
                    feature_path=feature_path,
                    horizon_bars=2,
                    tick_size=0.25,
                    min_count=1,
                )
            )

            self.assertEqual(result.rows, 6)
            self.assertEqual(result.labeled_rows, 4)
            self.assertTrue(result.rows_path.exists())
            self.assertTrue(result.markdown_path.exists())
            self.assertTrue(result.json_path.exists())

            with result.rows_path.open(encoding="utf-8") as handle:
                state_rows = list(csv.DictReader(handle))
            self.assertEqual(len(state_rows), 6)
            self.assertEqual(state_rows[0]["forward_ticks_2bar"], "8.0")
            self.assertIn("trend_up", state_rows[0]["state_key"])

            payload = json.loads(result.json_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["labeled_rows"], 4)
            self.assertTrue(payload["state_summaries"])


if __name__ == "__main__":
    unittest.main()
