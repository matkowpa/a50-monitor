"""Krok 1b (fallback): RSS/Atom — Google News, GDDKiA itp.

Uzupełnia surowy raport silnika o polskie media. Użycie:
    python scripts/fetch_feeds.py [--date YYYY-MM-DD] [--out-dir PATH]

Wyjście: data/raw/<dzień>/feeds.json — lista elementów
{title, url, source, published, snippet} przefiltrowanych po słowach kluczowych.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote_plus, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (REPO_ROOT, is_fresh, is_junk_title, load_config,  # noqa: E402
                    parse_date_hint, raw_dir, resolve_date, url_host)

FEEDS_FILE = REPO_ROOT / "feeds.txt"
URL_CACHE = REPO_ROOT / "data" / "url_cache.json"
UA = {"User-Agent": "Mozilla/5.0 (a50-monitor; +https://github.com)"}
GOOGLE_NEWS = ("https://news.google.com/rss/search?q={query}"
               "&hl=pl&gl=PL&ceid=PL:PL")
# Okno świeżości doklejamy do samego zapytania — parametr `when:` poza `q`
# jest ignorowany i do kolektora wracały artykuły z 2019 roku.
GOOGLE_FRESHNESS = "when:30d"


def parse_feeds_file(path: Path) -> list[tuple[str, str]]:
    entries = []
    if not path.exists():
        return entries
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        url, _, label = line.partition("|")
        entries.append((url.strip(), label.strip() or urlparse(url).netloc))
    return entries


def _text(el, *names: str) -> str:
    for name in names:
        child = el.find(name)
        if child is not None and (child.text or "").strip():
            return child.text.strip()
    return ""


def _link(el) -> str:
    link = el.find("link")
    if link is not None and (link.text or "").strip():
        return link.text.strip()
    if link is not None and link.get("href"):
        return link.get("href").strip()
    return ""


def _pubdate(el, *names: str) -> str:
    raw = _text(el, *names)
    if not raw:
        return ""
    try:
        return parsedate_to_datetime(raw).astimezone(timezone.utc).date().isoformat()
    except (TypeError, ValueError):
        return raw[:10]


def parse_feed(xml_bytes: bytes, label: str) -> list[dict]:
    root = ET.fromstring(xml_bytes)
    items = []
    for item in root.iter():
        tag = item.tag.rsplit("}", 1)[-1]
        if tag == "item":  # RSS
            items.append({
                "title": _text(item, "title"),
                "url": _link(item),
                "source": label,
                "publisher": publisher_of(item),
                "published": _pubdate(item, "pubDate", "dc:date"),
                "snippet": _text(item, "description")[:400],
            })
        elif tag == "entry":  # Atom
            items.append({
                "title": _text(item, "title"),
                "url": _link(item),
                "source": label,
                "published": _pubdate(item, "published", "updated"),
                "snippet": _text(item, "summary", "content")[:400],
            })
    return [i for i in items if i["url"]]


def rotation_window(pool: list[str], per_day: int, day: str) -> list[str]:
    """Deterministyczne okno zapytań na dany dzień.

    Stały zestaw zapytań zwraca codziennie ten sam top-30, więc „nowych”
    dowodów było 0-5 na dzień. Okno przesuwa się co dzień o `per_day` i po
    pełnym cyklu wraca do początku; ostatnie okno jest dociągane z początku
    listy, żeby każdego dnia liczba zapytań była identyczna.
    """
    pool = [q for q in pool if q.strip()]
    if not pool or int(per_day or 0) <= 0:
        return []
    per_day = min(int(per_day), len(pool))
    try:
        doy = date.fromisoformat(day[:10]).timetuple().tm_yday
    except ValueError:
        doy = 1
    start = ((doy - 1) * per_day) % len(pool)
    return [pool[(start + i) % len(pool)] for i in range(per_day)]


def load_url_cache() -> dict:
    if not URL_CACHE.exists():
        return {}
    try:
        data = json.loads(URL_CACHE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def save_url_cache(cache: dict, limit: int = 4000) -> None:
    if len(cache) > limit:                       # cache ma nie rosnąć bez końca
        for key in list(cache)[: len(cache) - limit]:
            cache.pop(key, None)
    URL_CACHE.parent.mkdir(parents=True, exist_ok=True)
    URL_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=0) + "\n",
                         encoding="utf-8")


def resolve_canonical(url: str, cache: dict | None = None,
                      timeout: int = 12) -> str:
    """Rozwija wrapper Google News do docelowego adresu.

    Bez tego 41% dowodów miało adres `news.google.com/rss/articles/...`,
    który nie deduplikuje się z linkiem kanonicznym i nie mówi, kto napisał.
    """
    cache = cache if cache is not None else {}
    if url_host(url) != "news.google.com":
        return url
    if url in cache:
        return cache[url]
    final = url
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            candidate = resp.geturl()
        if candidate and url_host(candidate) not in ("news.google.com", ""):
            final = candidate
    except Exception:
        final = url
    cache[url] = final
    return final


def google_query_url(query: str) -> str:
    return GOOGLE_NEWS.format(query=quote_plus(f"{query} {GOOGLE_FRESHNESS}"))


def publisher_of(item) -> str:
    """Domena wydawcy z atrybutu <source url> w kanale Google News.

    Google News nie oddaje docelowego adresu artykułu bez JavaScriptu,
    ale oddaje wydawcę — dzięki temu wiemy, kto napisał, i możemy
    deduplikować ten sam tekst pod wrapperem i pod linkiem bezpośrednim.
    """
    for child in item:
        if child.tag.rsplit("}", 1)[-1] == "source" and child.get("url"):
            return url_host(child.get("url"))
    return ""


def fetch_url(url: str, timeout: int = 60, retries: int = 1) -> bytes:
    """Pobranie kanału z jednym ponowieniem.

    gov.pl potrafi nie odpowiedzieć w 30 s (GDDKiA regularnie), a pojedynczy
    timeout pozbawiał run całego kanału urzędowego.
    """
    last: Exception | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers=UA)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as exc:
            last = exc
    raise last if last else RuntimeError("fetch failed")


def keyword_matches(text: str, keywords: list[str]) -> bool:
    low = text.lower()
    return any(k in low for k in keywords)


def collect_feeds(cfg: dict, day: str | None = None) -> list[dict]:
    keywords = [k.lower() for k in cfg.get("keywords", [])]
    day = day or resolve_date(None)
    lookback = int(cfg.get("lookback_days", 30))
    rotation = cfg.get("feed_rotation") or {}
    queries = rotation_window(rotation.get("queries") or [],
                              int(rotation.get("per_day", 0)), day)
    feeds = list(parse_feeds_file(FEEDS_FILE))
    feeds += [(google_query_url(q), f"Google News: {q[:60]}") for q in queries]

    cache = load_url_cache()
    results, seen = [], set()
    for url, label in feeds:
        try:
            items = parse_feed(fetch_url(url), label)
        except Exception as exc:  # feed nieosiągalny ≠ błąd krytyczny
            print(f"[feeds] pomijam {label}: {exc}", file=sys.stderr)
            continue
        for item in items:
            item["url"] = resolve_canonical(item["url"], cache)
            if url_host(item["url"]) == "news.google.com":
                # wrapper nie deduplikuje się po adresie: klucz z tytułu
                # i wydawcy łapie ten sam tekst pod linkiem bezpośrednim
                key = "gn:" + " ".join(
                    re.sub(r"[^a-z0-9ąćęłńóśźż ]", "",
                           (item.get("title") or "").lower()).split())[:90]
                key += "|" + (item.get("publisher") or "")
            else:
                key = item["url"].split("?")[0].rstrip("/").lower()
            if key in seen or not item["title"] or is_junk_title(item["title"]):
                continue
            hay = f"{item['title']} {item['snippet']} {item['url']}"
            if not keyword_matches(hay, keywords):
                continue
            if not item.get("published"):
                item["published"] = parse_date_hint(
                    f"{item['title']} {item['snippet']}", day)
            # Google News w RSS potrafi zignorować `when:` i oddać artykuły
            # sprzed lat — odsiewamy je tutaj, żeby nie zajmowały miejsca
            # w zestawie dowodów (assess i tak je odrzuci).
            if not is_fresh(item.get("published", ""), day, lookback):
                continue
            seen.add(key)
            results.append(item)
    save_url_cache(cache)
    results.sort(key=lambda i: i.get("published", ""), reverse=True)
    print(f"[feeds] zapytania rotacyjne ({len(queries)}): {queries}",
          file=sys.stderr)
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description="Fallback RSS/Atom dla monitoringu A50.")
    ap.add_argument("--date", help="Dzień raportu (ISO, domyślnie dziś)")
    ap.add_argument("--out-dir", help="Katalog wyjściowy (domyślnie data/raw/<dzień>)")
    args = ap.parse_args()

    day = resolve_date(args.date)
    out_dir = Path(args.out_dir) if args.out_dir else raw_dir(day)
    cfg = load_config()
    items = collect_feeds(cfg, day)

    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "feeds": [u for u, _ in parse_feeds_file(FEEDS_FILE)],
        "rotated_queries": rotation_window(
            (cfg.get("feed_rotation") or {}).get("queries") or [],
            int((cfg.get("feed_rotation") or {}).get("per_day", 0)), day),
        "items": items,
    }
    (out_dir / "feeds.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[feeds] OK: {len(items)} elementów -> {out_dir / 'feeds.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
