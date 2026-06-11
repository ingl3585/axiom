from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import gzip
import tempfile
import unittest

import _bootstrap  # noqa: F401
from retention import compress_old_realtime

NOW = datetime(2026, 6, 11, 12, 0, tzinfo=UTC)


def make_capture(root: Path, date: str, content: str = '{"x":1}\n') -> Path:
    directory = root / "raw" / "projectx" / "realtime" / f"date={date}" / "contract=C"
    directory.mkdir(parents=True)
    path = directory / "quotes.jsonl"
    path.write_text(content, encoding="utf-8")
    return path


class RetentionTests(unittest.TestCase):
    def test_compresses_old_keeps_recent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            old = make_capture(data_dir, "2026-05-01", '{"old":true}\n')
            recent = make_capture(data_dir, "2026-06-10")

            compressed = compress_old_realtime(data_dir, keep_days=14, now=NOW)

            self.assertEqual(len(compressed), 1)
            self.assertFalse(old.exists())  # original replaced...
            gz_path = old.with_suffix(".jsonl.gz")
            self.assertTrue(gz_path.exists())
            # ...with identical content preserved (compress, never delete).
            with gzip.open(gz_path, "rt", encoding="utf-8") as handle:
                self.assertEqual(handle.read(), '{"old":true}\n')
            self.assertTrue(recent.exists())

    def test_second_run_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            make_capture(data_dir, "2026-05-01")
            first = compress_old_realtime(data_dir, keep_days=14, now=NOW)
            second = compress_old_realtime(data_dir, keep_days=14, now=NOW)
            self.assertEqual(len(first), 1)
            self.assertEqual(second, [])

    def test_zero_keep_days_disables(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            old = make_capture(data_dir, "2026-05-01")
            self.assertEqual(compress_old_realtime(data_dir, 0, NOW), [])
            self.assertTrue(old.exists())


if __name__ == "__main__":
    unittest.main()
