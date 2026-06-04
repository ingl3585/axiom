from __future__ import annotations

from pathlib import Path
import csv
import json
import tempfile
import unittest

import _bootstrap  # noqa: F401
from axiom.normalize import normalize_bars_history_json, normalize_realtime_dir


class NormalizeTests(unittest.TestCase):
    def test_normalize_bars_history_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "raw.json"
            raw.write_text(
                json.dumps(
                    {
                        "contractId": "CON.F.US.MNQ.M26",
                        "startTime": "2026-06-03T20:00:00Z",
                        "endTime": "2026-06-03T20:02:00Z",
                        "unit": 2,
                        "unitNumber": 1,
                        "bars": [
                            {
                                "t": "2026-06-03T20:00:00+00:00",
                                "o": 100,
                                "h": 101,
                                "l": 99,
                                "c": 100.5,
                                "v": 10,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            output = normalize_bars_history_json(raw, root / "data")
            self.assertEqual(output.rows, 1)
            self.assertTrue(output.path.exists())
            with output.path.open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["c"], "100.5")

    def test_normalize_realtime_dir_flattens_batches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw_dir = (
                root
                / "data"
                / "raw"
                / "projectx"
                / "realtime"
                / "date=2026-06-03"
                / "contract=CON_F_US_MNQ_M26"
            )
            raw_dir.mkdir(parents=True)
            (raw_dir / "quotes.jsonl").write_text(
                '{"observedAt":"2026-06-03T20:00:00Z","contractId":"CON.F.US.MNQ.M26",'
                '"data":{"symbol":"F.US.MNQ","bestBid":100,"bestAsk":100.25,'
                '"lastUpdated":"2026-06-03T20:00:00Z","contract":"CON.F.US.MNQ.M26"}}\n',
                encoding="utf-8",
            )
            (raw_dir / "trades.jsonl").write_text(
                '{"observedAt":"2026-06-03T20:00:01Z","contractId":"CON.F.US.MNQ.M26",'
                '"data":['
                '{"symbolId":"F.US.MNQ","price":100.25,"volume":2,"type":0,'
                '"timestamp":"2026-06-03T20:00:01Z","contractId":"CON.F.US.MNQ.M26"},'
                '{"symbolId":"F.US.MNQ","price":100.5,"volume":3,"type":1,'
                '"timestamp":"2026-06-03T20:00:01Z","contractId":"CON.F.US.MNQ.M26"}]}\n',
                encoding="utf-8",
            )
            (raw_dir / "depth.jsonl").write_text(
                '{"observedAt":"2026-06-03T20:00:02Z","contractId":"CON.F.US.MNQ.M26",'
                '"data":[{"price":100,"volume":9,"currentVolume":0,"type":4,'
                '"timestamp":"2026-06-03T20:00:02Z"}]}\n',
                encoding="utf-8",
            )

            outputs = normalize_realtime_dir(raw_dir, root / "data")
            by_name = {output.name: output for output in outputs}
            self.assertEqual(by_name["quotes"].rows, 1)
            self.assertEqual(by_name["trades"].rows, 2)
            self.assertEqual(by_name["depth"].rows, 1)

            with by_name["trades"].path.open(encoding="utf-8") as handle:
                trade_rows = list(csv.DictReader(handle))
            self.assertEqual(trade_rows[1]["volume"], "3")
            self.assertEqual(trade_rows[1]["trade_type"], "1")


if __name__ == "__main__":
    unittest.main()
