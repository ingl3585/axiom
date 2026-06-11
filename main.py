from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def run() -> int:
    from pipeline import run_pipeline
    from projectx import ProjectXError

    try:
        args = sys.argv[1:]
        if args == ["signals"]:
            from walkforward import run_signals_command

            return run_signals_command()
        if args:
            print("Usage: python .\\main.py [signals]", file=sys.stderr)
            return 2
        return run_pipeline()
    except (ProjectXError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
