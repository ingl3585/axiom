from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import csv
import json
from typing import Any, Iterable

from .projectx import BarUnit, parse_utc_datetime
from .qa import event_timestamp, fmt_dt, parse_dt
from .storage import bars_csv_path, ensure_parent, safe_partition_value, write_bars_csv


@dataclass(frozen=True)
class NormalizedFile:
    name: str
    source: Path
    path: Path
    rows: int


def normalize_bars_history_json(raw_path: Path, data_dir: Path) -> NormalizedFile:
    payload = json.loads(raw_path.read_text(encoding="utf-8"))
    contract_id = str(payload["contractId"])
    unit = BarUnit(int(payload["unit"]))
    unit_number = int(payload.get("unitNumber", 1))
    start = parse_utc_datetime(str(payload["startTime"]))
    end = parse_utc_datetime(str(payload["endTime"]))
    bars = payload.get("bars", [])
    if not isinstance(bars, list):
        raise ValueError(f"Expected bars list in {raw_path}")

    output_path = bars_csv_path(data_dir, contract_id, unit, unit_number, start, end)
    write_bars_csv(output_path, [row for row in bars if isinstance(row, dict)])
    return NormalizedFile("bars", raw_path, output_path, len(bars))


def normalize_realtime_dir(raw_dir: Path, data_dir: Path) -> list[NormalizedFile]:
    outputs: list[NormalizedFile] = []
    for name in ("quotes", "trades", "depth"):
        source = raw_dir / f"{name}.jsonl"
        if not source.exists():
            continue
        rows = list(iter_realtime_rows(source, name))
        output = realtime_bronze_path(data_dir, raw_dir, name)
        write_csv(output, rows, realtime_fieldnames(name))
        outputs.append(NormalizedFile(name, source, output, len(rows)))
    return outputs


def iter_realtime_rows(source: Path, name: str) -> Iterable[dict[str, Any]]:
    with source.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except json.JSONDecodeError as exc:
                yield parse_error_row(name, source, line_number, str(exc))
                continue

            observed_at = parse_dt(frame.get("observedAt"))
            frame_contract_id = frame.get("contractId")
            payload = frame.get("data")
            records = payload if isinstance(payload, list) else [payload]
            for batch_index, record in enumerate(records):
                if not isinstance(record, dict):
                    continue
                base = base_event_row(
                    name,
                    source,
                    line_number,
                    batch_index,
                    observed_at,
                    event_timestamp(name, record),
                    str(record.get("contractId") or record.get("contract") or frame_contract_id or ""),
                )
                if name == "quotes":
                    yield normalize_quote_row(base, record)
                elif name == "trades":
                    yield normalize_trade_row(base, record)
                elif name == "depth":
                    yield normalize_depth_row(base, record)


def base_event_row(
    name: str,
    source: Path,
    line_number: int,
    batch_index: int,
    observed_at: Any,
    event_time: Any,
    contract_id: str,
) -> dict[str, Any]:
    lag_ms = ""
    if observed_at is not None and event_time is not None:
        lag_ms = round((observed_at - event_time).total_seconds() * 1000, 3)
    return {
        "source": str(source),
        "source_line": line_number,
        "batch_index": batch_index,
        "event_name": name,
        "observed_at": fmt_dt(observed_at) if observed_at else "",
        "event_time": fmt_dt(event_time) if event_time else "",
        "lag_ms": lag_ms,
        "contract_id": contract_id,
    }


def normalize_quote_row(base: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    bid = number_or_empty(record.get("bestBid"))
    ask = number_or_empty(record.get("bestAsk"))
    spread = ""
    if bid != "" and ask != "":
        spread = round(float(ask) - float(bid), 6)
    base.update(
        {
            "symbol": record.get("symbol", ""),
            "symbol_name": record.get("symbolName", ""),
            "best_bid": bid,
            "best_ask": ask,
            "spread": spread,
            "last_price": number_or_empty(record.get("lastPrice")),
            "session_open": number_or_empty(record.get("open")),
            "session_high": number_or_empty(record.get("high")),
            "session_low": number_or_empty(record.get("low")),
            "session_volume": number_or_empty(record.get("volume")),
            "change": number_or_empty(record.get("change")),
            "change_percent": number_or_empty(record.get("changePercent")),
            "source_timestamp": fmt_dt(parse_dt(str(record.get("timestamp") or ""))),
            "last_updated": fmt_dt(parse_dt(str(record.get("lastUpdated") or ""))),
        }
    )
    return base


def normalize_trade_row(base: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    base.update(
        {
            "symbol_id": record.get("symbolId", ""),
            "price": number_or_empty(record.get("price")),
            "volume": number_or_empty(record.get("volume")),
            "trade_type": number_or_empty(record.get("type")),
        }
    )
    return base


def normalize_depth_row(base: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    base.update(
        {
            "price": number_or_empty(record.get("price")),
            "volume": number_or_empty(record.get("volume")),
            "current_volume": number_or_empty(record.get("currentVolume")),
            "depth_type": number_or_empty(record.get("type")),
        }
    )
    return base


def parse_error_row(name: str, source: Path, line_number: int, error: str) -> dict[str, Any]:
    row = {field: "" for field in realtime_fieldnames(name)}
    row.update(
        {
            "source": str(source),
            "source_line": line_number,
            "event_name": name,
            "parse_error": error,
        }
    )
    return row


def number_or_empty(value: Any) -> Any:
    if value is None:
        return ""
    return value


def realtime_bronze_path(data_dir: Path, raw_dir: Path, name: str) -> Path:
    date_part = next((part for part in raw_dir.parts if part.startswith("date=")), "date=unknown")
    contract_part = next(
        (part for part in raw_dir.parts if part.startswith("contract=")),
        "contract=unknown",
    )
    return (
        data_dir
        / "bronze"
        / "projectx"
        / name
        / date_part
        / contract_part
        / f"{name}.csv"
    )


def realtime_fieldnames(name: str) -> list[str]:
    common = [
        "source",
        "source_line",
        "batch_index",
        "event_name",
        "observed_at",
        "event_time",
        "lag_ms",
        "contract_id",
    ]
    if name == "quotes":
        return common + [
            "symbol",
            "symbol_name",
            "best_bid",
            "best_ask",
            "spread",
            "last_price",
            "session_open",
            "session_high",
            "session_low",
            "session_volume",
            "change",
            "change_percent",
            "source_timestamp",
            "last_updated",
            "parse_error",
        ]
    if name == "trades":
        return common + ["symbol_id", "price", "volume", "trade_type", "parse_error"]
    if name == "depth":
        return common + ["price", "volume", "current_volume", "depth_type", "parse_error"]
    return common + ["parse_error"]


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> Path:
    ensure_parent(path)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def normalized_manifest_path(data_dir: Path) -> Path:
    return data_dir / "bronze" / "projectx" / "_normalization_manifest.jsonl"


def append_manifest(data_dir: Path, files: list[NormalizedFile]) -> Path:
    path = normalized_manifest_path(data_dir)
    ensure_parent(path)
    with path.open("a", encoding="utf-8") as handle:
        for item in files:
            handle.write(
                json.dumps(
                    {
                        "name": item.name,
                        "source": str(item.source),
                        "path": str(item.path),
                        "rows": item.rows,
                    },
                    sort_keys=True,
                )
            )
            handle.write("\n")
    return path


def contract_from_partition(path: Path) -> str:
    contract_part = next(
        (part for part in path.parts if part.startswith("contract=")),
        "contract=unknown",
    )
    return safe_partition_value(contract_part.split("=", 1)[1])

