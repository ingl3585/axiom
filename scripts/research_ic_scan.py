"""One-off research: IC scan of every model feature vs 5-bar forward ticks.

Run from the repo root:
    .\\.venv\\Scripts\\python.exe scripts\\research_ic_scan.py

Reads the bar feature table and the state profiler's labeled rows, computes the
Spearman rank correlation (information coefficient) of each stationary model
feature against forward ticks, overall and split RTH vs overnight. Read-only.
"""
from __future__ import annotations

from pathlib import Path
import csv
import math
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bar_features import model_feature_columns  # noqa: E402
from bars import parse_float  # noqa: E402

BASE = Path("data/silver/projectx")
CONTRACT = "contract=CON_F_US_MNQ_M26"
UNIT = "unit=minute_1"
FEATURES = BASE / "features" / "bars" / CONTRACT / UNIT / "features.csv"
STATES = BASE / "states" / "bars" / CONTRACT / UNIT / "states.csv"
LABEL = "forward_ticks_5bar"
MIN_N = 300
OVERLAP_BARS = 5  # forward windows overlap; effective n is roughly n / overlap


def rank(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        average = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = average
        i = j + 1
    return ranks


def pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    syy = sum((y - mean_y) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    return sxy / math.sqrt(sxx * syy)


def spearman(xs: list[float], ys: list[float]) -> float | None:
    return pearson(rank(xs), rank(ys))


def collect(feats, labels, column, rth_filter=None):
    xs, ys = [], []
    for row in feats:
        lab = labels.get(row["t"])
        if not lab or lab.get("has_forward_outcome") != "1":
            continue
        if rth_filter is not None and row.get("is_rth") != rth_filter:
            continue
        x = parse_float(row.get(column))
        y = parse_float(lab.get(LABEL))
        if x is None or y is None:
            continue
        xs.append(x)
        ys.append(y)
    return xs, ys


def scan(feats, labels, rth_filter=None):
    results = []
    for column in model_feature_columns():
        xs, ys = collect(feats, labels, column, rth_filter)
        if len(xs) < MIN_N:
            continue
        ic = spearman(xs, ys)
        if ic is None:
            continue
        t_eff = ic * math.sqrt(len(xs) / OVERLAP_BARS)
        results.append((column, len(xs), ic, t_eff))
    results.sort(key=lambda item: abs(item[2]), reverse=True)
    return results


def show(title: str, results, top: int = 12) -> None:
    print(f"\n=== {title} ===")
    print(f"{'feature':26} {'n':>7} {'IC':>8} {'t_eff':>7}")
    print("-" * 52)
    for column, n, ic, t_eff in results[:top]:
        print(f"{column:26} {n:7,} {ic:+8.4f} {t_eff:+7.2f}")


def main() -> int:
    if not FEATURES.exists() or not STATES.exists():
        print("Feature or state table missing. Run the pipeline first.")
        return 1
    with FEATURES.open(encoding="utf-8") as handle:
        feats = list(csv.DictReader(handle))
    with STATES.open(encoding="utf-8") as handle:
        labels = {row["t"]: row for row in csv.DictReader(handle)}

    show("ALL BARS: feature IC vs 5-bar forward ticks", scan(feats, labels))
    show("RTH ONLY", scan(feats, labels, rth_filter="1"))
    show("OVERNIGHT ONLY", scan(feats, labels, rth_filter="0"))

    print(
        "\nHow to read: IC is Spearman rank correlation; |IC| 0.02-0.05 is a"
        "\nnormal-but-real edge at this horizon, 0.10+ deserves suspicion."
        "\nt_eff discounts for 5-bar overlapping windows (autocorrelation);"
        "\n|t_eff| >= 2 is the interesting threshold. One month of data ="
        "\none regime - treat as hypothesis ranking, not proof."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
