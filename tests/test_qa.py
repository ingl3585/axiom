from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from axiom.qa import (
    analyze_bars_csv,
    analyze_bars_partition,
    analyze_realtime_dir,
    find_latest_bars_partition,
    parse_dt,
)


class QaTests(unittest.TestCase):
    def test_parse_dt_trims_projectx_fractional_precision(self) -> None:
        parsed = parse_dt("2026-06-03T20:56:00.6756059+00:00")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.microsecond, 675605)

    def test_analyze_bars_csv(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "unit=minute_1" / "sample.csv"
            path.parent.mkdir()
            path.write_text(
                "\n".join(
                    [
                        "t,o,h,l,c,v",
                        "2026-06-03T20:00:00+00:00,100,101,99,100.5,10",
                        "2026-06-03T20:01:00+00:00,100.5,102,100,101,0",
                        "2026-06-03T20:03:00+00:00,101,101.5,100.5,101.25,5",
                    ]
                ),
                encoding="utf-8",
            )

            report = analyze_bars_csv(path)
            self.assertEqual(report.rows, 3)
            self.assertEqual(report.zero_volume_bars, 1)
            self.assertEqual(report.calendar_gap_count, 1)
            self.assertEqual(report.max_gap_seconds, 120)

    def test_stitched_partition_dedupes_and_spans_windows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bars_root = Path(directory) / "bronze" / "projectx" / "bars"
            partition = bars_root / "contract=CON_F_US_MNQ_M26" / "unit=minute_1"
            partition.mkdir(parents=True)
            (partition / "window_a.csv").write_text(
                "\n".join(
                    [
                        "t,o,h,l,c,v",
                        "2026-06-03T20:00:00+00:00,100,101,99,100.5,10",
                        "2026-06-03T20:01:00+00:00,100.5,102,100,101,7",
                    ]
                ),
                encoding="utf-8",
            )
            # Overlapping window repeats 20:01 and extends to 20:02.
            (partition / "window_b.csv").write_text(
                "\n".join(
                    [
                        "t,o,h,l,c,v",
                        "2026-06-03T20:01:00+00:00,100.5,102,100,101,7",
                        "2026-06-03T20:02:00+00:00,101,101.5,100.5,101.25,5",
                    ]
                ),
                encoding="utf-8",
            )

            found = find_latest_bars_partition(bars_root)
            self.assertEqual(found, partition)

            report = analyze_bars_partition(partition)
            self.assertEqual(report.rows, 3)
            self.assertEqual(report.duplicate_timestamps, 0)
            self.assertEqual(report.expected_step_seconds, 60)
            self.assertEqual(report.calendar_gap_count, 0)

    def test_analyze_realtime_dir_flattens_batches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "quotes.jsonl").write_text(
                '{"observedAt":"2026-06-03T20:00:00Z","data":'
                '{"bestBid":100,"bestAsk":100.25,"timestamp":"2026-06-03T20:00:00Z"}}\n',
                encoding="utf-8",
            )
            (root / "trades.jsonl").write_text(
                '{"observedAt":"2026-06-03T20:00:01Z","data":['
                '{"price":100.25,"volume":2,"type":0,"timestamp":"2026-06-03T20:00:01Z"},'
                '{"price":100.5,"volume":3,"type":1,"timestamp":"2026-06-03T20:00:01Z"}]}\n',
                encoding="utf-8",
            )

            report = analyze_realtime_dir(root)
            by_name = {event.name: event for event in report.events}
            self.assertEqual(by_name["quotes"].payload_records, 1)
            self.assertEqual(by_name["quotes"].quote_spreads, [0.25])
            self.assertEqual(by_name["trades"].payload_records, 2)
            self.assertEqual(by_name["trades"].trade_volume, 5)

    def test_depth_splits_placeholder_and_missing_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # One real level, one book-snapshot sentinel (0001-01-01), one truly
            # missing timestamp. Placeholder and missing must be counted apart.
            (root / "depth.jsonl").write_text(
                '{"observedAt":"2026-06-03T20:00:00Z","data":['
                '{"price":100,"volume":5,"type":5,"timestamp":"2026-06-03T20:00:00Z"},'
                '{"price":101,"volume":3,"type":5,"timestamp":"0001-01-01T00:00:00+00:00"},'
                '{"price":102,"volume":2,"type":5,"timestamp":""}]}\n',
                encoding="utf-8",
            )

            report = analyze_realtime_dir(root)
            depth = {event.name: event for event in report.events}["depth"]
            self.assertEqual(depth.payload_records, 3)
            self.assertEqual(depth.placeholder_event_timestamps, 1)
            self.assertEqual(depth.missing_event_timestamps, 1)
            # Backward-compatible total still reflects both.
            self.assertEqual(depth.invalid_event_timestamps, 2)


if __name__ == "__main__":
    unittest.main()

