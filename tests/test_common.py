import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from common import Evidence, atomic_write_json, load_scores, upsert_entry


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


if __name__ == "__main__":
    unittest.main()
