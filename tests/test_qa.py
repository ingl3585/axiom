from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from axiom.qa import analyze_bars_csv, analyze_realtime_dir, parse_dt


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


if __name__ == "__main__":
    unittest.main()

