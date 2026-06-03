from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
import csv
import json
import math
from typing import Any


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if not text or text.startswith("0001-01-01"):
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    # Project X sometimes emits 7 fractional digits. Python accepts 6.
    if "." in text:
        prefix, suffix = text.split(".", 1)
        offset = ""
        fraction = suffix
        for marker in ("+", "-"):
            if marker in suffix:
                fraction, offset = suffix.split(marker, 1)
                offset = marker + offset
                break
        if len(fraction) > 6:
            text = f"{prefix}.{fraction[:6]}{offset}"

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def fmt_dt(value: datetime | None) -> str:
    if value is None:
        return "n/a"
    return value.isoformat().replace("+00:00", "Z")


def fmt_number(value: float | int | None, decimals: int = 2) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, int):
        return f"{value:,}"
    if not math.isfinite(value):
        return "n/a"
    return f"{value:,.{decimals}f}"


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = (len(ordered) - 1) * pct
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[int(index)]
    weight = index - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def find_latest_file(root: Path, pattern: str) -> Path | None:
    files = [path for path in root.rglob(pattern) if path.is_file()]
    if not files:
        return None
    return max(files, key=lambda path: path.stat().st_mtime)


def find_latest_realtime_dir(root: Path) -> Path | None:
    realtime_root = root / "raw" / "projectx" / "realtime"
    dirs = [path for path in realtime_root.rglob("contract=*") if path.is_dir()]
    if not dirs:
        return None
    return max(dirs, key=lambda path: path.stat().st_mtime)


@dataclass
class BarsQa:
    path: Path
    rows: int
    unique_timestamps: int
    duplicate_timestamps: int
    non_monotonic_steps: int
    start: datetime | None
    end: datetime | None
    expected_step_seconds: int | None
    calendar_gap_count: int
    max_gap_seconds: int | None
    top_gaps: list[tuple[datetime, datetime, int]]
    ohlc_violations: int
    zero_volume_bars: int
    total_volume: int
    avg_volume: float | None
    p50_volume: float | None
    p95_volume: float | None
    min_close: float | None
    max_close: float | None
    avg_abs_close_change_ticks: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "rows": self.rows,
            "unique_timestamps": self.unique_timestamps,
            "duplicate_timestamps": self.duplicate_timestamps,
            "non_monotonic_steps": self.non_monotonic_steps,
            "start": fmt_dt(self.start),
            "end": fmt_dt(self.end),
            "expected_step_seconds": self.expected_step_seconds,
            "calendar_gap_count": self.calendar_gap_count,
            "max_gap_seconds": self.max_gap_seconds,
            "top_gaps": [
                {"from": fmt_dt(start), "to": fmt_dt(end), "seconds": seconds}
                for start, end, seconds in self.top_gaps
            ],
            "ohlc_violations": self.ohlc_violations,
            "zero_volume_bars": self.zero_volume_bars,
            "total_volume": self.total_volume,
            "avg_volume": self.avg_volume,
            "p50_volume": self.p50_volume,
            "p95_volume": self.p95_volume,
            "min_close": self.min_close,
            "max_close": self.max_close,
            "avg_abs_close_change_ticks": self.avg_abs_close_change_ticks,
        }

    def to_markdown(self) -> str:
        lines = [
            "# Axiom Bar Data QA",
            "",
            f"- File: `{self.path}`",
            f"- Rows: {self.rows:,}",
            f"- Unique timestamps: {self.unique_timestamps:,}",
            f"- Duplicate timestamps: {self.duplicate_timestamps:,}",
            f"- Non-monotonic steps: {self.non_monotonic_steps:,}",
            f"- Start: {fmt_dt(self.start)}",
            f"- End: {fmt_dt(self.end)}",
            f"- Expected step: {self.expected_step_seconds or 'n/a'} seconds",
            f"- Calendar gaps above expected step: {self.calendar_gap_count:,}",
            f"- Max gap: {self.max_gap_seconds or 'n/a'} seconds",
            "",
            "## Price And Volume",
            "",
            f"- OHLC integrity violations: {self.ohlc_violations:,}",
            f"- Zero-volume bars: {self.zero_volume_bars:,}",
            f"- Total volume: {self.total_volume:,}",
            f"- Average volume/bar: {fmt_number(self.avg_volume)}",
            f"- P50 volume/bar: {fmt_number(self.p50_volume)}",
            f"- P95 volume/bar: {fmt_number(self.p95_volume)}",
            f"- Close range: {fmt_number(self.min_close)} to {fmt_number(self.max_close)}",
            (
                "- Average absolute close-to-close move: "
                f"{fmt_number(self.avg_abs_close_change_ticks)} ticks"
            ),
        ]

        if self.top_gaps:
            lines.extend(["", "## Largest Calendar Gaps", ""])
            for start, end, seconds in self.top_gaps:
                lines.append(f"- {fmt_dt(start)} to {fmt_dt(end)}: {seconds:,} seconds")
        return "\n".join(lines) + "\n"


def analyze_bars_csv(path: Path, tick_size: float = 0.25) -> BarsQa:
    with path.open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    return analyze_bars_rows(rows, path, tick_size=tick_size)


def find_latest_bars_partition(bars_root: Path) -> Path | None:
    """Return the contract/unit partition whose newest CSV was written last.

    Bronze bars are stored one CSV per backfill window, so picking the newest
    file by mtime (as ``find_latest_file`` does) usually lands on the smallest
    tail window. QA should instead look at a whole stitched partition.
    """
    if not bars_root.exists():
        return None
    partitions = [
        path
        for path in bars_root.rglob("unit=*")
        if path.is_dir() and any(path.glob("*.csv"))
    ]
    if not partitions:
        return None
    return max(partitions, key=_partition_mtime)


def _partition_mtime(partition: Path) -> float:
    return max(csv_path.stat().st_mtime for csv_path in partition.glob("*.csv"))


def stitch_bars_rows(partition_dir: Path) -> list[dict[str, Any]]:
    """Concatenate every CSV in a partition, de-duped by timestamp and sorted."""
    combined: dict[str, dict[str, Any]] = {}
    for csv_path in sorted(partition_dir.glob("*.csv")):
        with csv_path.open(encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                key = str(row.get("t") or "")
                if key:
                    combined[key] = row
    return [combined[key] for key in sorted(combined)]


def analyze_bars_partition(partition_dir: Path, tick_size: float = 0.25) -> BarsQa:
    rows = stitch_bars_rows(partition_dir)
    return analyze_bars_rows(rows, partition_dir, tick_size=tick_size)


def analyze_bars_rows(
    rows: list[dict[str, Any]],
    label: Path,
    tick_size: float = 0.25,
) -> BarsQa:
    timestamps: list[datetime] = []
    volumes: list[float] = []
    closes: list[float] = []
    ohlc_violations = 0
    zero_volume_bars = 0

    for row in rows:
        timestamp = parse_dt(row.get("t"))
        if timestamp is not None:
            timestamps.append(timestamp)

        try:
            open_price = float(row["o"])
            high_price = float(row["h"])
            low_price = float(row["l"])
            close_price = float(row["c"])
            volume = float(row["v"])
        except (KeyError, TypeError, ValueError):
            ohlc_violations += 1
            continue

        if high_price < max(open_price, low_price, close_price):
            ohlc_violations += 1
        if low_price > min(open_price, high_price, close_price):
            ohlc_violations += 1
        if volume == 0:
            zero_volume_bars += 1
        volumes.append(volume)
        closes.append(close_price)

    non_monotonic_steps = 0
    gaps: list[tuple[datetime, datetime, int]] = []
    expected_step_seconds = infer_expected_step_seconds(label, timestamps)
    for previous, current in zip(timestamps, timestamps[1:]):
        delta_seconds = int((current - previous).total_seconds())
        if delta_seconds <= 0:
            non_monotonic_steps += 1
        if expected_step_seconds and delta_seconds > expected_step_seconds:
            gaps.append((previous, current, delta_seconds))

    close_changes_ticks: list[float] = []
    if tick_size > 0:
        for previous, current in zip(closes, closes[1:]):
            close_changes_ticks.append(abs(current - previous) / tick_size)

    unique_timestamps = len(set(timestamps))
    return BarsQa(
        path=label,
        rows=len(rows),
        unique_timestamps=unique_timestamps,
        duplicate_timestamps=len(timestamps) - unique_timestamps,
        non_monotonic_steps=non_monotonic_steps,
        start=min(timestamps) if timestamps else None,
        end=max(timestamps) if timestamps else None,
        expected_step_seconds=expected_step_seconds,
        calendar_gap_count=len(gaps),
        max_gap_seconds=max((gap[2] for gap in gaps), default=None),
        top_gaps=sorted(gaps, key=lambda gap: gap[2], reverse=True)[:5],
        ohlc_violations=ohlc_violations,
        zero_volume_bars=zero_volume_bars,
        total_volume=int(sum(volumes)),
        avg_volume=mean(volumes),
        p50_volume=percentile(volumes, 0.50),
        p95_volume=percentile(volumes, 0.95),
        min_close=min(closes) if closes else None,
        max_close=max(closes) if closes else None,
        avg_abs_close_change_ticks=mean(close_changes_ticks),
    )


def infer_expected_step_seconds(path: Path, timestamps: list[datetime]) -> int | None:
    path_text = str(path).lower()
    units = {
        "second": 1,
        "minute": 60,
        "hour": 60 * 60,
        "day": 24 * 60 * 60,
        "week": 7 * 24 * 60 * 60,
    }
    for unit, seconds in units.items():
        marker = f"unit={unit}_"
        if marker in path_text:
            try:
                number_text = path_text.split(marker, 1)[1].split("\\", 1)[0].split("/", 1)[0]
                return int(number_text) * seconds
            except (IndexError, ValueError):
                return seconds

    deltas = [
        int((current - previous).total_seconds())
        for previous, current in zip(timestamps, timestamps[1:])
        if current > previous
    ]
    if not deltas:
        return None
    return int(percentile(deltas, 0.50) or 0) or None


@dataclass
class EventQa:
    name: str
    path: Path
    frames: int = 0
    parse_errors: int = 0
    payload_records: int = 0
    placeholder_event_timestamps: int = 0
    missing_event_timestamps: int = 0
    observed_start: datetime | None = None
    observed_end: datetime | None = None
    event_start: datetime | None = None
    event_end: datetime | None = None
    observed_lags_ms: list[float] = field(default_factory=list)
    quote_spreads: list[float] = field(default_factory=list)
    quote_crossed_or_locked: int = 0
    trade_volume: int = 0
    trade_prices: list[float] = field(default_factory=list)
    trade_types: Counter[int] = field(default_factory=Counter)
    depth_types: Counter[int] = field(default_factory=Counter)

    @property
    def invalid_event_timestamps(self) -> int:
        return self.placeholder_event_timestamps + self.missing_event_timestamps

    def duration_seconds(self) -> float | None:
        if not self.observed_start or not self.observed_end:
            return None
        return max((self.observed_end - self.observed_start).total_seconds(), 0.0)

    def frames_per_second(self) -> float | None:
        duration = self.duration_seconds()
        if not duration:
            return None
        return self.frames / duration

    def payload_records_per_second(self) -> float | None:
        duration = self.duration_seconds()
        if not duration:
            return None
        return self.payload_records / duration

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": str(self.path),
            "frames": self.frames,
            "parse_errors": self.parse_errors,
            "payload_records": self.payload_records,
            "placeholder_event_timestamps": self.placeholder_event_timestamps,
            "missing_event_timestamps": self.missing_event_timestamps,
            "placeholder_or_invalid_event_timestamps": self.invalid_event_timestamps,
            "observed_start": fmt_dt(self.observed_start),
            "observed_end": fmt_dt(self.observed_end),
            "event_start": fmt_dt(self.event_start),
            "event_end": fmt_dt(self.event_end),
            "duration_seconds": self.duration_seconds(),
            "frames_per_second": self.frames_per_second(),
            "payload_records_per_second": self.payload_records_per_second(),
            "lag_ms_avg": mean(self.observed_lags_ms),
            "lag_ms_p50": percentile(self.observed_lags_ms, 0.50),
            "lag_ms_p95": percentile(self.observed_lags_ms, 0.95),
            "spread_avg": mean(self.quote_spreads),
            "spread_p50": percentile(self.quote_spreads, 0.50),
            "spread_p95": percentile(self.quote_spreads, 0.95),
            "quote_crossed_or_locked": self.quote_crossed_or_locked,
            "trade_volume": self.trade_volume,
            "trade_price_min": min(self.trade_prices) if self.trade_prices else None,
            "trade_price_max": max(self.trade_prices) if self.trade_prices else None,
            "trade_types": dict(self.trade_types),
            "depth_types": dict(self.depth_types),
        }


@dataclass
class RealtimeQa:
    directory: Path
    events: list[EventQa]

    def to_dict(self) -> dict[str, Any]:
        return {
            "directory": str(self.directory),
            "events": [event.to_dict() for event in self.events],
        }

    def to_markdown(self) -> str:
        lines = [
            "# Axiom Real-Time Data QA",
            "",
            f"- Directory: `{self.directory}`",
        ]
        for event in self.events:
            lines.extend(
                [
                    "",
                    f"## {event.name}",
                    "",
                    f"- File: `{event.path}`",
                    f"- Frames: {event.frames:,}",
                    f"- Payload records: {event.payload_records:,}",
                    f"- Parse errors: {event.parse_errors:,}",
                    (
                        "- Placeholder event timestamps (book-snapshot sentinel): "
                        f"{event.placeholder_event_timestamps:,}"
                    ),
                    (
                        "- Missing/invalid event timestamps: "
                        f"{event.missing_event_timestamps:,}"
                    ),
                    f"- Observed start: {fmt_dt(event.observed_start)}",
                    f"- Observed end: {fmt_dt(event.observed_end)}",
                    f"- Event start: {fmt_dt(event.event_start)}",
                    f"- Event end: {fmt_dt(event.event_end)}",
                    f"- Duration: {fmt_number(event.duration_seconds())} seconds",
                    f"- Frames/sec: {fmt_number(event.frames_per_second())}",
                    f"- Payload records/sec: {fmt_number(event.payload_records_per_second())}",
                    f"- Lag p50/p95: {fmt_number(percentile(event.observed_lags_ms, 0.50))} ms / "
                    f"{fmt_number(percentile(event.observed_lags_ms, 0.95))} ms",
                ]
            )
            if event.quote_spreads:
                lines.extend(
                    [
                        f"- Spread avg/p50/p95: {fmt_number(mean(event.quote_spreads))} / "
                        f"{fmt_number(percentile(event.quote_spreads, 0.50))} / "
                        f"{fmt_number(percentile(event.quote_spreads, 0.95))}",
                        f"- Crossed or locked quotes: {event.quote_crossed_or_locked:,}",
                    ]
                )
            if event.trade_prices:
                lines.extend(
                    [
                        f"- Trade volume: {event.trade_volume:,}",
                        f"- Trade price range: {fmt_number(min(event.trade_prices))} to "
                        f"{fmt_number(max(event.trade_prices))}",
                        f"- Trade types: {dict(event.trade_types)}",
                    ]
                )
            if event.depth_types:
                lines.append(f"- Depth types: {dict(event.depth_types)}")
        return "\n".join(lines) + "\n"


def analyze_realtime_dir(directory: Path) -> RealtimeQa:
    files = {
        "quotes": directory / "quotes.jsonl",
        "trades": directory / "trades.jsonl",
        "depth": directory / "depth.jsonl",
    }
    events = [analyze_event_file(name, path) for name, path in files.items() if path.exists()]
    return RealtimeQa(directory=directory, events=events)


def analyze_event_file(name: str, path: Path) -> EventQa:
    qa = EventQa(name=name, path=path)
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            qa.frames += 1
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                qa.parse_errors += 1
                continue

            observed_at = parse_dt(frame.get("observedAt"))
            update_dt_range(qa, "observed", observed_at)
            payload = frame.get("data")
            records = payload if isinstance(payload, list) else [payload]
            for record in records:
                if not isinstance(record, dict):
                    continue
                qa.payload_records += 1
                event_time = event_timestamp(name, record)
                if event_time is None:
                    if is_placeholder_timestamp(raw_event_timestamp(name, record)):
                        qa.placeholder_event_timestamps += 1
                    else:
                        qa.missing_event_timestamps += 1
                else:
                    update_dt_range(qa, "event", event_time)
                    if observed_at:
                        qa.observed_lags_ms.append(
                            (observed_at - event_time).total_seconds() * 1000
                        )

                if name == "quotes":
                    analyze_quote_record(qa, record)
                elif name == "trades":
                    analyze_trade_record(qa, record)
                elif name == "depth":
                    analyze_depth_record(qa, record)
    return qa


def update_dt_range(qa: EventQa, prefix: str, value: datetime | None) -> None:
    if value is None:
        return
    start_name = f"{prefix}_start"
    end_name = f"{prefix}_end"
    start_value = getattr(qa, start_name)
    end_value = getattr(qa, end_name)
    if start_value is None or value < start_value:
        setattr(qa, start_name, value)
    if end_value is None or value > end_value:
        setattr(qa, end_name, value)


def raw_event_timestamp(name: str, record: dict[str, Any]) -> str:
    if name == "quotes":
        return str(record.get("lastUpdated") or record.get("timestamp") or "")
    return str(record.get("timestamp") or record.get("lastUpdated") or "")


def event_timestamp(name: str, record: dict[str, Any]) -> datetime | None:
    return parse_dt(raw_event_timestamp(name, record))


def is_placeholder_timestamp(text: str) -> bool:
    # Project X emits .NET DateTime.MinValue (0001-01-01) on records that carry
    # no per-record time, e.g. order-book snapshot levels. That is expected, not
    # a recorder fault, so it is tracked separately from truly missing values.
    return text.strip().startswith("0001-01-01")


def analyze_quote_record(qa: EventQa, record: dict[str, Any]) -> None:
    bid = record.get("bestBid")
    ask = record.get("bestAsk")
    if bid is None or ask is None:
        return
    try:
        spread = float(ask) - float(bid)
    except (TypeError, ValueError):
        return
    qa.quote_spreads.append(spread)
    if spread <= 0:
        qa.quote_crossed_or_locked += 1


def analyze_trade_record(qa: EventQa, record: dict[str, Any]) -> None:
    try:
        qa.trade_volume += int(record.get("volume") or 0)
    except (TypeError, ValueError):
        pass
    try:
        qa.trade_prices.append(float(record["price"]))
    except (KeyError, TypeError, ValueError):
        pass
    try:
        qa.trade_types[int(record["type"])] += 1
    except (KeyError, TypeError, ValueError):
        pass


def analyze_depth_record(qa: EventQa, record: dict[str, Any]) -> None:
    try:
        qa.depth_types[int(record["type"])] += 1
    except (KeyError, TypeError, ValueError):
        pass


def write_report_pair(report_dir: Path, stem: str, markdown: str, payload: dict[str, Any]) -> tuple[Path, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    md_path = report_dir / f"{stem}.md"
    json_path = report_dir / f"{stem}.json"
    md_path.write_text(markdown, encoding="utf-8")
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return md_path, json_path
