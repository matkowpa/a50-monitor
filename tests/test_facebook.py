import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import assess  # noqa: E402
import facebook  # noqa: E402

CFG = {"keywords": ["a50", "sobienie-jeziory", "obwodnica autostradowa"]}

# Markdown w stylu opencli browser extract: chrome grupy + wpisy (permalink → treść)
FB_MARKDOWN = """# Sobienie Jeziory bez cenzury ogłoszenia/informacje

Grupa publiczna · 3,1 tys. członków

[Dyskusja](/groups/3789393041111856/)

[2026-09-20](/groups/3789393041111856/posts/1234567890123456/?__cft__=abc)

GDDKiA potwierdza, że analizuje wariant A50 przez Sobienie-Jeziory.

Komentarze: 12

[2026-09-19](/groups/3789393041111856/posts/9999999999999999/)

Remont drogi gminnej w Śniadkowie — utrudnienia.

[2026-09-18](/groups/3789393041111856/posts/1111111111111111/)

Sport: remis w derbach gminy.
"""


class TestLabel(unittest.TestCase):
    def test_label_from_heading(self):
        label = facebook._label_from_markdown(FB_MARKDOWN, "https://fb/groups/1/")
        self.assertEqual(label, "Sobienie Jeziory bez cenzury ogłoszenia/informacje")

    def test_label_fallback_to_slug(self):
        label = facebook._label_from_markdown("brak naglowka", "https://fb/groups/777/")
        self.assertEqual(label, "FB 777")


class TestPostsFromMarkdown(unittest.TestCase):
    def test_keeps_only_keyword_posts_with_permalinks(self):
        items = facebook.posts_from_markdown(FB_MARKDOWN, CFG["keywords"], "FB grupa")
        self.assertEqual([i["url"] for i in items], [
            "https://www.facebook.com/groups/3789393041111856/posts/1234567890123456/"])
        self.assertIn("GDDKiA", items[0]["title"])
        self.assertEqual(items[0]["source"], "FB grupa")

    def test_url_made_absolute_and_deduped(self):
        md = ("[A50 przez gminę](/groups/1/posts/123/)\n"
              "[A50 przez gminę](/groups/1/posts/123/)\n")
        items = facebook.posts_from_markdown(md, ["a50"], "l")
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]["url"].startswith("https://www.facebook.com/"))

    def test_neighbour_post_text_does_not_leak(self):
        # wpis bez słów kluczowych tuż przed wpisem o A50 nie może zostać dowodem
        md = ("[2026-09-19](/groups/1/posts/1/)\n\nRemont drogi gminnej.\n\n"
              "[2026-09-18](/groups/1/posts/2/)\n\nA50 przez Sobienie-Jeziory.\n")
        items = facebook.posts_from_markdown(md, CFG["keywords"], "l")
        self.assertEqual([i["url"] for i in items],
                         ["https://www.facebook.com/groups/1/posts/2/"])

    def test_chrome_after_post_does_not_leak(self):
        # realny przypadek 2026-09-24: wpis o zbiórce, a po nim chrome grupy
        # z nazwą gminy — treść wpisu kończy się na pierwszym markerze chrome
        md = ("[8 min](/groups/1/posts/28993577706933375/?comment_id=1&__cft__=x)\n\n"
              "Każda złotówka przybliża Amelkę do Zielonego paska. Pomagajmy\n\n"
              "-   Odpowiedz\n\n## Informacje\n\npubliczna\n\n"
              "Każdy może sprawdzić listę członków grupy.\n\n"
              "Sobienie Jeziory, Siedlce, Poland\n\n## Najnowsze multimedia\n")
        self.assertEqual(facebook.posts_from_markdown(md, CFG["keywords"], "l"), [])
        self.assertEqual([i["url"] for i in facebook.posts_from_markdown(
            md, ["a50", "obwodnica"], "l")], [])

    def test_url_query_stripped_but_story_fbid_kept(self):
        md = ("[8 min](/groups/1/posts/28993577706933375/?comment_id=1&__cft__=x)\n\n"
              "A50 przez gminę\n")
        items = facebook.posts_from_markdown(md, ["a50"], "l")
        self.assertEqual(items[0]["url"],
                         "https://www.facebook.com/groups/1/posts/28993577706933375/")
        md2 = "[2026-09-20](/permalink.php?story_fbid=123&id=456)\n\nA50 przez gminę\n"
        items2 = facebook.posts_from_markdown(md2, ["a50"], "l")
        self.assertEqual(items2[0]["url"],
                         "https://www.facebook.com/permalink.php?story_fbid=123&id=456")

    def test_chrome_without_permalinks_gives_nothing(self):
        # przypadek z 2026-09-24: brak członkostwa → same placeholdery w DOM
        md = ("# Spam - gmina Sobienie-Jeziory\n\nGrupa publiczna · 420 członków\n\n"
              "Dołącz do grupy\n\nFacebook Facebook Facebook\n\nNapisz coś...\n")
        self.assertEqual(facebook.posts_from_markdown(md, CFG["keywords"], "l"), [])

    def test_snippet_centred_on_keyword(self):
        items = facebook.posts_from_markdown(FB_MARKDOWN, CFG["keywords"], "l")
        self.assertIn("A50", items[0]["snippet"])


class TestCollect(unittest.TestCase):
    def test_writes_items_raw_and_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            items, errors, ok = facebook.collect(
                CFG["keywords"], ["https://www.facebook.com/groups/3789393041111856/"],
                out, extract=lambda url: FB_MARKDOWN)
            self.assertEqual(ok, 1)
            self.assertEqual(len(items), 1)
            self.assertEqual(errors, [])   # wpis znaleziony → brak notki o permalinkach
            self.assertIn("facebook-raw-1.md", [p.name for p in out.iterdir()])
            raw = (out / "facebook-raw-1.md").read_text(encoding="utf-8")
            self.assertIn("<!-- source: https://www.facebook.com/groups/3789393041111856/",
                          raw)

    def test_failed_source_does_not_stop_others(self):
        def extract(url):
            if "bad" in url:
                raise RuntimeError("opencli exit 1")
            return FB_MARKDOWN

        with tempfile.TemporaryDirectory() as tmp:
            items, errors, ok = facebook.collect(
                CFG["keywords"],
                ["https://fb/bad", "https://www.facebook.com/groups/1/"],
                Path(tmp), extract=extract)
        self.assertEqual(ok, 1)
        self.assertEqual(len(items), 1)
        self.assertIn("opencli exit 1", errors[0])

    def test_empty_dom_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            items, errors, ok = facebook.collect(
                CFG["keywords"], ["https://www.facebook.com/groups/1/"], Path(tmp),
                extract=lambda url: "   ")
        self.assertEqual((items, ok), ([], 0))
        self.assertIn("brak treści", errors[0])

    def test_permalinks_without_keyword_match_reported(self):
        md = ("[2026-09-19](/groups/1/posts/1/)\n\nRemont drogi gminnej.\n\n"
              "[2026-09-18](/groups/1/posts/2/)\n\nZbiórka dla schroniska.\n")
        with tempfile.TemporaryDirectory() as tmp:
            items, errors, ok = facebook.collect(
                ["a50"], ["https://www.facebook.com/groups/1/"], Path(tmp),
                extract=lambda url: md)
        self.assertEqual((items, ok), ([], 1))
        self.assertIn("2 permalinków, 0 trafień", errors[0])


class TestMain(unittest.TestCase):
    def _run(self, argv):
        with mock.patch.object(sys, "argv", ["facebook.py", *argv]):
            return facebook.main()

    def test_skips_without_opencli(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(facebook, "opencli_bin", return_value=None):
                code = self._run(["--date", "2026-09-24", "--out-dir", tmp,
                                  "--source", "https://www.facebook.com/groups/1/"])
            self.assertEqual(code, 0)
            payload = json.loads(
                (Path(tmp) / "facebook.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "skipped")
            self.assertEqual(payload["items"], [])
            self.assertTrue(payload["errors"])

    def test_writes_results_and_closes_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(facebook, "opencli_bin", return_value="opencli"), \
                 mock.patch.object(facebook, "extract_source", return_value=FB_MARKDOWN), \
                 mock.patch.object(facebook, "close_session") as closed:
                code = self._run([
                    "--date", "2026-09-24", "--out-dir", tmp,
                    "--source", "https://www.facebook.com/groups/3789393041111856/"])
            self.assertEqual(code, 0)
            self.assertTrue(closed.called)
            payload = json.loads(
                (Path(tmp) / "facebook.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(len(payload["items"]), 1)


class TestContractWithAssess(unittest.TestCase):
    def test_payload_items_readable_by_assess(self):
        with tempfile.TemporaryDirectory() as tmp:
            items, errors, _ = facebook.collect(
                CFG["keywords"],
                ["https://www.facebook.com/groups/3789393041111856/"], Path(tmp),
                extract=lambda url: FB_MARKDOWN)
        payload = facebook.build_payload(["s"], items, errors)
        evidence = assess.extract_feed_items(payload, CFG)
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0].published, "")   # brak daty → przepuszczone
        self.assertTrue(evidence[0].url.startswith("https://www.facebook.com/"))


if __name__ == "__main__":
    unittest.main()
