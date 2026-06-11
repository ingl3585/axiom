from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import time

from bar_features import BarFeatureConfig, build_bar_features, contract_part_from_id
from bars import build_session_bars, date_contract_partitions, load_continuous_bars
from config import Settings
from execution import (
    ExecutionConfig,
    ExecutionController,
    format_execution_event,
)
from features import IntradayFeatureConfig, build_intraday_features
from history import HistoryBackfillResult, backfill_historical_bars
from live_signals import LiveSignalEngine, format_decision_line
from normalize import append_manifest, normalize_history_dir, normalize_realtime_dir
from projectx import (
    BarUnit,
    Contract,
    ProjectXClient,
    ProjectXError,
    bar_unit_from_name,
    compact_utc,
    unit_seconds,
)
from recording import RecordingConfig, start_realtime_recorder
from retention import compress_old_realtime
from state_profile import StateProfileConfig, build_state_profile
from walkforward import find_latest_states_path, run_signals_report

DEFAULT_SYMBOL = "MNQ"
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
        days=settings.history_days,
        unit=bar_unit_from_name(settings.bar_unit),
        unit_number=settings.bar_unit_number,
        live=settings.projectx_live,
    )
    print_backfill_result(backfill_result)

    print_section("Normalize")
    normalize_all(settings)

    compressed = compress_old_realtime(settings.data_dir, settings.raw_retention_days)
    if compressed:
        print(
            f"\nretention: compressed {len(compressed)} raw realtime files "
            f"older than {settings.raw_retention_days} days"
        )

    print_section("Features")
    build_latest_features(settings)

    print_section("Bar Features")
    build_bar_feature_table(settings, backfill_result.contract.id)

    print_section("Edge Gate")
    gate_open = False
    try:
        signal_report = run_signals_report(settings)
        gate_open = signal_report.result.gate_open
    except ValueError as exc:
        print(f"skipped signals evaluation: {exc}")

    print_section("Live Recording")
    code = record_live_data(
        settings=settings,
        client=client,
        contract_id=backfill_result.contract.id,
        gate_open=gate_open,
    )
    if code:
        return code

    print_section("Done")
    print("Axiom pipeline complete.")
    return 0


def find_latest_realtime_dir(data_dir: Path) -> Path | None:
    realtime_root = data_dir / "raw" / "projectx" / "realtime"
    dirs = [path for path in realtime_root.rglob("contract=*") if path.is_dir()]
    if not dirs:
        return None
    return max(dirs, key=lambda path: path.stat().st_mtime)


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


def build_bar_feature_table(settings: Settings, contract_id: str) -> None:
    build_bar_features_for_partition(
        data_dir=settings.data_dir,
        contract_part=contract_part_from_id(contract_id),
        bar_unit=bar_unit_from_name(settings.bar_unit),
        bar_unit_number=settings.bar_unit_number,
    )


def build_bar_features_for_partition(
    *,
    data_dir: Path,
    contract_part: str,
    bar_unit: BarUnit,
    bar_unit_number: int,
) -> None:
    result = build_bar_features(
        BarFeatureConfig(
            data_dir=data_dir,
            contract_part=contract_part,
            unit=bar_unit,
            unit_number=bar_unit_number,
        )
    )
    if result.bars == 0:
        print("skipped bar features: no continuous bars found.")
        return
    print(f"bar features: {result.rows:,} rows from {result.bars:,} bars")
    print(f"  output: {result.path}")

    profile = build_state_profile(
        StateProfileConfig(
            data_dir=data_dir,
            feature_path=result.path,
            horizon_bars=5,
            tick_size=DEFAULT_TICK_SIZE,
        )
    )
    print(
        f"state profile: {profile.labeled_rows:,} labeled rows "
        f"across {profile.states:,} states"
    )
    print(f"  states: {profile.rows_path}")
    print(f"  summary: {profile.markdown_path}")


def record_live_data(
    settings: Settings,
    client: ProjectXClient,
    contract_id: str,
    gate_open: bool,
) -> int:
    print("Recording real-time Project X data. Press Ctrl+C to stop.")
    sys.stdout.flush()
    bar_unit = bar_unit_from_name(settings.bar_unit)
    # Build the engine before the recorder starts so no bar can close in the
    # gap: the engine's bootstrap absorbs pre-existing live files silently,
    # and every bar the new recorder emits is then decided on.
    engine = build_live_engine(settings, contract_id, bar_unit)
    executor = build_execution_controller(settings, client, contract_id, gate_open)
    process = start_realtime_recorder(
        RecordingConfig(
            contract_id=contract_id,
            events=DEFAULT_RECORD_EVENTS,
            data_dir=settings.data_dir,
            live_features=True,
            feature_windows=DEFAULT_WINDOWS,
            feature_interval_seconds=DEFAULT_INTERVAL_SECONDS,
            bar_interval_seconds=unit_seconds(bar_unit, settings.bar_unit_number),
        )
    )
    code = watch_recording(process, engine, executor)

    print_section("Finalize Recorded Data")
    try:
        finalize_realtime_capture(
            data_dir=settings.data_dir,
            windows=DEFAULT_WINDOWS,
            horizons=DEFAULT_HORIZONS,
            interval_seconds=DEFAULT_INTERVAL_SECONDS,
            max_stale_quote_seconds=DEFAULT_MAX_STALE_QUOTE_SECONDS,
            tick_size=DEFAULT_TICK_SIZE,
            bar_unit=bar_unit,
            bar_unit_number=settings.bar_unit_number,
        )
    except ValueError as exc:
        print(f"skipped recorded-data finalization: {exc}")

    # Bar features, the state profile, and the edge gate are rebuilt at the
    # start of the next pipeline run (the run_forever loop restarts within a
    # minute), so the session is folded in exactly once - no duplicate rebuild.
    return code


def build_live_engine(
    settings: Settings,
    contract_id: str,
    bar_unit: BarUnit,
) -> LiveSignalEngine | None:
    states_path = find_latest_states_path(settings.data_dir)
    if states_path is None:
        print("live signals: no states.csv yet; live decisions disabled this session.")
        return None
    engine = LiveSignalEngine.from_disk(
        data_dir=settings.data_dir,
        contract_id=contract_id,
        unit=bar_unit,
        unit_number=settings.bar_unit_number,
        states_path=states_path,
    )
    print(
        "live signals: observe-only decision stream active "
        f"(ledger states: {len(engine.ledger):,}); no orders are placed."
    )
    return engine


def build_execution_controller(
    settings: Settings,
    client: ProjectXClient,
    contract_id: str,
    gate_open: bool,
) -> ExecutionController | None:
    if not settings.execution_enabled:
        return None
    controller = ExecutionController(
        client=client,
        config=ExecutionConfig.from_settings(settings),
        data_dir=settings.data_dir,
        contract_id=contract_id,
        gate_open=gate_open,
    )
    for event in controller.startup():
        print(format_execution_event(event))
    return controller


def watch_recording(
    process: subprocess.Popen,
    engine: LiveSignalEngine | None,
    executor: ExecutionController | None = None,
) -> int:
    try:
        while True:
            code = process.poll()
            if engine is not None:
                for payload in engine.poll():
                    print(format_decision_line(payload))
                    if executor is not None:
                        for event in executor.on_decision(payload):
                            print(format_execution_event(event))
                    sys.stdout.flush()
            if code is not None:
                return code
            time.sleep(1.0)
    except KeyboardInterrupt:
        # The console delivers Ctrl+C to the recorder too; let it close cleanly.
        print("\nRecording stopped by user.", file=sys.stderr)
        try:
            return process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            return 130


def finalize_realtime_capture(
    *,
    data_dir: Path,
    windows: str,
    horizons: str,
    interval_seconds: int,
    max_stale_quote_seconds: int,
    tick_size: float,
    bar_unit: BarUnit = BarUnit.MINUTE,
    bar_unit_number: int = 1,
) -> None:
    directory = find_latest_realtime_dir(data_dir)
    if directory is None:
        raise ValueError("No real-time capture directory found.")

    outputs = normalize_realtime_dir(directory, data_dir)
    append_manifest(data_dir, outputs)
    print_normalized_files(outputs)

    build_session_bars_from_outputs(data_dir, outputs, bar_unit, bar_unit_number)

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


def build_session_bars_from_outputs(
    data_dir: Path,
    outputs: list[object],
    bar_unit: BarUnit,
    bar_unit_number: int,
) -> None:
    trade_output = next((output for output in outputs if output.name == "trades"), None)
    if trade_output is None:
        print("session bars: no trades captured, skipped.")
        return

    bars_result = build_session_bars(
        data_dir,
        trade_output.path,
        unit=bar_unit,
        unit_number=bar_unit_number,
    )
    print(f"session bars: {bars_result.bars:,} rows")
    print(f"  source: {bars_result.source}")
    print(f"  output: {bars_result.path}")

    _, contract_part = date_contract_partitions(trade_output.path)
    continuous = load_continuous_bars(data_dir, contract_part, bar_unit, bar_unit_number)
    print(f"  continuous bars (history + live): {len(continuous):,}")
    # Bar features and the state profile are NOT rebuilt here: the next
    # pipeline run rebuilds them once at startup (before the edge gate), so
    # each session folds in exactly once across run_forever cycles.


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
