from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import json
import tempfile
import unittest

import _bootstrap  # noqa: F401
from history import backfill_historical_bars, load_history_state
from projectx import BarUnit, Contract


class FakeHistoryClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def retrieve_bars_chunked(self, **kwargs):
        self.calls.append(kwargs)
        start = kwargs["start"]
        end = kwargs["end"]
        yield (start, end), [
            {
                "t": "2026-06-03T20:00:00+00:00",
                "o": 100,
                "h": 101,
                "l": 99,
                "c": 100.5,
                "v": 10,
            }
        ]


def contract() -> Contract:
    return Contract(
        id="CON.F.US.MNQ.M26",
        name="MNQM6",
        description="Micro E-mini Nasdaq-100: June 2026",
        tick_size=0.25,
        tick_value=0.5,
        active_contract=True,
        symbol_id="F.US.MNQ",
    )


class HistoryBackfillTests(unittest.TestCase):
    def test_first_backfill_writes_raw_and_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory) / "data"
            client = FakeHistoryClient()
            end = datetime(2026, 6, 3, 21, 0, tzinfo=UTC)

            result = backfill_historical_bars(
                client=client,  # type: ignore[arg-type]
                data_dir=data_dir,
                symbol="MNQ",
                contract=contract(),
                unit=BarUnit.MINUTE,
                unit_number=1,
                initial_days=1,
                end=end,
            )

            self.assertFalse(result.skipped)
            self.assertEqual(result.bars, 1)
            self.assertEqual(len(result.raw_files), 1)
            self.assertTrue(result.raw_files[0].exists())
            payload = json.loads(result.raw_files[0].read_text(encoding="utf-8"))
            self.assertEqual(payload["contractId"], "CON.F.US.MNQ.M26")

            state = load_history_state(data_dir)
            entries = list(state["contracts"].values())
            self.assertEqual(entries[0]["lastEndTime"], "2026-06-03T21:00:00Z")

    def test_backfill_skips_when_state_is_current(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory) / "data"
            client = FakeHistoryClient()
            end = datetime(2026, 6, 3, 21, 0, tzinfo=UTC)

            backfill_historical_bars(
                client=client,  # type: ignore[arg-type]
                data_dir=data_dir,
                symbol="MNQ",
                contract=contract(),
                end=end,
            )
            result = backfill_historical_bars(
                client=client,  # type: ignore[arg-type]
                data_dir=data_dir,
                symbol="MNQ",
                contract=contract(),
                end=end,
            )

            self.assertTrue(result.skipped)
            self.assertEqual(result.reason, "already current")
            self.assertEqual(len(client.calls), 1)


if __name__ == "__main__":
    unittest.main()

