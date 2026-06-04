from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def run() -> int:
    from axiom.pipeline import run_pipeline, run_research
    from axiom.projectx import ProjectXError

    try:
        args = sys.argv[1:]
        if not args:
            return run_pipeline()
        if args == ["research"]:
            return run_research()
        print("Use `python .\\main.py` or `python .\\main.py research`.", file=sys.stderr)
        return 2
    except (ProjectXError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
