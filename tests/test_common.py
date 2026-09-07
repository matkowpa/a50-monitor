import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from common import (Evidence, atomic_write_json, is_fresh, load_scores,  # noqa: E402
                    upsert_entry)


class TestAtomicWrite(unittest.TestCase):
    def test_writes_and_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "sub" / "x.json"
            atomic_write_json(p, {"a": 1})
            self.assertEqual(json.loads(p.read_text(encoding="utf-8")), {"a": 1})
            atomic_write_json(p, {"a": 2})
            self.assertEqual(json.loads(p.read_text(encoding="utf-8"))["a"], 2)
            leftovers = list(p.parent.glob("*.tmp"))
            self.assertEqual(leftovers, [])


class TestUpsert(unittest.TestCase):
    def test_upsert_replaces_same_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "scores.json"
            upsert_entry({"date": "2026-09-01", "score": 10}, p)
            upsert_entry({"date": "2026-09-02", "score": 20}, p)
            data = upsert_entry({"date": "2026-09-02", "score": 25}, p)
            entries = data["entries"]
            self.assertEqual(len(entries), 2)
            by_date = {e["date"]: e["score"] for e in entries}
            self.assertEqual(by_date["2026-09-02"], 25)
            self.assertEqual([e["date"] for e in entries],
                             ["2026-09-01", "2026-09-02"])

    def test_load_scores_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = load_scores(Path(tmp) / "nie-ma.json")
            self.assertEqual(data, {"entries": []})


class TestEvidence(unittest.TestCase):
    def test_to_dict_fields(self):
        ev = Evidence(title="t", url="u", source="s", published="2026-09-01")
        d = ev.to_dict()
        self.assertEqual(d["stance"], "neutral")
        self.assertIn("snippet", d)


class TestIsFresh(unittest.TestCase):
    def test_recent_publication_kept(self):
        self.assertTrue(is_fresh("2026-09-01", "2026-09-06", 30))

    def test_older_than_window_rejected(self):
        # przypadek z runu 2026-09-06: publikacja RSS z 2026-04-24
        self.assertFalse(is_fresh("2026-04-24", "2026-09-06", 30))

    def test_boundary_exactly_max_age_kept(self):
        # dokładnie 30 dni = granica okna → zostaje (odrzucamy tylko starsze)
        self.assertTrue(is_fresh("2026-08-07", "2026-09-06", 30))

    def test_missing_or_garbage_date_kept(self):
        self.assertTrue(is_fresh("", "2026-09-06", 30))
        self.assertTrue(is_fresh("b.d.", "2026-09-06", 30))

    def test_future_date_kept(self):
        self.assertTrue(is_fresh("2026-09-10", "2026-09-06", 30))

    def test_bad_report_day_kept(self):
        self.assertTrue(is_fresh("2026-01-01", "", 30))


if __name__ == "__main__":
    unittest.main()
