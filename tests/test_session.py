from __future__ import annotations

from datetime import UTC, date, datetime
import unittest

import _bootstrap  # noqa: F401
from session import (
    eastern_offset_hours,
    is_rth,
    minutes_since_open,
    minutes_to_nearest_event,
    nth_weekday,
    session_bucket,
    session_day,
    to_eastern,
)


def utc(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


class SessionTests(unittest.TestCase):
    def test_dst_transition_dates(self) -> None:
        self.assertEqual(nth_weekday(2026, 3, 6, 2), date(2026, 3, 8))  # 2nd Sun March
        self.assertEqual(nth_weekday(2026, 11, 6, 1), date(2026, 11, 1))  # 1st Sun Nov

    def test_offset_edt_vs_est(self) -> None:
        self.assertEqual(eastern_offset_hours(utc(2026, 7, 1, 12)), -4)  # summer = EDT
        self.assertEqual(eastern_offset_hours(utc(2026, 1, 15, 12)), -5)  # winter = EST

    def test_cash_open_maps_to_0930_et_both_seasons(self) -> None:
        # 13:30 UTC in July = 09:30 EDT; 14:30 UTC in January = 09:30 EST.
        summer = to_eastern(utc(2026, 7, 1, 13, 30))
        winter = to_eastern(utc(2026, 1, 15, 14, 30))
        self.assertEqual((summer.hour, summer.minute), (9, 30))
        self.assertEqual((winter.hour, winter.minute), (9, 30))
        self.assertTrue(is_rth(utc(2026, 7, 1, 13, 30)))
        self.assertTrue(is_rth(utc(2026, 1, 15, 14, 30)))
        self.assertEqual(minutes_since_open(utc(2026, 7, 1, 13, 30)), 0)

    def test_session_buckets(self) -> None:
        self.assertEqual(session_bucket(utc(2026, 7, 1, 13, 30)), "open_hour")  # 09:30
        self.assertEqual(session_bucket(utc(2026, 7, 1, 15, 0)), "midday")      # 11:00
        self.assertEqual(session_bucket(utc(2026, 7, 1, 16, 30)), "lunch")      # 12:30
        self.assertEqual(session_bucket(utc(2026, 7, 1, 19, 30)), "close_hour") # 15:30
        self.assertEqual(session_bucket(utc(2026, 7, 1, 2, 0)), "overnight")    # 22:00 prior
        self.assertEqual(session_bucket(utc(2026, 7, 4, 18, 0)), "weekend")     # Saturday

    def test_session_day_anchors_at_cash_open(self) -> None:
        # 12:00 UTC July = 08:00 ET (pre-open) -> rolls into prior session day.
        self.assertEqual(session_day(utc(2026, 7, 1, 12, 0)), date(2026, 6, 30))
        # 14:00 UTC July = 10:00 ET (after open) -> same day.
        self.assertEqual(session_day(utc(2026, 7, 1, 14, 0)), date(2026, 7, 1))

    def test_event_proximity(self) -> None:
        # 12:30 UTC July = 08:30 ET = an event time.
        self.assertEqual(minutes_to_nearest_event(utc(2026, 7, 1, 12, 30)), 0)
        # 13:00 UTC July = 09:00 ET, 30 min from the 08:30 event.
        self.assertEqual(minutes_to_nearest_event(utc(2026, 7, 1, 13, 0)), 30)


if __name__ == "__main__":
    unittest.main()
