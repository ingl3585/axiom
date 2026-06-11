from __future__ import annotations

from datetime import UTC, datetime, timedelta
import unittest

import _bootstrap  # noqa: F401
from candidates import Setup
from signals import SignalConfig
from walkforward import (
    GateConfig,
    evaluate_walk_forward,
    trade_outcome,
    walkforward_markdown,
    write_candidate_records,
)

WEEK_MONDAYS = [
    datetime(2026, 5, 4, 14, 0, tzinfo=UTC),
    datetime(2026, 5, 11, 14, 0, tzinfo=UTC),
    datetime(2026, 5, 18, 14, 0, tzinfo=UTC),
    datetime(2026, 5, 25, 14, 0, tzinfo=UTC),
    datetime(2026, 6, 1, 14, 0, tzinfo=UTC),
    datetime(2026, 6, 8, 14, 0, tzinfo=UTC),
]


def make_rows(forward_ticks_for) -> list[dict[str, str]]:
    """Six weeks x 120 bars of merged feature+state rows.

    States alternate in blocks of six bars between stateA and stateB so that
    entries (which respect a 5-bar cooldown, i.e. every 6th bar) split evenly
    across both states. Adverse excursion alternates -2.0/-0.4 so the derived
    stop (0.75 x avg 1.2 = 0.9) sits above the entry bars' 0.4 adverse move:
    entries land on odd indices (bar after the even-index signal), so planted
    trades are not stopped out.
    """
    rows = []
    for monday in WEEK_MONDAYS:
        for i in range(120):
            stamp = monday + timedelta(minutes=i)
            ticks = forward_ticks_for(i)
            rows.append(
                {
                    "t": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "state_key": "stateA" if (i // 6) % 2 == 0 else "stateB",
                    "session_bucket": "midday",
                    "is_rth": "1",
                    "minutes_to_event": "60",
                    "minutes_since_open": "120",
                    "has_forward_outcome": "1",
                    "forward_return_5bar": str(ticks * 0.0001),
                    "forward_ticks_5bar": str(ticks),
                    "forward_mfe_ticks_5bar": str(abs(ticks) + 2),
                    "forward_mae_ticks_5bar": "-2.0" if i % 2 == 0 else "-0.4",
                }
            )
    return rows


def entry_row(forward: float, mfe: float, mae: float) -> dict[str, str]:
    return {
        "has_forward_outcome": "1",
        "forward_return_5bar": str(forward * 0.0001),
        "forward_ticks_5bar": str(forward),
        "forward_mfe_ticks_5bar": str(mfe),
        "forward_mae_ticks_5bar": str(mae),
    }


class TradeOutcomeTests(unittest.TestCase):
    config = SignalConfig()  # stop_slippage_ticks = 2.0

    def test_long_catastrophe_is_capped_at_stop_plus_slippage(self) -> None:
        # Raw outcome -400 ticks; with a 15-tick stop the loss is -(15 + 2).
        gross, stopped = trade_outcome(
            entry_row(-400.0, 5.0, -400.0), direction=1, stop_ticks=15.0,
            signal_config=self.config,
        )
        self.assertTrue(stopped)
        self.assertAlmostEqual(gross, -17.0)

    def test_long_clean_winner_is_untouched(self) -> None:
        gross, stopped = trade_outcome(
            entry_row(8.0, 10.0, -5.0), direction=1, stop_ticks=15.0,
            signal_config=self.config,
        )
        self.assertFalse(stopped)
        self.assertAlmostEqual(gross, 8.0)

    def test_short_adverse_is_the_favorable_excursion(self) -> None:
        # Price ripped up 400 ticks against a short: stopped.
        gross, stopped = trade_outcome(
            entry_row(400.0, 400.0, -3.0), direction=-1, stop_ticks=15.0,
            signal_config=self.config,
        )
        self.assertTrue(stopped)
        self.assertAlmostEqual(gross, -17.0)

    def test_winner_that_dipped_past_stop_counts_as_stopped(self) -> None:
        # Conservative: ordering within the window is unknown, so a trade that
        # breached the stop is a stop-out even if the window ended positive.
        gross, stopped = trade_outcome(
            entry_row(10.0, 12.0, -20.0), direction=1, stop_ticks=15.0,
            signal_config=self.config,
        )
        self.assertTrue(stopped)
        self.assertAlmostEqual(gross, -17.0)

    def test_no_stop_passes_raw_outcome(self) -> None:
        gross, stopped = trade_outcome(
            entry_row(-400.0, 5.0, -400.0), direction=1, stop_ticks=None,
            signal_config=self.config,
        )
        self.assertFalse(stopped)
        self.assertAlmostEqual(gross, -400.0)


class WalkForwardTests(unittest.TestCase):
    def test_planted_edge_opens_gate(self) -> None:
        # Both states genuinely average +8 ticks in every week.
        rows = make_rows(lambda i: 7.0 if i % 2 == 0 else 9.0)
        result = evaluate_walk_forward(rows)

        overall = result.overall()
        self.assertTrue(result.gate_open, result.gate_reasons)
        self.assertGreaterEqual(overall["trades"], 30)
        self.assertGreater(overall["net_ticks"], 0)
        # Receipts are calibrated against realized outcomes.
        self.assertAlmostEqual(overall["avg_expected_ticks_net"], 6.0, places=1)
        self.assertGreater(overall["avg_realized_net_ticks"], 0)
        # Profit is not carried by a single state.
        per_state = result.per_state_net()
        self.assertEqual(set(per_state), {"stateA", "stateB"})

    def test_first_week_only_trains_never_trades(self) -> None:
        rows = make_rows(lambda i: 7.0 if i % 2 == 0 else 9.0)
        result = evaluate_walk_forward(rows)
        # Folds start at week 2: five evaluated folds for six weeks of data.
        self.assertEqual(len(result.folds), 5)
        # Week 2 has only 60 observations per state in training (< min 100),
        # so it must abstain with insufficient_n rather than trade.
        first = result.folds[0]
        self.assertEqual(len(first.trades), 0)
        self.assertGreater(first.reasons["insufficient_n"], 0)

    def test_no_leakage_training_strictly_precedes_evaluation(self) -> None:
        rows = make_rows(lambda i: 7.0 if i % 2 == 0 else 9.0)
        result = evaluate_walk_forward(rows)
        for fold in result.folds:
            self.assertLess(fold.train_max_t, fold.eval_min_t)

    def test_noise_keeps_gate_closed(self) -> None:
        # Zero-mean outcomes: no state can clear the cost buffer.
        rows = make_rows(lambda i: 8.0 if i % 2 == 0 else -8.0)
        result = evaluate_walk_forward(rows)
        self.assertFalse(result.gate_open)
        self.assertEqual(result.overall()["trades"], 0)
        self.assertIn("no out-of-sample trades", result.gate_reasons)

    def test_candidates_are_observed_and_gated(self) -> None:
        # A planted always-long setup fires throughout; the gate blocks it in
        # the thin fold (insufficient_n) and approves it once states qualify.
        probe = Setup("always_long", "v1", "test setup", lambda row, prev: 1)
        rows = make_rows(lambda i: 7.0 if i % 2 == 0 else 9.0)
        result = evaluate_walk_forward(rows, setups=(probe,))

        summaries = {
            item["setup"]: item
            for item in result.to_dict()["candidate_summaries"]
        }
        self.assertIn("always_long@v1", summaries)
        stats = summaries["always_long@v1"]
        self.assertGreater(stats["fires"], 0)
        self.assertGreater(stats["approved"], 0)
        self.assertGreater(stats["blocked"].get("insufficient_n", 0), 0)
        self.assertGreater(stats["outcomes"], 0)
        # Observations are scored with the same costs/stops as trades.
        self.assertGreater(stats["total_net_ticks"], 0)
        # Per-setup cooldown keeps observation windows from overlapping:
        # 120 bars per fold / (horizon 5) caps fires per fold at 24.
        self.assertLessEqual(stats["fires"], 24 * len(result.folds))
        # Re-fires inside the cooldown are counted, not silently dropped.
        self.assertGreater(stats["suppressed_overlapping_fires"], 0)
        self.assertEqual(len(result.candidate_records), stats["fires"])

    def test_candidate_blocked_when_gate_opposes(self) -> None:
        probe = Setup("always_short", "v1", "test setup", lambda row, prev: -1)
        rows = make_rows(lambda i: 7.0 if i % 2 == 0 else 9.0)
        result = evaluate_walk_forward(rows, setups=(probe,))
        stats = {
            item["setup"]: item
            for item in result.to_dict()["candidate_summaries"]
        }["always_short@v1"]
        # In qualified folds the gate says LONG while the candidate says
        # SHORT: blocked as gate_opposes, never approved.
        self.assertEqual(stats["approved"], 0)
        self.assertGreater(stats["blocked"].get("gate_opposes", 0), 0)
        # The short candidate is stopped against ITS OWN adverse side (the
        # upside, avg mfe = 10 -> stop 7.5; entry-bar mfe 9 or 11 always
        # breaches it), not the long decision's MAE-based stop.
        self.assertGreater(stats["stopped"], 0)
        self.assertEqual(stats["stopped"], stats["outcomes"])
        # Every stopped short loses (7.5 + 2 slippage + 2 cost) = -11.5.
        self.assertAlmostEqual(
            stats["avg_net_ticks"], -11.5, places=6
        )

    def test_candidate_fires_are_logged_with_entry_features(self) -> None:
        import csv
        import tempfile
        from pathlib import Path

        probe = Setup("always_long", "v1", "test setup", lambda row, prev: 1)
        rows = make_rows(lambda i: 7.0 if i % 2 == 0 else 9.0)
        result = evaluate_walk_forward(rows, setups=(probe,))

        records = result.candidate_records
        self.assertGreater(len(records), 0)
        first = records[0]
        self.assertEqual(first.setup_key, "always_long@v1")
        # The entry-feature snapshot is captured per fire.
        self.assertEqual(first.features["session_bucket"], "midday")
        self.assertEqual(first.features["minutes_since_open"], "120")
        # Aggregate outcome count matches the records that carry an outcome.
        with_outcome = sum(1 for record in records if record.net_ticks is not None)
        summary = result.to_dict()["candidate_summaries"][0]
        self.assertEqual(with_outcome, summary["outcomes"])

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidates.csv"
            write_candidate_records(path, records)
            with path.open(encoding="utf-8") as handle:
                csv_rows = list(csv.DictReader(handle))
            self.assertEqual(len(csv_rows), len(records))
            self.assertEqual(csv_rows[0]["setup"], "always_long@v1")
            self.assertIn("rsi_9", csv_rows[0])
            self.assertIn(csv_rows[0]["approved"], {"0", "1"})

    def test_candidate_markdown_reports_suppressed_refires(self) -> None:
        probe = Setup("always_long", "v1", "test setup", lambda row, prev: 1)
        rows = make_rows(lambda i: 7.0 if i % 2 == 0 else 9.0)
        result = evaluate_walk_forward(rows, setups=(probe,))
        payload = result.to_dict()
        payload["generated_at"] = "2026-06-11T00:00:00Z"
        payload["states_path"] = "states.csv"

        markdown = walkforward_markdown(payload)

        self.assertIn("| setup | fires | suppressed | approved |", markdown)
        suppressed = payload["candidate_summaries"][0]["suppressed_overlapping_fires"]
        self.assertGreater(suppressed, 0)
        self.assertIn("| always_long@v1 |", markdown)
        self.assertIn(f"| {suppressed:,} |", markdown)

    def test_concentrated_profit_keeps_gate_closed(self) -> None:
        # stateA carries a strong edge, stateB none: trades happen but profit
        # concentrates in one state, so the gate must stay closed.
        def ticks(i: int) -> float:
            block_is_a = (i // 6) % 2 == 0
            if block_is_a:
                return 7.0 if i % 2 == 0 else 9.0
            return 0.5 if i % 2 == 0 else -0.5

        rows = make_rows(ticks)
        result = evaluate_walk_forward(rows, gate_config=GateConfig())
        self.assertGreater(result.overall()["trades"], 0)
        self.assertFalse(result.gate_open)
        self.assertTrue(
            any("carries" in reason for reason in result.gate_reasons),
            result.gate_reasons,
        )


if __name__ == "__main__":
    unittest.main()
