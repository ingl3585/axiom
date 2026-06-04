from __future__ import annotations

import unittest

from axiom.qa import parse_dt
from axiom.signals import MomentumSignalConfig, evaluate_momentum_signal


class SignalTests(unittest.TestCase):
    def test_evaluate_momentum_signal_long_candidate(self) -> None:
        decision = evaluate_momentum_signal(
            {
                "timestamp": "2026-06-04T20:00:00Z",
                "midPrice": 100.0,
                "spread": 0.5,
                "secondsSinceQuote": 0,
                "return_5s": 0.005,
            },
            MomentumSignalConfig(min_momentum_ticks=1.0),
        )

        self.assertEqual(decision.action, "LONG_CANDIDATE")
        self.assertEqual(decision.direction, 1)
        self.assertEqual(decision.reason, "momentum")
        self.assertEqual(decision.momentum_ticks, 2.0)

    def test_evaluate_momentum_signal_blocks_on_cooldown(self) -> None:
        decision = evaluate_momentum_signal(
            {
                "timestamp": "2026-06-04T20:00:10Z",
                "midPrice": 100.0,
                "spread": 0.5,
                "secondsSinceQuote": 0,
                "return_5s": -0.005,
            },
            MomentumSignalConfig(min_momentum_ticks=1.0, cooldown_seconds=30),
            last_signal_at=parse_dt("2026-06-04T20:00:00Z"),
        )

        self.assertEqual(decision.action, "NO_TRADE")
        self.assertEqual(decision.direction, 0)
        self.assertEqual(decision.reason, "cooldown")
        self.assertEqual(decision.cooldown_remaining_seconds, 20.0)

    def test_evaluate_momentum_signal_blocks_on_spread_and_threshold(self) -> None:
        threshold = evaluate_momentum_signal(
            {
                "timestamp": "2026-06-04T20:00:00Z",
                "midPrice": 100.0,
                "spread": 0.5,
                "secondsSinceQuote": 0,
                "return_5s": 0.001,
            },
            MomentumSignalConfig(min_momentum_ticks=1.0),
        )
        self.assertEqual(threshold.reason, "momentum_threshold")

        spread = evaluate_momentum_signal(
            {
                "timestamp": "2026-06-04T20:00:00Z",
                "midPrice": 100.0,
                "spread": 1.5,
                "secondsSinceQuote": 0,
                "return_5s": 0.005,
            },
            MomentumSignalConfig(min_momentum_ticks=1.0, max_spread_ticks=4.0),
        )
        self.assertEqual(spread.reason, "spread_filter")


if __name__ == "__main__":
    unittest.main()
