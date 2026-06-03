from __future__ import annotations

from argparse import ArgumentParser, Namespace
from datetime import UTC, datetime, timedelta
from pathlib import Path
import json
import sys

from .config import Settings
from .normalize import (
    append_manifest,
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
    analyze_realtime_dir,
    find_latest_file,
    find_latest_realtime_dir,
    write_report_pair,
)
from .recording import RecordingConfig, run_realtime_recorder
from .storage import bars_csv_path, history_raw_path, write_bars_csv, write_json


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(
        prog="axiom",
        description=(
            "Axiom data tooling. Run with no command to execute the default "
            "auth -> normalize -> QA pipeline."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser(
        "run", help="Run the default auth, normalize, and QA pipeline"
    )
    run_parser.add_argument("--skip-auth", action="store_true")
    run_parser.add_argument("--skip-normalize", action="store_true")
    run_parser.add_argument("--skip-qa", action="store_true")
    run_parser.add_argument("--skip-record", action="store_true")
    run_parser.add_argument("--tick-size", type=float, default=0.25)
    run_parser.add_argument("--no-write-qa", action="store_true")
    run_parser.add_argument("--record-contract-id")
    run_parser.add_argument("--record-symbol", default="MNQ")
    run_parser.add_argument("--record-events", default="quotes,trades,depth")
    run_parser.add_argument("--record-duration-seconds", type=int)
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
    record.set_defaults(handler=cmd_record)

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
    if not args.skip_auth:
        print_section("Project X Auth")
        auth_code = cmd_auth(Namespace())
        if auth_code:
            return auth_code

    if not args.skip_normalize:
        print_section("Normalize")
        normalize_code = cmd_normalize_all(Namespace())
        if normalize_code:
            return normalize_code

    if not args.skip_qa:
        print_section("QA")
        qa_code = cmd_qa_all(
            Namespace(tick_size=args.tick_size, no_write=args.no_write_qa)
        )
        if qa_code:
            return qa_code

    if not args.skip_record:
        print_section("Live Recording")
        record_code = cmd_record(
            Namespace(
                contract_id=args.record_contract_id,
                symbol=args.record_symbol,
                events=args.record_events,
                duration_seconds=args.record_duration_seconds,
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
    path = Path(args.path) if args.path else find_latest_file(
        settings.data_dir / "bronze" / "projectx" / "bars", "*.csv"
    )
    if path is None:
        raise ValueError("No bars CSV found. Run `axiom bootstrap` or pass --path.")

    report = analyze_bars_csv(path, tick_size=args.tick_size)
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
    bars_code = cmd_qa_bars(bar_args)
    print()
    realtime_code = cmd_qa_realtime(realtime_args)
    return max(bars_code, realtime_code)


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
    bars_code = cmd_normalize_bars(Namespace(path=None))
    print()
    realtime_code = cmd_normalize_realtime(Namespace(dir=None))
    return max(bars_code, realtime_code)


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
    return run_realtime_recorder(
        RecordingConfig(
            contract_id=contract_id,
            events=args.events,
            data_dir=settings.data_dir,
            duration_seconds=args.duration_seconds,
        )
    )


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
