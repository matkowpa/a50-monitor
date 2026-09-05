import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import assess  # noqa: E402
from common import Evidence  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CFG = {
    "focus": "Sobienie-Jeziory",
    "keywords": ["a50", "obwodnica autostradowa", "sobienie-jeziory"],
    "max_evidence": 10,
}


def load_raw():
    return json.loads((FIXTURES / "raw_sample.json").read_text(encoding="utf-8"))


class TestExtractEngineItems(unittest.TestCase):
    def test_filters_offtopic_and_dedupes(self):
        items = assess.extract_engine_items(load_raw(), CFG)
        urls = [i.url for i in items]
        self.assertEqual(len(items), 2)  # A1-wypadek odfiltrowany; duplikat URL odrzucony
        self.assertNotIn("https://www.reddit.com/r/Polska/comments/bbb/wypadek_a1", urls)
        self.assertEqual(len(set(urls)), len(urls))

    def test_published_and_source_extracted(self):
        items = assess.extract_engine_items(load_raw(), CFG)
        by_url = {i.url: i for i in items}
        reddit = by_url["https://www.reddit.com/r/Polska/comments/aaa/a50_przebieg/"]
        self.assertEqual(reddit.published, "2026-09-01")
        self.assertEqual(reddit.source, "reddit")
        self.assertNotEqual(reddit.snippet.lower(), "none")

    def test_sorted_desc_and_snippet_cleaned(self):
        items = assess.extract_engine_items(load_raw(), CFG)
        dates = [i.published for i in items]
        self.assertEqual(dates, sorted(dates, reverse=True))


class TestExtractFeedItems(unittest.TestCase):
    def test_filters_and_dedupes(self):
        feeds = {"items": [
            {"title": "A50: wariant przez Sobienie-Jeziory?", "url": "https://a.pl/x",
             "source": "Google News", "published": "2026-09-03T10:00Z", "snippet": "s"},
            {"title": "Sport: remis", "url": "https://a.pl/y",
             "source": "Google News", "published": "2026-09-03T11:00Z", "snippet": "s"},
            {"title": "A50 duplikat", "url": "https://a.pl/x?fbclid=1",
             "source": "Google News", "published": "2026-09-02T10:00Z", "snippet": "s"},
        ]}
        items = assess.extract_feed_items(feeds, CFG)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].published, "2026-09-03")


class TestMergeEvidence(unittest.TestCase):
    def test_merges_dedupes_and_caps(self):
        e1 = [Evidence(title="1", url="https://x.com/1", source="a", published="2026-09-01")]
        e2 = [Evidence(title="2", url="https://x.com/2", source="b", published="2026-09-02"),
              Evidence(title="1dup", url="https://x.com/1?y", source="b", published="2026-09-03")]
        merged = assess.merge_evidence(e1, e2, cap=1)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].url, "https://x.com/2")  # najnowszy pierwszy


class TestParseAssessment(unittest.TestCase):
    def test_valid(self):
        prev = {"date": "2026-09-04", "score": 30}
        out = assess.parse_assessment(
            {"score": 35, "confidence": "średnia", "summary": "s", "rationale": "r",
             "key_findings": [{"claim": "c", "evidence_urls": ["https://a.pl/1"]}]}, prev)
        self.assertEqual(out["score"], 35)
        self.assertEqual(out["trend_vs_prev"], 5)
        self.assertEqual(out["key_findings"][0]["evidence_urls"], ["https://a.pl/1"])

    def test_clamps_and_defaults(self):
        out = assess.parse_assessment({"score": 150, "confidence": "bogu"}, None)
        self.assertEqual(out["score"], 100)
        self.assertEqual(out["confidence"], "niska")
        self.assertEqual(out["trend_vs_prev"], 0)
        self.assertTrue(out["summary"])

    def test_drops_findings_without_claim(self):
        out = assess.parse_assessment({"score": 1, "key_findings": [{"claim": ""}, "x"]}, None)
        self.assertEqual(out["key_findings"], [])


class TestBuildPrompt(unittest.TestCase):
    def test_contains_focus_prev_and_evidence(self):
        ev = [Evidence(title="Tytuł A50", url="https://a.pl/1", source="web",
                       published="2026-09-01", snippet="snip")]
        prompt = assess.build_prompt(ev, {"date": "2026-09-04", "score": 30,
                                          "confidence": "niska", "rationale": "r"}, CFG)
        self.assertIn("Sobienie-Jeziory", prompt)
        self.assertIn("score=30", prompt)
        self.assertIn("https://a.pl/1", prompt)
        self.assertIn("RUBRYKA", prompt)

    def test_first_run_marker(self):
        prompt = assess.build_prompt([Evidence(title="t", url="u", source="s")], None, CFG)
        self.assertIn("pierwsza ocena", prompt)


class TestPrevAndNoEvidence(unittest.TestCase):
    def test_prev_entry_before(self):
        entries = [{"date": "2026-09-01"}, {"date": "2026-09-03"}]
        self.assertEqual(assess.prev_entry_before(entries, "2026-09-05")["date"], "2026-09-03")
        self.assertIsNone(assess.prev_entry_before(entries, "2026-09-01"))

    def test_no_evidence_keeps_prev_score(self):
        entry = assess.no_evidence_entry("2026-09-05", {"date": "2026-09-04", "score": 42},
                                         "no-evidence")
        self.assertEqual(entry["score"], 42)
        self.assertEqual(entry["confidence"], "niska")
        self.assertEqual(entry["engine_status"], "no-evidence")

    def test_no_evidence_first_run_neutral(self):
        entry = assess.no_evidence_entry("2026-09-05", None, "no-data")
        self.assertEqual(entry["score"], 50)
        self.assertEqual(entry["engine_status"], "no-data")


if __name__ == "__main__":
    unittest.main()
