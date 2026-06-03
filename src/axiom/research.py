from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import csv
import math
from typing import Any


# Columns that are labels (prediction targets), not predictive features.
LABEL_PREFIXES = (
    "forward_return_",
    "forward_mfe_ticks_",
    "forward_mae_ticks_",
    "forward_realized_vol_",
)

# Columns that are identifiers or non-stationary price levels, not features.
NON_FEATURE_COLUMNS = {
    "timestamp",
    "contract",
    "interval_seconds",
    "mid_price",
    "best_bid",
    "best_ask",
}


@dataclass(frozen=True)
class FeatureLabelStat:
    feature: str
    label: str
    samples: int
    pearson: float | None
    spearman: float | None
    top_quintile_mean: float | None
    bottom_quintile_mean: float | None

    @property
    def spread(self) -> float | None:
        if self.top_quintile_mean is None or self.bottom_quintile_mean is None:
            return None
        return self.top_quintile_mean - self.bottom_quintile_mean

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature": self.feature,
            "label": self.label,
            "samples": self.samples,
            "pearson": self.pearson,
            "spearman": self.spearman,
            "top_quintile_mean": self.top_quintile_mean,
            "bottom_quintile_mean": self.bottom_quintile_mean,
            "top_minus_bottom": self.spread,
        }


@dataclass(frozen=True)
class ResearchReport:
    path: Path
    rows: int
    features: list[str]
    labels: list[str]
    stats: list[FeatureLabelStat]

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "rows": self.rows,
            "features": self.features,
            "labels": self.labels,
            "stats": [stat.to_dict() for stat in self.stats],
        }

    def to_markdown(self, top: int = 15) -> str:
        lines = [
            "# Axiom Feature/Label Information Coefficient",
            "",
            f"- File: `{self.path}`",
            f"- Rows: {self.rows:,}",
            f"- Features analyzed: {len(self.features)}",
            f"- Labels analyzed: {len(self.labels)}",
            "",
            (
                "Information coefficient is the Spearman rank correlation between a "
                "feature and a forward label. Overlapping forward windows are "
                "autocorrelated, so treat |IC| as a ranking signal, not a p-value."
            ),
        ]
        for label in self.labels:
            label_stats = [stat for stat in self.stats if stat.label == label]
            label_stats.sort(key=lambda stat: _abs_or_zero(stat.spearman), reverse=True)
            if not label_stats:
                continue
            lines.extend(["", f"## {label}", ""])
            lines.append("| feature | n | spearman | pearson | top-bottom quintile |")
            lines.append("| --- | ---: | ---: | ---: | ---: |")
            for stat in label_stats[:top]:
                lines.append(
                    f"| {stat.feature} | {stat.samples:,} | "
                    f"{_fmt(stat.spearman)} | {_fmt(stat.pearson)} | "
                    f"{_fmt(stat.spread)} |"
                )
        return "\n".join(lines) + "\n"


def analyze_feature_ic(
    path: Path,
    labels: list[str] | None = None,
    min_samples: int = 30,
) -> ResearchReport:
    fieldnames, columns = read_feature_table(path)
    row_count = len(next(iter(columns.values()), []))

    available_labels = [name for name in fieldnames if name.startswith(LABEL_PREFIXES)]
    selected_labels = [name for name in (labels or available_labels) if name in columns]
    feature_names = [
        name
        for name in fieldnames
        if name not in NON_FEATURE_COLUMNS and name not in available_labels
    ]

    stats: list[FeatureLabelStat] = []
    for label in selected_labels:
        for feature in feature_names:
            xs, ys = aligned_pairs(columns[feature], columns[label])
            if len(xs) < min_samples:
                continue
            top_mean, bottom_mean = quintile_means(xs, ys)
            stats.append(
                FeatureLabelStat(
                    feature=feature,
                    label=label,
                    samples=len(xs),
                    pearson=pearson(xs, ys),
                    spearman=spearman(xs, ys),
                    top_quintile_mean=top_mean,
                    bottom_quintile_mean=bottom_mean,
                )
            )

    return ResearchReport(
        path=path,
        rows=row_count,
        features=feature_names,
        labels=selected_labels,
        stats=stats,
    )


def read_feature_table(path: Path) -> tuple[list[str], dict[str, list[str]]]:
    with path.open(encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        columns: dict[str, list[str]] = {name: [] for name in fieldnames}
        for row in reader:
            for name in fieldnames:
                columns[name].append(row.get(name, ""))
    return fieldnames, columns


def aligned_pairs(
    feature_values: list[str],
    label_values: list[str],
) -> tuple[list[float], list[float]]:
    xs: list[float] = []
    ys: list[float] = []
    for raw_x, raw_y in zip(feature_values, label_values):
        x = parse_float(raw_x)
        y = parse_float(raw_y)
        if x is None or y is None:
            continue
        xs.append(x)
        ys.append(y)
    return xs, ys


def pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 2:
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    syy = sum((y - mean_y) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    return sxy / math.sqrt(sxx * syy)


def spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 2:
        return None
    return pearson(rank(xs), rank(ys))


def rank(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    position = 0
    while position < len(order):
        end = position
        while end + 1 < len(order) and values[order[end + 1]] == values[order[position]]:
            end += 1
        average_rank = (position + end) / 2 + 1
        for index in range(position, end + 1):
            ranks[order[index]] = average_rank
        position = end + 1
    return ranks


def quintile_means(
    xs: list[float],
    ys: list[float],
    fraction: float = 0.2,
) -> tuple[float | None, float | None]:
    pairs = sorted(zip(xs, ys), key=lambda pair: pair[0])
    bucket = max(1, int(len(pairs) * fraction))
    top = [y for _, y in pairs[-bucket:]]
    bottom = [y for _, y in pairs[:bucket]]
    top_mean = sum(top) / len(top) if top else None
    bottom_mean = sum(bottom) / len(bottom) if bottom else None
    return top_mean, bottom_mean


def parse_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result):
        return None
    return result


def _abs_or_zero(value: float | None) -> float:
    return abs(value) if value is not None else 0.0


def _fmt(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.4f}"
