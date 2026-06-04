from __future__ import annotations

from argparse import ArgumentParser, Namespace
from datetime import UTC, datetime, timedelta
from pathlib import Path
import json
import sys

from .backtest import BacktestConfig, run_backtest
from .config import Settings
from .features import IntradayFeatureConfig, build_intraday_features
from .history import HistoryBackfillResult, backfill_historical_bars
from .normalize import (
    append_manifest,
    normalize_history_dir,
    normalize_bars_history_json,
    normalize_realtime_dir,
)
from .projectx import (
    BarUnit,
    Contract,
    ProjectXClient,
    ProjectXError,
    compact_utc,
    parse_bar_unit,
    parse_utc_datetime,
)
from .qa import (
    analyze_bars_csv,
    analyze_bars_partition,
    analyze_realtime_dir,
    find_latest_bars_partition,
    find_latest_file,
    find_latest_realtime_dir,
    write_report_pair,
)
from .recording import RecordingConfig, run_realtime_recorder
from .research import analyze_feature_ic
from .session import analyze_session
from .storage import bars_csv_path, history_raw_path, write_bars_csv, write_json


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(
        prog="axiom",
        description=(
            "Axiom data tooling. Run with no command to execute the default "
            "auth -> normalize -> features -> QA -> record pipeline."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser(
        "run",
        help="Run the default auth, backfill, normalize, features, QA, and record pipeline",
    )
    run_parser.add_argument("--skip-auth", action="store_true")
    run_parser.add_argument("--skip-backfill", action="store_true")
    run_parser.add_argument("--skip-normalize", action="store_true")
    run_parser.add_argument("--skip-features", action="store_true")
    run_parser.add_argument("--skip-qa", action="store_true")
    run_parser.add_argument("--skip-record", action="store_true")
    run_parser.add_argument("--backfill-symbol", default="MNQ")
    run_parser.add_argument("--backfill-days", type=int, default=30)
    run_parser.add_argument("--backfill-unit", default="minute", choices=sorted(_unit_choices()))
    run_parser.add_argument("--backfill-unit-number", type=int, default=1)
    run_parser.add_argument("--tick-size", type=float, default=0.25)
    run_parser.add_argument("--no-write-qa", action="store_true")
    run_parser.add_argument("--feature-windows", default="1,5,30,60")
    run_parser.add_argument("--feature-horizons", default="5,30,60")
    run_parser.add_argument("--feature-interval-seconds", type=int, default=1)
    run_parser.add_argument("--feature-max-stale-quote-seconds", type=int, default=5)
    run_parser.add_argument("--record-contract-id")
    run_parser.add_argument("--record-symbol", default="MNQ")
    run_parser.add_argument("--record-events", default="quotes,trades,depth")
    run_parser.add_argument("--record-duration-seconds", type=int)
    run_parser.add_argument("--record-no-live-features", action="store_true")
    run_parser.add_argument("--record-no-finalize", action="store_true")
    run_parser.add_argument("--record-no-session-summary", action="store_true")
    run_parser.add_argument("--record-feature-windows", default="1,5,30,60")
    run_parser.add_argument("--record-feature-interval-seconds", type=int, default=1)
    run_parser.add_argument("--session-gap-threshold-seconds", type=float, default=10.0)
    run_parser.add_argument("--session-stale-quote-seconds", type=float, default=5.0)
    run_parser.set_defaults(handler=cmd_run)

    auth_parser = subparsers.add_parser("auth", help="Authenticate with Project X")
    auth_parser.set_defaults(handler=cmd_auth)

    contracts = subparsers.add_parser("contracts", help="Project X contracts")
    contract_subparsers = contracts.add_subparsers(dest="contracts_command", required=True)

    search = contract_subparsers.add_parser("search", help="Search contracts")
    search.add_argument("text", help="Search text, for example MNQ")
    search.add_argument("--live", action="store_true", help="Use live data subscription")
    search.add_argument("--active-only", action="store_true")
    search.add_argument("--json", action="store_true", help="Print JSON instead of table")
    search.set_defaults(handler=cmd_contract_search)

    bars = subparsers.add_parser("bars", help="Historical bars")
    bar_subparsers = bars.add_subparsers(dest="bars_command", required=True)

    download = bar_subparsers.add_parser("download", help="Download historical bars")
    download.add_argument("--contract-id", required=True)
    download.add_argument("--start", required=True, help="UTC ISO timestamp")
    download.add_argument("--end", required=True, help="UTC ISO timestamp")
    download.add_argument("--unit", default="minute", choices=sorted(_unit_choices()))
    download.add_argument("--unit-number", type=int, default=1)
    download.add_argument("--limit", type=int, default=20_000)
    download.add_argument("--live", action="store_true")
    download.add_argument("--include-partial-bar", action="store_true")
    download.set_defaults(handler=cmd_bars_download)

    bootstrap = subparsers.add_parser(
        "bootstrap", help="Find active contract and download recent bars"
    )
    bootstrap.add_argument("--symbol", default="MNQ")
    bootstrap.add_argument("--days", type=int, default=30)
    bootstrap.add_argument("--unit", default="minute", choices=sorted(_unit_choices()))
    bootstrap.add_argument("--unit-number", type=int, default=1)
    bootstrap.add_argument("--live", action="store_true")
    bootstrap.set_defaults(handler=cmd_bootstrap)

    backfill = subparsers.add_parser(
        "backfill", help="Backfill missing historical Project X bars"
    )
    backfill.add_argument("--symbol", default="MNQ")
    backfill.add_argument("--days", type=int, default=30)
    backfill.add_argument("--unit", default="minute", choices=sorted(_unit_choices()))
    backfill.add_argument("--unit-number", type=int, default=1)
    backfill.add_argument("--live", action="store_true")
    backfill.set_defaults(handler=cmd_backfill)

    qa = subparsers.add_parser("qa", help="Data quality reports")
    qa_subparsers = qa.add_subparsers(dest="qa_command", required=True)

    qa_bars = qa_subparsers.add_parser("bars", help="QA the latest or selected bars CSV")
    qa_bars.add_argument("--path", help="Specific bars CSV path")
    qa_bars.add_argument("--tick-size", type=float, default=0.25)
    qa_bars.add_argument("--json", action="store_true", help="Print JSON instead of Markdown")
    qa_bars.add_argument("--no-write", action="store_true", help="Do not write report files")
    qa_bars.set_defaults(handler=cmd_qa_bars)

    qa_realtime = qa_subparsers.add_parser(
        "realtime", help="QA the latest or selected real-time capture directory"
    )
    qa_realtime.add_argument("--dir", help="Specific real-time contract directory")
    qa_realtime.add_argument("--json", action="store_true", help="Print JSON instead of Markdown")
    qa_realtime.add_argument("--no-write", action="store_true", help="Do not write report files")
    qa_realtime.set_defaults(handler=cmd_qa_realtime)

    qa_all = qa_subparsers.add_parser("all", help="Run bar and real-time QA")
    qa_all.add_argument("--tick-size", type=float, default=0.25)
    qa_all.add_argument("--no-write", action="store_true", help="Do not write report files")
    qa_all.set_defaults(handler=cmd_qa_all)

    normalize = subparsers.add_parser("normalize", help="Normalize raw data to bronze CSV")
    normalize_subparsers = normalize.add_subparsers(
        dest="normalize_command", required=True
    )

    normalize_bars = normalize_subparsers.add_parser(
        "bars", help="Normalize latest or selected raw history JSON"
    )
    normalize_bars.add_argument("--path", help="Specific raw history JSON path")
    normalize_bars.set_defaults(handler=cmd_normalize_bars)

    normalize_realtime = normalize_subparsers.add_parser(
        "realtime", help="Normalize latest or selected real-time capture directory"
    )
    normalize_realtime.add_argument("--dir", help="Specific raw real-time contract directory")
    normalize_realtime.set_defaults(handler=cmd_normalize_realtime)

    normalize_all = normalize_subparsers.add_parser(
        "all", help="Normalize latest raw bars and real-time capture"
    )
    normalize_all.set_defaults(handler=cmd_normalize_all)

    record = subparsers.add_parser("record", help="Record real-time Project X market data")
    record.add_argument("--contract-id")
    record.add_argument("--symbol", default="MNQ")
    record.add_argument("--events", default="quotes,trades,depth")
    record.add_argument("--duration-seconds", type=int)
    record.add_argument("--no-live-features", action="store_true")
    record.add_argument("--no-finalize", action="store_true")
    record.add_argument("--no-session-summary", action="store_true")
    record.add_argument("--feature-windows", default="1,5,30,60")
    record.add_argument("--feature-horizons", default="5,15,30,60")
    record.add_argument("--feature-interval-seconds", type=int, default=1)
    record.add_argument("--max-stale-quote-seconds", type=int, default=5)
    record.add_argument("--tick-size", type=float, default=0.25)
    record.add_argument("--session-gap-threshold-seconds", type=float, default=10.0)
    record.add_argument("--session-stale-quote-seconds", type=float, default=5.0)
    record.set_defaults(handler=cmd_record)

    session = subparsers.add_parser(
        "session", help="Summarize the latest real-time capture session"
    )
    session.add_argument("--dir", help="Specific real-time capture directory")
    session.add_argument("--since", help="UTC ISO timestamp to filter observed rows")
    session.add_argument("--gap-threshold-seconds", type=float, default=10.0)
    session.add_argument("--stale-quote-seconds", type=float, default=5.0)
    session.add_argument("--json", action="store_true", help="Print JSON instead of Markdown")
    session.add_argument("--no-write", action="store_true", help="Do not write report files")
    session.set_defaults(handler=cmd_session)

    features = subparsers.add_parser("features", help="Build model-ready feature tables")
    feature_subparsers = features.add_subparsers(dest="features_command", required=True)

    intraday = feature_subparsers.add_parser(
        "intraday", help="Build fixed-window intraday quote/trade/depth features"
    )
    intraday.add_argument("--quote-path", help="Specific bronze quotes CSV path")
    intraday.add_argument("--windows", default="1,5,30,60")
    intraday.add_argument("--horizons", default="5,15,30,60")
    intraday.add_argument("--interval-seconds", type=int, default=1)
    intraday.add_argument("--max-stale-quote-seconds", type=int, default=5)
    intraday.add_argument("--tick-size", type=float, default=0.25)
    intraday.set_defaults(handler=cmd_features_intraday)

    research = subparsers.add_parser(
        "research", help="Feature/label research diagnostics"
    )
    research_subparsers = research.add_subparsers(
        dest="research_command", required=True
    )

    ic = research_subparsers.add_parser(
        "ic", help="Information coefficient of features vs forward-return labels"
    )
    ic.add_argument("--path", help="Specific silver features CSV path")
    ic.add_argument("--min-samples", type=int, default=30)
    ic.add_argument("--top", type=int, default=15, help="Rows per label in the table")
    ic.add_argument("--json", action="store_true", help="Print JSON instead of Markdown")
    ic.add_argument("--no-write", action="store_true", help="Do not write report files")
    ic.set_defaults(handler=cmd_research_ic)

    backtest = research_subparsers.add_parser(
        "backtest", help="Run baseline signal backtests against silver features"
    )
    backtest.add_argument("--path", help="Specific silver features CSV path")
    backtest.add_argument("--horizon-seconds", type=int, default=30)
    backtest.add_argument("--signal-window-seconds", type=int, default=5)
    backtest.add_argument("--tick-size", type=float, default=0.25)
    backtest.add_argument("--cost-ticks", type=float, default=2.0)
    backtest.add_argument("--cooldown-seconds", type=int, default=0)
    backtest.add_argument("--imbalance-threshold", type=float, default=0.20)
    backtest.add_argument("--min-return-ticks", type=float, default=0.0)
    backtest.add_argument("--max-spread-ticks", type=float, default=4.0)
    backtest.add_argument("--json", action="store_true", help="Print JSON instead of Markdown")
    backtest.add_argument("--no-write", action="store_true", help="Do not write report files")
    backtest.set_defaults(handler=cmd_research_backtest)

    return parser


def _unit_choices() -> set[str]:
    return {
        "second",
        "minute",
        "hour",
        "day",
        "week",
        "month",
        "1",
        "2",
        "3",
        "4",
        "5",
        "6",
    }


def authenticated_client(settings: Settings) -> ProjectXClient:
    username, api_key = settings.require_projectx_credentials()
    client = ProjectXClient(base_url=settings.projectx_base_url)
    client.authenticate(username, api_key)
    return client


def cmd_run(args: Namespace) -> int:
    settings = Settings.from_env()
    client: ProjectXClient | None = None
    backfill_contract_id: str | None = None

    if not args.skip_auth:
        print_section("Project X Auth")
        client = authenticated_client(settings)
        print(f"Authenticated. Token prefix: {client.token[:12]}...")
        client.validate_session()
        print("Session validated.")

    if not args.skip_backfill:
        print_section("Historical Backfill")
        if client is None:
            client = authenticated_client(settings)
        backfill_result = run_historical_backfill(
            settings=settings,
            client=client,
            symbol=args.backfill_symbol,
            days=args.backfill_days,
            unit=parse_bar_unit(args.backfill_unit),
            unit_number=args.backfill_unit_number,
            live=settings.projectx_live,
        )
        backfill_contract_id = backfill_result.contract.id
        print_backfill_result(backfill_result)

    if not args.skip_normalize:
        print_section("Normalize")
        normalize_code = cmd_normalize_all(Namespace())
        if normalize_code:
            return normalize_code

    if not args.skip_features:
        print_section("Features")
        try:
            features_code = cmd_features_intraday(
                Namespace(
                    quote_path=None,
                    windows=args.feature_windows,
                    horizons=args.feature_horizons,
                    interval_seconds=args.feature_interval_seconds,
                    max_stale_quote_seconds=args.feature_max_stale_quote_seconds,
                    tick_size=args.tick_size,
                )
            )
            if features_code:
                return features_code
        except ValueError as exc:
            if "No bronze quote CSV found" not in str(exc):
                raise
            print(f"skipped features: {exc}")

    if not args.skip_qa:
        print_section("QA")
        qa_code = cmd_qa_all(
            Namespace(tick_size=args.tick_size, no_write=args.no_write_qa)
        )
        if qa_code:
            return qa_code

    if not args.skip_record:
        print_section("Live Recording")
        record_contract_id = args.record_contract_id or backfill_contract_id
        record_code = cmd_record(
            Namespace(
                contract_id=record_contract_id,
                symbol=args.record_symbol,
                events=args.record_events,
                duration_seconds=args.record_duration_seconds,
                no_live_features=args.record_no_live_features,
                no_finalize=args.record_no_finalize,
                no_session_summary=args.record_no_session_summary,
                feature_windows=args.record_feature_windows,
                feature_horizons=args.feature_horizons,
                feature_interval_seconds=args.record_feature_interval_seconds,
                max_stale_quote_seconds=args.feature_max_stale_quote_seconds,
                tick_size=args.tick_size,
                session_gap_threshold_seconds=args.session_gap_threshold_seconds,
                session_stale_quote_seconds=args.session_stale_quote_seconds,
            )
        )
        if record_code:
            return record_code

    print_section("Done")
    print("Axiom pipeline complete.")
    return 0


def cmd_auth(_: Namespace) -> int:
    settings = Settings.from_env()
    client = authenticated_client(settings)
    print(f"Authenticated. Token prefix: {client.token[:12]}...")
    client.validate_session()
    print("Session validated.")
    return 0


def cmd_backfill(args: Namespace) -> int:
    settings = Settings.from_env()
    client = authenticated_client(settings)
    result = run_historical_backfill(
        settings=settings,
        client=client,
        symbol=args.symbol,
        days=args.days,
        unit=parse_bar_unit(args.unit),
        unit_number=args.unit_number,
        live=args.live or settings.projectx_live,
    )
    print_backfill_result(result)
    return 0


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


def cmd_contract_search(args: Namespace) -> int:
    settings = Settings.from_env()
    client = authenticated_client(settings)
    contracts = client.search_contracts(args.text, live=args.live or settings.projectx_live)
    if args.active_only:
        contracts = [contract for contract in contracts if contract.active_contract]

    rows = [
        {
            "id": contract.id,
            "name": contract.name,
            "description": contract.description,
            "tickSize": contract.tick_size,
            "tickValue": contract.tick_value,
            "activeContract": contract.active_contract,
            "symbolId": contract.symbol_id,
        }
        for contract in contracts
    ]

    reference_path = (
        settings.data_dir
        / "reference"
        / "projectx"
        / "contracts"
        / f"{args.text.upper()}_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    )
    write_json(reference_path, rows)

    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        print_contract_table(rows)
    print(f"Saved contract snapshot: {reference_path}")
    return 0


def cmd_bars_download(args: Namespace) -> int:
    settings = Settings.from_env()
    client = authenticated_client(settings)
    unit = parse_bar_unit(args.unit)
    start = parse_utc_datetime(args.start)
    end = parse_utc_datetime(args.end)
    live = args.live or settings.projectx_live

    total = 0
    for (window_start, window_end), bars in client.retrieve_bars_chunked(
        contract_id=args.contract_id,
        start=start,
        end=end,
        unit=unit,
        unit_number=args.unit_number,
        live=live,
        limit=args.limit,
        include_partial_bar=args.include_partial_bar,
    ):
        raw_path = history_raw_path(
            settings.data_dir,
            args.contract_id,
            unit,
            args.unit_number,
            window_start,
            window_end,
        )
        csv_path = bars_csv_path(
            settings.data_dir,
            args.contract_id,
            unit,
            args.unit_number,
            window_start,
            window_end,
        )
        write_json(
            raw_path,
            {
                "contractId": args.contract_id,
                "live": live,
                "startTime": window_start.isoformat().replace("+00:00", "Z"),
                "endTime": window_end.isoformat().replace("+00:00", "Z"),
                "unit": int(unit),
                "unitNumber": args.unit_number,
                "bars": bars,
            },
        )
        write_bars_csv(csv_path, bars)
        total += len(bars)
        print(
            f"{compact_utc(window_start)} to {compact_utc(window_end)}: "
            f"{len(bars)} bars"
        )
        print(f"  raw: {raw_path}")
        print(f"  csv: {csv_path}")

    print(f"Downloaded {total} bars total.")
    return 0


def cmd_bootstrap(args: Namespace) -> int:
    settings = Settings.from_env()
    client = authenticated_client(settings)
    live = args.live or settings.projectx_live
    contracts = client.search_contracts(args.symbol, live=live)
    active = [contract for contract in contracts if contract.active_contract]
    if not active:
        raise ProjectXError(f"No active contract found for {args.symbol}")

    # Prefer the exact micro symbol if Project X returns multiple NQ-like contracts.
    contract = next(
        (
            item
            for item in active
            if item.symbol_id.upper().endswith(f".{args.symbol.upper()}")
            or item.name.upper().startswith(args.symbol.upper())
        ),
        active[0],
    )
    print(f"Selected {contract.name} ({contract.id}) - {contract.description}")

    end = datetime.now(UTC).replace(microsecond=0)
    start = end - timedelta(days=args.days)
    download_args = Namespace(
        contract_id=contract.id,
        start=start.isoformat().replace("+00:00", "Z"),
        end=end.isoformat().replace("+00:00", "Z"),
        unit=args.unit,
        unit_number=args.unit_number,
        limit=20_000,
        live=live,
        include_partial_bar=False,
    )
    return cmd_bars_download(download_args)


def cmd_qa_bars(args: Namespace) -> int:
    settings = Settings.from_env()
    if args.path:
        report = analyze_bars_csv(Path(args.path), tick_size=args.tick_size)
    else:
        partition = find_latest_bars_partition(
            settings.data_dir / "bronze" / "projectx" / "bars"
        )
        if partition is None:
            raise ValueError(
                "No bars CSV found. Run `axiom backfill`/`normalize` or pass --path."
            )
        report = analyze_bars_partition(partition, tick_size=args.tick_size)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(report.to_markdown())

    if not args.no_write:
        stem = f"bars_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
        md_path, json_path = write_report_pair(
            settings.data_dir / "reports" / "qa",
            stem,
            report.to_markdown(),
            report.to_dict(),
        )
        print(f"Saved reports: {md_path}, {json_path}")
    return 0


def cmd_qa_realtime(args: Namespace) -> int:
    settings = Settings.from_env()
    directory = Path(args.dir) if args.dir else find_latest_realtime_dir(settings.data_dir)
    if directory is None:
        raise ValueError(
            "No real-time capture directory found. Run the recorder or pass --dir."
        )

    report = analyze_realtime_dir(directory)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(report.to_markdown())

    if not args.no_write:
        stem = f"realtime_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
        md_path, json_path = write_report_pair(
            settings.data_dir / "reports" / "qa",
            stem,
            report.to_markdown(),
            report.to_dict(),
        )
        print(f"Saved reports: {md_path}, {json_path}")
    return 0


def cmd_qa_all(args: Namespace) -> int:
    bar_args = Namespace(path=None, tick_size=args.tick_size, json=False, no_write=args.no_write)
    realtime_args = Namespace(dir=None, json=False, no_write=args.no_write)
    codes: list[int] = []
    try:
        codes.append(cmd_qa_bars(bar_args))
    except ValueError as exc:
        if "No bars CSV found" not in str(exc):
            raise
        print(f"skipped bars QA: {exc}")
    print()
    try:
        codes.append(cmd_qa_realtime(realtime_args))
    except ValueError as exc:
        if "No real-time capture directory found" not in str(exc):
            raise
        print(f"skipped real-time QA: {exc}")
    return max(codes) if codes else 0


def cmd_normalize_bars(args: Namespace) -> int:
    settings = Settings.from_env()
    path = Path(args.path) if args.path else find_latest_file(
        settings.data_dir / "raw" / "projectx" / "history", "*.json"
    )
    if path is None:
        raise ValueError("No raw history JSON found. Run `axiom bootstrap` or pass --path.")

    output = normalize_bars_history_json(path, settings.data_dir)
    append_manifest(settings.data_dir, [output])
    print_normalized_files([output])
    return 0


def cmd_normalize_realtime(args: Namespace) -> int:
    settings = Settings.from_env()
    directory = Path(args.dir) if args.dir else find_latest_realtime_dir(settings.data_dir)
    if directory is None:
        raise ValueError(
            "No real-time capture directory found. Run the recorder or pass --dir."
        )

    outputs = normalize_realtime_dir(directory, settings.data_dir)
    append_manifest(settings.data_dir, outputs)
    print_normalized_files(outputs)
    return 0


def cmd_normalize_all(_: Namespace) -> int:
    settings = Settings.from_env()
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
        return 0
    realtime_outputs = normalize_realtime_dir(realtime_dir, settings.data_dir)
    append_manifest(settings.data_dir, realtime_outputs)
    print_normalized_files(realtime_outputs)
    return 0


def cmd_record(args: Namespace) -> int:
    settings = Settings.from_env()
    contract_id = args.contract_id
    if not contract_id:
        client = authenticated_client(settings)
        contract = resolve_active_contract(
            client,
            symbol=args.symbol,
            live=settings.projectx_live,
        )
        contract_id = contract.id
        print(f"Selected {contract.name} ({contract.id}) - {contract.description}")

    print(
        "Recording real-time Project X data. Press Ctrl+C to stop."
        if args.duration_seconds is None
        else f"Recording real-time Project X data for {args.duration_seconds} seconds."
    )
    sys.stdout.flush()
    recording_started_at = datetime.now(UTC)
    code = run_realtime_recorder(
        RecordingConfig(
            contract_id=contract_id,
            events=args.events,
            data_dir=settings.data_dir,
            duration_seconds=args.duration_seconds,
            live_features=not args.no_live_features,
            feature_windows=args.feature_windows,
            feature_interval_seconds=args.feature_interval_seconds,
        )
    )
    if not args.no_finalize:
        print_section("Finalize Recorded Data")
        try:
            finalize_realtime_capture(
                data_dir=settings.data_dir,
                windows=args.feature_windows,
                horizons=args.feature_horizons,
                interval_seconds=args.feature_interval_seconds,
                max_stale_quote_seconds=args.max_stale_quote_seconds,
                tick_size=args.tick_size,
            )
        except ValueError as exc:
            print(f"skipped recorded-data finalization: {exc}")

    if not args.no_session_summary:
        print_section("Session Health")
        try:
            write_session_report(
                settings=settings,
                directory=None,
                gap_threshold_seconds=args.session_gap_threshold_seconds,
                stale_quote_seconds=args.session_stale_quote_seconds,
                observed_since=recording_started_at,
                as_json=False,
                write=True,
            )
        except ValueError as exc:
            print(f"skipped session health: {exc}")
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
    print(f"intraday features: {result.rows:,} rows")
    print(f"  quotes: {result.quote_path}")
    if result.trade_path:
        print(f"  trades: {result.trade_path}")
    if result.depth_path:
        print(f"  depth: {result.depth_path}")
    print(f"  output: {result.path}")


def cmd_features_intraday(args: Namespace) -> int:
    settings = Settings.from_env()
    result = build_intraday_features(
        IntradayFeatureConfig(
            data_dir=settings.data_dir,
            quote_path=Path(args.quote_path) if args.quote_path else None,
            windows_seconds=parse_int_list(args.windows),
            horizons_seconds=parse_int_list(args.horizons),
            interval_seconds=args.interval_seconds,
            max_stale_quote_seconds=args.max_stale_quote_seconds,
            tick_size=args.tick_size,
        )
    )
    print(f"intraday features: {result.rows:,} rows")
    print(f"  quotes: {result.quote_path}")
    if result.trade_path:
        print(f"  trades: {result.trade_path}")
    if result.depth_path:
        print(f"  depth: {result.depth_path}")
    print(f"  output: {result.path}")
    return 0


def cmd_research_ic(args: Namespace) -> int:
    settings = Settings.from_env()
    path = Path(args.path) if args.path else find_latest_file(
        settings.data_dir / "silver" / "projectx" / "features", "*.csv"
    )
    if path is None:
        raise ValueError(
            "No silver features CSV found. Run `axiom features intraday` or pass --path."
        )

    report = analyze_feature_ic(path, min_samples=args.min_samples)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(report.to_markdown(top=args.top))

    if not args.no_write:
        stem = f"ic_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
        md_path, json_path = write_report_pair(
            settings.data_dir / "reports" / "research",
            stem,
            report.to_markdown(top=args.top),
            report.to_dict(),
        )
        print(f"Saved reports: {md_path}, {json_path}")
    return 0


def cmd_research_backtest(args: Namespace) -> int:
    settings = Settings.from_env()
    path = Path(args.path) if args.path else find_latest_file(
        settings.data_dir / "silver" / "projectx" / "features", "*.csv"
    )
    if path is None:
        raise ValueError(
            "No silver features CSV found. Run `axiom features intraday` or pass --path."
        )

    report = run_backtest(
        BacktestConfig(
            path=path,
            horizon_seconds=args.horizon_seconds,
            signal_window_seconds=args.signal_window_seconds,
            tick_size=args.tick_size,
            cost_ticks=args.cost_ticks,
            cooldown_seconds=args.cooldown_seconds,
            imbalance_threshold=args.imbalance_threshold,
            min_return_ticks=args.min_return_ticks,
            max_spread_ticks=args.max_spread_ticks,
        )
    )
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(report.to_markdown())

    if not args.no_write:
        stem = f"backtest_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
        md_path, json_path = write_report_pair(
            settings.data_dir / "reports" / "research",
            stem,
            report.to_markdown(),
            report.to_dict(),
        )
        print(f"Saved reports: {md_path}, {json_path}")
    return 0


def cmd_session(args: Namespace) -> int:
    settings = Settings.from_env()
    return write_session_report(
        settings=settings,
        directory=Path(args.dir) if args.dir else None,
        gap_threshold_seconds=args.gap_threshold_seconds,
        stale_quote_seconds=args.stale_quote_seconds,
        observed_since=parse_utc_datetime(args.since) if args.since else None,
        as_json=args.json,
        write=not args.no_write,
    )


def write_session_report(
    *,
    settings: Settings,
    directory: Path | None,
    gap_threshold_seconds: float,
    stale_quote_seconds: float,
    observed_since: datetime | None,
    as_json: bool,
    write: bool,
) -> int:
    report = analyze_session(
        settings.data_dir,
        directory=directory,
        gap_threshold_seconds=gap_threshold_seconds,
        stale_quote_seconds=stale_quote_seconds,
        observed_since=observed_since,
    )
    if as_json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(report.to_markdown())

    if write:
        stem = f"session_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
        md_path, json_path = write_report_pair(
            settings.data_dir / "reports" / "session",
            stem,
            report.to_markdown(),
            report.to_dict(),
        )
        print(f"Saved reports: {md_path}, {json_path}")
    return 0


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
        if not item:
            continue
        parsed.append(int(item))
    if not parsed:
        raise ValueError("Expected at least one integer value.")
    return parsed


def print_section(title: str) -> None:
    print()
    print(f"== {title} ==", flush=True)


def print_contract_table(rows: list[dict[str, object]]) -> None:
    if not rows:
        print("No contracts found.")
        return
    headers = ["name", "id", "tickSize", "tickValue", "activeContract", "symbolId"]
    widths = {
        header: max(len(header), *(len(str(row[header])) for row in rows))
        for header in headers
    }
    print("  ".join(header.ljust(widths[header]) for header in headers))
    print("  ".join("-" * widths[header] for header in headers))
    for row in rows:
        print("  ".join(str(row[header]).ljust(widths[header]) for header in headers))


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        argv = ["run"]

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (ProjectXError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
