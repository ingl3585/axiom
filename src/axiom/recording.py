from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess
import sys


@dataclass(frozen=True)
class RecordingConfig:
    contract_id: str
    events: str = "quotes,trades,depth"
    data_dir: Path = Path("data")
    duration_seconds: int | None = None
    live_features: bool = True
    feature_windows: str = "1,5,30,60"
    feature_interval_seconds: int = 1


def find_node_executable() -> str:
    bundled = (
        Path.home()
        / ".cache"
        / "codex-runtimes"
        / "codex-primary-runtime"
        / "dependencies"
        / "node"
        / "bin"
        / "node.exe"
    )
    if bundled.exists():
        return str(bundled)

    node = shutil.which("node")
    if node:
        return node

    raise ValueError(
        "Node.js was not found. Install Node.js or run from the Codex desktop runtime."
    )


def recorder_script_path() -> Path:
    return Path(__file__).resolve().parents[2] / "scripts" / "projectx_realtime.mjs"


def run_realtime_recorder(config: RecordingConfig) -> int:
    script = recorder_script_path()
    if not script.exists():
        raise ValueError(f"Recorder script not found: {script}")

    command = [
        find_node_executable(),
        str(script),
        "--contract-id",
        config.contract_id,
        "--events",
        config.events,
        "--data-dir",
        str(config.data_dir),
    ]
    if config.duration_seconds is not None:
        command.extend(["--duration-seconds", str(config.duration_seconds)])
    if config.live_features:
        command.append("--live-features")
    else:
        command.append("--no-live-features")
    command.extend(["--feature-windows", config.feature_windows])
    command.extend(["--feature-interval-seconds", str(config.feature_interval_seconds)])

    try:
        return subprocess.run(command, check=False).returncode
    except KeyboardInterrupt:
        print("\nRecording stopped by user.", file=sys.stderr)
        return 130
