from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def run() -> int:
    from axiom.cli import main

    return main()


if __name__ == "__main__":
    raise SystemExit(run())
