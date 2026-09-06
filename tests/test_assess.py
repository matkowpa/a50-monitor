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
    "baseline_scores": {"north": 45, "south": 28},
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


def _scenario(score=35, confidence="średnia", findings=None):
    return {"score": score, "confidence": confidence, "summary": "s",
            "rationale": "r",
            "key_findings": findings if findings is not None else []}


def _llm_data(n_score=35, s_score=20, confidence="średnia"):
    return {"scores": {
        "north": _scenario(n_score, confidence,
                           [{"claim": "c", "evidence_urls": ["https://a.pl/1"]}]),
        "south": _scenario(s_score, confidence),
    }}


class TestParseAssessment(unittest.TestCase):
    def test_valid(self):
        prev = {"date": "2026-09-04",
                "scores": {"north": {"score": 30}, "south": {"score": 25}}}
        out = assess.parse_assessment(_llm_data(), prev)
        self.assertEqual(out["scores"]["north"]["score"], 35)
        self.assertEqual(out["scores"]["north"]["trend_vs_prev"], 5)
        self.assertEqual(out["scores"]["south"]["trend_vs_prev"], -5)
        self.assertEqual(out["scores"]["north"]["key_findings"][0]["evidence_urls"],
                         ["https://a.pl/1"])

    def test_clamps_and_defaults(self):
        out = assess.parse_assessment(_llm_data(n_score=150, s_score=150,
                                                confidence="bogu"), None)
        self.assertEqual(out["scores"]["north"]["score"], 100)
        self.assertEqual(out["scores"]["south"]["score"], 100)
        self.assertEqual(out["scores"]["north"]["confidence"], "niska")
        self.assertEqual(out["scores"]["south"]["trend_vs_prev"], 0)
        self.assertTrue(out["scores"]["south"]["summary"])

    def test_drops_findings_without_claim(self):
        data = _llm_data()
        data["scores"]["north"]["key_findings"] = [{"claim": ""}, "x"]
        out = assess.parse_assessment(data, None)
        self.assertEqual(out["scores"]["north"]["key_findings"], [])

    def test_missing_scenario_raises(self):
        with self.assertRaises(ValueError):
            assess.parse_assessment({"score": 10}, None)


class TestExtractJson(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(assess.extract_json('{"a": 1}'), {"a": 1})

    def test_fenced(self):
        self.assertEqual(assess.extract_json('```json\n{"a": 1}\n```'), {"a": 1})

    def test_repairs_unescaped_quotes(self):
        broken = ('{"scores": {"north": {"score": 10, "summary": '
                  '"Ma "cytat" w środku"}, "south": {"score": 5, "summary": "ok"}}}')
        out = assess.extract_json(broken)
        self.assertEqual(out["scores"]["south"]["summary"], "ok")
        self.assertIn('"cytat"', out["scores"]["north"]["summary"])

    def test_repairs_raw_newlines_and_trailing_comma(self):
        broken = '{"a": "linia1\nlinia2", "b": [1, 2,],}'
        out = assess.extract_json(broken)
        self.assertEqual(out["a"], "linia1\nlinia2")
        self.assertEqual(out["b"], [1, 2])

    def test_repairs_polish_quote_before_comma(self):
        broken = ('{"scores": {"north": {"score": 10, "summary": '
                  '"GDDKiA: wariant "południowy", który omija gminę"}, '
                  '"south": {"score": 5, "summary": "ok"}}}')
        out = assess.extract_json(broken)
        self.assertEqual(out["scores"]["south"]["summary"], "ok")
        self.assertIn('"południowy"', out["scores"]["north"]["summary"])

    def test_repairs_quotes_inside_nested_value(self):
        broken = '{"a": "stwierdził: "nie"", "b": 2}'
        out = assess.extract_json(broken)
        self.assertEqual(out["b"], 2)
        self.assertIn('"nie"', out["a"])

    def test_valid_string_arrays_still_parse(self):
        ok = '{"urls": ["https://a.pl/1", "https://a.pl/2"], "n": 3}'
        out = assess.extract_json(ok)
        self.assertEqual(out["urls"], ["https://a.pl/1", "https://a.pl/2"])

    def test_error_includes_context_snippet(self):
        with self.assertRaises(json.JSONDecodeError) as ctx:
            assess.extract_json('{"a": }')
        self.assertIn("kontekst", str(ctx.exception))

    def test_fixes_mangled_closing_run_from_ci(self):
        # przypadek z CI: model skończył ogonem '"]}}}'  zamiast '"]}]}}}'
        broken = ('{"scores": {"north": {"score": 40, "summary": "n"}, '
                  '"south": {"score": 30, "confidence": "niska", '
                  '"summary": "s", "rationale": "r", "key_findings": '
                  '[{"claim": "c", "evidence_urls": ["https://x/1/"]}]}}')
        out = assess.extract_json(broken)
        self.assertEqual(out["scores"]["south"]["key_findings"][0]["claim"], "c")
        parsed = assess.parse_assessment(out, None)
        self.assertEqual(parsed["scores"]["south"]["score"], 30)

    def test_closes_truncated_tail(self):
        good = ('{"scores": {"north": {"score": 10, "summary": "s"}, '
                '"south": {"score": 5, "summary": "t"}}}')
        out = assess.extract_json(good[:-2])
        self.assertEqual(out["scores"]["south"]["score"], 5)

    def test_drops_extra_closer(self):
        self.assertEqual(assess.extract_json('{"a": [1]]}'), {"a": [1]})

    def test_closes_unterminated_string(self):
        self.assertEqual(assess.extract_json('{"a": "tek'), {"a": "tek"})

    def test_missing_score_raises(self):
        data = {"scores": {"north": {"summary": "s"}, "south": {"score": 5}}}
        with self.assertRaises(ValueError):
            assess.parse_assessment(data, None)

    def test_unrepairable_raises(self):
        with self.assertRaises(json.JSONDecodeError):
            assess.extract_json("totalnie nie json")


class TestBuildPrompt(unittest.TestCase):
    def test_contains_focus_prev_and_evidence(self):
        ev = [Evidence(title="Tytuł A50", url="https://a.pl/1", source="web",
                       published="2026-09-01", snippet="snip")]
        prev = {"date": "2026-09-04",
                "scores": {"north": {"score": 30, "confidence": "niska",
                                     "rationale": "r"},
                           "south": {"score": 25, "confidence": "niska",
                                     "rationale": "r"}}}
        prompt = assess.build_prompt(ev, prev, CFG, "### analiza-1\nTreść analizy.")
        self.assertIn("Sobienie-Jeziory", prompt)
        self.assertIn("score północ=30", prompt)
        self.assertIn("południe=25", prompt)
        self.assertIn("https://a.pl/1", prompt)
        self.assertIn("RUBRYKA", prompt)
        self.assertIn("SCENARIUSZ PÓŁNOC", prompt)
        self.assertIn("SCENARIUSZ POŁUDNIE", prompt)
        self.assertIn("PUNKT WYJŚCIA — ANALIZY EKSPERCKIE", prompt)
        self.assertIn("45%", prompt)
        self.assertIn("28%", prompt)
        self.assertIn("### analiza-1", prompt)
        self.assertIn("Treść analizy.", prompt)
        # geografia wg stanu faktycznego: DK50 na północ od gminy
        self.assertIn("DK50 biegnie", prompt)
        self.assertIn("nie przecina gminy", prompt)
        self.assertIn("starszy, nieaktualny przebieg DK50", prompt)
        self.assertNotIn("mniej więcej środkiem gminy", prompt)
        self.assertNotIn("kierunku Osiecka/Wilgi", prompt)

    def test_first_run_marker(self):
        prompt = assess.build_prompt([Evidence(title="t", url="u", source="s")], None, CFG)
        self.assertIn("pierwsza ocena", prompt)

    def test_prompt_without_analyses_text(self):
        prompt = assess.build_prompt([Evidence(title="t", url="u", source="s")], None, CFG)
        self.assertIn("PUNKT WYJŚCIA", prompt)
        self.assertNotIn("Pełne teksty analiz:", prompt)


class TestLoadAnalyses(unittest.TestCase):
    def test_loads_and_caps(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "b_analiza.md").write_text("B" * 30, encoding="utf-8")
            (d / "a_analiza.md").write_text("A" * 30, encoding="utf-8")
            assess.ANALIZY_DIR = d
            assess.ANALIZY_CAP = 10
            out = assess.load_analyses()
            self.assertIn("### a_analiza", out)
            self.assertIn("### b_analiza", out)
            self.assertLess(len(out), 80)  # cap zadziałał
            self.assertIn("…(skrócono)", out)
            # restore
            assess.ANALIZY_DIR = (Path(assess.__file__).resolve().parent.parent
                                  / "analizy")
            assess.ANALIZY_CAP = 8000


class TestPrevAndNoEvidence(unittest.TestCase):
    def test_prev_entry_before(self):
        entries = [{"date": "2026-09-01"}, {"date": "2026-09-03"}]
        self.assertEqual(assess.prev_entry_before(entries, "2026-09-05")["date"], "2026-09-03")
        self.assertIsNone(assess.prev_entry_before(entries, "2026-09-01"))

    def test_no_evidence_keeps_prev_scores(self):
        prev = {"date": "2026-09-04",
                "scores": {"north": {"score": 42}, "south": {"score": 7}}}
        entry = assess.no_evidence_entry("2026-09-05", prev, "no-evidence")
        self.assertEqual(entry["scores"]["north"]["score"], 42)
        self.assertEqual(entry["scores"]["south"]["score"], 7)
        self.assertEqual(entry["scores"]["north"]["confidence"], "niska")
        self.assertEqual(entry["engine_status"], "no-evidence")

    def test_no_evidence_first_run_uses_baseline(self):
        entry = assess.no_evidence_entry("2026-09-05", None, "no-data", CFG)
        self.assertEqual(entry["scores"]["north"]["score"], 45)
        self.assertEqual(entry["scores"]["south"]["score"], 28)
        self.assertEqual(entry["engine_status"], "no-data")

    def test_no_evidence_without_cfg_falls_back_neutral(self):
        entry = assess.no_evidence_entry("2026-09-05", None, "no-data", None)
        self.assertEqual(entry["scores"]["north"]["score"], 50)
        self.assertEqual(entry["scores"]["south"]["score"], 50)


if __name__ == "__main__":
    unittest.main()
