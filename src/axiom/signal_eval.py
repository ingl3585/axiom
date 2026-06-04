from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import csv
import json
import math
from typing import Any

from .backtest import CandidateBacktest, Trade
from .qa import fmt_dt, fmt_number, parse_dt
from .research import parse_float


@dataclass(frozen=True)
class SignalEvaluationConfig:
    signal_path: Path
    feature_path: Path
    horizon_seconds: int = 30
    tick_size: float = 0.25
    cost_ticks: float = 2.0
    max_match_lag_seconds: float = 1.5
    latest_run_only: bool = True
    run_gap_seconds: float = 120.0


@dataclass(frozen=True)
class SignalEvaluationReport:
    signal_path: Path
    feature_path: Path
    horizon_seconds: int
    cost_ticks: float
    source_signals: int
    total_signals: int
    run_filter: str
    run_start: datetime | None
    run_end: datetime | None
    candidates: int
    evaluated_candidates: int
    unmatched_candidates: int
    action_counts: Counter[str]
    reason_counts: Counter[str]
    policy_results: list[CandidateBacktest]

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_path": str(self.signal_path),
            "feature_path": str(self.feature_path),
            "horizon_seconds": self.horizon_seconds,
            "cost_ticks": self.cost_ticks,
            "source_signals": self.source_signals,
            "total_signals": self.total_signals,
            "run_filter": self.run_filter,
            "run_start": fmt_dt(self.run_start),
            "run_end": fmt_dt(self.run_end),
            "candidates": self.candidates,
            "evaluated_candidates": self.evaluated_candidates,
            "unmatched_candidates": self.unmatched_candidates,
            "action_counts": dict(self.action_counts),
            "reason_counts": dict(self.reason_counts),
            "policy_results": [result.to_dict() for result in self.policy_results],
        }

    def to_markdown(self) -> str:
        lines = [
            "# Axiom Live Signal Evaluation",
            "",
            f"- Signal file: `{self.signal_path}`",
            f"- Feature file: `{self.feature_path}`",
            f"- Run filter: {self.run_filter}",
            f"- Horizon: {self.horizon_seconds:,} seconds",
            f"- Cost: {fmt_number(self.cost_ticks)} ticks/candidate",
            f"- Raw signals in file: {self.source_signals:,}",
            f"- Signals evaluated: {self.total_signals:,}",
            f"- Evaluated span: {fmt_dt(self.run_start)} to {fmt_dt(self.run_end)}",
            f"- Candidates: {self.candidates:,}",
            f"- Evaluated candidates: {self.evaluated_candidates:,}",
            f"- Unmatched candidates: {self.unmatched_candidates:,}",
            "",
            "## Candidate Performance",
            "",
            "| policy | trades | long/short | win % | avg net | total net | pf | max dd | avg mfe | avg mae |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for result in sorted(
            self.policy_results,
            key=lambda item: item.avg_net_ticks() if item.avg_net_ticks() is not None else -math.inf,
            reverse=True,
        ):
            lines.append(
                f"| {result.name} | {result.trade_count:,} | "
                f"{result.long_count:,}/{result.short_count:,} | "
                f"{fmt_percent(result.win_rate())} | "
                f"{fmt_number(result.avg_net_ticks())} | "
                f"{fmt_number(result.total_net_ticks())} | "
                f"{fmt_number(result.profit_factor())} | "
                f"{fmt_number(result.max_drawdown_ticks())} | "
                f"{fmt_number(result.avg_mfe_ticks())} | "
                f"{fmt_number(result.avg_mae_ticks())} |"
            )

        lines.extend(["", "## Signal Reasons", ""])
        lines.append("| reason | count |")
        lines.append("| --- | ---: |")
        for reason, count in self.reason_counts.most_common():
            lines.append(f"| {reason} | {count:,} |")

        lines.extend(["", "## Signal Actions", ""])
        lines.append("| action | count |")
        lines.append("| --- | ---: |")
        for action, count in self.action_counts.most_common():
            lines.append(f"| {action} | {count:,} |")
        return "\n".join(lines) + "\n"


def evaluate_signal_file(config: SignalEvaluationConfig) -> SignalEvaluationReport:
    raw_signals = read_jsonl(config.signal_path)
    if config.latest_run_only:
        signals = latest_signal_run(raw_signals, config.run_gap_seconds)
        run_filter = f"latest run by timestamp gap > {fmt_number(config.run_gap_seconds)}s"
    else:
        signals = raw_signals
        run_filter = "all runs"
    run_start, run_end = signal_time_range(signals)

    features = read_feature_rows(config.feature_path)
    feature_times = [parse_dt(row.get("timestamp")) for row in features]

    action_counts = Counter(str(row.get("action") or "") for row in signals)
    reason_counts = Counter(str(row.get("reason") or "") for row in signals)
    trades_by_policy: dict[str, list[Trade]] = {}
    candidates = 0
    unmatched = 0

    for signal in signals:
        direction = int(signal.get("direction") or 0)
        if direction == 0:
            continue
        candidates += 1
        signal_time = parse_dt(str(signal.get("timestamp") or ""))
        feature = closest_feature(signal_time, features, feature_times, config.max_match_lag_seconds)
        if feature is None:
            unmatched += 1
            continue
        trade = trade_from_signal(signal, feature, direction, config)
        if trade is None:
            unmatched += 1
            continue
        policy = str(signal.get("policy") or "unknown")
        trades_by_policy.setdefault(policy, []).append(trade)

    policy_results = [
        CandidateBacktest(
            name=policy,
            description="Live candidate signal evaluation.",
            trades=trades,
        )
        for policy, trades in sorted(trades_by_policy.items())
    ]

    evaluated = sum(result.trade_count for result in policy_results)
    return SignalEvaluationReport(
        signal_path=config.signal_path,
        feature_path=config.feature_path,
        horizon_seconds=config.horizon_seconds,
        cost_ticks=config.cost_ticks,
        source_signals=len(raw_signals),
        total_signals=len(signals),
        run_filter=run_filter,
        run_start=run_start,
        run_end=run_end,
        candidates=candidates,
        evaluated_candidates=evaluated,
        unmatched_candidates=unmatched,
        action_counts=action_counts,
        reason_counts=reason_counts,
        policy_results=policy_results,
    )


def latest_signal_run(signals: list[dict[str, Any]], gap_seconds: float) -> list[dict[str, Any]]:
    if not signals or gap_seconds <= 0:
        return signals

    timestamps = [parse_dt(str(row.get("timestamp") or "")) for row in signals]
    latest_index = next(
        (index for index in range(len(timestamps) - 1, -1, -1) if timestamps[index] is not None),
        None,
    )
    if latest_index is None:
        return signals

    start_index = latest_index
    later_timestamp = timestamps[latest_index]
    for index in range(latest_index - 1, -1, -1):
        current_timestamp = timestamps[index]
        if current_timestamp is None:
            start_index = index
            continue
        gap = (later_timestamp - current_timestamp).total_seconds()
        if gap > gap_seconds:
            break
        start_index = index
        later_timestamp = current_timestamp
    return signals[start_index:]


def signal_time_range(signals: list[dict[str, Any]]) -> tuple[datetime | None, datetime | None]:
    timestamps = [
        timestamp
        for timestamp in (parse_dt(str(row.get("timestamp") or "")) for row in signals)
        if timestamp is not None
    ]
    if not timestamps:
        return None, None
    return min(timestamps), max(timestamps)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            try:
                row = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def read_feature_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def closest_feature(
    signal_time: Any,
    features: list[dict[str, str]],
    feature_times: list[Any],
    max_lag_seconds: float,
) -> dict[str, str] | None:
    if signal_time is None:
        return None
    best_index: int | None = None
    best_lag: float | None = None
    for index, feature_time in enumerate(feature_times):
        if feature_time is None:
            continue
        lag = abs((feature_time - signal_time).total_seconds())
        if best_lag is None or lag < best_lag:
            best_lag = lag
            best_index = index
    if best_index is None or best_lag is None or best_lag > max_lag_seconds:
        return None
    return features[best_index]


def trade_from_signal(
    signal: dict[str, Any],
    feature: dict[str, str],
    direction: int,
    config: SignalEvaluationConfig,
) -> Trade | None:
    mid_price = parse_float(feature.get("mid_price"))
    forward_return = parse_float(feature.get(f"forward_return_{config.horizon_seconds}s"))
    if mid_price is None or forward_return is None or config.tick_size <= 0:
        return None
    forward_ticks = forward_return * mid_price / config.tick_size
    gross_ticks = forward_ticks if direction > 0 else -forward_ticks
    mfe, mae = directional_excursions(feature, direction, config.horizon_seconds)
    return Trade(
        timestamp=str(signal.get("timestamp") or feature.get("timestamp") or ""),
        direction=1 if direction > 0 else -1,
        gross_ticks=gross_ticks,
        net_ticks=gross_ticks - config.cost_ticks,
        mfe_ticks=mfe,
        mae_ticks=mae,
    )


def directional_excursions(
    feature: dict[str, str],
    direction: int,
    horizon_seconds: int,
) -> tuple[float | None, float | None]:
    raw_mfe = parse_float(feature.get(f"forward_mfe_ticks_{horizon_seconds}s"))
    raw_mae = parse_float(feature.get(f"forward_mae_ticks_{horizon_seconds}s"))
    if raw_mfe is None or raw_mae is None:
        return None, None
    if direction > 0:
        return max(raw_mfe, 0.0), min(raw_mae, 0.0)
    return max(-raw_mae, 0.0), min(-raw_mfe, 0.0)


def fmt_percent(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.1f}%"
