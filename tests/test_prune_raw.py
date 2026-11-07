import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import prune_raw  # noqa: E402


class TestOldDays(unittest.TestCase):
    def test_selects_only_dated_dirs_older_than_cutoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp)
            for name in ("2026-01-01", "2026-11-01", "not-a-date", "2026-11-07"):
                (raw / name).mkdir()
            got = [p.name for p in prune_raw.old_days(raw, 120, today=date(2026, 11, 7))]
            self.assertEqual(got, ["2026-01-01"])

    def test_missing_dir_is_safe(self):
        self.assertEqual(prune_raw.old_days(Path("/nonexistent/x"), 30), [])


if __name__ == "__main__":
    unittest.main()
