import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import build_site  # noqa: E402


class TestBand(unittest.TestCase):
    def test_bands(self):
        self.assertEqual(build_site.band(10)[0], "niskie")
        self.assertEqual(build_site.band(30)[0], "umiarkowane")
        self.assertEqual(build_site.band(60)[0], "podwyższone")
        self.assertEqual(build_site.band(90)[0], "wysokie")


class TestSvg(unittest.TestCase):
    def test_gauge_contains_score(self):
        svg = build_site.render_gauge_svg(42, "var(--yellow)")
        self.assertIn("42%", svg)
        self.assertIn("<svg", svg)
        self.assertIn("var(--yellow)", svg)

    def test_gauge_clamps(self):
        svg0 = build_site.render_gauge_svg(0, "var(--green)")
        svg100 = build_site.render_gauge_svg(100, "var(--red)")
        self.assertIn("0%", svg0)
        self.assertIn("100%", svg100)

    def test_trend_needs_two_points(self):
        self.assertIn("pojawi się", build_site.render_trend_svg([{"score": 10}]))
        svg = build_site.render_trend_svg([{"date": "2026-09-01", "score": 10},
                                           {"date": "2026-09-02", "score": 20}])
        self.assertIn("<polyline", svg)


class TestBuild(unittest.TestCase):
    def _scores(self, tmp: Path):
        p = tmp / "scores.json"
        p.write_text(json.dumps({"entries": [
            {"date": "2026-09-01", "score": 30, "confidence": "niska",
             "summary": "Podsum <b>x</b>", "trend_vs_prev": 0,
             "rationale": "r", "key_findings": [], "evidence": [],
             "sources_found": 0, "engine_status": "no-data",
             "assessment_path": ""},
            {"date": "2026-09-02", "score": 45, "confidence": "średnia",
             "summary": "Nowe dowody", "trend_vs_prev": 15,
             "rationale": "r", "key_findings": [
                 {"claim": "Claim <script>alert(1)</script>",
                  "evidence_urls": ["https://a.pl/1"]}],
             "evidence": [{"title": "Ty <tu>ł</tu>", "url": "https://a.pl/1",
                           "source": "web", "published": "2026-09-02",
                           "snippet": "s", "stance": "neutral"}],
             "sources_found": 1, "engine_status": "ok",
             "assessment_path": "data/assessments/2026-09-02.json"},
        ]}, ensure_ascii=False), encoding="utf-8")
        return p

    def test_build_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "site"
            build_site.build(self._scores(Path(tmp)), out)
            for name in ("index.html", "about.html", "style.css",
                         "day-2026-09-01.html", "day-2026-09-02.html"):
                self.assertTrue((out / name).exists(), name)
            idx = (out / "index.html").read_text(encoding="utf-8")
            self.assertIn("45%", idx)
            self.assertIn("lang=\"pl\"", idx)
            # escaping: raw HTML z danych NIE może się pojawić
            day2 = (out / "day-2026-09-02.html").read_text(encoding="utf-8")
            self.assertNotIn("<script>alert(1)</script>", day2)
            self.assertIn("&lt;script&gt;", day2)
            self.assertNotIn("<tu>", day2)
            # trend używa 2 punktów
            self.assertIn("<polyline", idx)


if __name__ == "__main__":
    unittest.main()
