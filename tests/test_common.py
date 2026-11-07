import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import common  # noqa: E402
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


class TestEvidenceGrade(unittest.TestCase):
    def test_official_gov_hosts(self):
        for url in ("https://www.gov.pl/web/gddkia/a50s50-od-dk92-do-s8",
                    "https://www.gov.pl/web/infrastruktura/petycja",
                    "https://rdos.gov.pl/wnioski",
                    "https://isap.sejm.gov.pl/isap.nsf/x"):
            self.assertEqual(common.evidence_grade(url), "official", url)

    def test_local_gov_bip_and_council_title(self):
        self.assertEqual(common.evidence_grade("https://bip.powiat-otwocki.pl/a"), "local_gov")
        self.assertEqual(
            common.evidence_grade("https://www.sobienie.pl/plik.pdf",
                                  title="Uchwała XXIII/192/2026 Rady Gminy"),
            "local_gov")

    def test_media_and_social(self):
        self.assertEqual(common.evidence_grade("https://news.google.com/rss/articles/x"), "media")
        self.assertEqual(common.evidence_grade("https://www.rynekinfrastruktury.pl/a"), "media")
        self.assertEqual(common.evidence_grade("https://www.reddit.com/r/x/comments/1"), "social")
        self.assertEqual(common.evidence_grade("https://www.facebook.com/groups/1/"), "social")

    def test_best_grade_orders_by_strength(self):
        self.assertEqual(common.best_grade(["social", "media", "official"]), "official")
        self.assertEqual(common.best_grade([]), "media")


class TestGateScore(unittest.TestCase):
    def test_media_change_is_capped_at_two(self):
        self.assertEqual(common.gate_score(38, 35, "media"), (37, True))
        self.assertEqual(common.gate_score(30, 35, "media"), (33, True))

    def test_media_small_change_passes(self):
        self.assertEqual(common.gate_score(36, 35, "media"), (36, False))

    def test_official_allows_large_move(self):
        self.assertEqual(common.gate_score(55, 35, "official"), (55, False))
        self.assertEqual(common.gate_score(70, 35, "official"), (55, True))

    def test_first_assessment_not_capped(self):
        self.assertEqual(common.gate_score(70, None, "social"), (70, False))

    def test_out_of_range_clamped(self):
        self.assertEqual(common.gate_score(130, None, "media"), (100, False))


class TestQualityHelpers(unittest.TestCase):
    def test_junk_titles(self):
        for bad in ("––––––––––––", "9. Starostwo Powiatowe w Grójcu", "A50", ""):
            self.assertTrue(common.is_junk_title(bad), bad)
        for good in ("A50 przez powiat otwocki? GDDKiA: wariant DK50 będzie analizowany",
                     "Uchwała Rady Gminy Sobienie-Jeziory w sprawie A50"):
            self.assertFalse(common.is_junk_title(good), good)

    def test_parse_date_hint_formats(self):
        self.assertEqual(common.parse_date_hint("2026-11-04 — wariant"), "2026-11-04")
        self.assertEqual(common.parse_date_hint("Opublikowano: 03.07.2026"), "2026-07-03")
        self.assertEqual(common.parse_date_hint("z dnia 17 marca 2023 r."), "2023-03-17")
        self.assertEqual(common.parse_date_hint("brak daty"), "")

    def test_parse_date_hint_rejects_bad_calendar_and_future(self):
        self.assertEqual(common.parse_date_hint("31.02.2026", today="2026-11-07"), "")
        self.assertEqual(common.parse_date_hint("2027-05-01", today="2026-11-07"), "")
