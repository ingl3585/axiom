from __future__ import annotations

from pathlib import Path
import csv
import tempfile
import unittest

import _bootstrap  # noqa: F401
from bars import aggregate_trade_bars, build_session_bars, load_continuous_bars
from projectx import BarUnit


class AggregateTradeBarsTests(unittest.TestCase):
    def test_buckets_trades_into_ohlcv(self) -> None:
        trades = [
            (0.0, 100.0, 1.0),
            (30.0, 103.0, 2.0),
            (59.0, 99.0, 3.0),
            (60.0, 101.0, 1.0),
            (119.0, 105.0, 2.0),
        ]
        bars = aggregate_trade_bars(trades, interval_seconds=60)

        self.assertEqual(len(bars), 2)
        first, second = bars
        self.assertEqual((first["o"], first["h"], first["l"], first["c"], first["v"]),
                         (100.0, 103.0, 99.0, 99.0, 6.0))
        self.assertEqual((second["o"], second["h"], second["l"], second["c"], second["v"]),
                         (101.0, 105.0, 101.0, 105.0, 3.0))
        self.assertLess(first["t"], second["t"])

    def test_sorts_unordered_trades_so_open_and_close_are_correct(self) -> None:
        trades = [(59.0, 99.0, 3.0), (0.0, 100.0, 1.0), (30.0, 103.0, 2.0)]
        bars = aggregate_trade_bars(trades, interval_seconds=60)
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0]["o"], 100.0)  # earliest trade
        self.assertEqual(bars[0]["c"], 99.0)   # latest trade

    def test_rejects_nonpositive_interval(self) -> None:
        with self.assertRaises(ValueError):
            aggregate_trade_bars([(0.0, 100.0, 1.0)], interval_seconds=0)

    def test_tracks_buy_sell_aggressor_volume(self) -> None:
        # (epoch, price, volume, trade_type): type 0 = buy, type 1 = sell.
        trades = [
            (0.0, 100.0, 5.0, 0),
            (10.0, 101.0, 3.0, 1),
            (20.0, 102.0, 2.0, 0),
        ]
        bars = aggregate_trade_bars(trades, interval_seconds=60)
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0]["v"], 10.0)
        self.assertEqual(bars[0]["bv"], 7.0)  # 5 + 2 buys
        self.assertEqual(bars[0]["sv"], 3.0)

    def test_missing_trade_type_yields_zero_buy_sell(self) -> None:
        bars = aggregate_trade_bars([(0.0, 100.0, 5.0)], interval_seconds=60)
        self.assertEqual(bars[0]["bv"], 0.0)
        self.assertEqual(bars[0]["sv"], 0.0)


class ContinuousBarsTests(unittest.TestCase):
    def test_api_bars_win_over_live_at_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            partition = (
                data_dir / "bronze" / "projectx" / "bars"
                / "contract=CON_F_US_MNQ_M26" / "unit=minute_1"
            )
            partition.mkdir(parents=True)
            # API history bar for 20:00 (authoritative volume 10).
            (partition / "window.csv").write_text(
                "t,o,h,l,c,v\n2026-06-04T20:00:00+00:00,100,101,99,100.5,10\n",
                encoding="utf-8",
            )
            # Live session: overlaps 20:00 (volume 99) and extends to 20:01.
            (partition / "live_2026-06-04.csv").write_text(
                "t,o,h,l,c,v\n"
                "2026-06-04T20:00:00+00:00,100,101,99,100.5,99\n"
                "2026-06-04T20:01:00+00:00,100.5,102,100,101,5\n",
                encoding="utf-8",
            )

            bars = load_continuous_bars(
                data_dir, "contract=CON_F_US_MNQ_M26", BarUnit.MINUTE, 1
            )

            self.assertEqual(len(bars), 2)
            by_time = {bar["t"]: bar for bar in bars}
            overlap = next(bar for bar in bars if bar["t"].startswith("2026-06-04T20:00:00"))
            self.assertEqual(overlap["v"], "10")  # API wins, not the live 99
            self.assertEqual(len(by_time), 2)  # de-duplicated by timestamp


class BuildSessionBarsTests(unittest.TestCase):
    def test_builds_bars_from_bronze_trades(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            trade_dir = (
                data_dir / "bronze" / "projectx" / "trades"
                / "date=2026-06-04" / "contract=CON_F_US_MNQ_M26"
            )
            trade_dir.mkdir(parents=True)
            trades_path = trade_dir / "trades.csv"
            trades_path.write_text(
                "\n".join(
                    [
                        "event_time,observed_at,price,volume",
                        "2026-06-04T20:00:01Z,,100.0,2",
                        "2026-06-04T20:00:40Z,,100.5,3",
                        "2026-06-04T20:01:10Z,,101.0,1",
                    ]
                ),
                encoding="utf-8",
            )

            result = build_session_bars(data_dir, trades_path, BarUnit.MINUTE, 1)

            self.assertEqual(result.bars, 2)
            self.assertTrue(result.path.exists())
            self.assertEqual(result.path.name, "live_2026-06-04.csv")
            with result.path.open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 2)
            self.assertEqual(float(rows[0]["o"]), 100.0)
            self.assertEqual(float(rows[0]["c"]), 100.5)
            self.assertEqual(float(rows[0]["v"]), 5.0)


if __name__ == "__main__":
    unittest.main()
