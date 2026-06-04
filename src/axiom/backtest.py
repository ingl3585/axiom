from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import csv
import hashlib
import math
from typing import Any, Callable

from .qa import fmt_number, parse_dt
from .research import parse_float


SignalFn = Callable[[dict[str, str]], int]


@dataclass(frozen=True)
class BacktestConfig:
    path: Path
    horizon_seconds: int = 30
    signal_window_seconds: int = 5
    tick_size: float = 0.25
    cost_ticks: float = 2.0
    cooldown_seconds: int = 0
    imbalance_threshold: float = 0.20
    min_return_ticks: float = 0.0
    max_spread_ticks: float = 4.0


@dataclass(frozen=True)
class Candidate:
    name: str
    description: str
    signal: SignalFn


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
class CandidateBacktest:
    name: str
    description: str
    trades: list[Trade]

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

    def avg_gross_ticks(self) -> float | None:
        return mean([trade.gross_ticks for trade in self.trades])

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
            "avg_gross_ticks": self.avg_gross_ticks(),
            "avg_net_ticks": self.avg_net_ticks(),
            "total_net_ticks": self.total_net_ticks(),
            "profit_factor": finite_or_none(self.profit_factor()),
            "max_drawdown_ticks": self.max_drawdown_ticks(),
            "avg_mfe_ticks": self.avg_mfe_ticks(),
            "avg_mae_ticks": self.avg_mae_ticks(),
            "sample_trades": [trade.to_dict() for trade in self.trades[:10]],
        }


@dataclass(frozen=True)
class BacktestReport:
    path: Path
    rows: int
    horizon_seconds: int
    signal_window_seconds: int
    tick_size: float
    cost_ticks: float
    cooldown_seconds: int
    candidates: list[CandidateBacktest]

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "rows": self.rows,
            "horizon_seconds": self.horizon_seconds,
            "signal_window_seconds": self.signal_window_seconds,
            "tick_size": self.tick_size,
            "cost_ticks": self.cost_ticks,
            "cooldown_seconds": self.cooldown_seconds,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }

    def to_markdown(self) -> str:
        lines = [
            "# Axiom Research Backtest",
            "",
            f"- File: `{self.path}`",
            f"- Rows: {self.rows:,}",
            f"- Horizon: {self.horizon_seconds:,} seconds",
            f"- Signal window: {self.signal_window_seconds:,} seconds",
            f"- Cost: {fmt_number(self.cost_ticks)} ticks/trade",
            f"- Cooldown: {self.cooldown_seconds:,} seconds",
            "",
            (
                "This is a research harness, not an execution simulator. It evaluates "
                "candidate rules on feature rows using forward labels. If cooldown is "
                "0, trades can overlap."
            ),
            "",
            "| candidate | trades | long/short | win % | avg net | total net | pf | max dd | avg mfe | avg mae |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for candidate in sorted(
            self.candidates,
            key=lambda item: item.avg_net_ticks() if item.avg_net_ticks() is not None else -math.inf,
            reverse=True,
        ):
            lines.append(
                f"| {candidate.name} | {candidate.trade_count:,} | "
                f"{candidate.long_count:,}/{candidate.short_count:,} | "
                f"{fmt_percent(candidate.win_rate())} | "
                f"{fmt_number(candidate.avg_net_ticks())} | "
                f"{fmt_number(candidate.total_net_ticks())} | "
                f"{fmt_number(candidate.profit_factor())} | "
                f"{fmt_number(candidate.max_drawdown_ticks())} | "
                f"{fmt_number(candidate.avg_mfe_ticks())} | "
                f"{fmt_number(candidate.avg_mae_ticks())} |"
            )
        return "\n".join(lines) + "\n"


def run_backtest(config: BacktestConfig) -> BacktestReport:
    rows = read_rows(config.path)
    candidates = default_candidates(config)
    results = [
        evaluate_candidate(rows, candidate, config)
        for candidate in candidates
    ]
    return BacktestReport(
        path=config.path,
        rows=len(rows),
        horizon_seconds=config.horizon_seconds,
        signal_window_seconds=config.signal_window_seconds,
        tick_size=config.tick_size,
        cost_ticks=config.cost_ticks,
        cooldown_seconds=config.cooldown_seconds,
        candidates=results,
    )


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def default_candidates(config: BacktestConfig) -> list[Candidate]:
    window = config.signal_window_seconds
    return_col = f"return_{window}s"
    imbalance_col = f"trade_type0_1_imbalance_{window}s"
    spread_col = f"avg_spread_{window}s"

    return [
        Candidate(
            name="random_baseline",
            description="Stable pseudo-random long/short baseline by timestamp.",
            signal=stable_random_signal,
        ),
        Candidate(
            name=f"momentum_{window}s",
            description=f"Follow positive/negative {window}s trailing return.",
            signal=lambda row: thresholded_sign(
                return_ticks(row, return_col, config.tick_size),
                config.min_return_ticks,
            ),
        ),
        Candidate(
            name=f"mean_reversion_{window}s",
            description=f"Fade positive/negative {window}s trailing return.",
            signal=lambda row: -thresholded_sign(
                return_ticks(row, return_col, config.tick_size),
                config.min_return_ticks,
            ),
        ),
        Candidate(
            name=f"order_flow_follow_{window}s",
            description=f"Follow trade imbalance over {window}s.",
            signal=lambda row: thresholded_sign(
                parse_float(row.get(imbalance_col)),
                config.imbalance_threshold,
            ),
        ),
        Candidate(
            name=f"order_flow_fade_{window}s",
            description=f"Fade trade imbalance over {window}s.",
            signal=lambda row: -thresholded_sign(
                parse_float(row.get(imbalance_col)),
                config.imbalance_threshold,
            ),
        ),
        Candidate(
            name=f"spread_filtered_momentum_{window}s",
            description=(
                f"Follow {window}s momentum only when average spread is below "
                f"{config.max_spread_ticks:g} ticks."
            ),
            signal=lambda row: (
                thresholded_sign(
                    return_ticks(row, return_col, config.tick_size),
                    config.min_return_ticks,
                )
                if spread_ticks(row, spread_col, config.tick_size) <= config.max_spread_ticks
                else 0
            ),
        ),
    ]


def evaluate_candidate(
    rows: list[dict[str, str]],
    candidate: Candidate,
    config: BacktestConfig,
) -> CandidateBacktest:
    trades: list[Trade] = []
    last_entry_timestamp = None
    for row in rows:
        timestamp = parse_dt(row.get("timestamp"))
        if (
            config.cooldown_seconds > 0
            and timestamp is not None
            and last_entry_timestamp is not None
            and (timestamp - last_entry_timestamp).total_seconds() < config.cooldown_seconds
        ):
            continue

        direction = candidate.signal(row)
        if direction == 0:
            continue
        trade = build_trade(row, direction, config)
        if trade is None:
            continue
        trades.append(trade)
        if timestamp is not None:
            last_entry_timestamp = timestamp
    return CandidateBacktest(
        name=candidate.name,
        description=candidate.description,
        trades=trades,
    )


def build_trade(
    row: dict[str, str],
    direction: int,
    config: BacktestConfig,
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
    config: BacktestConfig,
) -> tuple[float | None, float | None]:
    raw_mfe = parse_float(row.get(f"forward_mfe_ticks_{config.horizon_seconds}s"))
    raw_mae = parse_float(row.get(f"forward_mae_ticks_{config.horizon_seconds}s"))
    if raw_mfe is None or raw_mae is None:
        return None, None
    if direction > 0:
        return max(raw_mfe, 0.0), min(raw_mae, 0.0)
    return max(-raw_mae, 0.0), min(-raw_mfe, 0.0)


def thresholded_sign(value: float | None, threshold: float) -> int:
    if value is None:
        return 0
    if value > threshold:
        return 1
    if value < -threshold:
        return -1
    return 0


def return_ticks(row: dict[str, str], column: str, tick_size: float) -> float | None:
    mid_price = parse_float(row.get("mid_price"))
    value = parse_float(row.get(column))
    if mid_price is None or value is None or tick_size <= 0:
        return None
    return value * mid_price / tick_size


def spread_ticks(row: dict[str, str], column: str, tick_size: float) -> float:
    value = parse_float(row.get(column))
    if value is None or tick_size <= 0:
        return math.inf
    return value / tick_size


def stable_random_signal(row: dict[str, str]) -> int:
    key = row.get("timestamp") or row.get("mid_price") or ""
    digest = hashlib.blake2b(key.encode("utf-8"), digest_size=1).digest()
    return 1 if digest[0] % 2 == 0 else -1


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
