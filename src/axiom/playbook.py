from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
import csv
import math
from typing import Any

from .projectx import parse_dt


@dataclass(frozen=True)
class PlaybookConfig:
    path: Path
    horizon_seconds: int = 30
    tick_size: float = 0.25
    cost_ticks: float = 2.0
    cooldown_seconds: int = 30
    impulse_window_seconds: int = 30
    trigger_window_seconds: int = 5
    min_impulse_ticks: float = 12.0
    min_trigger_ticks: float = 2.0
    min_flow_imbalance: float = 0.20
    min_trigger_volume: float = 20.0
    max_spread_ticks: float = 2.0


def parse_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def fmt_number(value: float | int | None, decimals: int = 2) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, int):
        return f"{value:,}"
    if not math.isfinite(value):
        return "n/a"
    return f"{value:,.{decimals}f}"


@dataclass(frozen=True)
class SetupDecision:
    direction: int
    reason: str


@dataclass(frozen=True)
class Trade:
    timestamp: str
    direction: int
    gross_ticks: float
    net_ticks: float
    mfe_ticks: float | None
    mae_ticks: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "direction": self.direction,
            "gross_ticks": self.gross_ticks,
            "net_ticks": self.net_ticks,
            "mfe_ticks": self.mfe_ticks,
            "mae_ticks": self.mae_ticks,
        }


@dataclass(frozen=True)
class PlaybookResult:
    name: str
    description: str
    trades: list[Trade]
    reason_counts: Counter[str]

    @property
    def trade_count(self) -> int:
        return len(self.trades)

    @property
    def long_count(self) -> int:
        return sum(1 for trade in self.trades if trade.direction > 0)

    @property
    def short_count(self) -> int:
        return sum(1 for trade in self.trades if trade.direction < 0)

    def win_rate(self) -> float | None:
        if not self.trades:
            return None
        return sum(1 for trade in self.trades if trade.net_ticks > 0) / len(self.trades)

    def avg_net_ticks(self) -> float | None:
        return mean([trade.net_ticks for trade in self.trades])

    def total_net_ticks(self) -> float:
        return sum(trade.net_ticks for trade in self.trades)

    def profit_factor(self) -> float | None:
        profits = sum(trade.net_ticks for trade in self.trades if trade.net_ticks > 0)
        losses = abs(sum(trade.net_ticks for trade in self.trades if trade.net_ticks < 0))
        if profits <= 0 and losses <= 0:
            return None
        if losses <= 0:
            return math.inf
        return profits / losses

    def max_drawdown_ticks(self) -> float:
        equity = 0.0
        peak = 0.0
        max_drawdown = 0.0
        for trade in self.trades:
            equity += trade.net_ticks
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, peak - equity)
        return max_drawdown

    def avg_mfe_ticks(self) -> float | None:
        return mean([trade.mfe_ticks for trade in self.trades if trade.mfe_ticks is not None])

    def avg_mae_ticks(self) -> float | None:
        return mean([trade.mae_ticks for trade in self.trades if trade.mae_ticks is not None])

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "trades": self.trade_count,
            "longs": self.long_count,
            "shorts": self.short_count,
            "win_rate": self.win_rate(),
            "avg_net_ticks": self.avg_net_ticks(),
            "total_net_ticks": self.total_net_ticks(),
            "profit_factor": finite_or_none(self.profit_factor()),
            "max_drawdown_ticks": self.max_drawdown_ticks(),
            "avg_mfe_ticks": self.avg_mfe_ticks(),
            "avg_mae_ticks": self.avg_mae_ticks(),
            "reason_counts": dict(self.reason_counts),
            "sample_trades": [trade.to_dict() for trade in self.trades[:10]],
        }


@dataclass(frozen=True)
class PlaybookReport:
    path: Path
    rows: int
    horizon_seconds: int
    tick_size: float
    cost_ticks: float
    cooldown_seconds: int
    config: PlaybookConfig
    result: PlaybookResult

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "rows": self.rows,
            "horizon_seconds": self.horizon_seconds,
            "tick_size": self.tick_size,
            "cost_ticks": self.cost_ticks,
            "cooldown_seconds": self.cooldown_seconds,
            "playbook": self.result.to_dict(),
            "config": {
                "impulse_window_seconds": self.config.impulse_window_seconds,
                "trigger_window_seconds": self.config.trigger_window_seconds,
                "min_impulse_ticks": self.config.min_impulse_ticks,
                "min_trigger_ticks": self.config.min_trigger_ticks,
                "min_flow_imbalance": self.config.min_flow_imbalance,
                "min_trigger_volume": self.config.min_trigger_volume,
                "max_spread_ticks": self.config.max_spread_ticks,
            },
        }

    def to_markdown(self) -> str:
        result = self.result
        lines = [
            "# Axiom Playbook Evaluation",
            "",
            f"- File: `{self.path}`",
            f"- Rows: {self.rows:,}",
            f"- Playbook: {result.name}",
            f"- Horizon: {self.horizon_seconds:,} seconds",
            f"- Cost: {fmt_number(self.cost_ticks)} ticks/trade",
            f"- Cooldown: {self.cooldown_seconds:,} seconds",
            "",
            result.description,
            "",
            "## Setup",
            "",
            f"- Impulse window: {self.config.impulse_window_seconds:,} seconds",
            f"- Trigger window: {self.config.trigger_window_seconds:,} seconds",
            f"- Minimum impulse: {fmt_number(self.config.min_impulse_ticks)} ticks",
            f"- Minimum reversal trigger: {fmt_number(self.config.min_trigger_ticks)} ticks",
            f"- Minimum trigger flow imbalance: {fmt_number(self.config.min_flow_imbalance)}",
            f"- Minimum trigger volume: {fmt_number(self.config.min_trigger_volume)}",
            f"- Maximum average spread: {fmt_number(self.config.max_spread_ticks)} ticks",
            "",
            "## Candidate Performance",
            "",
            "| trades | long/short | win % | avg net | total net | pf | max dd | avg mfe | avg mae |",
            "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
            (
                f"| {result.trade_count:,} | {result.long_count:,}/{result.short_count:,} | "
                f"{fmt_percent(result.win_rate())} | {fmt_number(result.avg_net_ticks())} | "
                f"{fmt_number(result.total_net_ticks())} | {fmt_number(result.profit_factor())} | "
                f"{fmt_number(result.max_drawdown_ticks())} | {fmt_number(result.avg_mfe_ticks())} | "
                f"{fmt_number(result.avg_mae_ticks())} |"
            ),
            "",
            "## Setup Reasons",
            "",
            "| reason | rows |",
            "| --- | ---: |",
        ]
        for reason, count in result.reason_counts.most_common():
            lines.append(f"| {reason} | {count:,} |")
        return "\n".join(lines) + "\n"


def evaluate_playbook(config: PlaybookConfig) -> PlaybookReport:
    rows = read_rows(config.path)
    result = evaluate_exhaustion_reversal(rows, config)
    return PlaybookReport(
        path=config.path,
        rows=len(rows),
        horizon_seconds=config.horizon_seconds,
        tick_size=config.tick_size,
        cost_ticks=config.cost_ticks,
        cooldown_seconds=config.cooldown_seconds,
        config=config,
        result=result,
    )


def evaluate_exhaustion_reversal(
    rows: list[dict[str, str]],
    config: PlaybookConfig,
) -> PlaybookResult:
    trades: list[Trade] = []
    reason_counts: Counter[str] = Counter()
    last_entry_timestamp = None

    for row in rows:
        timestamp = parse_dt(row.get("timestamp"))
        decision = exhaustion_reversal_decision(row, config)
        if decision.direction == 0:
            reason_counts[decision.reason] += 1
            continue
        if (
            config.cooldown_seconds > 0
            and timestamp is not None
            and last_entry_timestamp is not None
            and (timestamp - last_entry_timestamp).total_seconds() < config.cooldown_seconds
        ):
            reason_counts["cooldown"] += 1
            continue

        trade = build_trade(row, decision.direction, config)
        if trade is None:
            reason_counts["missing_forward_label"] += 1
            continue
        trades.append(trade)
        reason_counts["candidate"] += 1
        if timestamp is not None:
            last_entry_timestamp = timestamp

    return PlaybookResult(
        name="exhaustion_reversal",
        description=(
            "Fade a fast directional impulse only after the short trigger window "
            "starts pushing back with matching trade-flow pressure and a tight spread."
        ),
        trades=trades,
        reason_counts=reason_counts,
    )


def exhaustion_reversal_decision(
    row: dict[str, str],
    config: PlaybookConfig,
) -> SetupDecision:
    impulse_ticks = return_ticks(
        row,
        f"return_{config.impulse_window_seconds}s",
        config.tick_size,
    )
    trigger_ticks = return_ticks(
        row,
        f"return_{config.trigger_window_seconds}s",
        config.tick_size,
    )
    flow_imbalance = parse_float(
        row.get(f"trade_type0_1_imbalance_{config.trigger_window_seconds}s")
    )
    trigger_volume = parse_float(row.get(f"trade_volume_{config.trigger_window_seconds}s"))
    spread_ticks = spread_ticks_from_row(
        row,
        f"avg_spread_{config.trigger_window_seconds}s",
        config.tick_size,
    )

    if impulse_ticks is None:
        return no_setup("missing_impulse")
    if abs(impulse_ticks) < config.min_impulse_ticks:
        return no_setup("impulse_too_small")
    if trigger_ticks is None:
        return no_setup("missing_trigger")

    direction = -1 if impulse_ticks > 0 else 1
    if trigger_ticks * direction < config.min_trigger_ticks:
        return no_setup("trigger_not_reversing")
    if flow_imbalance is None:
        return no_setup("missing_flow")
    if flow_imbalance * direction < config.min_flow_imbalance:
        return no_setup("flow_not_confirming")
    if trigger_volume is None:
        return no_setup("missing_volume")
    if trigger_volume < config.min_trigger_volume:
        return no_setup("volume_too_low")
    if spread_ticks is None:
        return no_setup("missing_spread")
    if spread_ticks > config.max_spread_ticks:
        return no_setup("spread_too_wide")

    return SetupDecision(
        direction=direction,
        reason="candidate",
    )


def no_setup(reason: str) -> SetupDecision:
    return SetupDecision(direction=0, reason=reason)


def build_trade(
    row: dict[str, str],
    direction: int,
    config: PlaybookConfig,
) -> Trade | None:
    mid_price = parse_float(row.get("mid_price"))
    forward_return = parse_float(row.get(f"forward_return_{config.horizon_seconds}s"))
    if mid_price is None or forward_return is None or config.tick_size <= 0:
        return None

    forward_ticks = forward_return * mid_price / config.tick_size
    gross_ticks = forward_ticks if direction > 0 else -forward_ticks
    mfe_ticks, mae_ticks = directional_excursions(row, direction, config)
    return Trade(
        timestamp=row.get("timestamp", ""),
        direction=1 if direction > 0 else -1,
        gross_ticks=gross_ticks,
        net_ticks=gross_ticks - config.cost_ticks,
        mfe_ticks=mfe_ticks,
        mae_ticks=mae_ticks,
    )


def directional_excursions(
    row: dict[str, str],
    direction: int,
    config: PlaybookConfig,
) -> tuple[float | None, float | None]:
    raw_mfe = parse_float(row.get(f"forward_mfe_ticks_{config.horizon_seconds}s"))
    raw_mae = parse_float(row.get(f"forward_mae_ticks_{config.horizon_seconds}s"))
    if raw_mfe is None or raw_mae is None:
        return None, None
    if direction > 0:
        return max(raw_mfe, 0.0), min(raw_mae, 0.0)
    return max(-raw_mae, 0.0), min(-raw_mfe, 0.0)


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def return_ticks(row: dict[str, str], column: str, tick_size: float) -> float | None:
    mid_price = parse_float(row.get("mid_price"))
    value = parse_float(row.get(column))
    if mid_price is None or value is None or tick_size <= 0:
        return None
    return value * mid_price / tick_size


def spread_ticks_from_row(row: dict[str, str], column: str, tick_size: float) -> float | None:
    value = parse_float(row.get(column))
    if value is None or tick_size <= 0:
        return None
    return value / tick_size


def mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def finite_or_none(value: float | None) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return value


def fmt_percent(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.1f}%"
