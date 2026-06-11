from __future__ import annotations

from datetime import UTC, datetime, timedelta
import unittest

import _bootstrap  # noqa: F401
from candidates import Setup
from live_signals import LiveSignalEngine, format_decision_line
from signals import EdgeLedger, SignalConfig


def ledger_with(key: str, ticks: list[float]) -> EdgeLedger:
    rows = []
    for value in ticks:
        rows.append(
            {
                "state_key": key,
                "has_forward_outcome": "1",
                "forward_return_5bar": str(value * 0.0001),
                "forward_ticks_5bar": str(value),
                "forward_mfe_ticks_5bar": str(abs(value) + 2),
                "forward_mae_ticks_5bar": "-1.0",
            }
        )
    return EdgeLedger.from_state_rows(rows)


def make_bars(start: datetime, count: int) -> list[dict[str, object]]:
    bars = []
    price = 100.0
    for index in range(count):
        stamp = start + timedelta(minutes=index)
        price += 0.25 if index % 2 == 0 else -0.25
        bars.append(
            {
                "t": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "o": price,
                "h": price + 0.5,
                "l": price - 0.5,
                "c": price,
                "v": 100,
            }
        )
    return bars


# 14:30 UTC in July = 10:30 ET: inside RTH, 30 minutes from the 10:00 event.
RTH_START = datetime(2026, 7, 1, 14, 30, tzinfo=UTC)
# 08:00 UTC in July = 04:00 ET: overnight.
OVERNIGHT_START = datetime(2026, 7, 1, 8, 0, tzinfo=UTC)


class LiveSignalEngineTests(unittest.TestCase):
    def test_unknown_state_abstains_on_live_bar(self) -> None:
        engine = LiveSignalEngine(
            ledger=ledger_with("never_matches", [8.0] * 120),
            bars=make_bars(RTH_START, 30),
        )
        payload = engine.evaluate_latest()
        self.assertIsNotNone(payload)
        self.assertEqual(payload["direction"], 0)
        self.assertEqual(payload["reason"], "unknown_state")
        self.assertTrue(payload["t"].startswith("2026-07-01T14:59"))

    def test_overnight_bar_is_evaluated_by_default(self) -> None:
        # Overnight is no longer session-vetoed; it reaches the ledger lookup.
        engine = LiveSignalEngine(
            ledger=ledger_with("never_matches", [8.0] * 120),
            bars=make_bars(OVERNIGHT_START, 30),
        )
        payload = engine.evaluate_latest()
        self.assertEqual(payload["direction"], 0)
        self.assertEqual(payload["reason"], "unknown_state")

    def test_overnight_bar_vetoed_when_rth_only(self) -> None:
        engine = LiveSignalEngine(
            ledger=ledger_with("never_matches", [8.0] * 120),
            bars=make_bars(OVERNIGHT_START, 30),
            signal_config=SignalConfig(rth_only=True),
        )
        payload = engine.evaluate_latest()
        self.assertEqual(payload["reason"], "veto_not_rth")

    def test_planted_ledger_state_goes_long_live(self) -> None:
        bars = make_bars(RTH_START, 30)
        probe = LiveSignalEngine(ledger=ledger_with("x", [8.0] * 120), bars=bars)
        state_key = str(probe.evaluate_latest()["state_key"])
        self.assertTrue(state_key)

        engine = LiveSignalEngine(
            ledger=ledger_with(state_key, [7.0, 9.0] * 60),
            bars=bars,
        )
        payload = engine.evaluate_latest()
        self.assertEqual(payload["direction"], 1)
        self.assertEqual(payload["reason"], "edge_long")
        self.assertEqual(payload["n"], 120)
        self.assertAlmostEqual(payload["expected_ticks_net"], 6.0)

    def test_ingest_dedupes_by_canonical_timestamp(self) -> None:
        engine = LiveSignalEngine(ledger=ledger_with("x", [8.0] * 120))
        engine.ingest_bar({"t": "2026-07-01T14:30:00.000Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1})
        engine.ingest_bar({"t": "2026-07-01T14:30:00Z", "o": 2, "h": 2, "l": 2, "c": 2, "v": 2})
        self.assertEqual(len(engine.bars_by_key), 1)
        only = next(iter(engine.bars_by_key.values()))
        self.assertEqual(only["c"], 2)  # later ingest wins

    def test_live_payload_reports_blocked_candidates(self) -> None:
        probe = Setup("always_long", "v1", "test setup", lambda row, prev: 1)
        engine = LiveSignalEngine(
            ledger=ledger_with("never_matches", [8.0] * 120),
            bars=make_bars(RTH_START, 30),
            setups=(probe,),
        )
        payload = engine.evaluate_latest()
        self.assertEqual(len(payload["candidates"]), 1)
        candidate = payload["candidates"][0]
        self.assertEqual(candidate["setup"], "always_long@v1")
        self.assertFalse(candidate["approved"])
        self.assertEqual(candidate["gate_reason"], "unknown_state")
        # The blocked candidate is visible in the printed line.
        line = format_decision_line(payload)
        self.assertIn("cand: always_long@v1 LONG blocked(unknown_state)", line)

    def test_format_decision_lines_are_ascii(self) -> None:
        flat = format_decision_line(
            {"t": "2026-07-01T14:59:00Z", "close": 100.0, "direction": 0, "reason": "edge_below_cost"}
        )
        self.assertIn("FLAT edge_below_cost", flat)
        long_line = format_decision_line(
            {
                "t": "2026-07-01T14:59:00Z",
                "close": 100.0,
                "direction": 1,
                "reason": "edge_long",
                "n": 120,
                "lcb_ticks": 7.8,
                "expected_ticks_net": 6.0,
                "stop_ticks": 0.75,
                "state_key": "a" * 100,
            }
        )
        self.assertIn("LONG", long_line)
        self.assertIn("...", long_line)  # long state keys truncate
        flat.encode("ascii")
        long_line.encode("ascii")


if __name__ == "__main__":
    unittest.main()
