from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import json
from typing import Any

from bar_features import (
    DEFAULT_BAR_FEATURE_WINDOWS,
    compute_bar_features,
    contract_part_from_id,
)
from bars import canonical_bar_key, load_continuous_bars
from candidates import SETUPS, Setup, fire_candidates
from projectx import BarUnit
from signals import EdgeLedger, SignalConfig, decide
from state_profile import causal_thresholds, classify_market_state, read_rows

STATE_KEY_PRINT_WIDTH = 64


class LiveSignalEngine:
    """Evaluates the signal engine on live bars as the recorder emits them.

    The edge ledger is frozen at startup (built from the existing states.csv),
    so live decisions are out-of-sample by construction. Decisions are
    observational: they are printed and logged with full receipts, no orders.
    """

    def __init__(
        self,
        ledger: EdgeLedger,
        bars: list[dict[str, Any]] | None = None,
        signal_config: SignalConfig = SignalConfig(),
        windows: list[int] | None = None,
        data_dir: Path | None = None,
        contract_id: str = "",
        setups: tuple[Setup, ...] = SETUPS,
    ):
        self.ledger = ledger
        self.signal_config = signal_config
        self.setups = setups
        self.windows = sorted(set(windows or DEFAULT_BAR_FEATURE_WINDOWS))
        self.data_dir = data_dir
        self.contract_id = contract_id
        self.bars_by_key: dict[str, dict[str, Any]] = {}
        self._offsets: dict[Path, int] = {}
        for bar in bars or []:
            self.ingest_bar(bar)

    @classmethod
    def from_disk(
        cls,
        data_dir: Path,
        contract_id: str,
        unit: BarUnit,
        unit_number: int,
        states_path: Path,
        signal_config: SignalConfig = SignalConfig(),
    ) -> "LiveSignalEngine":
        ledger = EdgeLedger.from_state_rows(read_rows(states_path))
        contract_part = contract_part_from_id(contract_id)
        bars = load_continuous_bars(data_dir, contract_part, unit, unit_number)
        engine = cls(
            ledger=ledger,
            bars=bars,
            signal_config=signal_config,
            data_dir=data_dir,
            contract_id=contract_id,
        )
        # Absorb live bars already on disk without emitting decisions for them.
        engine._read_new_bars()
        return engine

    def ingest_bar(self, bar: dict[str, Any]) -> None:
        key = canonical_bar_key(bar.get("t"))
        if key:
            self.bars_by_key[key] = {**bar, "t": key}

    def evaluate_latest(self) -> dict[str, Any] | None:
        """Classify the newest bar and decide, returning the decision payload."""
        if not self.bars_by_key:
            return None
        rows = [self.bars_by_key[key] for key in sorted(self.bars_by_key)]
        feature_rows = compute_bar_features(rows, self.windows)
        if not feature_rows:
            return None
        thresholds = causal_thresholds(feature_rows)[-1]
        last = dict(feature_rows[-1])
        state = classify_market_state(last, thresholds)
        last["state_key"] = state.key
        last["broad_state_key"] = state.broad_key
        decision = decide(last, self.ledger, self.signal_config)
        prev = dict(feature_rows[-2]) if len(feature_rows) >= 2 else None
        candidates = [
            {
                "setup": candidate.setup_key,
                "direction": candidate.direction,
                "approved": decision.direction != 0
                and decision.direction == candidate.direction,
                "gate_reason": "gate_opposes"
                if decision.direction != 0 and decision.direction != candidate.direction
                else decision.reason,
            }
            for candidate in fire_candidates(last, prev, self.setups)
        ]
        return {
            "t": last.get("t", ""),
            "close": last.get("c", ""),
            "detailed_state_key": state.key,
            "candidates": candidates,
            **decision.to_dict(),
        }

    def poll(self) -> list[dict[str, Any]]:
        """Pick up newly closed bars from the recorder and decide on them."""
        if not self._read_new_bars():
            return []
        payload = self.evaluate_latest()
        if payload is None:
            return []
        self._append_decision(payload)
        return [payload]

    def _read_new_bars(self) -> bool:
        if self.data_dir is None or not self.contract_id:
            return False
        ingested = False
        for path in self._live_bar_files():
            offset = self._offsets.get(path, 0)
            size = path.stat().st_size
            if size <= offset:
                continue
            with path.open(encoding="utf-8") as handle:
                handle.seek(offset)
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        bar = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(bar, dict):
                        self.ingest_bar(bar)
                        ingested = True
                self._offsets[path] = handle.tell()
        return ingested

    def _live_bar_files(self) -> list[Path]:
        root = self.data_dir / "live" / "projectx" / "bars"
        if not root.exists():
            return []
        contract_part = contract_part_from_id(self.contract_id)
        return sorted(root.glob(f"date=*/{contract_part}/bars.jsonl"))

    def _append_decision(self, payload: dict[str, Any]) -> None:
        if self.data_dir is None or not self.contract_id:
            return
        date = datetime.now(UTC).date().isoformat()
        contract_part = contract_part_from_id(self.contract_id)
        path = (
            self.data_dir
            / "live"
            / "projectx"
            / "signals"
            / f"date={date}"
            / contract_part
            / "decisions.jsonl"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")


def format_decision_line(payload: dict[str, Any]) -> str:
    t = payload.get("t", "")
    close = payload.get("close", "")
    direction = payload.get("direction", 0)
    if direction == 0:
        line = f"{t} close={close} FLAT {payload.get('reason', '')}"
    else:
        side = "LONG" if direction > 0 else "SHORT"
        state_key = str(payload.get("state_key", ""))
        if len(state_key) > STATE_KEY_PRINT_WIDTH:
            state_key = state_key[: STATE_KEY_PRINT_WIDTH - 3] + "..."
        line = (
            f"{t} close={close} {side} n={payload.get('n')} "
            f"lcb={_fmt(payload.get('lcb_ticks'))} "
            f"exp={_fmt(payload.get('expected_ticks_net'))} "
            f"stop={_fmt(payload.get('stop_ticks'))} state={state_key}"
        )
    candidates = payload.get("candidates") or []
    if candidates:
        parts = []
        for candidate in candidates:
            side = "LONG" if candidate.get("direction", 0) > 0 else "SHORT"
            status = (
                "approved"
                if candidate.get("approved")
                else f"blocked({candidate.get('gate_reason', '')})"
            )
            parts.append(f"{candidate.get('setup', '')} {side} {status}")
        line += " | cand: " + "; ".join(parts)
    return line


def _fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):+.1f}"
