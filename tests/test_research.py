from __future__ import annotations

from pathlib import Path
import csv
import tempfile
import unittest

from axiom.research import analyze_feature_ic, pearson, spearman


class ResearchTests(unittest.TestCase):
    def test_pearson_perfect_correlation(self) -> None:
        xs = [1.0, 2.0, 3.0, 4.0]
        ys = [2.0, 4.0, 6.0, 8.0]
        self.assertAlmostEqual(pearson(xs, ys), 1.0)
        self.assertAlmostEqual(pearson(xs, list(reversed(ys))), -1.0)

    def test_spearman_handles_monotonic_nonlinear(self) -> None:
        xs = [1.0, 2.0, 3.0, 4.0]
        ys = [1.0, 4.0, 9.0, 16.0]
        # Monotonic but non-linear: Spearman is exactly 1, Pearson is below 1.
        self.assertAlmostEqual(spearman(xs, ys), 1.0)
        self.assertLess(pearson(xs, ys), 1.0)

    def test_analyze_feature_ic_ranks_predictive_feature(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "features_1s.csv"
            fieldnames = [
                "timestamp",
                "contract",
                "mid_price",
                "signal",
                "noise",
                "forward_return_5s",
            ]
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                for i in range(40):
                    writer.writerow(
                        {
                            "timestamp": f"2026-06-03T20:00:{i:02d}Z",
                            "contract": "CON_F_US_MNQ_M26",
                            "mid_price": 100 + i,
                            "signal": i,
                            "noise": (i * 7) % 5,
                            "forward_return_5s": i * 0.001,
                        }
                    )

            report = analyze_feature_ic(path, min_samples=10)

            self.assertEqual(report.rows, 40)
            self.assertIn("signal", report.features)
            self.assertNotIn("mid_price", report.features)
            self.assertEqual(report.labels, ["forward_return_5s"])

            by_feature = {
                stat.feature: stat
                for stat in report.stats
                if stat.label == "forward_return_5s"
            }
            self.assertAlmostEqual(by_feature["signal"].spearman, 1.0)
            self.assertGreater(
                by_feature["signal"].top_quintile_mean,
                by_feature["signal"].bottom_quintile_mean,
            )


if __name__ == "__main__":
    unittest.main()
