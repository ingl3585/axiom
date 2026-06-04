from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import json
import math
from typing import Any

from .qa import (
    EventQa,
    analyze_event_file,
    find_latest_realtime_dir,
    fmt_dt,
    fmt_number,
    mean,
    parse_dt,
    percentile,
)


EVENT_FILES = {
    "quotes": "quotes.jsonl",
    "trades": "trades.jsonl",
    "depth": "depth.jsonl",
}


@dataclass(frozen=True)
class GapStats:
    count: int
    max_seconds: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "max_seconds": self.max_seconds,
        }


@dataclass(frozen=True)
class EventSessionHealth:
    name: str
    path: Path
    frames: int
    payload_records: int
    parse_errors: int
    placeholder_event_timestamps: int
    missing_event_timestamps: int
    observed_start: datetime | None
    observed_end: datetime | None
    gaps: GapStats
    frames_per_second: float | None
    payload_records_per_second: float | None
    spread_avg: float | None
    spread_p50: float | None
    spread_p95: float | None
    quote_crossed_or_locked: int
    trade_volume: int
    trade_price_min: float | None
    trade_price_max: float | None

    @classmethod
    def from_qa(cls, qa: EventQa, gaps: GapStats) -> EventSessionHealth:
        return cls(
            name=qa.name,
            path=qa.path,
            frames=qa.frames,
            payload_records=qa.payload_records,
            parse_errors=qa.parse_errors,
            placeholder_event_timestamps=qa.placeholder_event_timestamps,
            missing_event_timestamps=qa.missing_event_timestamps,
            observed_start=qa.observed_start,
            observed_end=qa.observed_end,
            gaps=gaps,
            frames_per_second=qa.frames_per_second(),
            payload_records_per_second=qa.payload_records_per_second(),
            spread_avg=mean(qa.quote_spreads),
            spread_p50=percentile(qa.quote_spreads, 0.50),
            spread_p95=percentile(qa.quote_spreads, 0.95),
            quote_crossed_or_locked=qa.quote_crossed_or_locked,
            trade_volume=qa.trade_volume,
            trade_price_min=min(qa.trade_prices) if qa.trade_prices else None,
            trade_price_max=max(qa.trade_prices) if qa.trade_prices else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": str(self.path),
            "frames": self.frames,
            "payload_records": self.payload_records,
            "parse_errors": self.parse_errors,
            "placeholder_event_timestamps": self.placeholder_event_timestamps,
            "missing_event_timestamps": self.missing_event_timestamps,
            "observed_start": fmt_dt(self.observed_start),
            "observed_end": fmt_dt(self.observed_end),
            "gaps": self.gaps.to_dict(),
            "frames_per_second": self.frames_per_second,
            "payload_records_per_second": self.payload_records_per_second,
            "spread_avg": self.spread_avg,
            "spread_p50": self.spread_p50,
            "spread_p95": self.spread_p95,
            "quote_crossed_or_locked": self.quote_crossed_or_locked,
            "trade_volume": self.trade_volume,
            "trade_price_min": self.trade_price_min,
            "trade_price_max": self.trade_price_max,
        }


@dataclass(frozen=True)
class FeatureSessionHealth:
    path: Path
    rows: int
    start: datetime | None
    end: datetime | None
    gaps: GapStats
    stale_quote_rows: int
    mid_price_min: float | None
    mid_price_max: float | None
    spread_avg: float | None
    spread_p50: float | None
    spread_p95: float | None

    def stale_quote_ratio(self) -> float | None:
        if not self.rows:
            return None
        return self.stale_quote_rows / self.rows

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "rows": self.rows,
            "start": fmt_dt(self.start),
            "end": fmt_dt(self.end),
            "gaps": self.gaps.to_dict(),
            "stale_quote_rows": self.stale_quote_rows,
            "stale_quote_ratio": self.stale_quote_ratio(),
            "mid_price_min": self.mid_price_min,
            "mid_price_max": self.mid_price_max,
            "spread_avg": self.spread_avg,
            "spread_p50": self.spread_p50,
            "spread_p95": self.spread_p95,
        }


@dataclass(frozen=True)
class SessionHealth:
    directory: Path
    generated_at: datetime
    gap_threshold_seconds: float
    stale_quote_seconds: float
    observed_since: datetime | None
    events: list[EventSessionHealth]
    features: FeatureSessionHealth | None

    def observed_start(self) -> datetime | None:
        starts = [event.observed_start for event in self.events if event.observed_start]
        return min(starts) if starts else None

    def observed_end(self) -> datetime | None:
        ends = [event.observed_end for event in self.events if event.observed_end]
        return max(ends) if ends else None

    def duration_seconds(self) -> float | None:
        start = self.observed_start()
        end = self.observed_end()
        if not start or not end:
            return None
        return max((end - start).total_seconds(), 0.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "directory": str(self.directory),
            "generated_at": fmt_dt(self.generated_at),
            "observed_start": fmt_dt(self.observed_start()),
            "observed_end": fmt_dt(self.observed_end()),
            "duration_seconds": self.duration_seconds(),
            "gap_threshold_seconds": self.gap_threshold_seconds,
            "stale_quote_seconds": self.stale_quote_seconds,
            "observed_since": fmt_dt(self.observed_since),
            "events": [event.to_dict() for event in self.events],
            "features": self.features.to_dict() if self.features else None,
        }

    def to_markdown(self) -> str:
        lines = [
            "# Axiom Session Health",
            "",
            f"- Capture directory: `{self.directory}`",
            f"- Generated: {fmt_dt(self.generated_at)}",
            f"- Filtered since: {fmt_dt(self.observed_since)}",
            (
                f"- Observed span: {fmt_dt(self.observed_start())} to "
                f"{fmt_dt(self.observed_end())}"
            ),
            f"- Duration: {fmt_number(self.duration_seconds())} seconds",
            f"- Gap threshold: {fmt_number(self.gap_threshold_seconds)} seconds",
            "",
            "## Raw Events",
            "",
            (
                "| stream | frames | records | records/sec | parse errors | "
                "gaps | max gap | missing ts |"
            ),
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for event in self.events:
            lines.append(
                f"| {event.name} | {event.frames:,} | {event.payload_records:,} | "
                f"{fmt_number(event.payload_records_per_second)} | "
                f"{event.parse_errors:,} | {event.gaps.count:,} | "
                f"{fmt_number(event.gaps.max_seconds)} | "
                f"{event.missing_event_timestamps:,} |"
            )

        quote = next((event for event in self.events if event.name == "quotes"), None)
        trades = next((event for event in self.events if event.name == "trades"), None)
        depth = next((event for event in self.events if event.name == "depth"), None)

        if quote and quote.spread_avg is not None:
            lines.extend(
                [
                    "",
                    "## Market Microstructure",
                    "",
                    (
                        "- Quote spread avg/p50/p95: "
                        f"{fmt_number(quote.spread_avg)} / "
                        f"{fmt_number(quote.spread_p50)} / "
                        f"{fmt_number(quote.spread_p95)}"
                    ),
                    f"- Crossed or locked quotes: {quote.quote_crossed_or_locked:,}",
                ]
            )
        if trades and trades.trade_price_min is not None:
            lines.extend(
                [
                    f"- Trade volume: {trades.trade_volume:,}",
                    (
                        "- Trade price range: "
                        f"{fmt_number(trades.trade_price_min)} to "
                        f"{fmt_number(trades.trade_price_max)}"
                    ),
                ]
            )
        if depth:
            lines.append(
                "- Depth placeholder timestamps: "
                f"{depth.placeholder_event_timestamps:,}"
            )

        lines.extend(["", "## Live Features", ""])
        if self.features is None:
            lines.append("- No live feature file found for this capture.")
        else:
            features = self.features
            lines.extend(
                [
                    f"- File: `{features.path}`",
                    f"- Rows: {features.rows:,}",
                    f"- Span: {fmt_dt(features.start)} to {fmt_dt(features.end)}",
                    f"- Gaps above threshold: {features.gaps.count:,}",
                    f"- Max gap: {fmt_number(features.gaps.max_seconds)} seconds",
                    (
                        "- Stale quote rows: "
                        f"{features.stale_quote_rows:,} "
                        f"({fmt_number(features.stale_quote_ratio())})"
                    ),
                    (
                        "- Mid range: "
                        f"{fmt_number(features.mid_price_min)} to "
                        f"{fmt_number(features.mid_price_max)}"
                    ),
                    (
                        "- Feature spread avg/p50/p95: "
                        f"{fmt_number(features.spread_avg)} / "
                        f"{fmt_number(features.spread_p50)} / "
                        f"{fmt_number(features.spread_p95)}"
                    ),
                ]
            )
        return "\n".join(lines) + "\n"


def analyze_session(
    data_dir: Path,
    directory: Path | None = None,
    gap_threshold_seconds: float = 10.0,
    stale_quote_seconds: float = 5.0,
    observed_since: datetime | None = None,
) -> SessionHealth:
    capture_dir = directory or find_latest_realtime_dir(data_dir)
    if capture_dir is None:
        raise ValueError("No real-time capture directory found.")

    events: list[EventSessionHealth] = []
    for name, filename in EVENT_FILES.items():
        path = capture_dir / filename
        if not path.exists():
            continue
        qa = analyze_event_file(name, path, observed_since=observed_since)
        events.append(
            EventSessionHealth.from_qa(
                qa,
                observed_gap_stats(path, gap_threshold_seconds, observed_since),
            )
        )

    feature_path = live_feature_path(data_dir, capture_dir)
    features = (
        analyze_live_feature_file(
            feature_path,
            gap_threshold_seconds=gap_threshold_seconds,
            stale_quote_seconds=stale_quote_seconds,
            observed_since=observed_since,
        )
        if feature_path.exists()
        else None
    )

    return SessionHealth(
        directory=capture_dir,
        generated_at=datetime.now(UTC).replace(microsecond=0),
        gap_threshold_seconds=gap_threshold_seconds,
        stale_quote_seconds=stale_quote_seconds,
        observed_since=observed_since,
        events=events,
        features=features,
    )


def live_feature_path(data_dir: Path, capture_dir: Path) -> Path:
    date_part = partition_part(capture_dir, "date=")
    contract_part = partition_part(capture_dir, "contract=")
    return (
        data_dir
        / "live"
        / "projectx"
        / "features"
        / date_part
        / contract_part
        / "features.jsonl"
    )


def partition_part(path: Path, prefix: str) -> str:
    part = next((item for item in path.parts if item.startswith(prefix)), None)
    if part is None:
        raise ValueError(f"Could not infer {prefix} partition from {path}")
    return part


def observed_gap_stats(
    path: Path,
    threshold_seconds: float,
    observed_since: datetime | None = None,
) -> GapStats:
    timestamps: list[datetime] = []
    for frame in read_jsonl(path):
        timestamp = parse_dt(str(frame.get("observedAt") or ""))
        if (
            observed_since is not None
            and timestamp is not None
            and timestamp < observed_since
        ):
            continue
        if timestamp is not None:
            timestamps.append(timestamp)
    return gap_stats(timestamps, threshold_seconds)


def analyze_live_feature_file(
    path: Path,
    gap_threshold_seconds: float = 10.0,
    stale_quote_seconds: float = 5.0,
    observed_since: datetime | None = None,
) -> FeatureSessionHealth:
    timestamps: list[datetime] = []
    mid_prices: list[float] = []
    spreads: list[float] = []
    stale_quote_rows = 0
    rows = 0

    for row in read_jsonl(path):
        timestamp = parse_dt(str(row.get("timestamp") or ""))
        if (
            observed_since is not None
            and timestamp is not None
            and timestamp < observed_since
        ):
            continue
        rows += 1
        if timestamp is not None:
            timestamps.append(timestamp)
        mid = finite_float(row.get("midPrice"))
        if mid is not None:
            mid_prices.append(mid)
        spread = finite_float(row.get("spread"))
        if spread is not None:
            spreads.append(spread)
        seconds_since_quote = finite_float(row.get("secondsSinceQuote"))
        if seconds_since_quote is not None and seconds_since_quote > stale_quote_seconds:
            stale_quote_rows += 1

    return FeatureSessionHealth(
        path=path,
        rows=rows,
        start=min(timestamps) if timestamps else None,
        end=max(timestamps) if timestamps else None,
        gaps=gap_stats(timestamps, gap_threshold_seconds),
        stale_quote_rows=stale_quote_rows,
        mid_price_min=min(mid_prices) if mid_prices else None,
        mid_price_max=max(mid_prices) if mid_prices else None,
        spread_avg=mean(spreads),
        spread_p50=percentile(spreads, 0.50),
        spread_p95=percentile(spreads, 0.95),
    )


def gap_stats(timestamps: list[datetime], threshold_seconds: float) -> GapStats:
    ordered = sorted(timestamps)
    gaps: list[float] = []
    for previous, current in zip(ordered, ordered[1:]):
        seconds = (current - previous).total_seconds()
        if seconds > threshold_seconds:
            gaps.append(seconds)
    return GapStats(
        count=len(gaps),
        max_seconds=max(gaps) if gaps else None,
    )


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


def finite_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result):
        return None
    return result
