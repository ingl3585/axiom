from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest

from axiom.qa import parse_dt
from axiom.session import analyze_session


class SessionHealthTests(unittest.TestCase):
    def test_analyze_session_summarizes_raw_and_live_features(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory) / "data"
            capture_dir = (
                data_dir
                / "raw"
                / "projectx"
                / "realtime"
                / "date=2026-06-03"
                / "contract=CON_F_US_MNQ_M26"
            )
            capture_dir.mkdir(parents=True)
            feature_dir = (
                data_dir
                / "live"
                / "projectx"
                / "features"
                / "date=2026-06-03"
                / "contract=CON_F_US_MNQ_M26"
            )
            feature_dir.mkdir(parents=True)

            write_jsonl(
                capture_dir / "quotes.jsonl",
                [
                    {
                        "observedAt": "2026-06-03T20:00:00Z",
                        "data": {
                            "bestBid": 100,
                            "bestAsk": 100.25,
                            "timestamp": "2026-06-03T20:00:00Z",
                        },
                    },
                    {
                        "observedAt": "2026-06-03T20:00:12Z",
                        "data": {
                            "bestBid": 100.5,
                            "bestAsk": 101,
                            "timestamp": "2026-06-03T20:00:12Z",
                        },
                    },
                ],
            )
            write_jsonl(
                capture_dir / "trades.jsonl",
                [
                    {
                        "observedAt": "2026-06-03T20:00:01Z",
                        "data": [
                            {
                                "price": 100.25,
                                "volume": 2,
                                "type": 0,
                                "timestamp": "2026-06-03T20:00:01Z",
                            },
                            {
                                "price": 100.5,
                                "volume": 3,
                                "type": 1,
                                "timestamp": "2026-06-03T20:00:01Z",
                            },
                        ],
                    }
                ],
            )
            write_jsonl(
                feature_dir / "features.jsonl",
                [
                    {
                        "timestamp": "2026-06-03T20:00:00Z",
                        "midPrice": 100.125,
                        "spread": 0.25,
                        "secondsSinceQuote": 0,
                    },
                    {
                        "timestamp": "2026-06-03T20:00:01Z",
                        "midPrice": 100.25,
                        "spread": 0.5,
                        "secondsSinceQuote": 6,
                    },
                    {
                        "timestamp": "2026-06-03T20:00:13Z",
                        "midPrice": 100.75,
                        "spread": 0.75,
                        "secondsSinceQuote": 1,
                    },
                ],
            )

            report = analyze_session(
                data_dir,
                directory=capture_dir,
                gap_threshold_seconds=10,
                stale_quote_seconds=5,
            )

            events = {event.name: event for event in report.events}
            self.assertEqual(events["quotes"].frames, 2)
            self.assertEqual(events["quotes"].gaps.count, 1)
            self.assertEqual(events["quotes"].gaps.max_seconds, 12)
            self.assertEqual(events["quotes"].spread_p95, 0.4875)
            self.assertEqual(events["trades"].payload_records, 2)
            self.assertEqual(events["trades"].trade_volume, 5)

            self.assertIsNotNone(report.features)
            assert report.features is not None
            self.assertEqual(report.features.rows, 3)
            self.assertEqual(report.features.gaps.count, 1)
            self.assertEqual(report.features.stale_quote_rows, 1)
            self.assertEqual(report.features.mid_price_max, 100.75)

            filtered = analyze_session(
                data_dir,
                directory=capture_dir,
                gap_threshold_seconds=10,
                stale_quote_seconds=5,
                observed_since=parse_dt("2026-06-03T20:00:10Z"),
            )

            filtered_events = {event.name: event for event in filtered.events}
            self.assertEqual(filtered_events["quotes"].frames, 1)
            self.assertEqual(filtered_events["quotes"].gaps.count, 0)
            self.assertEqual(filtered_events["quotes"].spread_avg, 0.5)
            self.assertEqual(filtered_events["trades"].payload_records, 0)

            self.assertIsNotNone(filtered.features)
            assert filtered.features is not None
            self.assertEqual(filtered.features.rows, 1)
            self.assertEqual(filtered.features.gaps.count, 0)
            self.assertEqual(filtered.features.stale_quote_rows, 0)
            self.assertEqual(filtered.features.mid_price_min, 100.75)


def write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(f"{json.dumps(row, separators=(',', ':'))}\n" for row in rows),
        encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()
