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
    eastern_minutes,
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
OPENING_RANGE_MINUTES = 30
ROUND_LEVEL = 100.0

# Reference-level features, populated only on RTH bars; blank otherwise.
REFERENCE_FIELDS = (
    "prior_rth_high",
    "prior_rth_low",
    "prior_rth_close",
    "dist_prior_high",
    "dist_prior_low",
    "dist_prior_close",
    "overnight_high",
    "overnight_low",
    "dist_overnight_high",
    "dist_overnight_low",
    "gap",
    "or_high",
    "or_low",
    "or_breakout",
    "dist_or_high",
    "dist_or_low",
)


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
    buy_volumes: list[float | None] = []
    sell_volumes: list[float | None] = []
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
        buy_volumes.append(parse_float(bar.get("bv")))
        sell_volumes.append(parse_float(bar.get("sv")))

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
    reference = compute_session_reference(
        parsed_times, opens, highs, lows, closes, volumes
    )
    order_flow = compute_order_flow(buy_volumes, sell_volumes, volumes, session_keys)

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
            f"dist_ema_{EMA_FAST_PERIOD}": dist_from(closes[index], ema_fast[index]),
            f"dist_ema_{EMA_SLOW_PERIOD}": dist_from(closes[index], ema_slow[index]),
            "vwap": blank_if_none(vwap_values[index]),
            "dist_vwap": dist_from(closes[index], vwap_values[index]),
            "vwap_sigma": vwap_zscore(
                closes[index], vwap_values[index], vwap_sigma_values[index]
            ),
        }
        row.update(time_features(parsed_times[index]))
        row.update(reference[index])
        row.update(order_flow[index])
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


def compute_session_reference(
    parsed_times: list[datetime | None],
    opens: list[float | None],
    highs: list[float | None],
    lows: list[float | None],
    closes: list[float],
    volumes: list[float],
) -> list[dict[str, Any]]:
    """Day-trading reference features in one causal forward pass.

    Opening range, prior-RTH-day levels, overnight high/low, the opening gap,
    relative volume, and distance to the nearest round level. Reference levels
    are exposed on RTH bars only (they are still forming or undefined otherwise),
    and everything uses completed past data only, so there is no lookahead.
    """
    count = len(closes)
    out: list[dict[str, Any]] = []

    cur_rth_date = None
    cur_high = cur_low = cur_close = None
    prior_high = prior_low = prior_close = None
    or_high = or_low = None
    or_complete = False
    overnight_high = overnight_low = None  # accumulating toward the next open
    day_overnight_high = day_overnight_low = None  # frozen at this day's open
    gap = None
    minute_volume: dict[int, list[float]] = {}

    for index in range(count):
        parsed = parsed_times[index]
        close = closes[index]
        high = highs[index]
        low = lows[index]
        volume = volumes[index]
        feat: dict[str, Any] = {}

        if parsed is None:
            feat["rvol"] = ""
            feat["dist_round_100"] = ""
            feat.update(dict.fromkeys(REFERENCE_FIELDS, ""))
            out.append(feat)
            continue

        # Relative volume vs prior bars at the same ET minute-of-day (causal).
        minute = eastern_minutes(parsed)
        bucket = minute_volume.get(minute)
        if bucket and bucket[1] > 0 and bucket[0] > 0:
            feat["rvol"] = volume / (bucket[0] / bucket[1])
        else:
            feat["rvol"] = ""
        if bucket is None:
            minute_volume[minute] = [volume, 1.0]
        else:
            bucket[0] += volume
            bucket[1] += 1.0

        nearest = round(close / ROUND_LEVEL) * ROUND_LEVEL
        feat["dist_round_100"] = (close - nearest) / close if close else ""

        if is_rth(parsed):
            minutes = minutes_since_open(parsed)
            rth_date = session_day(parsed)
            if rth_date != cur_rth_date:
                if cur_rth_date is not None:
                    prior_high, prior_low, prior_close = cur_high, cur_low, cur_close
                cur_rth_date = rth_date
                cur_high, cur_low, cur_close = high, low, close
                day_open = opens[index] if opens[index] is not None else close
                day_overnight_high, day_overnight_low = overnight_high, overnight_low
                overnight_high = overnight_low = None
                gap = (day_open / prior_close - 1) if prior_close else None
                or_high, or_low = high, low
                or_complete = False
            else:
                cur_high = max_opt(cur_high, high)
                cur_low = min_opt(cur_low, low)
                cur_close = close
                if 0 <= minutes < OPENING_RANGE_MINUTES:
                    or_high = max_opt(or_high, high)
                    or_low = min_opt(or_low, low)
            if minutes >= OPENING_RANGE_MINUTES:
                or_complete = True

            feat["prior_rth_high"] = blank_if_none(prior_high)
            feat["prior_rth_low"] = blank_if_none(prior_low)
            feat["prior_rth_close"] = blank_if_none(prior_close)
            feat["dist_prior_high"] = dist_from(close, prior_high)
            feat["dist_prior_low"] = dist_from(close, prior_low)
            feat["dist_prior_close"] = dist_from(close, prior_close)
            feat["overnight_high"] = blank_if_none(day_overnight_high)
            feat["overnight_low"] = blank_if_none(day_overnight_low)
            feat["dist_overnight_high"] = dist_from(close, day_overnight_high)
            feat["dist_overnight_low"] = dist_from(close, day_overnight_low)
            feat["gap"] = blank_if_none(gap)
            if or_complete and or_high is not None and or_low is not None:
                feat["or_high"] = or_high
                feat["or_low"] = or_low
                feat["or_breakout"] = 1 if close > or_high else -1 if close < or_low else 0
                feat["dist_or_high"] = dist_from(close, or_high)
                feat["dist_or_low"] = dist_from(close, or_low)
            else:
                feat["or_high"] = ""
                feat["or_low"] = ""
                feat["or_breakout"] = ""
                feat["dist_or_high"] = ""
                feat["dist_or_low"] = ""
        else:
            overnight_high = max_opt(overnight_high, high)
            overnight_low = min_opt(overnight_low, low)
            feat.update(dict.fromkeys(REFERENCE_FIELDS, ""))

        out.append(feat)
    return out


def compute_order_flow(
    buy_volumes: list[float | None],
    sell_volumes: list[float | None],
    volumes: list[float],
    session_keys: list[str],
) -> list[dict[str, Any]]:
    """Per-bar and session-cumulative order-flow delta (buy minus sell volume).

    `delta` = buy - sell volume, `delta_ratio` = delta / volume (bar pressure,
    -1..1), `cum_delta` = running delta since the session open. Bars without
    aggressor data (e.g. API history bars) are blank and do not advance the
    cumulative.
    """
    count = len(volumes)
    out: list[dict[str, Any]] = []
    cumulative = 0.0
    cumulative_volume = 0.0
    current_session: str | None = None
    for index in range(count):
        if session_keys[index] != current_session:
            current_session = session_keys[index]
            cumulative = 0.0
            cumulative_volume = 0.0
        buy = buy_volumes[index]
        sell = sell_volumes[index]
        if buy is None or sell is None:
            out.append(
                {"delta": "", "delta_ratio": "", "cum_delta": "", "cum_delta_ratio": ""}
            )
            continue
        delta = buy - sell
        cumulative += delta
        volume = volumes[index]
        cumulative_volume += volume
        out.append(
            {
                "delta": delta,
                "delta_ratio": delta / volume if volume else "",
                "cum_delta": cumulative,
                "cum_delta_ratio": cumulative / cumulative_volume if cumulative_volume else "",
            }
        )
    return out


def max_opt(current: float | None, value: float | None) -> float | None:
    if value is None:
        return current
    if current is None:
        return value
    return max(current, value)


def min_opt(current: float | None, value: float | None) -> float | None:
    if value is None:
        return current
    if current is None:
        return value
    return min(current, value)


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
        f"dist_ema_{EMA_FAST_PERIOD}",
        f"dist_ema_{EMA_SLOW_PERIOD}",
        "vwap",
        "dist_vwap",
        "vwap_sigma",
        "minutes_since_open",
        "session_bucket",
        "is_rth",
        "minutes_to_event",
        "rvol",
        "dist_round_100",
        *REFERENCE_FIELDS,
        "delta",
        "delta_ratio",
        "cum_delta",
        "cum_delta_ratio",
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


def non_model_columns() -> set[str]:
    """Identifiers and raw price/volume levels — non-stationary.

    These are useful as reference/levels (plotting, rules like "near prior
    high"), but a model should not train on them directly.
    """
    return {
        "t",
        "o",
        "h",
        "l",
        "c",
        "v",
        f"ema_{EMA_FAST_PERIOD}",
        f"ema_{EMA_SLOW_PERIOD}",
        "vwap",
        "prior_rth_high",
        "prior_rth_low",
        "prior_rth_close",
        "overnight_high",
        "overnight_low",
        "or_high",
        "or_low",
        "delta",
        "cum_delta",
    }


def model_feature_columns(windows: list[int] | None = None) -> list[str]:
    """The stationary, model-ready subset of the feature table.

    Excludes timestamp/OHLCV identifiers and raw price/volume levels, leaving the
    distances, ratios, oscillators, and bounded session/time features a model can
    actually generalize from.
    """
    selected = sorted(set(windows or DEFAULT_BAR_FEATURE_WINDOWS))
    exclude = non_model_columns()
    return [name for name in bar_feature_fieldnames(selected) if name not in exclude]


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
