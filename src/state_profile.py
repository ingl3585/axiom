from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import csv
import json
import math
from typing import Any

from bars import parse_float

DEFAULT_HORIZON_BARS = 5
DEFAULT_TICK_SIZE = 0.25
SUMMARY_LIMIT = 15

STATE_DIMENSIONS = [
    "session_state",
    "trend_state",
    "volatility_state",
    "activity_state",
    "location_state",
    "structure_state",
    "flow_state",
    "rsi_state",
]


@dataclass(frozen=True)
class StateProfileConfig:
    data_dir: Path
    feature_path: Path
    horizon_bars: int = DEFAULT_HORIZON_BARS
    tick_size: float = DEFAULT_TICK_SIZE
    min_count: int = 3


@dataclass(frozen=True)
class MarketState:
    session_state: str
    trend_state: str
    volatility_state: str
    activity_state: str
    location_state: str
    structure_state: str
    flow_state: str
    rsi_state: str

    @property
    def key(self) -> str:
        return "|".join(
            [
                self.session_state,
                self.trend_state,
                self.volatility_state,
                self.activity_state,
                self.location_state,
                self.structure_state,
                self.flow_state,
                self.rsi_state,
            ]
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "session_state": self.session_state,
            "trend_state": self.trend_state,
            "volatility_state": self.volatility_state,
            "activity_state": self.activity_state,
            "location_state": self.location_state,
            "structure_state": self.structure_state,
            "flow_state": self.flow_state,
            "rsi_state": self.rsi_state,
            "state_key": self.key,
        }


@dataclass(frozen=True)
class ProfileThresholds:
    volatility_low: float | None
    volatility_high: float | None


@dataclass(frozen=True)
class ForwardOutcome:
    forward_return: float
    forward_ticks: float
    mfe_ticks: float
    mae_ticks: float


@dataclass(frozen=True)
class StateProfileResult:
    feature_path: Path
    rows_path: Path
    markdown_path: Path
    json_path: Path
    rows: int
    labeled_rows: int
    states: int


@dataclass
class SummaryStats:
    count: int = 0
    wins: int = 0
    sum_forward_ticks: float = 0.0
    sum_mfe_ticks: float = 0.0
    sum_mae_ticks: float = 0.0

    def add(self, outcome: ForwardOutcome) -> None:
        self.count += 1
        self.wins += 1 if outcome.forward_ticks > 0 else 0
        self.sum_forward_ticks += outcome.forward_ticks
        self.sum_mfe_ticks += outcome.mfe_ticks
        self.sum_mae_ticks += outcome.mae_ticks

    def to_dict(self, name: str) -> dict[str, Any]:
        return {
            "name": name,
            "count": self.count,
            "win_rate": self.wins / self.count if self.count else None,
            "avg_forward_ticks": self.sum_forward_ticks / self.count if self.count else None,
            "avg_mfe_ticks": self.sum_mfe_ticks / self.count if self.count else None,
            "avg_mae_ticks": self.sum_mae_ticks / self.count if self.count else None,
        }


def build_state_profile(config: StateProfileConfig) -> StateProfileResult:
    if config.horizon_bars <= 0:
        raise ValueError("horizon_bars must be positive")
    if config.tick_size <= 0:
        raise ValueError("tick_size must be positive")

    rows = read_rows(config.feature_path)
    thresholds = profile_thresholds(rows)
    profiled_rows = profile_rows(rows, thresholds, config.horizon_bars, config.tick_size)

    output_dir = state_profile_dir(config.data_dir, config.feature_path)
    rows_path = output_dir / "states.csv"
    markdown_path = output_dir / "summary.md"
    json_path = output_dir / "summary.json"

    write_csv(rows_path, profiled_rows, state_fieldnames(config.horizon_bars))
    payload = profile_payload(
        feature_path=config.feature_path,
        rows_path=rows_path,
        rows=profiled_rows,
        thresholds=thresholds,
        horizon_bars=config.horizon_bars,
        min_count=config.min_count,
    )
    write_json(json_path, payload)
    markdown_path.write_text(profile_markdown(payload), encoding="utf-8")

    return StateProfileResult(
        feature_path=config.feature_path,
        rows_path=rows_path,
        markdown_path=markdown_path,
        json_path=json_path,
        rows=len(profiled_rows),
        labeled_rows=sum(1 for row in profiled_rows if row.get("has_forward_outcome") == "1"),
        states=len(payload["state_summaries"]),
    )


def profile_rows(
    rows: list[dict[str, str]],
    thresholds: ProfileThresholds,
    horizon_bars: int,
    tick_size: float,
) -> list[dict[str, Any]]:
    profiled: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        state = classify_market_state(row, thresholds)
        outcome = forward_outcome(rows, index, horizon_bars, tick_size)
        output: dict[str, Any] = {
            "t": row.get("t", ""),
            "c": row.get("c", ""),
            **state.to_dict(),
            "has_forward_outcome": "1" if outcome else "0",
            f"forward_return_{horizon_bars}bar": "",
            f"forward_ticks_{horizon_bars}bar": "",
            f"forward_mfe_ticks_{horizon_bars}bar": "",
            f"forward_mae_ticks_{horizon_bars}bar": "",
        }
        if outcome:
            output[f"forward_return_{horizon_bars}bar"] = outcome.forward_return
            output[f"forward_ticks_{horizon_bars}bar"] = outcome.forward_ticks
            output[f"forward_mfe_ticks_{horizon_bars}bar"] = outcome.mfe_ticks
            output[f"forward_mae_ticks_{horizon_bars}bar"] = outcome.mae_ticks
        profiled.append(output)
    return profiled


def classify_market_state(row: dict[str, str], thresholds: ProfileThresholds) -> MarketState:
    return MarketState(
        session_state=nonempty(row.get("session_bucket"), "unknown_session"),
        trend_state=trend_state(row),
        volatility_state=volatility_state(row, thresholds),
        activity_state=activity_state(row),
        location_state=location_state(row),
        structure_state=structure_state(row),
        flow_state=flow_state(row),
        rsi_state=rsi_state(row),
    )


def trend_state(row: dict[str, str]) -> str:
    ret20 = parse_float(row.get("return_20bar"))
    fast = parse_float(row.get("dist_ema_9"))
    slow = parse_float(row.get("dist_ema_21"))
    if ret20 is None or fast is None or slow is None:
        return "trend_unknown"
    if abs(ret20) < 0.0005:
        return "flat"
    if ret20 > 0 and fast > 0 and slow > 0:
        return "trend_up"
    if ret20 < 0 and fast < 0 and slow < 0:
        return "trend_down"
    return "mixed_trend"


def volatility_state(row: dict[str, str], thresholds: ProfileThresholds) -> str:
    vol = parse_float(row.get("vol_20bar"))
    if vol is None:
        return "vol_unknown"
    if thresholds.volatility_low is None or thresholds.volatility_high is None:
        return "vol_normal"
    if vol <= thresholds.volatility_low:
        return "vol_low"
    if vol >= thresholds.volatility_high:
        return "vol_high"
    return "vol_normal"


def activity_state(row: dict[str, str]) -> str:
    ratio = parse_float(row.get("vol_ratio_20bar"))
    if ratio is None:
        return "activity_unknown"
    if ratio >= 1.5:
        return "activity_high"
    if ratio <= 0.75:
        return "activity_low"
    return "activity_normal"


def location_state(row: dict[str, str]) -> str:
    sigma = parse_float(row.get("vwap_sigma"))
    dist = parse_float(row.get("dist_vwap"))
    if sigma is not None:
        if sigma >= 1.5:
            return "extreme_above_vwap"
        if sigma <= -1.5:
            return "extreme_below_vwap"
    if dist is None:
        return "location_unknown"
    if dist > 0:
        return "above_vwap"
    if dist < 0:
        return "below_vwap"
    return "at_vwap"


def structure_state(row: dict[str, str]) -> str:
    breakout = parse_float(row.get("or_breakout"))
    if breakout is None:
        return "structure_unknown"
    if breakout > 0:
        return "or_breakout_up"
    if breakout < 0:
        return "or_breakout_down"
    return "inside_or"


def flow_state(row: dict[str, str]) -> str:
    flow = parse_float(row.get("delta_ratio"))
    if flow is None:
        flow = parse_float(row.get("cum_delta_ratio"))
    if flow is None:
        return "flow_unknown"
    if flow >= 0.20:
        return "buy_pressure"
    if flow <= -0.20:
        return "sell_pressure"
    return "flow_neutral"


def rsi_state(row: dict[str, str]) -> str:
    value = parse_float(row.get("rsi_9"))
    if value is None:
        return "rsi_unknown"
    if value >= 70:
        return "overbought"
    if value <= 30:
        return "oversold"
    if value >= 55:
        return "rsi_bullish"
    if value <= 45:
        return "rsi_bearish"
    return "rsi_neutral"


def forward_outcome(
    rows: list[dict[str, str]],
    index: int,
    horizon_bars: int,
    tick_size: float,
) -> ForwardOutcome | None:
    future_index = index + horizon_bars
    if future_index >= len(rows):
        return None
    close = parse_float(rows[index].get("c"))
    future_close = parse_float(rows[future_index].get("c"))
    if close is None or future_close is None or close <= 0:
        return None

    highs: list[float] = []
    lows: list[float] = []
    for row in rows[index + 1 : future_index + 1]:
        high = parse_float(row.get("h")) or parse_float(row.get("c"))
        low = parse_float(row.get("l")) or parse_float(row.get("c"))
        if high is not None:
            highs.append(high)
        if low is not None:
            lows.append(low)
    if not highs or not lows:
        return None

    return ForwardOutcome(
        forward_return=future_close / close - 1,
        forward_ticks=(future_close - close) / tick_size,
        mfe_ticks=(max(highs) - close) / tick_size,
        mae_ticks=(min(lows) - close) / tick_size,
    )


def profile_thresholds(rows: list[dict[str, str]]) -> ProfileThresholds:
    volatility = [value for row in rows if (value := parse_float(row.get("vol_20bar"))) is not None]
    return ProfileThresholds(
        volatility_low=quantile(volatility, 0.33),
        volatility_high=quantile(volatility, 0.67),
    )


def profile_payload(
    *,
    feature_path: Path,
    rows_path: Path,
    rows: list[dict[str, Any]],
    thresholds: ProfileThresholds,
    horizon_bars: int,
    min_count: int,
) -> dict[str, Any]:
    state_summaries = summarize(rows, "state_key", min_count)
    dimension_summaries = {
        dimension: summarize(rows, dimension, min_count=1)
        for dimension in STATE_DIMENSIONS
    }
    return {
        "feature_path": str(feature_path),
        "rows_path": str(rows_path),
        "rows": len(rows),
        "labeled_rows": sum(1 for row in rows if row.get("has_forward_outcome") == "1"),
        "horizon_bars": horizon_bars,
        "thresholds": {
            "volatility_low": thresholds.volatility_low,
            "volatility_high": thresholds.volatility_high,
        },
        "state_summaries": state_summaries,
        "dimension_summaries": dimension_summaries,
    }


def summarize(
    rows: list[dict[str, Any]],
    key: str,
    min_count: int,
) -> list[dict[str, Any]]:
    groups: dict[str, SummaryStats] = {}
    for row in rows:
        outcome = outcome_from_profiled_row(row)
        if outcome is None:
            continue
        name = str(row.get(key) or "unknown")
        groups.setdefault(name, SummaryStats()).add(outcome)

    summaries = [
        stats.to_dict(name)
        for name, stats in groups.items()
        if stats.count >= min_count
    ]
    return sorted(
        summaries,
        key=lambda item: (-int(item["count"]), str(item["name"])),
    )


def outcome_from_profiled_row(row: dict[str, Any]) -> ForwardOutcome | None:
    if row.get("has_forward_outcome") != "1":
        return None
    forward = first_float_by_prefix(row, "forward_return_")
    ticks = first_float_by_prefix(row, "forward_ticks_")
    mfe = first_float_by_prefix(row, "forward_mfe_ticks_")
    mae = first_float_by_prefix(row, "forward_mae_ticks_")
    if forward is None or ticks is None or mfe is None or mae is None:
        return None
    return ForwardOutcome(forward, ticks, mfe, mae)


def profile_markdown(payload: dict[str, Any]) -> str:
    state_summaries = payload["state_summaries"]
    best = sorted(
        state_summaries,
        key=lambda item: item["avg_forward_ticks"] if item["avg_forward_ticks"] is not None else -math.inf,
        reverse=True,
    )[:SUMMARY_LIMIT]
    worst = sorted(
        state_summaries,
        key=lambda item: item["avg_forward_ticks"] if item["avg_forward_ticks"] is not None else math.inf,
    )[:SUMMARY_LIMIT]

    lines = [
        "# Axiom Market State Profile",
        "",
        f"- Feature file: `{payload['feature_path']}`",
        f"- State rows: `{payload['rows_path']}`",
        f"- Rows: {payload['rows']:,}",
        f"- Labeled rows: {payload['labeled_rows']:,}",
        f"- Forward horizon: {payload['horizon_bars']:,} bars",
        "",
        "## Most Common States",
        "",
    ]
    lines.extend(summary_table(state_summaries[:SUMMARY_LIMIT]))
    lines.extend(["", "## Strongest Forward States", ""])
    lines.extend(summary_table(best))
    lines.extend(["", "## Weakest Forward States", ""])
    lines.extend(summary_table(worst))
    return "\n".join(lines) + "\n"


def summary_table(rows: list[dict[str, Any]]) -> list[str]:
    lines = [
        "| state | rows | win % | avg fwd | avg mfe | avg mae |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    if not rows:
        lines.append("| n/a | 0 | n/a | n/a | n/a | n/a |")
        return lines
    for row in rows:
        lines.append(
            f"| `{row['name']}` | {row['count']:,} | "
            f"{fmt_percent(row['win_rate'])} | "
            f"{fmt_number(row['avg_forward_ticks'])} | "
            f"{fmt_number(row['avg_mfe_ticks'])} | "
            f"{fmt_number(row['avg_mae_ticks'])} |"
        )
    return lines


def state_fieldnames(horizon_bars: int) -> list[str]:
    return [
        "t",
        "c",
        "state_key",
        *STATE_DIMENSIONS,
        "has_forward_outcome",
        f"forward_return_{horizon_bars}bar",
        f"forward_ticks_{horizon_bars}bar",
        f"forward_mfe_ticks_{horizon_bars}bar",
        f"forward_mae_ticks_{horizon_bars}bar",
    ]


def state_profile_dir(data_dir: Path, feature_path: Path) -> Path:
    contract_part = next(
        (part for part in feature_path.parts if part.startswith("contract=")),
        "contract=unknown",
    )
    unit_part = next(
        (part for part in feature_path.parts if part.startswith("unit=")),
        "unit=unknown",
    )
    return data_dir / "silver" / "projectx" / "states" / "bars" / contract_part / unit_part


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise ValueError(f"Feature table not found: {path}")
    with path.open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def first_float_by_prefix(row: dict[str, Any], prefix: str) -> float | None:
    for key, value in row.items():
        if key.startswith(prefix):
            return parse_float(value)
    return None


def nonempty(value: Any, default: str) -> str:
    text = str(value or "").strip()
    return text if text else default


def fmt_number(value: Any) -> str:
    parsed = parse_float(value)
    if parsed is None or not math.isfinite(parsed):
        return "n/a"
    return f"{parsed:.2f}"


def fmt_percent(value: Any) -> str:
    parsed = parse_float(value)
    if parsed is None:
        return "n/a"
    return f"{parsed * 100:.1f}%"
