from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import json
import sys
from typing import Any

from .config import Settings
from .features import IntradayFeatureConfig, build_intraday_features
from .history import HistoryBackfillResult, backfill_historical_bars
from .normalize import append_manifest, normalize_history_dir, normalize_realtime_dir
from .playbook import PlaybookConfig, evaluate_playbook
from .projectx import (
    BarUnit,
    Contract,
    ProjectXClient,
    ProjectXError,
    compact_utc,
)
from .recording import RecordingConfig, run_realtime_recorder

DEFAULT_SYMBOL = "MNQ"
DEFAULT_HISTORY_DAYS = 30
DEFAULT_TICK_SIZE = 0.25
DEFAULT_WINDOWS = "1,5,30,60"
DEFAULT_HORIZONS = "5,15,30,60"
DEFAULT_INTERVAL_SECONDS = 1
DEFAULT_MAX_STALE_QUOTE_SECONDS = 5
DEFAULT_RECORD_EVENTS = "quotes,trades,depth"


def run_pipeline() -> int:
    settings = Settings.from_env()

    print_section("Project X Auth")
    client = authenticated_client(settings)
    print(f"Authenticated. Token prefix: {client.token[:12]}...")
    client.validate_session()
    print("Session validated.")

    print_section("Historical Backfill")
    backfill_result = run_historical_backfill(
        settings=settings,
        client=client,
        symbol=DEFAULT_SYMBOL,
        days=DEFAULT_HISTORY_DAYS,
        unit=BarUnit.MINUTE,
        unit_number=1,
        live=settings.projectx_live,
    )
    print_backfill_result(backfill_result)

    print_section("Normalize")
    normalize_all(settings)

    print_section("Features")
    build_latest_features(settings)

    print_section("Live Recording")
    code = record_live_data(
        settings=settings,
        contract_id=backfill_result.contract.id,
    )
    if code:
        return code

    print_section("Done")
    print("Axiom pipeline complete.")
    return 0


def run_research() -> int:
    settings = Settings.from_env()
    path = find_latest_file(settings.data_dir / "silver" / "projectx" / "features", "*.csv")
    if path is None:
        raise ValueError("No silver features CSV found. Run `python .\\main.py` first.")

    report = evaluate_playbook(
        PlaybookConfig(
            path=path,
            horizon_seconds=30,
            tick_size=DEFAULT_TICK_SIZE,
            cost_ticks=2.0,
            cooldown_seconds=30,
            impulse_window_seconds=30,
            trigger_window_seconds=5,
            min_impulse_ticks=12.0,
            min_trigger_ticks=2.0,
            min_flow_imbalance=0.20,
            min_trigger_volume=20.0,
            max_spread_ticks=2.0,
        )
    )
    print(report.to_markdown())

    stem = f"playbook_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    md_path, json_path = write_report_pair(
        settings.data_dir / "reports" / "research",
        stem,
        report.to_markdown(),
        report.to_dict(),
    )
    print(f"Saved reports: {md_path}, {json_path}")
    return 0


def find_latest_file(root: Path, pattern: str) -> Path | None:
    files = [path for path in root.rglob(pattern) if path.is_file()] if root.exists() else []
    if not files:
        return None
    return max(files, key=lambda path: path.stat().st_mtime)


def find_latest_realtime_dir(data_dir: Path) -> Path | None:
    realtime_root = data_dir / "raw" / "projectx" / "realtime"
    dirs = [path for path in realtime_root.rglob("contract=*") if path.is_dir()]
    if not dirs:
        return None
    return max(dirs, key=lambda path: path.stat().st_mtime)


def write_report_pair(
    report_dir: Path,
    stem: str,
    markdown: str,
    payload: dict[str, Any],
) -> tuple[Path, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    md_path = report_dir / f"{stem}.md"
    json_path = report_dir / f"{stem}.json"
    md_path.write_text(markdown, encoding="utf-8")
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return md_path, json_path


def authenticated_client(settings: Settings) -> ProjectXClient:
    username, api_key = settings.require_projectx_credentials()
    client = ProjectXClient(base_url=settings.projectx_base_url)
    client.authenticate(username, api_key)
    return client


def run_historical_backfill(
    *,
    settings: Settings,
    client: ProjectXClient,
    symbol: str,
    days: int,
    unit: BarUnit,
    unit_number: int,
    live: bool,
) -> HistoryBackfillResult:
    contract = resolve_active_contract(client, symbol=symbol, live=live)
    print(f"Selected {contract.name} ({contract.id}) - {contract.description}")
    return backfill_historical_bars(
        client=client,
        data_dir=settings.data_dir,
        symbol=symbol,
        contract=contract,
        unit=unit,
        unit_number=unit_number,
        initial_days=days,
        live=live,
    )


def normalize_all(settings: Settings) -> None:
    history_outputs = normalize_history_dir(settings.data_dir)
    if history_outputs:
        append_manifest(settings.data_dir, history_outputs)
        print_normalized_files(history_outputs)
    else:
        print("No raw historical bar files found.")

    print()
    realtime_dir = find_latest_realtime_dir(settings.data_dir)
    if realtime_dir is None:
        print("No raw real-time capture directory found.")
        return
    realtime_outputs = normalize_realtime_dir(realtime_dir, settings.data_dir)
    append_manifest(settings.data_dir, realtime_outputs)
    print_normalized_files(realtime_outputs)


def build_latest_features(settings: Settings) -> None:
    try:
        result = build_intraday_features(
            IntradayFeatureConfig(
                data_dir=settings.data_dir,
                windows_seconds=parse_int_list(DEFAULT_WINDOWS),
                horizons_seconds=parse_int_list(DEFAULT_HORIZONS),
                interval_seconds=DEFAULT_INTERVAL_SECONDS,
                max_stale_quote_seconds=DEFAULT_MAX_STALE_QUOTE_SECONDS,
                tick_size=DEFAULT_TICK_SIZE,
            )
        )
    except ValueError as exc:
        if "No bronze quote CSV found" not in str(exc):
            raise
        print(f"skipped features: {exc}")
        return
    print_feature_result(result)


def record_live_data(settings: Settings, contract_id: str) -> int:
    print("Recording real-time Project X data. Press Ctrl+C to stop.")
    sys.stdout.flush()
    code = run_realtime_recorder(
        RecordingConfig(
            contract_id=contract_id,
            events=DEFAULT_RECORD_EVENTS,
            data_dir=settings.data_dir,
            live_features=True,
            feature_windows=DEFAULT_WINDOWS,
            feature_interval_seconds=DEFAULT_INTERVAL_SECONDS,
        )
    )

    print_section("Finalize Recorded Data")
    try:
        finalize_realtime_capture(
            data_dir=settings.data_dir,
            windows=DEFAULT_WINDOWS,
            horizons=DEFAULT_HORIZONS,
            interval_seconds=DEFAULT_INTERVAL_SECONDS,
            max_stale_quote_seconds=DEFAULT_MAX_STALE_QUOTE_SECONDS,
            tick_size=DEFAULT_TICK_SIZE,
        )
    except ValueError as exc:
        print(f"skipped recorded-data finalization: {exc}")

    return code


def finalize_realtime_capture(
    *,
    data_dir: Path,
    windows: str,
    horizons: str,
    interval_seconds: int,
    max_stale_quote_seconds: int,
    tick_size: float,
) -> None:
    directory = find_latest_realtime_dir(data_dir)
    if directory is None:
        raise ValueError("No real-time capture directory found.")

    outputs = normalize_realtime_dir(directory, data_dir)
    append_manifest(data_dir, outputs)
    print_normalized_files(outputs)

    quote_output = next((output for output in outputs if output.name == "quotes"), None)
    if quote_output is None:
        raise ValueError("No normalized quote file found for latest capture.")

    result = build_intraday_features(
        IntradayFeatureConfig(
            data_dir=data_dir,
            quote_path=quote_output.path,
            windows_seconds=parse_int_list(windows),
            horizons_seconds=parse_int_list(horizons),
            interval_seconds=interval_seconds,
            max_stale_quote_seconds=max_stale_quote_seconds,
            tick_size=tick_size,
        )
    )
    print_feature_result(result)


def resolve_active_contract(
    client: ProjectXClient,
    symbol: str,
    live: bool,
) -> Contract:
    contracts = client.search_contracts(symbol, live=live)
    active = [contract for contract in contracts if contract.active_contract]
    if not active:
        raise ProjectXError(f"No active contract found for {symbol}")
    return next(
        (
            item
            for item in active
            if item.symbol_id.upper().endswith(f".{symbol.upper()}")
            or item.name.upper().startswith(symbol.upper())
        ),
        active[0],
    )


def print_normalized_files(outputs: list[object]) -> None:
    if not outputs:
        print("No files normalized.")
        return
    for output in outputs:
        print(f"{output.name}: {output.rows:,} rows")
        print(f"  source: {output.source}")
        print(f"  output: {output.path}")


def print_feature_result(result: object) -> None:
    print(f"intraday features: {result.rows:,} rows")
    print(f"  quotes: {result.quote_path}")
    if result.trade_path:
        print(f"  trades: {result.trade_path}")
    if result.depth_path:
        print(f"  depth: {result.depth_path}")
    print(f"  output: {result.path}")


def print_backfill_result(result: HistoryBackfillResult) -> None:
    print(
        f"{result.symbol} {result.contract.name} {result.unit.name.lower()}_"
        f"{result.unit_number}: {compact_utc(result.start)} to {compact_utc(result.end)}"
    )
    if result.skipped:
        print(f"  skipped: {result.reason}")
    else:
        print(f"  downloaded bars: {result.bars:,}")
        print(f"  raw files: {len(result.raw_files):,}")
        for raw_file in result.raw_files:
            print(f"    {raw_file}")
    print(f"  state: {result.state_path}")


def parse_int_list(value: str) -> list[int]:
    parsed: list[int] = []
    for item in value.split(","):
        item = item.strip()
        if item:
            parsed.append(int(item))
    if not parsed:
        raise ValueError("Expected at least one integer value.")
    return parsed


def print_section(title: str) -> None:
    print()
    print(f"== {title} ==", flush=True)
