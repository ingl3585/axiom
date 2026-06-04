from __future__ import annotations

from pathlib import Path
from contextlib import redirect_stdout
import csv
import io
import tempfile
import unittest

import _bootstrap  # noqa: F401
from pipeline import finalize_realtime_capture


class PipelineFinalizeTests(unittest.TestCase):
    def test_finalize_realtime_capture_refreshes_bronze_and_silver(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory) / "data"
            raw_dir = (
                data_dir
                / "raw"
                / "projectx"
                / "realtime"
                / "date=2026-06-04"
                / "contract=CON_F_US_MNQ_M26"
            )
            raw_dir.mkdir(parents=True)
            (raw_dir / "quotes.jsonl").write_text(
                "\n".join(
                    [
                        (
                            '{"observedAt":"2026-06-04T20:00:00Z",'
                            '"contractId":"CON.F.US.MNQ.M26","data":'
                            '{"symbol":"F.US.MNQ","bestBid":100,"bestAsk":100.25,'
                            '"timestamp":"2026-06-04T20:00:00Z"}}'
                        ),
                        (
                            '{"observedAt":"2026-06-04T20:00:01Z",'
                            '"contractId":"CON.F.US.MNQ.M26","data":'
                            '{"symbol":"F.US.MNQ","bestBid":100.5,"bestAsk":100.75,'
                            '"timestamp":"2026-06-04T20:00:01Z"}}'
                        ),
                        (
                            '{"observedAt":"2026-06-04T20:00:02Z",'
                            '"contractId":"CON.F.US.MNQ.M26","data":'
                            '{"symbol":"F.US.MNQ","bestBid":101,"bestAsk":101.25,'
                            '"timestamp":"2026-06-04T20:00:02Z"}}'
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            (raw_dir / "trades.jsonl").write_text(
                (
                    '{"observedAt":"2026-06-04T20:00:01Z",'
                    '"contractId":"CON.F.US.MNQ.M26","data":'
                    '[{"price":100.5,"volume":2,"type":0,'
                    '"timestamp":"2026-06-04T20:00:01Z"}]}\n'
                ),
                encoding="utf-8",
            )
            (raw_dir / "depth.jsonl").write_text(
                (
                    '{"observedAt":"2026-06-04T20:00:01Z",'
                    '"contractId":"CON.F.US.MNQ.M26","data":'
                    '[{"price":100.5,"volume":4,"type":4,'
                    '"timestamp":"2026-06-04T20:00:01Z"}]}\n'
                ),
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                finalize_realtime_capture(
                    data_dir=data_dir,
                    windows="1,2",
                    horizons="1",
                    interval_seconds=1,
                    max_stale_quote_seconds=5,
                    tick_size=0.25,
                )

            quote_path = (
                data_dir
                / "bronze"
                / "projectx"
                / "quotes"
                / "date=2026-06-04"
                / "contract=CON_F_US_MNQ_M26"
                / "quotes.csv"
            )
            feature_path = (
                data_dir
                / "silver"
                / "projectx"
                / "features"
                / "intraday"
                / "date=2026-06-04"
                / "contract=CON_F_US_MNQ_M26"
                / "features_1s.csv"
            )

            self.assertTrue(quote_path.exists())
            self.assertTrue(feature_path.exists())
            with feature_path.open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[0]["forward_mfe_ticks_1s"], "2.0")


if __name__ == "__main__":
    unittest.main()
