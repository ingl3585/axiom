from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import csv
import json
import re
from typing import Any

from .projectx import BarUnit, compact_utc


def safe_partition_value(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_")


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, payload: Any) -> Path:
    ensure_parent(path)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def append_jsonl(path: Path, payload: dict[str, Any]) -> Path:
    ensure_parent(path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, separators=(",", ":"), sort_keys=True))
        handle.write("\n")
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


def bars_csv_path(
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
        / "bronze"
        / "projectx"
        / "bars"
        / f"contract={contract}"
        / f"unit={unit_name}_{unit_number}"
        / f"{compact_utc(start)}_{compact_utc(end)}.csv"
    )


def write_bars_csv(path: Path, bars: list[dict[str, Any]]) -> Path:
    ensure_parent(path)
    fieldnames = ["t", "o", "h", "l", "c", "v"]
    unique = {str(row.get("t")): row for row in bars if row.get("t")}
    ordered = [unique[key] for key in sorted(unique)]

    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in ordered:
            writer.writerow({name: row.get(name) for name in fieldnames})
    return path


def realtime_event_path(data_dir: Path, contract_id: str, event_name: str) -> Path:
    today = datetime.now(UTC).date().isoformat()
    contract = safe_partition_value(contract_id)
    filename = {
        "GatewayQuote": "quotes.jsonl",
        "GatewayTrade": "trades.jsonl",
        "GatewayDepth": "depth.jsonl",
    }.get(event_name, f"{safe_partition_value(event_name).lower()}.jsonl")
    return (
        data_dir
        / "raw"
        / "projectx"
        / "realtime"
        / f"date={today}"
        / f"contract={contract}"
        / filename
    )

