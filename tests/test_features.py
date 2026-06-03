from __future__ import annotations

from pathlib import Path
import csv
import tempfile
import unittest

from axiom.features import IntradayFeatureConfig, build_intraday_features


class FeatureTests(unittest.TestCase):
    def test_build_intraday_features(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory) / "data"
            base = (
                data_dir
                / "bronze"
                / "projectx"
            )
            quote_dir = (
                base
                / "quotes"
                / "date=2026-06-03"
                / "contract=CON_F_US_MNQ_M26"
            )
            trade_dir = (
                base
                / "trades"
                / "date=2026-06-03"
                / "contract=CON_F_US_MNQ_M26"
            )
            depth_dir = (
                base
                / "depth"
                / "date=2026-06-03"
                / "contract=CON_F_US_MNQ_M26"
            )
            quote_dir.mkdir(parents=True)
            trade_dir.mkdir(parents=True)
            depth_dir.mkdir(parents=True)

            quote_path = quote_dir / "quotes.csv"
            quote_path.write_text(
                "\n".join(
                    [
                        "event_time,observed_at,best_bid,best_ask,spread",
                        "2026-06-03T20:00:00Z,,100,100.25,0.25",
                        "2026-06-03T20:00:01Z,,100.5,100.75,0.25",
                        "2026-06-03T20:00:02Z,,101,101.25,0.25",
                    ]
                ),
                encoding="utf-8",
            )
            (trade_dir / "trades.csv").write_text(
                "\n".join(
                    [
                        "event_time,observed_at,volume,trade_type",
                        "2026-06-03T20:00:01Z,,2,0",
                        "2026-06-03T20:00:01Z,,1,1",
                    ]
                ),
                encoding="utf-8",
            )
            (depth_dir / "depth.csv").write_text(
                "\n".join(
                    [
                        "event_time,observed_at,depth_type",
                        "2026-06-03T20:00:01Z,,4",
                        "2026-06-03T20:00:02Z,,5",
                    ]
                ),
                encoding="utf-8",
            )

            result = build_intraday_features(
                IntradayFeatureConfig(
                    data_dir=data_dir,
                    quote_path=quote_path,
                    windows_seconds=[1, 2],
                    horizons_seconds=[1],
                    max_stale_quote_seconds=1,
                )
            )

            self.assertEqual(result.rows, 3)
            self.assertTrue(result.path.exists())
            with result.path.open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))

            self.assertEqual(rows[1]["trade_volume_1s"], "3.0")
            self.assertEqual(rows[1]["trade_type0_1_imbalance_1s"], str((2 - 1) / 3))
            self.assertEqual(rows[1]["depth_updates_1s"], "1.0")
            self.assertNotEqual(rows[0]["forward_return_1s"], "")

            # Mid rises 0.5 (2 ticks at 0.25) each second, so MFE == MAE == 2.0.
            self.assertEqual(rows[0]["forward_mfe_ticks_1s"], "2.0")
            self.assertEqual(rows[0]["forward_mae_ticks_1s"], "2.0")
            self.assertGreater(float(rows[0]["forward_realized_vol_1s"]), 0.0)
            # The final bucket has no future bucket, so forward labels are blank.
            self.assertEqual(rows[2]["forward_return_1s"], "")
            self.assertEqual(rows[2]["forward_mfe_ticks_1s"], "")


if __name__ == "__main__":
    unittest.main()

