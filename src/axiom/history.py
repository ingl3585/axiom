from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
import json
from typing import Any

from .projectx import (
    BarUnit,
    Contract,
    ProjectXClient,
    compact_utc,
    iso_utc,
    parse_utc_datetime,
    safe_partition_value,
    unit_seconds,
)


@dataclass(frozen=True)
class HistoryBackfillResult:
    symbol: str
    contract: Contract
    unit: BarUnit
    unit_number: int
    start: datetime
    end: datetime
    raw_files: list[Path]
    bars: int
    state_path: Path
    skipped: bool = False
    reason: str = ""


def history_state_path(data_dir: Path) -> Path:
    return data_dir / "state" / "history_state.json"


def write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def history_raw_path(
    data_dir: Path,
    contract_id: str,
    unit: BarUnit,
    unit_number: int,
    start: datetime,
    end: datetime,
) -> Path:
    contract = safe_partition_value(contract_id)
    unit_name = unit.name.lower()
    return (
        data_dir
        / "raw"
        / "projectx"
        / "history"
        / f"contract={contract}"
        / f"unit={unit_name}_{unit_number}"
        / f"{compact_utc(start)}_{compact_utc(end)}.json"
    )


def load_history_state(data_dir: Path) -> dict[str, Any]:
    path = history_state_path(data_dir)
    if not path.exists():
        return {"version": 1, "contracts": {}}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return {"version": 1, "contracts": {}}
    payload.setdefault("version", 1)
    payload.setdefault("contracts", {})
    return payload


def save_history_state(data_dir: Path, state: dict[str, Any]) -> Path:
    path = history_state_path(data_dir)
    write_json(path, state)
    return path


def history_state_key(
    contract_id: str,
    unit: BarUnit,
    unit_number: int,
    live: bool,
) -> str:
    account_type = "live" if live else "sim"
    return f"{contract_id}|{unit.name.lower()}_{unit_number}|{account_type}"


def backfill_historical_bars(
    *,
    client: ProjectXClient,
    data_dir: Path,
    symbol: str,
    contract: Contract,
    unit: BarUnit = BarUnit.MINUTE,
    unit_number: int = 1,
    initial_days: int = 30,
    live: bool = False,
    end: datetime | None = None,
    limit: int = 20_000,
) -> HistoryBackfillResult:
    end_utc = (end or datetime.now(UTC)).replace(microsecond=0)
    state = load_history_state(data_dir)
    key = history_state_key(contract.id, unit, unit_number, live)
    entry = state["contracts"].get(key, {})

    start_utc = backfill_start_time(
        data_dir=data_dir,
        contract_id=contract.id,
        unit=unit,
        unit_number=unit_number,
        entry=entry,
        end=end_utc,
        initial_days=initial_days,
    )

    minimum_seconds = unit_seconds(unit, unit_number)
    if (end_utc - start_utc).total_seconds() < minimum_seconds:
        return HistoryBackfillResult(
            symbol=symbol,
            contract=contract,
            unit=unit,
            unit_number=unit_number,
            start=start_utc,
            end=end_utc,
            raw_files=[],
            bars=0,
            state_path=history_state_path(data_dir),
            skipped=True,
            reason="already current",
        )

    raw_files: list[Path] = []
    total_bars = 0
    retrieved_at = iso_utc(datetime.now(UTC))

    for (window_start, window_end), bars in client.retrieve_bars_chunked(
        contract_id=contract.id,
        start=start_utc,
        end=end_utc,
        unit=unit,
        unit_number=unit_number,
        live=live,
        limit=limit,
        include_partial_bar=False,
    ):
        raw_path = history_raw_path(
            data_dir,
            contract.id,
            unit,
            unit_number,
            window_start,
            window_end,
        )
        write_json(
            raw_path,
            {
                "symbol": symbol,
                "contractId": contract.id,
                "contractName": contract.name,
                "contractDescription": contract.description,
                "live": live,
                "startTime": iso_utc(window_start),
                "endTime": iso_utc(window_end),
                "unit": int(unit),
                "unitNumber": unit_number,
                "retrievedAt": retrieved_at,
                "bars": bars,
            },
        )
        raw_files.append(raw_path)
        total_bars += len(bars)

    state["contracts"][key] = {
        "symbol": symbol,
        "contractId": contract.id,
        "contractName": contract.name,
        "contractDescription": contract.description,
        "unit": unit.name.lower(),
        "unitNumber": unit_number,
        "live": live,
        "lastEndTime": iso_utc(end_utc),
        "updatedAt": retrieved_at,
        "rawFiles": [str(path) for path in raw_files],
    }
    state_path = save_history_state(data_dir, state)

    return HistoryBackfillResult(
        symbol=symbol,
        contract=contract,
        unit=unit,
        unit_number=unit_number,
        start=start_utc,
        end=end_utc,
        raw_files=raw_files,
        bars=total_bars,
        state_path=state_path,
    )


def backfill_start_time(
    *,
    data_dir: Path,
    contract_id: str,
    unit: BarUnit,
    unit_number: int,
    entry: dict[str, Any],
    end: datetime,
    initial_days: int,
) -> datetime:
    last_end = entry.get("lastEndTime")
    if last_end:
        return parse_utc_datetime(str(last_end))

    inferred = latest_raw_history_end(data_dir, contract_id, unit, unit_number)
    if inferred:
        return inferred

    return end - timedelta(days=initial_days)


def latest_raw_history_end(
    data_dir: Path,
    contract_id: str,
    unit: BarUnit,
    unit_number: int,
) -> datetime | None:
    root = (
        data_dir
        / "raw"
        / "projectx"
        / "history"
        / f"contract={safe_partition_value(contract_id)}"
        / f"unit={unit.name.lower()}_{unit_number}"
    )
    if not root.exists():
        return None

    latest: datetime | None = None
    for path in root.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            end_time = parse_utc_datetime(str(payload["endTime"]))
        except (OSError, KeyError, ValueError, json.JSONDecodeError):
            continue
        if latest is None or end_time > latest:
            latest = end_time
    return latest
