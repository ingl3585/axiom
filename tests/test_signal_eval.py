from __future__ import annotations

from pathlib import Path
import csv
import json
import tempfile
import unittest

from axiom.signal_eval import SignalEvaluationConfig, evaluate_signal_file


class SignalEvaluationTests(unittest.TestCase):
    def test_evaluate_signal_file_scores_candidates_and_reasons(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            signal_path = root / "signals.jsonl"
            feature_path = root / "features_1s.csv"
            write_signals(signal_path)
            write_features(feature_path)

            report = evaluate_signal_file(
                SignalEvaluationConfig(
                    signal_path=signal_path,
                    feature_path=feature_path,
                    horizon_seconds=30,
                    tick_size=0.25,
                    cost_ticks=2.0,
                    max_match_lag_seconds=1.5,
                )
            )

            self.assertEqual(report.total_signals, 4)
            self.assertEqual(report.candidates, 3)
            self.assertEqual(report.evaluated_candidates, 2)
            self.assertEqual(report.unmatched_candidates, 1)
            self.assertEqual(report.reason_counts["cooldown"], 1)

            result = report.policy_results[0]
            self.assertEqual(result.name, "momentum_5s")
            self.assertEqual(result.trade_count, 2)
            self.assertEqual(result.long_count, 1)
            self.assertEqual(result.short_count, 1)
            self.assertEqual(result.total_net_ticks(), 4.0)
            self.assertIn("momentum_5s", report.to_markdown())


def write_signals(path: Path) -> None:
    rows = [
        {
            "timestamp": "2026-06-04T20:00:00.200Z",
            "policy": "momentum_5s",
            "action": "LONG_CANDIDATE",
            "direction": 1,
            "reason": "momentum",
        },
        {
            "timestamp": "2026-06-04T20:00:01.200Z",
            "policy": "momentum_5s",
            "action": "SHORT_CANDIDATE",
            "direction": -1,
            "reason": "momentum",
        },
        {
            "timestamp": "2026-06-04T20:00:02.000Z",
            "policy": "momentum_5s",
            "action": "NO_TRADE",
            "direction": 0,
            "reason": "cooldown",
        },
        {
            "timestamp": "2026-06-04T20:10:00.000Z",
            "policy": "momentum_5s",
            "action": "LONG_CANDIDATE",
            "direction": 1,
            "reason": "momentum",
        },
    ]
    path.write_text(
        "".join(f"{json.dumps(row, separators=(',', ':'))}\n" for row in rows),
        encoding="utf-8",
    )


def write_features(path: Path) -> None:
    fieldnames = [
        "timestamp",
        "mid_price",
        "forward_return_30s",
        "forward_mfe_ticks_30s",
        "forward_mae_ticks_30s",
    ]
    rows = [
        {
            "timestamp": "2026-06-04T20:00:00Z",
            "mid_price": 100.0,
            "forward_return_30s": 0.01,
            "forward_mfe_ticks_30s": 5,
            "forward_mae_ticks_30s": -1,
        },
        {
            "timestamp": "2026-06-04T20:00:01Z",
            "mid_price": 100.0,
            "forward_return_30s": -0.01,
            "forward_mfe_ticks_30s": 1,
            "forward_mae_ticks_30s": -5,
        },
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    unittest.main()
