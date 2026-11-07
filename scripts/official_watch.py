"""Monitoring źródeł oficjalnych: hash treści + diff zamiast czekania na media.

Media donoszą o decyzji po fakcie, a czasem wcale. Ten krok odpytuje strony,
na których proces inwestycyjny faktycznie się rozstrzyga — projekt OAW i
odcinek A50/S50 w GDDKiA, aktualności gminy Sobienie-Jeziory, RDOŚ Warszawa —
i porównuje odcisk treści z poprzednim runem.

Wyjście: data/raw/<dzień>/official.json w tym samym kształcie co feeds.json,
więc assess.py traktuje wynik jak każdy inny dowód (a klasyfikacja
`evidence_grade` oznacza go jako official). Zmiana dostaje własny URL z
fragmentem `#zmiana-<hash>`, bo sam adres strony jest stały i bez tego
okno „już widziane” wyciszyłoby każdą kolejną zmianę.

Pierwszy run dla danego źródła zapisuje tylko punkt odniesienia — nie
generuje dowodu (inaczej każdy start od zera zgłaszałby „zmianę”).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import REPO_ROOT, atomic_write_json, load_config, raw_dir, resolve_date  # noqa: E402

STATE_PATH = REPO_ROOT / "data" / "official_state.json"
UA = {"User-Agent": "Mozilla/5.0 (a50-monitor; +https://github.com/matkowpa/a50-monitor)"}

SCRIPT_RE = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
SPACE_RE = re.compile(r"[ \t\r\f\v]+")


def html_to_text(html: str) -> str:
    """HTML → tekst: bez skryptów, stylów i tagów, z sensownymi liniami."""
    body = SCRIPT_RE.sub(" ", html)
    body = re.sub(r"</(p|div|li|h[1-6]|tr|br)>", "\n", body, flags=re.IGNORECASE)
    body = re.sub(r"<br\s*/?>", "\n", body, flags=re.IGNORECASE)
    body = TAG_RE.sub(" ", body)
    body = body.replace("&nbsp;", " ").replace("&amp;", "&")
    lines = [SPACE_RE.sub(" ", ln).strip() for ln in body.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def page_title(html: str) -> str:
    m = TITLE_RE.search(html)
    return SPACE_RE.sub(" ", TAG_RE.sub("", m.group(1))).strip() if m else ""


def fingerprint(text: str) -> str:
    """Odcisk treści: bez wielkości liter i nadmiarowych spacji."""
    norm = " ".join(text.lower().split())
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def diff_excerpt(old: str, new: str, limit: int = 320) -> str:
    """Fragment nowej treści, którego nie było w starej (linia po linii)."""
    old_lines = {" ".join(l.split()).lower() for l in old.splitlines() if l.strip()}
    fresh = [l for l in new.splitlines()
             if l.strip() and " ".join(l.split()).lower() not in old_lines]
    text = " ⏎ ".join(fresh)
    return text[:limit]


def fetch_page(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    for enc in ("utf-8", "iso-8859-2"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def load_state(path: Path | None = None) -> dict:
    p = path or STATE_PATH
    if not p.exists():
        return {"sources": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"sources": {}}
    data.setdefault("sources", {})
    return data


def check_source(source: dict, state: dict, day: str, fetcher=fetch_page) -> tuple[dict | None, dict]:
    """Sprawdza jedno źródło. Zwraca (dowód albo None, nowy wpis stanu)."""
    url = source["url"]
    entry = {"url": url, "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    try:
        html = fetcher(url)
    except Exception as exc:  # źródło niedostępne ≠ błąd krytyczny runu
        entry.update({"status": "error", "error": str(exc)[:200]})
        return None, entry

    text = html_to_text(html)
    title = page_title(html) or source.get("label", url)
    fp = fingerprint(text)
    prev = (state.get("sources") or {}).get(source["id"]) or {}
    entry.update({"status": "ok", "fingerprint": fp, "title": title,
                  "chars": len(text), "text": text[-20000:]})
    if not prev.get("fingerprint"):
        entry["changed"] = False
        entry["baseline"] = True
        return None, entry
    if prev.get("fingerprint") == fp:
        entry["changed"] = False
        return None, entry

    excerpt = diff_excerpt(prev.get("text") or "", text)
    entry.update({"changed": True, "previous": prev.get("fingerprint"),
                  "excerpt": excerpt, "text": text[-20000:]})
    item = {
        "title": f"{source.get('label', title)} — zmiana na stronie ({title[:90]})",
        "url": f"{url}#zmiana-{fp}",
        "source": f"official:{source['id']}",
        "published": day,
        "snippet": excerpt or "Treść strony zmieniła się od poprzedniego sprawdzenia.",
    }
    return item, entry


def main() -> int:
    ap = argparse.ArgumentParser(description="Monitoring oficjalnych źródeł A50/OAW.")
    ap.add_argument("--date", help="Dzień raportu (ISO, domyślnie dziś)")
    ap.add_argument("--out-dir", help="Katalog wyjściowy (domyślnie data/raw/<dzień>)")
    args = ap.parse_args()

    day = resolve_date(args.date)
    out_dir = Path(args.out_dir) if args.out_dir else raw_dir(day)
    out_dir.mkdir(parents=True, exist_ok=True)

    cfg = load_config()
    sources = cfg.get("official_sources") or []
    state = load_state()
    items, report = [], []
    for src in sources:
        item, entry = check_source(src, state, day)
        state["sources"][src["id"]] = entry
        report.append({"id": src["id"], "label": src.get("label"), **{
            k: entry.get(k) for k in ("status", "changed", "baseline", "error", "chars")}})
        if item:
            items.append(item)

    # Tekst trzymamy tylko dla źródeł, które się zmieniły (diff next run),
    # i tylko dla ostatniego odczytu — inaczej stan rośnie bez końca.
    atomic_write_json(STATE_PATH, state)
    payload = {"day": day, "sources": report, "items": items}
    (out_dir / "official.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    changed = [r["id"] for r in report if r.get("changed")]
    errors = [r["id"] for r in report if r.get("status") == "error"]
    print(f"[official] {len(sources)} źródeł, zmiany: {changed or 'brak'}, "
          f"błędy: {errors or 'brak'} -> {out_dir / 'official.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
