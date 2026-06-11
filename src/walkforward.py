from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
import csv
import json
from typing import Any

from config import Settings
from projectx import parse_dt
from signals import EdgeLedger, SignalConfig, decide
from state_profile import first_float_by_prefix

DEFAULT_MIN_OOS_TRADES = 30
DEFAULT_MAX_STATE_SHARE = 0.5


@dataclass(frozen=True)
class GateConfig:
    min_oos_trades: int = DEFAULT_MIN_OOS_TRADES
    max_state_share: float = DEFAULT_MAX_STATE_SHARE


@dataclass(frozen=True)
class TradeRecord:
    t: str
    week: str
    state_key: str
    direction: int
    expected_ticks_net: float | None
    realized_net_ticks: float
    stopped: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "t": self.t,
            "week": self.week,
            "state_key": self.state_key,
            "direction": self.direction,
            "expected_ticks_net": self.expected_ticks_net,
            "realized_net_ticks": self.realized_net_ticks,
            "stopped": self.stopped,
        }


@dataclass
class FoldResult:
    week: str
    train_rows: int
    train_max_t: str
    eval_min_t: str
    trades: list[TradeRecord] = field(default_factory=list)
    reasons: Counter = field(default_factory=Counter)

    @property
    def net_ticks(self) -> float:
        return sum(trade.realized_net_ticks for trade in self.trades)

    def win_rate(self) -> float | None:
        if not self.trades:
            return None
        wins = sum(1 for trade in self.trades if trade.realized_net_ticks > 0)
        return wins / len(self.trades)

    def to_dict(self) -> dict[str, Any]:
        return {
            "week": self.week,
            "train_rows": self.train_rows,
            "train_max_t": self.train_max_t,
            "eval_min_t": self.eval_min_t,
            "trades": len(self.trades),
            "net_ticks": self.net_ticks,
            "win_rate": self.win_rate(),
            "reasons": dict(self.reasons),
        }


@dataclass(frozen=True)
class WalkForwardResult:
    folds: list[FoldResult]
    gate_open: bool
    gate_reasons: list[str]

    @property
    def trades(self) -> list[TradeRecord]:
        return [trade for fold in self.folds for trade in fold.trades]

    @property
    def reason_counts(self) -> Counter:
        total: Counter = Counter()
        for fold in self.folds:
            total.update(fold.reasons)
        return total

    def overall(self) -> dict[str, Any]:
        trades = self.trades
        net = sum(trade.realized_net_ticks for trade in trades)
        wins = sum(1 for trade in trades if trade.realized_net_ticks > 0)
        expected = [
            trade.expected_ticks_net
            for trade in trades
            if trade.expected_ticks_net is not None
        ]
        return {
            "trades": len(trades),
            "stopped_trades": sum(1 for trade in trades if trade.stopped),
            "net_ticks": net,
            "win_rate": wins / len(trades) if trades else None,
            "avg_expected_ticks_net": sum(expected) / len(expected) if expected else None,
            "avg_realized_net_ticks": net / len(trades) if trades else None,
        }

    def per_state_net(self) -> dict[str, float]:
        totals: dict[str, float] = {}
        for trade in self.trades:
            totals[trade.state_key] = (
                totals.get(trade.state_key, 0.0) + trade.realized_net_ticks
            )
        return totals

    def to_dict(self) -> dict[str, Any]:
        return {
            "folds": [fold.to_dict() for fold in self.folds],
            "overall": self.overall(),
            "per_state_net": self.per_state_net(),
            "reason_counts": dict(self.reason_counts),
            "gate": {"open": self.gate_open, "reasons": self.gate_reasons},
            "sample_trades": [trade.to_dict() for trade in self.trades[:20]],
        }


def week_key(timestamp: str) -> str:
    parsed = parse_dt(timestamp)
    if parsed is None:
        return "unknown"
    iso = parsed.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def evaluate_walk_forward(
    rows: list[dict[str, Any]],
    signal_config: SignalConfig = SignalConfig(),
    gate_config: GateConfig = GateConfig(),
) -> WalkForwardResult:
    """Chronological weekly walk-forward over merged feature+state rows.

    For each week after the first, the edge ledger is built from all strictly
    earlier weeks and decisions are evaluated on that week only. Entry is the
    bar after the signal bar; the realized outcome is that bar's forward-ticks
    label net of cost. Entries respect a horizon-length cooldown so trade
    outcomes never overlap.
    """
    ordered = sorted(rows, key=lambda row: str(row.get("t") or ""))
    weeks: list[str] = []
    by_week: dict[str, list[dict[str, Any]]] = {}
    for row in ordered:
        key = week_key(str(row.get("t") or ""))
        if key not in by_week:
            by_week[key] = []
            weeks.append(key)
        by_week[key].append(row)

    folds: list[FoldResult] = []
    train: list[dict[str, Any]] = []
    for index, week in enumerate(weeks):
        fold_rows = by_week[week]
        if index >= 1 and train:
            ledger = EdgeLedger.from_state_rows(train)
            fold = FoldResult(
                week=week,
                train_rows=len(train),
                train_max_t=str(train[-1].get("t") or ""),
                eval_min_t=str(fold_rows[0].get("t") or ""),
            )
            evaluate_fold(fold, fold_rows, ledger, signal_config)
            folds.append(fold)
        train.extend(fold_rows)

    gate_open, gate_reasons = edge_gate(folds, gate_config)
    return WalkForwardResult(folds=folds, gate_open=gate_open, gate_reasons=gate_reasons)


def evaluate_fold(
    fold: FoldResult,
    fold_rows: list[dict[str, Any]],
    ledger: EdgeLedger,
    signal_config: SignalConfig,
) -> None:
    cooldown = 0
    for index, row in enumerate(fold_rows):
        if cooldown > 0:
            cooldown -= 1
            fold.reasons["cooldown"] += 1
            continue
        decision = decide(row, ledger, signal_config)
        fold.reasons[decision.reason] += 1
        if decision.direction == 0:
            continue
        entry_row = next_entry_row(fold_rows, index)
        outcome = (
            trade_outcome(entry_row, decision.direction, decision.stop_ticks, signal_config)
            if entry_row is not None
            else None
        )
        if outcome is None:
            fold.reasons["no_next_bar_outcome"] += 1
            continue
        gross, stopped = outcome
        cost = decision.cost_ticks if decision.cost_ticks is not None else signal_config.cost_ticks
        net = gross - cost
        fold.trades.append(
            TradeRecord(
                t=str(row.get("t") or ""),
                week=fold.week,
                state_key=decision.state_key,
                direction=decision.direction,
                expected_ticks_net=decision.expected_ticks_net,
                realized_net_ticks=net,
                stopped=stopped,
            )
        )
        cooldown = signal_config.horizon_bars


def next_entry_row(rows: list[dict[str, Any]], index: int) -> dict[str, Any] | None:
    """The bar after the signal bar: where the trade is entered and measured."""
    if index + 1 >= len(rows):
        return None
    next_row = rows[index + 1]
    if str(next_row.get("has_forward_outcome") or "") != "1":
        return None
    return next_row


def trade_outcome(
    entry_row: dict[str, Any],
    direction: int,
    stop_ticks: float | None,
    signal_config: SignalConfig,
) -> tuple[float, bool] | None:
    """Gross ticks for the trade, applying the stop the live engine carries.

    Bar labels give the window's worst adverse excursion but not its ordering,
    so any window whose adverse move reaches the stop counts as stopped out -
    conservative for trades that dipped and recovered. Stopped exits fill
    `stop_slippage_ticks` beyond the stop, because stops slip in fast tape.
    """
    forward = first_float_by_prefix(entry_row, "forward_ticks_")
    if forward is None:
        return None
    gross = direction * forward
    if stop_ticks is not None:
        if direction > 0:
            mae = first_float_by_prefix(entry_row, "forward_mae_ticks_")
            adverse = abs(mae) if mae is not None else None
        else:
            mfe = first_float_by_prefix(entry_row, "forward_mfe_ticks_")
            adverse = mfe if mfe is not None else None
        if adverse is not None and adverse >= stop_ticks:
            return -(stop_ticks + signal_config.stop_slippage_ticks), True
    return gross, False


def edge_gate(folds: list[FoldResult], config: GateConfig) -> tuple[bool, list[str]]:
    trades = [trade for fold in folds for trade in fold.trades]
    reasons: list[str] = []
    if not trades:
        return False, ["no out-of-sample trades"]

    net = sum(trade.realized_net_ticks for trade in trades)
    if len(trades) < config.min_oos_trades:
        reasons.append(
            f"only {len(trades)} OOS trades (need {config.min_oos_trades})"
        )
    if net <= 0:
        reasons.append(f"OOS net ticks not positive ({net:+.1f})")
    elif trades:
        per_state: dict[str, float] = {}
        for trade in trades:
            per_state[trade.state_key] = (
                per_state.get(trade.state_key, 0.0) + trade.realized_net_ticks
            )
        top_state, top_net = max(per_state.items(), key=lambda item: item[1])
        if top_net > config.max_state_share * net:
            share = top_net / net if net else 0.0
            reasons.append(
                f"single state carries {share:.0%} of profit ({top_state})"
            )
    return (not reasons), reasons


def merge_feature_state_rows(
    features_path: Path,
    states_path: Path,
) -> list[dict[str, Any]]:
    with features_path.open(encoding="utf-8") as handle:
        features = {row["t"]: row for row in csv.DictReader(handle)}
    merged: list[dict[str, Any]] = []
    with states_path.open(encoding="utf-8") as handle:
        for state_row in csv.DictReader(handle):
            feature_row = features.get(state_row.get("t", ""))
            if feature_row is None:
                continue
            merged.append({**feature_row, **state_row})
    return merged


def walkforward_markdown(payload: dict[str, Any]) -> str:
    overall = payload["overall"]
    gate = payload["gate"]
    lines = [
        "# Axiom Walk-Forward Signal Evaluation",
        "",
        f"- Generated: {payload['generated_at']}",
        f"- States file: `{payload['states_path']}`",
        f"- Edge gate: {'OPEN' if gate['open'] else 'CLOSED'}",
    ]
    for reason in gate["reasons"]:
        lines.append(f"  - {reason}")
    lines.extend(
        [
            "",
            "The engine is abstention-first: it only emits LONG/SHORT when a "
            "state's confidence bound clears costs on training data, and the "
            "gate only opens on positive, diversified out-of-sample results. "
            "A CLOSED gate on weak data is correct behavior.",
            "",
            "## Overall (out-of-sample)",
            "",
            f"- Trades: {overall['trades']:,}",
            f"- Stopped out: {overall['stopped_trades']:,} "
            "(stop = 0.75x state avg adverse excursion, +2 ticks slippage)",
            f"- Net ticks: {fmt(overall['net_ticks'])}",
            f"- Win rate: {fmt_percent(overall['win_rate'])}",
            f"- Avg expected ticks/trade (receipts): {fmt(overall['avg_expected_ticks_net'])}",
            f"- Avg realized ticks/trade: {fmt(overall['avg_realized_net_ticks'])}",
            "",
            "## Folds",
            "",
            "| week | train rows | trades | net ticks | win % |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for fold in payload["folds"]:
        lines.append(
            f"| {fold['week']} | {fold['train_rows']:,} | {fold['trades']:,} | "
            f"{fmt(fold['net_ticks'])} | {fmt_percent(fold['win_rate'])} |"
        )
    lines.extend(["", "## Abstention Reasons", "", "| reason | bars |", "| --- | ---: |"])
    counts = payload["reason_counts"]
    for reason in sorted(counts, key=lambda key: -counts[key]):
        lines.append(f"| {reason} | {counts[reason]:,} |")
    return "\n".join(lines) + "\n"


def fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.2f}"


def fmt_percent(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.1f}%"


def find_latest_states_path(data_dir: Path) -> Path | None:
    root = data_dir / "silver" / "projectx" / "states" / "bars"
    if not root.exists():
        return None
    candidates = [path for path in root.rglob("states.csv") if path.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def features_path_for_states(states_path: Path) -> Path:
    parts = ["features" if part == "states" else part for part in states_path.parts]
    return Path(*parts).with_name("features.csv")


def run_signals_command() -> int:
    settings = Settings.from_env()
    states_path = find_latest_states_path(settings.data_dir)
    if states_path is None:
        raise ValueError("No states.csv found. Run `python .\\main.py` first.")
    features_path = features_path_for_states(states_path)
    if not features_path.exists():
        raise ValueError(f"Feature table not found next to states: {features_path}")

    rows = merge_feature_state_rows(features_path, states_path)
    result = evaluate_walk_forward(rows)

    payload = result.to_dict()
    payload["generated_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload["states_path"] = str(states_path)
    payload["features_path"] = str(features_path)

    report_dir = settings.data_dir / "reports" / "signals"
    report_dir.mkdir(parents=True, exist_ok=True)
    stem = f"walkforward_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    markdown = walkforward_markdown(payload)
    md_path = report_dir / f"{stem}.md"
    json_path = report_dir / f"{stem}.json"
    md_path.write_text(markdown, encoding="utf-8")
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    print(markdown)
    print(f"Saved reports: {md_path}, {json_path}")
    return 0
