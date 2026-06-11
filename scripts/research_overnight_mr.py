"""One-off research: overnight mean-reversion composite, decile analysis.

Run from the repo root:
    .\\.venv\\Scripts\\python.exe scripts\\research_overnight_mr.py

The IC scan showed a coherent negative cluster overnight: short-term extension
(return_1/5bar, dist_sma_5bar, range_pos_5bar, dist_ema_9) anti-correlates with
the next 5 bars. This composites those five into one score and reports forward
ticks per decile - if the effect is tradeable it must concentrate in the tails
and clear ~2 ticks round-trip cost. Includes a stride-5 pass (every 5th bar) so
overlapping windows don't flatter the stats. Read-only.
"""
from __future__ import annotations

from pathlib import Path
import csv
import math
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bars import parse_float  # noqa: E402

BASE = Path("data/silver/projectx")
CONTRACT = "contract=CON_F_US_MNQ_M26"
UNIT = "unit=minute_1"
FEATURES = BASE / "features" / "bars" / CONTRACT / UNIT / "features.csv"
STATES = BASE / "states" / "bars" / CONTRACT / UNIT / "states.csv"
LABEL = "forward_ticks_5bar"
CLUSTER = ["return_5bar", "dist_sma_5bar", "range_pos_5bar", "return_1", "dist_ema_9"]
DECILES = 10
COST_TICKS = 2.0


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


def load_samples() -> list[tuple[list[float], float]]:
    with FEATURES.open(encoding="utf-8") as handle:
        feats = list(csv.DictReader(handle))
    with STATES.open(encoding="utf-8") as handle:
        labels = {row["t"]: row for row in csv.DictReader(handle)}

    samples = []
    for row in feats:
        if row.get("is_rth") != "0":
            continue
        lab = labels.get(row["t"])
        if not lab or lab.get("has_forward_outcome") != "1":
            continue
        forward = parse_float(lab.get(LABEL))
        values = [parse_float(row.get(name)) for name in CLUSTER]
        if forward is None or any(value is None for value in values):
            continue
        samples.append((values, forward))
    return samples


def decile_report(samples: list[tuple[list[float], float]], title: str) -> None:
    if len(samples) < DECILES * 20:
        print(f"\n=== {title}: too few samples ({len(samples)}) ===")
        return
    # Composite score: average of per-feature ranks (all same sign family).
    per_feature_ranks = [
        rank([sample[0][i] for sample in samples]) for i in range(len(CLUSTER))
    ]
    scores = [
        sum(per_feature_ranks[i][j] for i in range(len(CLUSTER))) / len(CLUSTER)
        for j in range(len(samples))
    ]
    order = sorted(range(len(samples)), key=lambda j: scores[j])
    bucket = len(samples) // DECILES

    print(f"\n=== {title} (n={len(samples):,}) ===")
    print("score decile 1 = least extended (most oversold), 10 = most extended")
    print(f"{'decile':>6} {'n':>7} {'avg fwd':>9} {'win %':>7} {'after-cost fade':>16}")
    print("-" * 50)
    for d in range(DECILES):
        chunk = order[d * bucket : (d + 1) * bucket] if d < DECILES - 1 else order[d * bucket :]
        forwards = [samples[j][1] for j in chunk]
        avg = sum(forwards) / len(forwards)
        wins = sum(1 for f in forwards if f > 0) / len(forwards)
        # Fade direction: long the bottom deciles, short the top deciles.
        direction = 1 if d < DECILES / 2 else -1
        net = direction * avg - COST_TICKS
        print(f"{d + 1:>6} {len(forwards):>7,} {avg:>+9.2f} {wins * 100:>6.1f}% {net:>+15.2f}")

    bottom = [samples[j][1] for j in order[:bucket]]
    top = [samples[j][1] for j in order[-bucket:]]
    spread = sum(bottom) / len(bottom) - sum(top) / len(top)
    se = math.sqrt(
        _var(bottom) / len(bottom) + _var(top) / len(top)
    ) if len(bottom) > 1 and len(top) > 1 else float("nan")
    print(f"\nD1-D10 spread: {spread:+.2f} ticks  (~{spread / 2:+.2f}/side, "
          f"2se {2 * se:.2f}); after {COST_TICKS:.0f}-tick cost per side: "
          f"{spread / 2 - COST_TICKS:+.2f}")


def _var(values: list[float]) -> float:
    mean = sum(values) / len(values)
    return sum((v - mean) ** 2 for v in values) / (len(values) - 1)


def main() -> int:
    if not FEATURES.exists() or not STATES.exists():
        print("Feature or state table missing. Run the pipeline first.")
        return 1
    samples = load_samples()
    decile_report(samples, "OVERNIGHT, all bars (overlapping windows)")
    decile_report(samples[::5], "OVERNIGHT, every 5th bar (no overlap)")
    print(
        "\nCaveats: one month of data = one regime. Forward ticks are measured"
        "\non mid/close prices - overnight 'reversion' can be bid-ask bounce"
        "\nyou cannot actually capture. If the tails look tradeable here, the"
        "\nnext step is re-measuring entries/exits against recorded quotes."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
