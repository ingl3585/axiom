from __future__ import annotations

from pathlib import Path
import csv
import tempfile
import unittest

from axiom.backtest import BacktestConfig, run_backtest


class BacktestTests(unittest.TestCase):
    def test_run_backtest_scores_baseline_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "features_1s.csv"
            write_feature_rows(path)

            report = run_backtest(
                BacktestConfig(
                    path=path,
                    horizon_seconds=5,
                    signal_window_seconds=5,
                    tick_size=0.25,
                    cost_ticks=0.0,
                    cooldown_seconds=0,
                    imbalance_threshold=0.2,
                )
            )

            by_name = {candidate.name: candidate for candidate in report.candidates}
            momentum = by_name["momentum_5s"]
            reversion = by_name["mean_reversion_5s"]
            flow = by_name["order_flow_follow_5s"]

            self.assertEqual(report.rows, 6)
            self.assertEqual(momentum.trade_count, 6)
            self.assertEqual(momentum.long_count, 3)
            self.assertEqual(momentum.short_count, 3)
            self.assertGreater(momentum.avg_net_ticks(), 0)
            self.assertEqual(momentum.win_rate(), 1.0)

            self.assertLess(reversion.avg_net_ticks(), 0)
            self.assertEqual(reversion.win_rate(), 0.0)

            self.assertEqual(flow.trade_count, 5)
            self.assertIn("momentum_5s", report.to_markdown())


def write_feature_rows(path: Path) -> None:
    fieldnames = [
        "timestamp",
        "contract",
        "interval_seconds",
        "mid_price",
        "best_bid",
        "best_ask",
        "return_5s",
        "avg_spread_5s",
        "trade_type0_1_imbalance_5s",
        "forward_return_5s",
        "forward_mfe_ticks_5s",
        "forward_mae_ticks_5s",
    ]
    rows = [
        ("00", 100.0, 0.005, 0.4, 0.005, 2.0, 0.0),
        ("01", 100.0, 0.005, 0.4, 0.005, 2.0, 0.0),
        ("02", 100.0, 0.005, 0.4, 0.005, 2.0, 0.0),
        ("03", 100.0, -0.005, -0.4, -0.005, 0.0, -2.0),
        ("04", 100.0, -0.005, -0.4, -0.005, 0.0, -2.0),
        ("05", 100.0, -0.005, 0.0, -0.005, 0.0, -2.0),
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for second, mid, trailing, imbalance, forward, mfe, mae in rows:
            writer.writerow(
                {
                    "timestamp": f"2026-06-04T20:00:{second}Z",
                    "contract": "CON_F_US_MNQ_M26",
                    "interval_seconds": 1,
                    "mid_price": mid,
                    "best_bid": mid - 0.125,
                    "best_ask": mid + 0.125,
                    "return_5s": trailing,
                    "avg_spread_5s": 0.5,
                    "trade_type0_1_imbalance_5s": imbalance,
                    "forward_return_5s": forward,
                    "forward_mfe_ticks_5s": mfe,
                    "forward_mae_ticks_5s": mae,
                }
            )


if __name__ == "__main__":
    unittest.main()
