from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import _bootstrap  # noqa: F401
from execution import ExecutionConfig, ExecutionController, stop_ticks_from_payload


class FakeExecutionClient:
    def __init__(self) -> None:
        self.orders: list[dict] = []
        self.closed: list[dict] = []
        self.positions: list[object] = []

    def search_accounts(self, only_active_accounts: bool = True) -> list[object]:
        return [
            type(
                "Account",
                (),
                {"id": 123, "name": "Practice", "can_trade": True, "is_visible": True},
            )()
        ]

    def search_open_positions(self, account_id: int) -> list[object]:
        return self.positions

    def place_market_order(self, **kwargs) -> object:
        self.orders.append(kwargs)
        return type("OrderResult", (), {"order_id": 99})()

    def close_contract_position(self, **kwargs) -> dict:
        self.closed.append(kwargs)
        return {"success": True}


def config(**overrides) -> ExecutionConfig:
    values = {
        "enabled": True,
        "dry_run": True,
        "account_id": 123,
        "max_contracts": 1,
        "require_gate_open": True,
        "allow_live": False,
        "projectx_live": False,
        "horizon_bars": 2,
    }
    values.update(overrides)
    return ExecutionConfig(**values)


def decision(direction: int, stop_ticks: float | None = 7.5) -> dict:
    return {
        "t": "2026-06-11T14:30:00Z",
        "direction": direction,
        "stop_ticks": stop_ticks,
    }


class ExecutionControllerTests(unittest.TestCase):
    def test_stop_ticks_rounds_up_for_projectx_bracket(self) -> None:
        self.assertEqual(stop_ticks_from_payload({"stop_ticks": 7.5}), 8)
        self.assertEqual(stop_ticks_from_payload({"stop_ticks": 1.0}), 1)
        self.assertIsNone(stop_ticks_from_payload({"stop_ticks": ""}))

    def test_gate_closed_blocks_signal_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = ExecutionController(
                client=FakeExecutionClient(),
                config=config(),
                data_dir=Path(directory),
                contract_id="CON.F.US.MNQ.M26",
                gate_open=False,
            )
            controller.startup()

            events = controller.on_decision(decision(1))

            self.assertEqual(events[0].action, "blocked")
            self.assertEqual(events[0].reason, "global_gate_closed")
            self.assertIsNone(controller.position)

    def test_dry_run_can_open_and_time_exit_when_gate_override_is_set(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = ExecutionController(
                client=FakeExecutionClient(),
                config=config(require_gate_open=False),
                data_dir=Path(directory),
                contract_id="CON.F.US.MNQ.M26",
                gate_open=False,
            )
            controller.startup()

            opened = controller.on_decision(decision(1))
            held = controller.on_decision(decision(0))
            closed = controller.on_decision(decision(0))

            self.assertEqual(opened[0].action, "dry_run_open")
            self.assertEqual(held, [])
            self.assertEqual(closed[0].action, "dry_run_close")
            self.assertEqual(closed[0].reason, "time_exit")
            self.assertIsNone(controller.position)

    def test_real_mode_places_and_closes_practice_order(self) -> None:
        client = FakeExecutionClient()
        with tempfile.TemporaryDirectory() as directory:
            controller = ExecutionController(
                client=client,
                config=config(dry_run=False, require_gate_open=False),
                data_dir=Path(directory),
                contract_id="CON.F.US.MNQ.M26",
                gate_open=False,
            )
            controller.startup()

            opened = controller.on_decision(decision(-1, stop_ticks=6.25))
            controller.position = type("ManagedPosition", (), {"direction": -1, "size": 1, "bars_held": 1})()
            client.positions = [
                type(
                    "OpenPosition",
                    (),
                    {"contract_id": "CON.F.US.MNQ.M26", "type": 2, "size": 1},
                )()
            ]
            closed = controller.on_decision(decision(0))

            self.assertEqual(opened[0].action, "open")
            self.assertEqual(client.orders[0]["side"], 1)  # sell/ask
            self.assertEqual(client.orders[0]["stop_loss_ticks"], 7)
            self.assertEqual(closed[0].action, "close")
            self.assertEqual(client.closed[0]["contract_id"], "CON.F.US.MNQ.M26")


if __name__ == "__main__":
    unittest.main()
