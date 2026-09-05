"""Krok 1b (fallback): RSS/Atom — Google News, GDDKiA itp.

Uzupełnia surowy raport silnika o polskie media. Użycie:
    python scripts/fetch_feeds.py [--date YYYY-MM-DD] [--out-dir PATH]

Wyjście: data/raw/<dzień>/feeds.json — lista elementów
{title, url, source, published, snippet} przefiltrowanych po słowach kluczowych.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import REPO_ROOT, load_config, raw_dir, resolve_date  # noqa: E402

FEEDS_FILE = REPO_ROOT / "feeds.txt"
UA = {"User-Agent": "Mozilla/5.0 (a50-monitor; +https://github.com)"}


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


def fetch_url(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def keyword_matches(text: str, keywords: list[str]) -> bool:
    low = text.lower()
    return any(k in low for k in keywords)


def collect_feeds(cfg: dict) -> list[dict]:
    keywords = [k.lower() for k in cfg.get("keywords", [])]
    results, seen = [], set()
    for url, label in parse_feeds_file(FEEDS_FILE):
        try:
            items = parse_feed(fetch_url(url), label)
        except Exception as exc:  # feed nieosiągalny ≠ błąd krytyczny
            print(f"[feeds] pomijam {label}: {exc}", file=sys.stderr)
            continue
        for item in items:
            key = item["url"].split("?")[0].rstrip("/").lower()
            if key in seen or not item["title"]:
                continue
            hay = f"{item['title']} {item['snippet']} {item['url']}"
            if not keyword_matches(hay, keywords):
                continue
            seen.add(key)
            results.append(item)
    results.sort(key=lambda i: i.get("published", ""), reverse=True)
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description="Fallback RSS/Atom dla monitoringu A50.")
    ap.add_argument("--date", help="Dzień raportu (ISO, domyślnie dziś)")
    ap.add_argument("--out-dir", help="Katalog wyjściowy (domyślnie data/raw/<dzień>)")
    args = ap.parse_args()

    day = resolve_date(args.date)
    out_dir = Path(args.out_dir) if args.out_dir else raw_dir(day)
    cfg = load_config()
    items = collect_feeds(cfg)

    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "feeds": [u for u, _ in parse_feeds_file(FEEDS_FILE)],
        "items": items,
    }
    (out_dir / "feeds.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[feeds] OK: {len(items)} elementów -> {out_dir / 'feeds.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
