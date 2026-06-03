from __future__ import annotations

from datetime import UTC, datetime
import unittest

from axiom.projectx import BarUnit, history_windows, parse_bar_unit, parse_utc_datetime
from axiom.storage import safe_partition_value


class ProjectXTimeTests(unittest.TestCase):
    def test_parse_bar_unit_aliases(self) -> None:
        self.assertEqual(parse_bar_unit("minute"), BarUnit.MINUTE)
        self.assertEqual(parse_bar_unit("2"), BarUnit.MINUTE)
        self.assertEqual(parse_bar_unit(1), BarUnit.SECOND)

    def test_parse_utc_datetime_accepts_z(self) -> None:
        parsed = parse_utc_datetime("2026-06-03T14:30:00Z")
        self.assertEqual(parsed.tzinfo, UTC)
        self.assertEqual(parsed.hour, 14)

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

