import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import official_watch as ow  # noqa: E402

SRC = {"id": "test-src", "label": "Strona testowa", "url": "https://example.gov.pl/a50"}


class TestHtmlToText(unittest.TestCase):
    def test_strips_scripts_tags_and_keeps_lines(self):
        html = ("<html><head><title>A50 — komunikat</title>"
                "<script>var x = 1;</script></head>"
                "<body><h1>Komunikat</h1><p>Wariant północny</p><ul><li>Punkt 1</li></ul></body></html>")
        text = ow.html_to_text(html)
        self.assertNotIn("var x", text)
        self.assertNotIn("<h1>", text)
        self.assertIn("Komunikat", text)
        self.assertIn("Wariant północny", text)
        self.assertIn("Punkt 1", text)
        self.assertEqual(ow.page_title(html), "A50 — komunikat")

    def test_fingerprint_ignores_case_and_spacing(self):
        self.assertEqual(ow.fingerprint("A50  Komunikat\nGDDKiA"),
                         ow.fingerprint("a50 komunikat gddkia"))

    def test_diff_excerpt_returns_only_new_lines(self):
        old = "Menu\nKomunikat o wariancie\nStopka"
        new = "Menu\nKomunikat o wariancie\nWybrano wariant północny\nStopka"
        excerpt = ow.diff_excerpt(old, new)
        self.assertIn("Wybrano wariant północny", excerpt)
        self.assertNotIn("Menu", excerpt)


class TestCheckSource(unittest.TestCase):
    def _fetcher(self, html):
        return lambda url: html

    def test_first_run_is_baseline_without_item(self):
        item, entry = ow.check_source(SRC, {"sources": {}}, "2026-11-07",
                                      fetcher=self._fetcher("<p>A50 start</p>"))
        self.assertIsNone(item)
        self.assertTrue(entry["baseline"])
        self.assertFalse(entry["changed"])

    def test_unchanged_page_emits_nothing(self):
        html = "<p>A50 bez zmian</p>"
        _, first = ow.check_source(SRC, {"sources": {}}, "2026-11-07",
                                   fetcher=self._fetcher(html))
        state = {"sources": {"test-src": first}}
        item, entry = ow.check_source(SRC, state, "2026-11-08",
                                      fetcher=self._fetcher(html))
        self.assertIsNone(item)
        self.assertFalse(entry["changed"])

    def test_change_emits_official_evidence(self):
        _, first = ow.check_source(SRC, {"sources": {}}, "2026-11-07",
                                   fetcher=self._fetcher("<p>A50 start</p>"))
        state = {"sources": {"test-src": first}}
        item, entry = ow.check_source(SRC, state, "2026-11-08",
                                      fetcher=self._fetcher("<p>A50 start</p><p>Wybrano wariant</p>"))
        self.assertTrue(entry["changed"])
        self.assertIsNotNone(item)
        self.assertTrue(item["url"].startswith("https://example.gov.pl/a50#zmiana-"))
        self.assertEqual(item["source"], "official:test-src")
        self.assertEqual(item["published"], "2026-11-08")
        self.assertIn("Wybrano wariant", item["snippet"])

    def test_fetch_error_is_reported_not_raised(self):
        def boom(url):
            raise OSError("connection refused")
        item, entry = ow.check_source(SRC, {"sources": {}}, "2026-11-07", fetcher=boom)
        self.assertIsNone(item)
        self.assertEqual(entry["status"], "error")
        self.assertIn("connection refused", entry["error"])


if __name__ == "__main__":
    unittest.main()
