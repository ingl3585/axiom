from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
import csv
import math
from typing import Any

from .qa import find_latest_file, fmt_dt, parse_dt
from .storage import ensure_parent


DEFAULT_WINDOWS = [1, 5, 30, 60]
DEFAULT_HORIZONS = [5, 15, 30, 60]


@dataclass(frozen=True)
class FeatureBuildResult:
    path: Path
    rows: int
    quote_path: Path
    trade_path: Path | None
    depth_path: Path | None


@dataclass(frozen=True)
class IntradayFeatureConfig:
    data_dir: Path
    quote_path: Path | None = None
    windows_seconds: list[int] | None = None
    horizons_seconds: list[int] | None = None
    interval_seconds: int = 1
    max_stale_quote_seconds: int = 5
    tick_size: float = 0.25


def build_intraday_features(config: IntradayFeatureConfig) -> FeatureBuildResult:
    quote_path = config.quote_path or find_latest_file(
        config.data_dir / "bronze" / "projectx" / "quotes", "quotes.csv"
    )
    if quote_path is None:
        raise ValueError("No bronze quote CSV found. Run `main.py normalize realtime` first.")

    date_part, contract_part = date_contract_partitions(quote_path)
    trade_path = sibling_bronze_path(config.data_dir, "trades", date_part, contract_part)
    depth_path = sibling_bronze_path(config.data_dir, "depth", date_part, contract_part)

    windows = sorted(set(config.windows_seconds or DEFAULT_WINDOWS))
    horizons = sorted(set(config.horizons_seconds or DEFAULT_HORIZONS))
    interval_seconds = config.interval_seconds
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")

    quotes = read_quote_events(quote_path)
    if not quotes:
        raise ValueError(f"No usable quote rows found in {quote_path}")

    trades = read_trade_events(trade_path) if trade_path and trade_path.exists() else []
    depth = read_depth_events(depth_path) if depth_path and depth_path.exists() else []

    start = floor_dt(min(event["time"] for event in quotes), interval_seconds)
    end = floor_dt(max(event["time"] for event in quotes), interval_seconds)
    bucket_count = int((end - start).total_seconds() // interval_seconds) + 1

    arrays = empty_arrays(bucket_count)
    dynamic_depth_types: set[int] = set()
    last_quote_by_bucket: list[dict[str, Any] | None] = [None] * bucket_count

    for event in quotes:
        index = bucket_index(start, event["time"], interval_seconds)
        if not 0 <= index < bucket_count:
            continue
        arrays["quote_updates"][index] += 1
        arrays["spread_sum"][index] += event["spread"]
        arrays["spread_count"][index] += 1
        last_quote_by_bucket[index] = event

    for event in trades:
        index = bucket_index(start, event["time"], interval_seconds)
        if not 0 <= index < bucket_count:
            continue
        arrays["trade_count"][index] += 1
        arrays["trade_volume"][index] += event["volume"]
        if event["trade_type"] == 0:
            arrays["trade_type0_volume"][index] += event["volume"]
        elif event["trade_type"] == 1:
            arrays["trade_type1_volume"][index] += event["volume"]

    for event in depth:
        index = bucket_index(start, event["time"], interval_seconds)
        if not 0 <= index < bucket_count:
            continue
        arrays["depth_updates"][index] += 1
        depth_type = event["depth_type"]
        if depth_type is not None:
            dynamic_depth_types.add(depth_type)
            key = f"depth_type{depth_type}_updates"
            arrays.setdefault(key, [0.0] * bucket_count)
            arrays[key][index] += 1

    mid = [None] * bucket_count
    bid = [None] * bucket_count
    ask = [None] * bucket_count
    seconds_since_quote = [None] * bucket_count
    last_quote_index: int | None = None
    last_mid: float | None = None
    last_bid: float | None = None
    last_ask: float | None = None

    for index, quote in enumerate(last_quote_by_bucket):
        if quote is not None:
            last_quote_index = index
            last_mid = quote["mid"]
            last_bid = quote["bid"]
            last_ask = quote["ask"]
        if last_quote_index is not None:
            mid[index] = last_mid
            bid[index] = last_bid
            ask[index] = last_ask
            seconds_since_quote[index] = (index - last_quote_index) * interval_seconds

    log_return_sq = [0.0] * bucket_count
    for index in range(1, bucket_count):
        if mid[index] and mid[index - 1] and mid[index] > 0 and mid[index - 1] > 0:
            log_return_sq[index] = math.log(mid[index] / mid[index - 1]) ** 2

    prefixes = {name: prefix_sum(values) for name, values in arrays.items()}
    log_return_sq_prefix = prefix_sum(log_return_sq)
    depth_type_keys = [f"depth_type{value}_updates" for value in sorted(dynamic_depth_types)]

    fieldnames = feature_fieldnames(windows, horizons, depth_type_keys)
    rows: list[dict[str, Any]] = []
    for index in range(bucket_count):
        stale = seconds_since_quote[index]
        if mid[index] is None or stale is None or stale > config.max_stale_quote_seconds:
            continue

        row: dict[str, Any] = {
            "timestamp": fmt_dt(start + timedelta(seconds=index * interval_seconds)),
            "contract": contract_part.split("=", 1)[1],
            "interval_seconds": interval_seconds,
            "mid_price": mid[index],
            "best_bid": bid[index],
            "best_ask": ask[index],
            "seconds_since_quote": stale,
        }

        for window in windows:
            add_window_features(
                row,
                window,
                index,
                interval_seconds,
                prefixes,
                log_return_sq_prefix,
                mid,
                depth_type_keys,
            )

        for horizon in horizons:
            future_index = index + math.ceil(horizon / interval_seconds)
            row[f"forward_return_{horizon}s"] = forward_return(
                mid,
                seconds_since_quote,
                index,
                future_index,
                config.max_stale_quote_seconds,
            )
            mfe, mae = forward_excursion_ticks(
                mid,
                seconds_since_quote,
                index,
                future_index,
                config.max_stale_quote_seconds,
                config.tick_size,
            )
            row[f"forward_mfe_ticks_{horizon}s"] = mfe
            row[f"forward_mae_ticks_{horizon}s"] = mae
            row[f"forward_realized_vol_{horizon}s"] = forward_realized_vol(
                log_return_sq_prefix,
                index,
                future_index,
                bucket_count,
            )

        rows.append(row)

    output_path = intraday_feature_path(
        config.data_dir,
        date_part,
        contract_part,
        interval_seconds,
    )
    write_feature_csv(output_path, rows, fieldnames)
    return FeatureBuildResult(output_path, len(rows), quote_path, trade_path, depth_path)


def empty_arrays(bucket_count: int) -> dict[str, list[float]]:
    return {
        "quote_updates": [0.0] * bucket_count,
        "spread_sum": [0.0] * bucket_count,
        "spread_count": [0.0] * bucket_count,
        "trade_count": [0.0] * bucket_count,
        "trade_volume": [0.0] * bucket_count,
        "trade_type0_volume": [0.0] * bucket_count,
        "trade_type1_volume": [0.0] * bucket_count,
        "depth_updates": [0.0] * bucket_count,
    }


def read_quote_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            timestamp = parse_dt(row.get("event_time")) or parse_dt(row.get("observed_at"))
            bid = parse_float(row.get("best_bid"))
            ask = parse_float(row.get("best_ask"))
            spread = parse_float(row.get("spread"))
            if timestamp is None or bid is None or ask is None:
                continue
            if spread is None:
                spread = ask - bid
            events.append(
                {
                    "time": timestamp,
                    "bid": bid,
                    "ask": ask,
                    "spread": spread,
                    "mid": (bid + ask) / 2,
                }
            )
    return sorted(events, key=lambda event: event["time"])


def read_trade_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            timestamp = parse_dt(row.get("event_time")) or parse_dt(row.get("observed_at"))
            volume = parse_float(row.get("volume")) or 0.0
            trade_type = parse_int(row.get("trade_type"))
            if timestamp is None:
                continue
            events.append(
                {
                    "time": timestamp,
                    "volume": volume,
                    "trade_type": trade_type,
                }
            )
    return sorted(events, key=lambda event: event["time"])


def read_depth_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            timestamp = parse_dt(row.get("event_time")) or parse_dt(row.get("observed_at"))
            if timestamp is None:
                continue
            events.append(
                {
                    "time": timestamp,
                    "depth_type": parse_int(row.get("depth_type")),
                }
            )
    return sorted(events, key=lambda event: event["time"])


def add_window_features(
    row: dict[str, Any],
    window: int,
    index: int,
    interval_seconds: int,
    prefixes: dict[str, list[float]],
    log_return_sq_prefix: list[float],
    mid: list[float | None],
    depth_type_keys: list[str],
) -> None:
    bucket_window = max(1, math.ceil(window / interval_seconds))
    start_index = max(0, index - bucket_window + 1)

    quote_updates = window_sum(prefixes["quote_updates"], start_index, index)
    spread_sum = window_sum(prefixes["spread_sum"], start_index, index)
    spread_count = window_sum(prefixes["spread_count"], start_index, index)
    trade_volume = window_sum(prefixes["trade_volume"], start_index, index)
    type0_volume = window_sum(prefixes["trade_type0_volume"], start_index, index)
    type1_volume = window_sum(prefixes["trade_type1_volume"], start_index, index)

    row[f"quote_updates_{window}s"] = quote_updates
    row[f"avg_spread_{window}s"] = spread_sum / spread_count if spread_count else ""
    row[f"trade_count_{window}s"] = window_sum(prefixes["trade_count"], start_index, index)
    row[f"trade_volume_{window}s"] = trade_volume
    row[f"trade_type0_volume_{window}s"] = type0_volume
    row[f"trade_type1_volume_{window}s"] = type1_volume
    row[f"trade_type0_1_imbalance_{window}s"] = (
        (type0_volume - type1_volume) / trade_volume if trade_volume else ""
    )
    row[f"depth_updates_{window}s"] = window_sum(prefixes["depth_updates"], start_index, index)
    for key in depth_type_keys:
        row[f"{key}_{window}s"] = window_sum(prefixes[key], start_index, index)

    trailing_index = index - bucket_window
    row[f"return_{window}s"] = (
        mid[index] / mid[trailing_index] - 1
        if trailing_index >= 0 and mid[index] and mid[trailing_index]
        else ""
    )
    variance = window_sum(log_return_sq_prefix, start_index, index)
    row[f"realized_vol_{window}s"] = math.sqrt(variance) if variance else 0.0


def forward_return(
    mid: list[float | None],
    seconds_since_quote: list[int | None],
    index: int,
    future_index: int,
    max_stale_quote_seconds: int,
) -> float | str:
    if not 0 <= future_index < len(mid):
        return ""
    if not mid[index] or not mid[future_index]:
        return ""
    future_stale = seconds_since_quote[future_index]
    if future_stale is None or future_stale > max_stale_quote_seconds:
        return ""
    return mid[future_index] / mid[index] - 1


def forward_excursion_ticks(
    mid: list[float | None],
    seconds_since_quote: list[int | None],
    index: int,
    future_index: int,
    max_stale_quote_seconds: int,
    tick_size: float,
) -> tuple[float | str, float | str]:
    """Max favorable / adverse excursion (in ticks) over the forward window.

    MFE is the largest mid advance above the snapshot mid; MAE is the largest
    decline below it. Both are signed (MFE >= 0, MAE <= 0) and only consider
    buckets backed by a fresh quote so labels never read stale prices.
    """
    base = mid[index]
    if not 0 <= future_index < len(mid) or not base or tick_size <= 0:
        return "", ""
    highest: float | None = None
    lowest: float | None = None
    for forward in range(index + 1, future_index + 1):
        price = mid[forward]
        stale = seconds_since_quote[forward]
        if price is None or stale is None or stale > max_stale_quote_seconds:
            continue
        highest = price if highest is None else max(highest, price)
        lowest = price if lowest is None else min(lowest, price)
    if highest is None or lowest is None:
        return "", ""
    return (highest - base) / tick_size, (lowest - base) / tick_size


def forward_realized_vol(
    log_return_sq_prefix: list[float],
    index: int,
    future_index: int,
    bucket_count: int,
) -> float | str:
    """Realized volatility (sqrt of summed squared log returns) ahead of index."""
    if not 0 <= future_index < bucket_count or future_index <= index:
        return ""
    variance = window_sum(log_return_sq_prefix, index + 1, future_index)
    return math.sqrt(variance) if variance > 0 else 0.0


def feature_fieldnames(
    windows: list[int],
    horizons: list[int],
    depth_type_keys: list[str],
) -> list[str]:
    fields = [
        "timestamp",
        "contract",
        "interval_seconds",
        "mid_price",
        "best_bid",
        "best_ask",
        "seconds_since_quote",
    ]
    for window in windows:
        fields.extend(
            [
                f"quote_updates_{window}s",
                f"avg_spread_{window}s",
                f"trade_count_{window}s",
                f"trade_volume_{window}s",
                f"trade_type0_volume_{window}s",
                f"trade_type1_volume_{window}s",
                f"trade_type0_1_imbalance_{window}s",
                f"depth_updates_{window}s",
            ]
        )
        fields.extend(f"{key}_{window}s" for key in depth_type_keys)
        fields.extend([f"return_{window}s", f"realized_vol_{window}s"])
    for horizon in horizons:
        fields.extend(
            [
                f"forward_return_{horizon}s",
                f"forward_mfe_ticks_{horizon}s",
                f"forward_mae_ticks_{horizon}s",
                f"forward_realized_vol_{horizon}s",
            ]
        )
    return fields


def prefix_sum(values: list[float]) -> list[float]:
    prefix = [0.0]
    total = 0.0
    for value in values:
        total += value
        prefix.append(total)
    return prefix


def window_sum(prefix: list[float], start_index: int, end_index: int) -> float:
    return prefix[end_index + 1] - prefix[start_index]


def parse_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def floor_dt(value: datetime, interval_seconds: int) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    epoch = int(value.timestamp())
    floored = epoch - (epoch % interval_seconds)
    return datetime.fromtimestamp(floored, tz=UTC)


def bucket_index(start: datetime, value: datetime, interval_seconds: int) -> int:
    return int((value - start).total_seconds() // interval_seconds)


def date_contract_partitions(path: Path) -> tuple[str, str]:
    date_part = next((part for part in path.parts if part.startswith("date=")), None)
    contract_part = next((part for part in path.parts if part.startswith("contract=")), None)
    if not date_part or not contract_part:
        raise ValueError(f"Could not infer date/contract partitions from {path}")
    return date_part, contract_part


def sibling_bronze_path(
    data_dir: Path,
    event_name: str,
    date_part: str,
    contract_part: str,
) -> Path:
    return (
        data_dir
        / "bronze"
        / "projectx"
        / event_name
        / date_part
        / contract_part
        / f"{event_name}.csv"
    )


def intraday_feature_path(
    data_dir: Path,
    date_part: str,
    contract_part: str,
    interval_seconds: int,
) -> Path:
    return (
        data_dir
        / "silver"
        / "projectx"
        / "features"
        / "intraday"
        / date_part
        / contract_part
        / f"features_{interval_seconds}s.csv"
    )


def write_feature_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> Path:
    ensure_parent(path)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path
