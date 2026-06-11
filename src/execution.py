from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from math import ceil
from pathlib import Path
from typing import Any
import json
import uuid

from bar_features import contract_part_from_id
from config import Settings
from projectx import ProjectXClient, ProjectXError

ORDER_SIDE_BUY = 0
ORDER_SIDE_SELL = 1
POSITION_LONG = 1
POSITION_SHORT = 2


@dataclass(frozen=True)
class ExecutionConfig:
    enabled: bool
    dry_run: bool
    account_id: int | None
    max_contracts: int
    require_gate_open: bool
    allow_live: bool
    projectx_live: bool
    horizon_bars: int = 5

    @classmethod
    def from_settings(cls, settings: Settings) -> "ExecutionConfig":
        return cls(
            enabled=settings.execution_enabled,
            dry_run=settings.execution_dry_run,
            account_id=settings.execution_account_id,
            max_contracts=settings.execution_max_contracts,
            require_gate_open=settings.execution_require_gate_open,
            allow_live=settings.execution_allow_live,
            projectx_live=settings.projectx_live,
        )


@dataclass
class ManagedPosition:
    direction: int
    size: int
    bars_held: int = 0


@dataclass(frozen=True)
class ExecutionEvent:
    t: str
    action: str
    reason: str
    direction: int = 0
    size: int = 0
    order_id: int | None = None
    dry_run: bool = True
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "t": self.t,
            "action": self.action,
            "reason": self.reason,
            "direction": self.direction,
            "size": self.size,
            "order_id": self.order_id,
            "dry_run": self.dry_run,
            "message": self.message,
        }


class ExecutionController:
    """Practice-account order bridge for observe-only signal decisions.

    The controller is deliberately conservative: one contract by default,
    no instant reversal, explicit enable flags, ProjectX-live guard, and a
    global-gate interlock unless the user disables it for practice testing.
    """

    def __init__(
        self,
        *,
        client: ProjectXClient,
        config: ExecutionConfig,
        data_dir: Path,
        contract_id: str,
        gate_open: bool,
    ) -> None:
        self.client = client
        self.config = config
        self.data_dir = data_dir
        self.contract_id = contract_id
        self.gate_open = gate_open
        self.ready = False
        self.position: ManagedPosition | None = None

    def startup(self) -> list[ExecutionEvent]:
        events: list[ExecutionEvent] = []
        now = timestamp_now()
        if not self.config.enabled:
            return events
        if self.config.account_id is None:
            return [self._record(ExecutionEvent(now, "disabled", "missing_account_id"))]
        if self.config.projectx_live and not self.config.allow_live:
            return [self._record(ExecutionEvent(now, "disabled", "live_account_guard"))]
        if self.config.max_contracts <= 0:
            return [self._record(ExecutionEvent(now, "disabled", "invalid_size"))]

        try:
            accounts = self.client.search_accounts(only_active_accounts=True)
        except ProjectXError as exc:
            return [
                self._record(
                    ExecutionEvent(now, "disabled", "account_check_failed", message=str(exc))
                )
            ]

        account = next(
            (item for item in accounts if item.id == self.config.account_id),
            None,
        )
        if account is None:
            return [self._record(ExecutionEvent(now, "disabled", "account_not_found"))]
        if not account.can_trade:
            return [self._record(ExecutionEvent(now, "disabled", "account_cannot_trade"))]

        self.ready = True
        self.sync_position()
        events.append(
            self._record(
                ExecutionEvent(
                    now,
                    "ready",
                    "execution_ready",
                    dry_run=self.config.dry_run,
                    message=(
                        f"account={account.id} size={self.config.max_contracts} "
                        f"gate_open={self.gate_open} "
                        f"require_gate_open={self.config.require_gate_open}"
                    ),
                )
            )
        )
        return events

    def on_decision(self, payload: dict[str, Any]) -> list[ExecutionEvent]:
        if not self.ready:
            return []

        t = str(payload.get("t") or timestamp_now())
        decision_direction = int(payload.get("direction") or 0)
        self.sync_position()

        if self.position is not None:
            self.position.bars_held += 1
            if self.position.bars_held >= self.config.horizon_bars:
                return [self.close_position(t, "time_exit")]
            if decision_direction and decision_direction != self.position.direction:
                return [self.close_position(t, "opposite_signal")]
            return []

        if decision_direction == 0:
            return []
        if self.config.require_gate_open and not self.gate_open:
            return [
                self._record(
                    ExecutionEvent(
                        t,
                        "blocked",
                        "global_gate_closed",
                        direction=decision_direction,
                        dry_run=self.config.dry_run,
                    )
                )
            ]

        return [self.open_position(t, decision_direction, payload)]

    def open_position(
        self,
        t: str,
        direction: int,
        payload: dict[str, Any],
    ) -> ExecutionEvent:
        size = self.config.max_contracts
        side = ORDER_SIDE_BUY if direction > 0 else ORDER_SIDE_SELL
        stop_ticks = stop_ticks_from_payload(payload)
        if self.config.dry_run:
            self.position = ManagedPosition(direction=direction, size=size)
            return self._record(
                ExecutionEvent(
                    t,
                    "dry_run_open",
                    "signal",
                    direction=direction,
                    size=size,
                    dry_run=True,
                    message=f"side={side} stop_ticks={stop_ticks or 'n/a'}",
                )
            )

        try:
            result = self.client.place_market_order(
                account_id=require_account_id(self.config),
                contract_id=self.contract_id,
                side=side,
                size=size,
                custom_tag=f"axiom-{uuid.uuid4().hex[:20]}",
                stop_loss_ticks=stop_ticks,
            )
        except ProjectXError as exc:
            return self._record(
                ExecutionEvent(
                    t,
                    "error",
                    "order_failed",
                    direction=direction,
                    size=size,
                    dry_run=False,
                    message=str(exc),
                )
            )

        self.position = ManagedPosition(direction=direction, size=size)
        return self._record(
            ExecutionEvent(
                t,
                "open",
                "signal",
                direction=direction,
                size=size,
                order_id=result.order_id,
                dry_run=False,
                message=f"side={side} stop_ticks={stop_ticks or 'n/a'}",
            )
        )

    def close_position(self, t: str, reason: str) -> ExecutionEvent:
        position = self.position or ManagedPosition(direction=0, size=0)
        if self.config.dry_run:
            self.position = None
            return self._record(
                ExecutionEvent(
                    t,
                    "dry_run_close",
                    reason,
                    direction=position.direction,
                    size=position.size,
                    dry_run=True,
                )
            )

        try:
            self.client.close_contract_position(
                account_id=require_account_id(self.config),
                contract_id=self.contract_id,
            )
        except ProjectXError as exc:
            return self._record(
                ExecutionEvent(
                    t,
                    "error",
                    "close_failed",
                    direction=position.direction,
                    size=position.size,
                    dry_run=False,
                    message=str(exc),
                )
            )
        self.position = None
        return self._record(
            ExecutionEvent(
                t,
                "close",
                reason,
                direction=position.direction,
                size=position.size,
                dry_run=False,
            )
        )

    def sync_position(self) -> None:
        if self.config.dry_run:
            return
        if self.config.account_id is None:
            self.position = None
            return
        try:
            positions = self.client.search_open_positions(self.config.account_id)
        except ProjectXError:
            return
        current = next(
            (item for item in positions if item.contract_id == self.contract_id),
            None,
        )
        if current is None:
            self.position = None
            return
        direction = position_type_direction(current.type)
        if direction == 0:
            return
        bars_held = (
            self.position.bars_held
            if self.position is not None and self.position.direction == direction
            else 0
        )
        self.position = ManagedPosition(
            direction=direction,
            size=current.size,
            bars_held=bars_held,
        )

    def _record(self, event: ExecutionEvent) -> ExecutionEvent:
        path = execution_log_path(self.data_dir, self.contract_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.to_dict(), sort_keys=True) + "\n")
        return event


def stop_ticks_from_payload(payload: dict[str, Any]) -> int | None:
    raw = payload.get("stop_ticks")
    if raw is None or raw == "":
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return max(1, ceil(value))


def position_type_direction(position_type: int) -> int:
    if position_type == POSITION_LONG:
        return 1
    if position_type == POSITION_SHORT:
        return -1
    return 0


def require_account_id(config: ExecutionConfig) -> int:
    if config.account_id is None:
        raise ProjectXError("AXIOM_EXECUTION_ACCOUNT_ID is required for execution.")
    return config.account_id


def execution_log_path(data_dir: Path, contract_id: str) -> Path:
    date = datetime.now(UTC).date().isoformat()
    return (
        data_dir
        / "live"
        / "projectx"
        / "execution"
        / f"date={date}"
        / contract_part_from_id(contract_id)
        / "events.jsonl"
    )


def timestamp_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def format_execution_event(event: ExecutionEvent) -> str:
    if event.action == "ready":
        return f"execution ready ({event.message})"
    if event.action == "blocked":
        side = "LONG" if event.direction > 0 else "SHORT"
        return f"execution blocked {side}: {event.reason}"
    if event.action in {"open", "dry_run_open"}:
        side = "LONG" if event.direction > 0 else "SHORT"
        return f"execution {event.action} {side} size={event.size} {event.message}"
    if event.action in {"close", "dry_run_close"}:
        return f"execution {event.action} reason={event.reason} size={event.size}"
    if event.action == "disabled":
        return f"execution disabled: {event.reason} {event.message}".strip()
    return f"execution {event.action}: {event.reason} {event.message}".strip()
