import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import events  # noqa: E402
from common import Evidence  # noqa: E402


def ev(title, url="https://www.gov.pl/web/gddkia/x", published="", snippet=""):
    return Evidence(title=title, url=url, source="test",
                    published=published, snippet=snippet)


class TestClassify(unittest.TestCase):
    def test_event_types(self):
        cases = {
            "Wybór wariantu przebiegu OAW przez Sobienie-Jeziory": "variant",
            "Wniosek o decyzję środowiskową dla OAW złożony": "decision",
            "Ruszamy z przetargiem na kolejny odcinek OAW": "tender",
            "Uchwała Rady Gminy Sobienie-Jeziory w sprawie A50": "council",
            "Konsultacje społeczne w sprawie OAW": "consultation",
            "Komunikat GDDKiA o stanie prac": "statement",
        }
        for title, expected in cases.items():
            self.assertEqual(events.classify(title), expected, title)

    def test_non_event_returns_none(self):
        self.assertIsNone(events.classify("A50 odciąży Warszawę i zmieni ruch"))


class TestExtractEvents(unittest.TestCase):
    def test_official_variant_is_confirmed(self):
        out = events.extract_events(
            [ev("Wybór wariantu przebiegu OAW przez gminę Sobienie-Jeziory",
                "https://www.gov.pl/web/gddkia/komunikat")], "2026-11-07")
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]["confirmed"])
        self.assertEqual(out[0]["grade"], "official")

    def test_media_variant_is_unconfirmed(self):
        out = events.extract_events(
            [ev("Wybór wariantu OAW — nieoficjalne doniesienia portalu",
                "https://www.rynekinfrastruktury.pl/a50")], "2026-11-07")
        self.assertEqual(len(out), 1)
        self.assertFalse(out[0]["confirmed"])

    def test_media_council_protest_is_not_material(self):
        out = events.extract_events(
            [ev("Mieszkańcy protestują przeciw przebiegowi A50 przez gminę",
                "https://gazetaotwock.pl/a")], "2026-11-07")
        self.assertEqual(out, [])

    def test_offtopic_is_dropped(self):
        out = events.extract_events(
            [ev("Uchwała w sprawie nazewnictwa ulic w Grójcu",
                "https://bip.grojec.pl/u")], "2026-11-07")
        self.assertEqual(out, [])

    def test_junk_title_is_dropped(self):
        out = events.extract_events(
            [ev("9. Starostwo Powiatowe w Grójcu ul. Piłsudskiego 59",
                "https://bip.powiat-grojec.pl/u")], "2026-11-07")
        self.assertEqual(out, [])

    def test_stale_undated_document_is_dropped(self):
        out = events.extract_events(
            [ev("UCHWAŁA NR LIX/584/23 RADY MIEJSKIEJ W GRÓJCU z dnia 17 marca 2023 r. w sprawie A50",
                "https://bip.grojec.pl/u")], "2026-11-07")
        self.assertEqual(out, [])

    def test_old_published_is_dropped(self):
        out = events.extract_events(
            [ev("Wybór wariantu OAW przez gminę Sobienie-Jeziory",
                "https://www.gov.pl/web/gddkia/komunikat", published="2025-01-01")],
            "2026-11-07")
        self.assertEqual(out, [])


class TestMergeEvents(unittest.TestCase):
    def test_dedupes_by_url_and_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "events.json"
            a = events.extract_events(
                [ev("Wybór wariantu OAW przez gminę Sobienie-Jeziory",
                    "https://www.gov.pl/web/gddkia/a")], "2026-11-01")
            b = events.extract_events(
                [ev("Wybór wariantu OAW przez gminę Sobienie-Jeziory",
                    "https://news.google.com/rss/articles/xyz")], "2026-11-02")
            events.merge_events(a, p)
            data = events.merge_events(b, p)
            self.assertEqual(len(data["events"]), 1)
            self.assertEqual(json.loads(p.read_text(encoding="utf-8"))["events"][0]["date"],
                             "2026-11-01")


if __name__ == "__main__":
    unittest.main()
