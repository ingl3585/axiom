from __future__ import annotations

import unittest

import _bootstrap  # noqa: F401
from signals import (
    Decision,
    EdgeLedger,
    Position,
    SignalConfig,
    decide,
    step,
)


def state_rows(key: str, ticks: list[float], mae: float = -1.0) -> list[dict[str, str]]:
    rows = []
    for value in ticks:
        rows.append(
            {
                "state_key": key,
                "has_forward_outcome": "1",
                "forward_return_5bar": str(value * 0.0001),
                "forward_ticks_5bar": str(value),
                "forward_mfe_ticks_5bar": str(abs(value) + 2),
                "forward_mae_ticks_5bar": str(mae),
            }
        )
    return rows


def tradeable_row(state_key: str = "good") -> dict[str, str]:
    return {
        "state_key": state_key,
        "session_bucket": "midday",
        "is_rth": "1",
        "minutes_to_event": "60",
        "minutes_since_open": "120",
    }


def ledger_with(key: str, ticks: list[float], mae: float = -1.0) -> EdgeLedger:
    return EdgeLedger.from_state_rows(state_rows(key, ticks, mae=mae))


class DecideTests(unittest.TestCase):
    def test_strong_state_goes_long_with_full_receipt(self) -> None:
        # 120 observations averaging +8 ticks: lcb ~7.8 clears the 3-tick buffer.
        ledger = ledger_with("good", [7.0, 9.0] * 60)
        decision = decide(tradeable_row("good"), ledger)

        self.assertEqual(decision.direction, 1)
        self.assertEqual(decision.reason, "edge_long")
        self.assertEqual(decision.state_key, "good")
        self.assertEqual(decision.n, 120)
        self.assertGreater(decision.lcb_ticks, 3.0)
        self.assertAlmostEqual(decision.expected_ticks_net, 6.0)  # avg 8 - cost 2
        self.assertAlmostEqual(decision.stop_ticks, 0.75)  # 0.75 x |avg mae 1.0|
        self.assertAlmostEqual(decision.cost_ticks, 2.0)  # RTH cost

    def test_strong_negative_state_goes_short(self) -> None:
        ledger = ledger_with("bad", [-7.0, -9.0] * 60)
        decision = decide(tradeable_row("bad"), ledger)
        self.assertEqual(decision.direction, -1)
        self.assertEqual(decision.reason, "edge_short")
        self.assertLess(decision.ucb_ticks, -3.0)
        self.assertAlmostEqual(decision.expected_ticks_net, 6.0)

    def test_weak_state_abstains_with_edge_below_cost(self) -> None:
        ledger = ledger_with("weak", [1.0, -1.0] * 60)
        decision = decide(tradeable_row("weak"), ledger)
        self.assertEqual(decision.direction, 0)
        self.assertEqual(decision.reason, "edge_below_cost")
        self.assertEqual(decision.n, 120)

    def test_insufficient_sample_abstains(self) -> None:
        ledger = ledger_with("thin", [8.0] * 50)  # strong but only 50 obs
        decision = decide(tradeable_row("thin"), ledger)
        self.assertEqual(decision.direction, 0)
        self.assertEqual(decision.reason, "insufficient_n")
        self.assertEqual(decision.n, 50)

    def test_unknown_state_abstains(self) -> None:
        ledger = ledger_with("good", [7.0, 9.0] * 60)
        decision = decide(tradeable_row("never_seen"), ledger)
        self.assertEqual(decision.direction, 0)
        self.assertEqual(decision.reason, "unknown_state")

    def test_lottery_state_is_vetoed_for_risk(self) -> None:
        # Average +25 ticks with n=120 clears every edge check, but the state's
        # typical adverse excursion (90 ticks) exceeds the 40-tick risk cap.
        ledger = ledger_with("lottery", [24.0, 26.0] * 60, mae=-90.0)
        decision = decide(tradeable_row("lottery"), ledger)
        self.assertEqual(decision.direction, 0)
        self.assertEqual(decision.reason, "risk_too_wide")
        self.assertEqual(decision.n, 120)
        # The same state with contained risk trades normally.
        calm = ledger_with("lottery", [24.0, 26.0] * 60, mae=-30.0)
        self.assertEqual(decide(tradeable_row("lottery"), calm).reason, "edge_long")

    def test_vetoes_fire_before_edge_lookup(self) -> None:
        ledger = ledger_with("good", [7.0, 9.0] * 60)

        closed = tradeable_row("good")
        closed["session_bucket"] = "closed"
        self.assertEqual(decide(closed, ledger).reason, "veto_session_closed")

        near_event = tradeable_row("good")
        near_event["minutes_to_event"] = "5"
        self.assertEqual(decide(near_event, ledger).reason, "veto_event_window")

        near_close = tradeable_row("good")
        near_close["minutes_since_open"] = "380"  # RTH is 390 minutes
        self.assertEqual(decide(near_close, ledger).reason, "veto_close_window")

        # Overnight is allowed by default but can be vetoed via rth_only.
        overnight = tradeable_row("good")
        overnight["is_rth"] = "0"
        config = SignalConfig(rth_only=True)
        self.assertEqual(decide(overnight, ledger, config).reason, "veto_not_rth")

    def test_overnight_trades_carry_higher_cost(self) -> None:
        # Strong state: avg +8, lcb ~7.8 clears both thresholds.
        ledger = ledger_with("good", [7.0, 9.0] * 60)
        overnight = tradeable_row("good")
        overnight["is_rth"] = "0"
        overnight["session_bucket"] = "overnight"

        decision = decide(overnight, ledger)
        self.assertEqual(decision.reason, "edge_long")
        self.assertAlmostEqual(decision.cost_ticks, 4.0)  # overnight cost
        self.assertAlmostEqual(decision.expected_ticks_net, 4.0)  # avg 8 - 4

    def test_marginal_edge_clears_rth_but_not_overnight_bar(self) -> None:
        # Average ~+4 ticks: clears the RTH threshold (2 + 1) but not the
        # overnight one (4 + 1), so the same state trades RTH-only on cost.
        ledger = ledger_with("marginal", [3.5, 4.5] * 60)

        rth_decision = decide(tradeable_row("marginal"), ledger)
        self.assertEqual(rth_decision.reason, "edge_long")

        overnight = tradeable_row("marginal")
        overnight["is_rth"] = "0"
        overnight["session_bucket"] = "overnight"
        overnight_decision = decide(overnight, ledger)
        self.assertEqual(overnight_decision.reason, "edge_below_cost")


class StateMachineTests(unittest.TestCase):
    def long_decision(self) -> Decision:
        return Decision(
            direction=1, reason="edge_long", state_key="good", n=120, stop_ticks=6.0
        )

    def flat_decision(self) -> Decision:
        return Decision(direction=0, reason="edge_below_cost")

    def short_decision(self) -> Decision:
        return Decision(direction=-1, reason="edge_short", state_key="bad", n=120)

    def test_opens_from_flat_and_carries_stop(self) -> None:
        position, action = step(Position(), self.long_decision())
        self.assertEqual(action, "open_long")
        self.assertEqual(position.direction, 1)
        self.assertEqual(position.stop_ticks, 6.0)

    def test_holds_then_exits_on_time_stop(self) -> None:
        position = Position(direction=1, state_key="good")
        actions = []
        for _ in range(5):
            position, action = step(position, self.flat_decision())
            actions.append(action)
        self.assertEqual(actions, ["hold", "hold", "hold", "hold", "exit_time"])
        self.assertEqual(position.direction, 0)

    def test_opposite_signal_exits_without_reversing(self) -> None:
        position = Position(direction=1, state_key="good")
        position, action = step(position, self.short_decision())
        self.assertEqual(action, "exit_opposite")
        self.assertEqual(position.direction, 0)
        # Reversal requires a second step from flat.
        position, action = step(position, self.short_decision())
        self.assertEqual(action, "open_short")
        self.assertEqual(position.direction, -1)

    def test_stop_loss_exits_first(self) -> None:
        position = Position(direction=1, state_key="good", stop_ticks=6.0)
        position, action = step(
            position, self.flat_decision(), adverse_ticks=7.0
        )
        self.assertEqual(action, "exit_stop")
        self.assertEqual(position.direction, 0)

    def test_stays_flat_on_flat_decision(self) -> None:
        position, action = step(Position(), self.flat_decision())
        self.assertEqual(action, "stay_flat")
        self.assertEqual(position.direction, 0)


if __name__ == "__main__":
    unittest.main()
