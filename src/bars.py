from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import csv
from typing import Any

from projectx import BarUnit, fmt_dt, parse_dt, unit_seconds

BAR_FIELDNAMES = ["t", "o", "h", "l", "c", "v"]


@dataclass(frozen=True)
class SessionBarsResult:
    path: Path
    bars: int
    source: Path
    interval_seconds: int


def aggregate_trade_bars(
    trades: list[tuple[float, float | None, float | None]],
    interval_seconds: int,
) -> list[dict[str, Any]]:
    """Aggregate (epoch_seconds, price, volume) trades into clock-aligned OHLCV bars.

    Bars are bucketed on fixed `interval_seconds` boundaries in UTC. Open is the
    first trade in the bucket, close the last, high/low the extremes, volume the
    sum. Trades are sorted by time so open/close are correct regardless of input
    order.
    """
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")

    buckets: dict[int, dict[str, float]] = {}
    for epoch, price, volume in sorted(trades, key=lambda item: item[0]):
        if price is None:
            continue
        bucket = int(epoch // interval_seconds) * interval_seconds
        added_volume = volume if volume is not None else 0.0
        bar = buckets.get(bucket)
        if bar is None:
            buckets[bucket] = {
                "o": price,
                "h": price,
                "l": price,
                "c": price,
                "v": added_volume,
            }
        else:
            bar["h"] = max(bar["h"], price)
            bar["l"] = min(bar["l"], price)
            bar["c"] = price
            bar["v"] += added_volume

    rows: list[dict[str, Any]] = []
    for bucket in sorted(buckets):
        bar = buckets[bucket]
        rows.append(
            {
                "t": fmt_dt(datetime.fromtimestamp(bucket, tz=UTC)),
                "o": bar["o"],
                "h": bar["h"],
                "l": bar["l"],
                "c": bar["c"],
                "v": bar["v"],
            }
        )
    return rows


def build_session_bars(
    data_dir: Path,
    trades_path: Path,
    unit: BarUnit = BarUnit.MINUTE,
    unit_number: int = 1,
) -> SessionBarsResult:
    """Build OHLCV bars from a normalized bronze trades CSV for one session.

    Output is written next to the API history bars (same contract/unit
    partition) as a `live_<date>.csv` file, so history + live form one
    continuous series. API bars stay canonical at any overlap.
    """
    interval_seconds = unit_seconds(unit, unit_number)
    date_part, contract_part = date_contract_partitions(trades_path)
    trades = read_session_trades(trades_path)
    rows = aggregate_trade_bars(trades, interval_seconds)

    output_path = (
        bars_partition_dir(data_dir, contract_part, unit, unit_number)
        / f"live_{date_part.split('=', 1)[1]}.csv"
    )
    write_bars_csv(output_path, rows)
    return SessionBarsResult(
        path=output_path,
        bars=len(rows),
        source=trades_path,
        interval_seconds=interval_seconds,
    )


def load_continuous_bars(
    data_dir: Path,
    contract_part: str,
    unit: BarUnit = BarUnit.MINUTE,
    unit_number: int = 1,
) -> list[dict[str, Any]]:
    """Stitch every bar CSV in the partition into one continuous series.

    Bars are keyed by canonical UTC timestamp and de-duplicated. API history
    bars take precedence over live-built bars wherever both cover a timestamp.
    """
    partition = bars_partition_dir(data_dir, contract_part, unit, unit_number)
    if not partition.exists():
        return []

    api_bars: dict[str, dict[str, Any]] = {}
    live_bars: dict[str, dict[str, Any]] = {}
    for csv_path in sorted(partition.glob("*.csv")):
        target = live_bars if csv_path.name.startswith("live") else api_bars
        with csv_path.open(encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                key = canonical_bar_key(row.get("t"))
                if key:
                    target[key] = {**row, "t": key}

    merged = dict(live_bars)
    merged.update(api_bars)
    return [merged[key] for key in sorted(merged)]


def read_session_trades(path: Path) -> list[tuple[float, float | None, float | None]]:
    trades: list[tuple[float, float | None, float | None]] = []
    with path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            timestamp = parse_dt(row.get("event_time")) or parse_dt(row.get("observed_at"))
            price = parse_float(row.get("price"))
            if timestamp is None or price is None:
                continue
            trades.append((timestamp.timestamp(), price, parse_float(row.get("volume"))))
    return trades


def write_bars_csv(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=BAR_FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def bars_partition_dir(
    data_dir: Path,
    contract_part: str,
    unit: BarUnit,
    unit_number: int,
) -> Path:
    return (
        data_dir
        / "bronze"
        / "projectx"
        / "bars"
        / contract_part
        / f"unit={unit.name.lower()}_{unit_number}"
    )


def date_contract_partitions(path: Path) -> tuple[str, str]:
    date_part = next((part for part in path.parts if part.startswith("date=")), None)
    contract_part = next((part for part in path.parts if part.startswith("contract=")), None)
    if not date_part or not contract_part:
        raise ValueError(f"Could not infer date/contract partitions from {path}")
    return date_part, contract_part


def canonical_bar_key(value: Any) -> str:
    parsed = parse_dt(value)
    return fmt_dt(parsed) if parsed else ""


def parse_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
