from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import csv
import math
from typing import Any

from bars import load_continuous_bars, parse_float
from projectx import BarUnit, parse_dt, safe_partition_value
from session import (
    is_rth,
    minutes_since_open,
    minutes_to_nearest_event,
    session_bucket,
    session_day,
)

DEFAULT_BAR_FEATURE_WINDOWS = [5, 20, 60]
RSI_PERIOD = 9
EMA_FAST_PERIOD = 9
EMA_SLOW_PERIOD = 21


@dataclass(frozen=True)
class BarFeatureConfig:
    data_dir: Path
    contract_part: str
    unit: BarUnit = BarUnit.MINUTE
    unit_number: int = 1
    windows: list[int] | None = None


@dataclass(frozen=True)
class BarFeatureResult:
    path: Path
    rows: int
    bars: int


def build_bar_features(config: BarFeatureConfig) -> BarFeatureResult:
    windows = sorted(set(config.windows or DEFAULT_BAR_FEATURE_WINDOWS))
    bars = load_continuous_bars(
        config.data_dir, config.contract_part, config.unit, config.unit_number
    )
    rows = compute_bar_features(bars, windows)

    output_path = bar_features_path(
        config.data_dir, config.contract_part, config.unit, config.unit_number
    )
    write_features_csv(output_path, rows, bar_feature_fieldnames(windows))
    return BarFeatureResult(path=output_path, rows=len(rows), bars=len(bars))


def compute_bar_features(
    bars: list[dict[str, Any]],
    windows: list[int],
) -> list[dict[str, Any]]:
    """Compute trailing-only indicators from a continuous bar series.

    Every feature looks backward only, so a row never uses information from a
    future bar. Rows without enough history for a given window leave that
    window's columns blank.
    """
    times: list[Any] = []
    opens: list[float | None] = []
    highs: list[float | None] = []
    lows: list[float | None] = []
    closes: list[float] = []
    volumes: list[float] = []
    for bar in bars:
        close = parse_float(bar.get("c"))
        if close is None:
            continue
        times.append(bar.get("t"))
        opens.append(parse_float(bar.get("o")))
        highs.append(parse_float(bar.get("h")))
        lows.append(parse_float(bar.get("l")))
        closes.append(close)
        volumes.append(parse_float(bar.get("v")) or 0.0)

    count = len(closes)
    returns = [0.0] * count
    for index in range(1, count):
        if closes[index - 1]:
            returns[index] = closes[index] / closes[index - 1] - 1

    close_prefix = prefix_sum(closes)
    volume_prefix = prefix_sum(volumes)
    return_prefix = prefix_sum(returns)
    return_sq_prefix = prefix_sum([value * value for value in returns])

    ema_fast = ema(closes, EMA_FAST_PERIOD)
    ema_slow = ema(closes, EMA_SLOW_PERIOD)
    rsi_values = rsi(closes, RSI_PERIOD)
    parsed_times = [parse_dt(value) for value in times]
    session_keys = [session_key(parsed) for parsed in parsed_times]
    vwap_values, vwap_sigma_values = session_vwap(
        highs, lows, closes, volumes, session_keys
    )

    rows: list[dict[str, Any]] = []
    for index in range(count):
        row: dict[str, Any] = {
            "t": times[index],
            "o": opens[index] if opens[index] is not None else "",
            "h": highs[index] if highs[index] is not None else "",
            "l": lows[index] if lows[index] is not None else "",
            "c": closes[index],
            "v": volumes[index],
            "return_1": returns[index] if index >= 1 else "",
            "bar_range": bar_range(highs[index], lows[index], closes[index]),
            f"rsi_{RSI_PERIOD}": blank_if_none(rsi_values[index]),
            f"ema_{EMA_FAST_PERIOD}": blank_if_none(ema_fast[index]),
            f"ema_{EMA_SLOW_PERIOD}": blank_if_none(ema_slow[index]),
            "vwap": blank_if_none(vwap_values[index]),
            "dist_vwap": dist_from(closes[index], vwap_values[index]),
            "vwap_sigma": vwap_zscore(
                closes[index], vwap_values[index], vwap_sigma_values[index]
            ),
        }
        row.update(time_features(parsed_times[index]))
        for window in windows:
            add_window_features(
                row,
                window,
                index,
                closes,
                highs,
                lows,
                volumes,
                close_prefix,
                volume_prefix,
                return_prefix,
                return_sq_prefix,
            )
        rows.append(row)
    return rows


def add_window_features(
    row: dict[str, Any],
    window: int,
    index: int,
    closes: list[float],
    highs: list[float | None],
    lows: list[float | None],
    volumes: list[float],
    close_prefix: list[float],
    volume_prefix: list[float],
    return_prefix: list[float],
    return_sq_prefix: list[float],
) -> None:
    # Momentum: simple return over the trailing window.
    row[f"return_{window}bar"] = (
        closes[index] / closes[index - window] - 1
        if index >= window and closes[index - window]
        else ""
    )

    # Trend: distance of price from its moving average.
    if index >= window - 1:
        sma = window_mean(close_prefix, index - window + 1, index, window)
        row[f"dist_sma_{window}bar"] = closes[index] / sma - 1 if sma else ""
    else:
        row[f"dist_sma_{window}bar"] = ""

    # Volatility: standard deviation of 1-bar returns over the window.
    if index >= window:
        mean = window_mean(return_prefix, index - window + 1, index, window)
        mean_sq = window_mean(return_sq_prefix, index - window + 1, index, window)
        variance = max(0.0, mean_sq - mean * mean)
        row[f"vol_{window}bar"] = math.sqrt(variance)
    else:
        row[f"vol_{window}bar"] = ""

    # Mean-reversion oscillator: where close sits in its recent high/low range.
    row[f"range_pos_{window}bar"] = range_position(closes, highs, lows, index, window)

    # Activity: volume relative to its trailing average.
    if index >= window - 1:
        avg_volume = window_mean(volume_prefix, index - window + 1, index, window)
        row[f"vol_ratio_{window}bar"] = volumes[index] / avg_volume if avg_volume else ""
    else:
        row[f"vol_ratio_{window}bar"] = ""


def range_position(
    closes: list[float],
    highs: list[float | None],
    lows: list[float | None],
    index: int,
    window: int,
) -> float | str:
    if index < window - 1:
        return ""
    window_highs = [value for value in highs[index - window + 1 : index + 1] if value is not None]
    window_lows = [value for value in lows[index - window + 1 : index + 1] if value is not None]
    if not window_highs or not window_lows:
        return ""
    highest = max(window_highs)
    lowest = min(window_lows)
    if highest <= lowest:
        return ""
    return (closes[index] - lowest) / (highest - lowest)


def bar_range(
    high: float | None,
    low: float | None,
    close: float,
) -> float | str:
    if high is None or low is None or not close:
        return ""
    return (high - low) / close


def ema(values: list[float], period: int) -> list[float | None]:
    """Exponential moving average, seeded with the SMA of the first `period` values."""
    count = len(values)
    out: list[float | None] = [None] * count
    if period <= 0 or count < period:
        return out
    alpha = 2.0 / (period + 1)
    average = sum(values[:period]) / period
    out[period - 1] = average
    for index in range(period, count):
        average = alpha * values[index] + (1 - alpha) * average
        out[index] = average
    return out


def rsi(values: list[float], period: int) -> list[float | None]:
    """Wilder's Relative Strength Index (0-100)."""
    count = len(values)
    out: list[float | None] = [None] * count
    if period <= 0 or count <= period:
        return out

    gains = 0.0
    losses = 0.0
    for index in range(1, period + 1):
        change = values[index] - values[index - 1]
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = rsi_from_averages(avg_gain, avg_loss)

    for index in range(period + 1, count):
        change = values[index] - values[index - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period
        out[index] = rsi_from_averages(avg_gain, avg_loss)
    return out


def rsi_from_averages(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def blank_if_none(value: float | None) -> float | str:
    return value if value is not None else ""


def dist_from(close: float, reference: float | None) -> float | str:
    if not reference:
        return ""
    return close / reference - 1


def session_key(parsed: datetime | None) -> str:
    """Trading-day key for VWAP/session resets, anchored at the 09:30 ET open."""
    if parsed is None:
        return ""
    return session_day(parsed).isoformat()


def vwap_by_session(
    highs: list[float | None],
    lows: list[float | None],
    closes: list[float],
    volumes: list[float],
    session_keys: list[str],
) -> list[float | None]:
    vwap, _ = session_vwap(highs, lows, closes, volumes, session_keys)
    return vwap


def session_vwap(
    highs: list[float | None],
    lows: list[float | None],
    closes: list[float],
    volumes: list[float],
    session_keys: list[str],
) -> tuple[list[float | None], list[float | None]]:
    """Session VWAP and its volume-weighted standard deviation (for sigma bands).

    Resets at the start of each session. Uses typical price (high+low+close)/3
    per bar, falling back to close when a bar has no high/low.
    """
    count = len(closes)
    vwap: list[float | None] = [None] * count
    sigma: list[float | None] = [None] * count
    cumulative_pv = 0.0
    cumulative_pv2 = 0.0
    cumulative_volume = 0.0
    current_session: str | None = None
    for index in range(count):
        if session_keys[index] != current_session:
            current_session = session_keys[index]
            cumulative_pv = 0.0
            cumulative_pv2 = 0.0
            cumulative_volume = 0.0
        high = highs[index]
        low = lows[index]
        close = closes[index]
        typical = (high + low + close) / 3 if high is not None and low is not None else close
        volume = volumes[index]
        cumulative_pv += typical * volume
        cumulative_pv2 += typical * typical * volume
        cumulative_volume += volume
        if cumulative_volume > 0:
            mean = cumulative_pv / cumulative_volume
            variance = max(0.0, cumulative_pv2 / cumulative_volume - mean * mean)
            vwap[index] = mean
            sigma[index] = math.sqrt(variance)
    return vwap, sigma


def vwap_zscore(close: float, vwap: float | None, sigma: float | None) -> float | str:
    if not vwap or not sigma:
        return ""
    return (close - vwap) / sigma


def time_features(parsed: datetime | None) -> dict[str, Any]:
    if parsed is None:
        return {
            "minutes_since_open": "",
            "session_bucket": "",
            "is_rth": "",
            "minutes_to_event": "",
        }
    return {
        "minutes_since_open": minutes_since_open(parsed),
        "session_bucket": session_bucket(parsed),
        "is_rth": 1 if is_rth(parsed) else 0,
        "minutes_to_event": minutes_to_nearest_event(parsed),
    }


def bar_feature_fieldnames(windows: list[int]) -> list[str]:
    fields = [
        "t",
        "o",
        "h",
        "l",
        "c",
        "v",
        "return_1",
        "bar_range",
        f"rsi_{RSI_PERIOD}",
        f"ema_{EMA_FAST_PERIOD}",
        f"ema_{EMA_SLOW_PERIOD}",
        "vwap",
        "dist_vwap",
        "vwap_sigma",
        "minutes_since_open",
        "session_bucket",
        "is_rth",
        "minutes_to_event",
    ]
    for window in windows:
        fields.extend(
            [
                f"return_{window}bar",
                f"dist_sma_{window}bar",
                f"vol_{window}bar",
                f"range_pos_{window}bar",
                f"vol_ratio_{window}bar",
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


def window_mean(prefix: list[float], start_index: int, end_index: int, window: int) -> float:
    return (prefix[end_index + 1] - prefix[start_index]) / window


def bar_features_path(
    data_dir: Path,
    contract_part: str,
    unit: BarUnit,
    unit_number: int,
) -> Path:
    return (
        data_dir
        / "silver"
        / "projectx"
        / "features"
        / "bars"
        / contract_part
        / f"unit={unit.name.lower()}_{unit_number}"
        / "features.csv"
    )


def contract_part_from_id(contract_id: str) -> str:
    return f"contract={safe_partition_value(contract_id)}"


def write_features_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path
