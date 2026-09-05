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
        self.assertIn("pojawi się", build_site.render_trend_svg([{"scores": {"north": {"score": 10}, "south": {"score": 10}}}]))
        svg = build_site.render_trend_svg([
            {"date": "2026-09-01", "scores": {"north": {"score": 10}, "south": {"score": 20}}},
            {"date": "2026-09-02", "scores": {"north": {"score": 20}, "south": {"score": 30}}}])
        self.assertEqual(svg.count("<polyline"), 2)
        self.assertIn("var(--accent)", svg)
        self.assertIn("var(--orange)", svg)


def _entry(date, north, south, **kw):
    base = {"date": date,
            "scores": {
                "north": {"score": north, "confidence": "niska", "summary": "s",
                          "trend_vs_prev": 0, "rationale": "r", "key_findings": []},
                "south": {"score": south, "confidence": "średnia", "summary": "s",
                          "trend_vs_prev": 0, "rationale": "r", "key_findings": []}},
            "evidence": [], "sources_found": 0, "engine_status": "ok",
            "assessment_path": ""}
    base.update(kw)
    return base


class TestBuild(unittest.TestCase):
    def _scores(self, tmp: Path):
        p = tmp / "scores.json"
        p.write_text(json.dumps({"entries": [
            _entry("2026-09-01", 30, 15, engine_status="no-data"),
            _entry("2026-09-02", 45, 35,
                   summary="Nowe dowody",
                   scores={
                       "north": {"score": 45, "confidence": "średnia",
                                 "summary": "Nowe dowody północ", "trend_vs_prev": 15,
                                 "rationale": "r",
                                 "key_findings": [
                                     {"claim": "Claim <script>alert(1)</script>",
                                      "evidence_urls": ["https://a.pl/1"]}]},
                       "south": {"score": 35, "confidence": "średnia",
                                 "summary": "Nowe dowody południe", "trend_vs_prev": 20,
                                 "rationale": "r", "key_findings": []}},
                   evidence=[{"title": "Ty <tu>ł</tu>", "url": "https://a.pl/1",
                              "source": "web", "published": "2026-09-02",
                              "snippet": "s", "stance": "neutral"}],
                   sources_found=1,
                   assessment_path="data/assessments/2026-09-02.json"),
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
            self.assertIn("35%", idx)
            self.assertIn("Północ gminy", idx)
            self.assertIn("Południe gminy", idx)
            self.assertIn("lang=\"pl\"", idx)
            # escaping: raw HTML z danych NIE może się pojawić
            day2 = (out / "day-2026-09-02.html").read_text(encoding="utf-8")
            self.assertNotIn("<script>alert(1)</script>", day2)
            self.assertIn("&lt;script&gt;", day2)
            self.assertNotIn("<tu>", day2)
            # tabela dowodów ma klasę evidence (mobile: table-layout fixed)
            self.assertIn('<table class="evidence">', day2)
            # trend używa 2 linii (po jednej na scenariusz)
            self.assertEqual(idx.count("<polyline"), 2)
            # etykieta pochodzenia oceny (nie „status silnika")
            self.assertIn("źródło oceny:", day2)
            # metodologia: punkt wyjścia + zastrzeżenia bez sprzeczności
            about = (out / "about.html").read_text(encoding="utf-8")
            self.assertIn("Punktem wyjścia obu score'y", about)
            self.assertIn("baza: północ 45%", about)
            self.assertIn("nie są</strong> informacją oficjalną ani opinią", about)
            self.assertNotIn("prognozą ekspercką człowieka", about)
            self.assertIn("standardzie S50", about)
            # geografia wg stanu faktycznego (DK50 na północ od gminy)
            self.assertIn("drogach wojewódzkich 801 i 739", about)
            self.assertIn("DK50 biegnie na północ od gminy", about)
            self.assertIn("nie przecina gminy", about)
            self.assertNotIn("środkiem gminy przez samą wieś", about)
            self.assertNotIn("kierunku Osiecka/Wilgi", about)

    def test_status_note_baseline_vs_no_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "site"
            p = Path(tmp) / "scores.json"
            p.write_text(json.dumps({"entries": [
                _entry("2026-09-01", 45, 28,
                       engine_status="baseline-analizy")]},
                ensure_ascii=False), encoding="utf-8")
            build_site.build(p, out)
            idx = (out / "index.html").read_text(encoding="utf-8")
            self.assertIn("Punkt wyjścia — ocena bazowa", idx)
            self.assertNotIn("nie ma nowych dowodów", idx)
            p.write_text(json.dumps({"entries": [
                _entry("2026-09-01", 45, 28, engine_status="no-data")]},
                ensure_ascii=False), encoding="utf-8")
            build_site.build(p, out)
            idx = (out / "index.html").read_text(encoding="utf-8")
            self.assertIn("nie ma nowych dowodów", idx)
            self.assertNotIn("Punkt wyjścia — ocena bazowa", idx)


class TestMdToHtml(unittest.TestCase):
    def test_inline_formatting(self):
        self.assertEqual(build_site.md_inline("**b** i *k*"),
                         "<strong>b</strong> i <em>k</em>")

    def test_inline_escapes_html(self):
        self.assertEqual(build_site.md_inline("<x> & y"),
                         "&lt;x&gt; &amp; y")

    def test_blocks(self):
        html = build_site.md_to_html(
            "# Nagłówek\n\nAkapit z **bold**.\n\n- p1\n- p2\n\n"
            "1. k1\n2. k2\n\n| A | B |\n|---|---|\n| 1 | **2** |\n\n---\n")
        self.assertIn("<h1>Nagłówek</h1>", html)
        self.assertIn("<p>Akapit z <strong>bold</strong>.</p>", html)
        self.assertIn("<ul><li>p1</li><li>p2</li></ul>", html)
        self.assertIn("<ol><li>k1</li><li>k2</li></ol>", html)
        self.assertIn("<th>A</th>", html)
        self.assertIn("<td><strong>2</strong></td>", html)
        self.assertIn("<hr>", html)

    def test_title(self):
        self.assertEqual(build_site.md_title("# Tytuł\n\ntreść"), "Tytuł")


class TestAnalizyPages(unittest.TestCase):
    def _scores(self, tmp: Path) -> Path:
        p = tmp / "scores.json"
        p.write_text(json.dumps({"entries": [_entry("2026-09-01", 30, 20)]},
                                ensure_ascii=False), encoding="utf-8")
        return p

    def test_build_generates_analizy_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            analizy = tmp / "analizy"
            analizy.mkdir()
            (analizy / "2026-09-05_test.md").write_text(
                "# Test analiza\n\nSekcja z **wytłuszczeniem**.\n",
                encoding="utf-8")
            out = tmp / "site"
            build_site.build(self._scores(tmp), out, analizy_dir=analizy)
            page = (out / "2026-09-05_test.html").read_text(encoding="utf-8")
            self.assertIn("<h1>Test analiza</h1>", page)
            self.assertIn("<strong>wytłuszczeniem</strong>", page)
            listing = (out / "analizy.html").read_text(encoding="utf-8")
            self.assertIn('href="2026-09-05_test.html"', listing)
            idx = (out / "index.html").read_text(encoding="utf-8")
            self.assertIn("2026-09-05_test.html", idx)

    def test_no_analizy_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            out = tmp / "site"
            build_site.build(self._scores(tmp), out, analizy_dir=tmp / "brak")
            self.assertFalse((out / "analizy.html").exists())


if __name__ == "__main__":
    unittest.main()
