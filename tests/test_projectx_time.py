from __future__ import annotations

from datetime import UTC, datetime
import unittest

import _bootstrap  # noqa: F401
from projectx import (
    BarUnit,
    history_windows,
    parse_dt,
    parse_utc_datetime,
    safe_partition_value,
)


class ProjectXTimeTests(unittest.TestCase):
    def test_parse_utc_datetime_accepts_z(self) -> None:
        parsed = parse_utc_datetime("2026-06-03T14:30:00Z")
        self.assertEqual(parsed.tzinfo, UTC)
        self.assertEqual(parsed.hour, 14)

    def test_parse_dt_trims_projectx_fractional_precision(self) -> None:
        parsed = parse_dt("2026-06-03T20:56:00.6756059+00:00")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.microsecond, 675605)

    def test_history_windows_chunk_by_bar_limit(self) -> None:
        start = datetime(2026, 6, 1, tzinfo=UTC)
        end = datetime(2026, 6, 3, tzinfo=UTC)
        windows = history_windows(start, end, BarUnit.MINUTE, 1, limit=1_000)
        self.assertEqual(len(windows), 3)
        self.assertEqual(windows[0][0], start)
        self.assertEqual(windows[-1][1], end)

    def test_safe_partition_value(self) -> None:
        self.assertEqual(safe_partition_value("CON.F.US.MNQ.U25"), "CON_F_US_MNQ_U25")


if __name__ == "__main__":
    unittest.main()
