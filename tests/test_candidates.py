from __future__ import annotations

import unittest

import _bootstrap  # noqa: F401
from candidates import (
    Setup,
    exhaustion_reversal,
    failed_breakout,
    fire_candidates,
    trend_pullback,
    vwap_reclaim,
)


class SetupRuleTests(unittest.TestCase):
    def test_trend_pullback_fires_long_on_reset_dip_in_uptrend(self) -> None:
        row = {
            # dist_ema_9 < dist_ema_21 means the 9 EMA is above the 21 EMA.
            "dist_ema_9": "-0.0002",
            "dist_ema_21": "0.0005",
            "dist_vwap": "0.001",
            "rsi_9": "45",
        }
        self.assertEqual(trend_pullback(row, None), 1)
        # Still overbought: no reset, no fire.
        self.assertEqual(trend_pullback({**row, "rsi_9": "65"}, None), 0)
        # Below VWAP: not the uptrend regime this setup wants.
        self.assertEqual(trend_pullback({**row, "dist_vwap": "-0.001"}, None), 0)
        # Missing fields never fire.
        self.assertEqual(trend_pullback({}, None), 0)

    def test_vwap_reclaim_requires_cross_from_below(self) -> None:
        prev = {"dist_vwap": "-0.001"}
        row = {"dist_vwap": "0.0005", "vol_ratio_20bar": "1.2"}
        self.assertEqual(vwap_reclaim(row, prev), 1)
        self.assertEqual(vwap_reclaim(row, None), 0)  # no prior bar
        self.assertEqual(vwap_reclaim(row, {"dist_vwap": "0.0001"}), 0)  # no cross
        quiet = {**row, "vol_ratio_20bar": "0.5"}
        self.assertEqual(vwap_reclaim(quiet, prev), 0)  # no participation

    def test_failed_breakout_shorts_a_fallback_inside_the_range(self) -> None:
        self.assertEqual(failed_breakout({"or_breakout": "0"}, {"or_breakout": "1"}), -1)
        self.assertEqual(failed_breakout({"or_breakout": "0"}, {"or_breakout": "0"}), 0)
        self.assertEqual(failed_breakout({"or_breakout": "1"}, {"or_breakout": "1"}), 0)
        # Overnight rows leave or_breakout blank: never fires.
        self.assertEqual(failed_breakout({"or_breakout": ""}, {"or_breakout": "1"}), 0)

    def test_exhaustion_reversal_needs_stretch_overbought_and_stall(self) -> None:
        row = {"vwap_sigma": "1.8", "rsi_9": "75", "return_1": "-0.0003"}
        self.assertEqual(exhaustion_reversal(row, None), -1)
        self.assertEqual(exhaustion_reversal({**row, "return_1": "0.0002"}, None), 0)
        self.assertEqual(exhaustion_reversal({**row, "vwap_sigma": "0.8"}, None), 0)
        self.assertEqual(exhaustion_reversal({**row, "rsi_9": "60"}, None), 0)


class FireCandidatesTests(unittest.TestCase):
    def test_returns_fired_setups_with_versioned_keys(self) -> None:
        always_long = Setup("always", "v1", "test", lambda row, prev: 1)
        never = Setup("never", "v1", "test", lambda row, prev: 0)
        fired = fire_candidates({}, None, setups=(always_long, never))
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0].setup_key, "always@v1")
        self.assertEqual(fired[0].direction, 1)


if __name__ == "__main__":
    unittest.main()
