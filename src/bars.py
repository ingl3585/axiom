from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import csv
from typing import Any

from projectx import BarUnit, fmt_dt, parse_dt, unit_seconds

BAR_FIELDNAMES = ["t", "o", "h", "l", "c", "v", "bv", "sv"]


@dataclass(frozen=True)
class SessionBarsResult:
    path: Path
    bars: int
    source: Path
    interval_seconds: int


def aggregate_trade_bars(
    trades: list[tuple[Any, ...]],
    interval_seconds: int,
) -> list[dict[str, Any]]:
    """Aggregate trades into clock-aligned OHLCV bars with buy/sell volume.

    Each trade is (epoch_seconds, price, volume[, trade_type]). Bars bucket on
    fixed `interval_seconds` boundaries in UTC: open is the first trade, close the
    last, high/low the extremes, volume the sum. Buy volume (`bv`) is aggressor
    type 0, sell volume (`sv`) is type 1; other types count toward volume only.
    Trades are sorted by time so open/close are correct regardless of input order.
    """
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")

    buckets: dict[int, dict[str, float]] = {}
    for trade in sorted(trades, key=lambda item: item[0]):
        epoch, price, volume = trade[0], trade[1], trade[2]
        trade_type = trade[3] if len(trade) > 3 else None
        if price is None:
            continue
        bucket = int(epoch // interval_seconds) * interval_seconds
        added_volume = volume if volume is not None else 0.0
        buy_volume = added_volume if trade_type == 0 else 0.0
        sell_volume = added_volume if trade_type == 1 else 0.0
        bar = buckets.get(bucket)
        if bar is None:
            buckets[bucket] = {
                "o": price,
                "h": price,
                "l": price,
                "c": price,
                "v": added_volume,
                "bv": buy_volume,
                "sv": sell_volume,
            }
        else:
            bar["h"] = max(bar["h"], price)
            bar["l"] = min(bar["l"], price)
            bar["c"] = price
            bar["v"] += added_volume
            bar["bv"] += buy_volume
            bar["sv"] += sell_volume

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
                "bv": bar["bv"],
                "sv": bar["sv"],
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

    merged: dict[str, dict[str, Any]] = {}
    for key in set(api_bars) | set(live_bars):
        api_row = api_bars.get(key)
        live_row = live_bars.get(key)
        if api_row and live_row:
            # API wins on shared OHLCV; keep live-only fields (bv/sv) it lacks.
            merged[key] = {**live_row, **api_row}
        else:
            merged[key] = api_row or live_row  # type: ignore[assignment]
    return [merged[key] for key in sorted(merged)]


def read_session_trades(
    path: Path,
) -> list[tuple[float, float | None, float | None, int | None]]:
    trades: list[tuple[float, float | None, float | None, int | None]] = []
    with path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            timestamp = parse_dt(row.get("event_time")) or parse_dt(row.get("observed_at"))
            price = parse_float(row.get("price"))
            if timestamp is None or price is None:
                continue
            trades.append(
                (
                    timestamp.timestamp(),
                    price,
                    parse_float(row.get("volume")),
                    parse_int(row.get("trade_type")),
                )
            )
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


def parse_int(value: Any) -> int | None:
    parsed = parse_float(value)
    return int(parsed) if parsed is not None else None
