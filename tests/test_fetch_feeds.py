import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import fetch_feeds  # noqa: E402


class TestRotationWindow(unittest.TestCase):
    POOL = [f"q{i}" for i in range(10)]

    def test_window_has_exact_size(self):
        for day in ("2026-11-07", "2026-11-08", "2026-11-09"):
            self.assertEqual(len(fetch_feeds.rotation_window(self.POOL, 3, day)), 3)

    def test_same_day_is_deterministic(self):
        a = fetch_feeds.rotation_window(self.POOL, 3, "2026-11-07")
        b = fetch_feeds.rotation_window(self.POOL, 3, "2026-11-07")
        self.assertEqual(a, b)

    def test_consecutive_days_do_not_repeat_within_cycle(self):
        # 10 zapytań / 3 na dzień => 3 pełne dni bez powtórzeń; czwarty
        # domyka cykl i z definicji wraca do początku listy.
        seen = set()
        for day in ("2026-11-07", "2026-11-08", "2026-11-09"):
            window = fetch_feeds.rotation_window(self.POOL, 3, day)
            self.assertFalse(seen & set(window), f"powtórka w {day}: {window}")
            seen |= set(window)

    def test_full_cycle_covers_whole_pool(self):
        seen = set()
        for day in range(1, 32):
            seen |= set(fetch_feeds.rotation_window(self.POOL, 3, f"2026-12-{day:02d}"))
        self.assertEqual(seen, set(self.POOL))

    def test_cycle_wraps_instead_of_truncating(self):
        pool = ["a", "b", "c", "d", "e"]
        windows = [fetch_feeds.rotation_window(pool, 2, f"2026-12-{d:02d}")
                   for d in (1, 2, 3)]
        self.assertTrue(all(len(w) == 2 for w in windows), windows)
        self.assertEqual(set().union(*windows), set(pool))  # cykl domknięty

    def test_empty_pool_or_zero_per_day(self):
        self.assertEqual(fetch_feeds.rotation_window([], 5, "2026-11-07"), [])
        self.assertEqual(fetch_feeds.rotation_window(self.POOL, 0, "2026-11-07"), [])


class TestCanonicalResolution(unittest.TestCase):
    def test_non_google_url_is_returned_unchanged(self):
        url = "https://www.gov.pl/web/gddkia/x"
        self.assertEqual(fetch_feeds.resolve_canonical(url, {}), url)

    def test_cache_prevents_refetch(self):
        url = "https://news.google.com/rss/articles/abc"
        cache = {url: "https://www.rynekinfrastruktury.pl/a50"}
        self.assertEqual(fetch_feeds.resolve_canonical(url, cache),
                         "https://www.rynekinfrastruktury.pl/a50")


if __name__ == "__main__":
    unittest.main()
