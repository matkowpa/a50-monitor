import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import agent_reach  # noqa: E402
import assess  # noqa: E402

CFG = {"keywords": ["a50", "sobienie-jeziory"]}

# Odpowiedź Exa (mcporter --output json → content[].text): rekordy rozdzielone
# liniami "---", fragmenty "Highlights" liniami "..."
EXA_TEXT = """Title: Stanowisko w sprawie przebiegu drogi A50
URL: https://www.sobieniejeziory.pl/asp/stanowisko,1,1119
Published: 2026-07-03T00:00:00.000Z
Author: N/A
Highlights:
Sprzeciw wobec przebiegu drogi A50 przez gminę Sobienie-Jeziory
...
Dalsza część komunikatu
---
Title: Sport: remis w derbach
URL: https://sport.pl/remis
Published: N/A
Author: N/A
Highlights:
Nic o drogach
---
Title: A50 przez powiat otwocki
URL: https://www.przegladotwocki.pl/artykul/5250,a50
Published: 2026-06-24T00:00:00.000Z
Author: Agnieszka Deja
Highlights:
Uchwała Rady Gminy Sobienie-Jeziory w sprawie A50
"""


class TestParseResults(unittest.TestCase):
    def test_parses_all_records(self):
        items = agent_reach.parse_results(EXA_TEXT)
        self.assertEqual(len(items), 3)
        first = items[0]
        self.assertEqual(first["title"], "Stanowisko w sprawie przebiegu drogi A50")
        self.assertEqual(first["source"], "exa")
        self.assertEqual(first["published"], "2026-07-03")
        self.assertIn("Sobienie-Jeziory", first["snippet"])

    def test_highlights_fragments_joined(self):
        items = agent_reach.parse_results(EXA_TEXT)
        self.assertIn("Sprzeciw wobec przebiegu", items[0]["snippet"])
        self.assertFalse(items[0]["snippet"].startswith("..."))

    def test_missing_published_is_empty(self):
        items = agent_reach.parse_results(EXA_TEXT)
        self.assertEqual(items[1]["published"], "")

    def test_record_without_url_skipped(self):
        self.assertEqual(agent_reach.parse_results(
            "Title: Bez URL\nPublished: 2026-01-01\nHighlights:\ntekst"), [])

    def test_empty_input(self):
        self.assertEqual(agent_reach.parse_results(""), [])


class TestCollectItems(unittest.TestCase):
    def test_filters_offtopic_and_dedupes(self):
        items, errors = agent_reach.collect_items(
            CFG, ["q1", "q2"], 8, search=lambda q, n: EXA_TEXT)
        self.assertEqual(errors, [])
        self.assertEqual([i["url"] for i in items], [
            "https://www.sobieniejeziory.pl/asp/stanowisko,1,1119",
            "https://www.przegladotwocki.pl/artykul/5250,a50",
        ])

    def test_sorted_by_published_desc(self):
        items, _ = agent_reach.collect_items(
            CFG, ["q1"], 8, search=lambda q, n: EXA_TEXT)
        self.assertEqual([i["published"] for i in items],
                         ["2026-07-03", "2026-06-24"])

    def test_failed_query_does_not_stop_others(self):
        def search(query, num_results):
            if query == "zepsute":
                raise RuntimeError("mcporter exit 1")
            return EXA_TEXT

        items, errors = agent_reach.collect_items(
            CFG, ["zepsute", "ok"], 8, search=search)
        self.assertEqual(len(errors), 1)
        self.assertIn("mcporter exit 1", errors[0])
        self.assertEqual(len(items), 2)

    def test_no_queries_means_no_items(self):
        items, errors = agent_reach.collect_items(CFG, [], 8)
        self.assertEqual((items, errors), ([], []))


class TestMain(unittest.TestCase):
    def _run(self, argv):
        with mock.patch.object(sys, "argv", ["agent_reach.py", *argv]):
            return agent_reach.main()

    def test_skips_without_mcporter(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(agent_reach, "mcporter_bin", return_value=None):
                code = self._run(["--date", "2026-09-24", "--out-dir", tmp,
                                  "--query", "test"])
            self.assertEqual(code, 0)
            payload = json.loads(
                (Path(tmp) / "agent_reach.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "skipped")
            self.assertEqual(payload["items"], [])
            self.assertEqual(payload["queries"], ["test"])
            self.assertTrue(payload["errors"])

    def test_writes_results_from_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(agent_reach, "mcporter_bin", return_value="mcporter"), \
                 mock.patch.object(agent_reach, "run_search",
                                   return_value=EXA_TEXT):
                code = self._run(["--date", "2026-09-24", "--out-dir", tmp,
                                  "--query", "test"])
            self.assertEqual(code, 0)
            payload = json.loads(
                (Path(tmp) / "agent_reach.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(len(payload["items"]), 2)


class TestContractWithAssess(unittest.TestCase):
    def test_payload_items_readable_by_assess(self):
        items, _ = agent_reach.collect_items(
            CFG, ["q"], 8, search=lambda q, n: EXA_TEXT)
        payload = agent_reach.build_payload(["q"], items, [])
        evidence = assess.extract_feed_items(payload, CFG)
        self.assertEqual(len(evidence), 2)
        self.assertEqual(evidence[0].published, "2026-07-03")
        self.assertEqual(evidence[0].source, "exa")


if __name__ == "__main__":
    unittest.main()
