from __future__ import annotations

from pathlib import Path
import csv
import tempfile
import unittest

import _bootstrap  # noqa: F401
from axiom.playbook import PlaybookConfig, evaluate_playbook


class PlaybookTests(unittest.TestCase):
    def test_evaluate_playbook_scores_exhaustion_reversal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "features_1s.csv"
            write_feature_rows(path)

            report = evaluate_playbook(
                PlaybookConfig(
                    path=path,
                    horizon_seconds=30,
                    tick_size=0.25,
                    cost_ticks=2.0,
                    cooldown_seconds=0,
                    min_impulse_ticks=12.0,
                    min_trigger_ticks=2.0,
                    min_flow_imbalance=0.20,
                    min_trigger_volume=20.0,
                    max_spread_ticks=2.0,
                )
            )

            result = report.result
            self.assertEqual(report.rows, 4)
            self.assertEqual(result.trade_count, 2)
            self.assertEqual(result.long_count, 1)
            self.assertEqual(result.short_count, 1)
            self.assertAlmostEqual(result.total_net_ticks(), 10.0)
            self.assertEqual(result.reason_counts["candidate"], 2)
            self.assertEqual(result.reason_counts["flow_not_confirming"], 1)
            self.assertEqual(result.reason_counts["impulse_too_small"], 1)
            self.assertIn("exhaustion_reversal", report.to_markdown())


def write_feature_rows(path: Path) -> None:
    fieldnames = [
        "timestamp",
        "contract",
        "interval_seconds",
        "mid_price",
        "best_bid",
        "best_ask",
        "return_5s",
        "return_30s",
        "avg_spread_5s",
        "trade_volume_5s",
        "trade_type0_1_imbalance_5s",
        "forward_return_30s",
        "forward_mfe_ticks_30s",
        "forward_mae_ticks_30s",
    ]
    rows = [
        ("00", 100.0, 0.0400, -0.0075, -0.30, 40.0, -0.0175, 5.0, -1.0),
        ("01", 100.0, -0.0400, 0.0075, 0.30, 40.0, 0.0175, 5.0, -1.0),
        ("02", 100.0, 0.0400, -0.0075, 0.10, 40.0, -0.0175, 5.0, -1.0),
        ("03", 100.0, 0.0010, -0.0075, -0.30, 40.0, -0.0175, 5.0, -1.0),
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for second, mid, impulse, trigger, flow, volume, forward, mfe, mae in rows:
            writer.writerow(
                {
                    "timestamp": f"2026-06-04T20:00:{second}Z",
                    "contract": "CON_F_US_MNQ_M26",
                    "interval_seconds": 1,
                    "mid_price": mid,
                    "best_bid": mid - 0.125,
                    "best_ask": mid + 0.125,
                    "return_5s": trigger,
                    "return_30s": impulse,
                    "avg_spread_5s": 0.5,
                    "trade_volume_5s": volume,
                    "trade_type0_1_imbalance_5s": flow,
                    "forward_return_30s": forward,
                    "forward_mfe_ticks_30s": mfe,
                    "forward_mae_ticks_30s": mae,
                }
            )


if __name__ == "__main__":
    unittest.main()
