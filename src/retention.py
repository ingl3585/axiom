from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import gzip
import shutil


def compress_old_realtime(
    data_dir: Path,
    keep_days: int,
    now: datetime | None = None,
) -> list[Path]:
    """Gzip raw realtime JSONL captures older than `keep_days`.

    Compress-in-place, never delete: raw files are the audit trail. Recent
    days stay uncompressed because the pipeline still normalizes from them.
    Returns the newly created .gz paths. keep_days <= 0 disables retention.
    """
    root = data_dir / "raw" / "projectx" / "realtime"
    if keep_days <= 0 or not root.exists():
        return []
    cutoff = ((now or datetime.now(UTC)) - timedelta(days=keep_days)).date()

    compressed: list[Path] = []
    for date_dir in sorted(root.glob("date=*")):
        try:
            date = datetime.strptime(date_dir.name.split("=", 1)[1], "%Y-%m-%d").date()
        except ValueError:
            continue
        if date >= cutoff:
            continue
        for path in sorted(date_dir.rglob("*.jsonl")):
            target = path.with_suffix(".jsonl.gz")
            with path.open("rb") as source, gzip.open(target, "wb") as destination:
                shutil.copyfileobj(source, destination)
            path.unlink()
            compressed.append(target)
    return compressed
