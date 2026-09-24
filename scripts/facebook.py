"""Krok 1d (LOKALNY): treści z Facebooka przez sesję OpenCLI w Twojej przeglądarce.

Użycie:
    python scripts/facebook.py [--date YYYY-MM-DD] [--out-dir PATH] [--source URL]

Źródła bierzemy z config.json (facebook.sources) — grupy i/lub strony widoczne
dla Twojego konta. Każde źródło otwieramy w przeglądarce (`opencli browser
<sesja> open`), wyciągamy markdown (`extract`) i składamy wynik w formacie
feeds.json / agent_reach.json, więc assess.py czyta go tą samą funkcją.

Wymagania: Chrome z rozszerzeniem OpenCLI i zalogowany facebook.com (FB pokazuje
treść dyskusji tylko członkom grupy). Bez logowania nie ma treści, dlatego ten
krok **nie działa w CI** — na runnerze brakuje opencli i sesji, więc skrypt
zapisuje payload ze statusem "skipped" i kończy się kodem 0, tak jak
agent_reach.py.

Surowy markdown każdego źródła ląduje też w data/raw/<dzień>/facebook-raw-<n>.md:
kształt DOM-u FB zmienia się, więc materiał do dostrojenia parsera zostaje.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import REPO_ROOT, load_config, raw_dir, resolve_date  # noqa: E402

OPENCLI = "opencli"
SESSION = "a50fb"          # nazwa sesji przeglądarki OpenCLI (zakładka jest leasingowana)
MAX_CHUNKS = 3             # ile porcji markdownu na źródło (extract paginuje)
CHUNK_CHARS = 20000
COMMAND_TIMEOUT_S = 180

LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")
POST_HINT = re.compile(r"(/posts/|/permalink/|multi_permalinks=|story_fbid=)\d")
# Granice „chrome" grupy: od nich kończy się treść wpisu (inaczej opis grupy
# i statystyki udawałyby treść postu — realny fałszywy traf z 2026-09-24).
CHROME = re.compile(r"(?m)^\s*(?:##\s|-\s{2,})|Grupa publiczna|Dołącz do grupy|"
                    r"Każdy może|Napisz coś|Odpowiedz\s*$|Udostępnij\s*$|"
                    r"Wyświetl wszystko|Zaproś znajomych")


def keyword_matches(text: str, keywords: list[str]) -> bool:
    low = text.lower()
    return any(k in low for k in keywords)


def _norm_url(url: str) -> str:
    return url.split("?")[0].split("#")[0].rstrip("/").lower()


def _clean(text: str) -> str:
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text or "")
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def _absolute(url: str) -> str:
    if url.startswith("/"):
        return "https://www.facebook.com" + url
    return url


def _post_url(target: str) -> str:
    """URL wpisu: bez parametrów śledzących, ale permalink.php zostaje w całości
    (tam „?story_fbid=…&id=…” jest nośnikiem, bez query link nie działa)."""
    url = _absolute(target)
    if "story_fbid=" in url:
        return url
    return url.split("?")[0]


def _snippet(text: str, keywords: list[str], size: int = 400) -> str:
    """Fragment wokół pierwszego trafienia słowa kluczowego (inaczej początek)."""
    low = text.lower()
    for kw in keywords:
        idx = low.find(kw)
        if idx >= 0:
            start = max(0, idx - size // 3)
            return text[start:start + size]
    return text[:size]


def post_permalinks(markdown: str) -> list[re.Match]:
    """Linki do konkretnych wpisów w markdownie (bez nich jesteśmy „ślepi")."""
    return [m for m in LINK.finditer(markdown or "") if POST_HINT.search(m.group(2))]


def _label_from_markdown(markdown: str, url: str) -> str:
    """Nazwa grupy/strony z pierwszego nagłówka markdownu, inaczej z URL-a."""
    for match in re.finditer(r"(?m)^#\s+(.+)$", markdown):
        title = _clean(match.group(1))
        if title and not title.lower().startswith("facebook"):
            return title[:80]
    slug = url.rstrip("/").rsplit("/", 1)[-1] or "facebook"
    return f"FB {slug}"


def posts_from_markdown(markdown: str, keywords: list[str],
                        source: str) -> list[dict]:
    """Wpisy z markdownu FB — na razie tylko tam, gdzie dowód ma własny URL.

    Bierzemy wyłącznie linki do konkretnych wpisów (…/posts/<id>,
    multi_permalinks=<id>, …/permalink/<id>): tylko taki wpis daje trwały link do
    źródła, którego wymaga strona. Fragmenty „chrome" strony (nazwa grupy, nawigacja)
    bywają nośnikiem słów kluczowych, więc NIE zamieniamy ich na dowody — brak
    permalinków zgłaszamy jako notkę, a surowy markdown zostaje do dostrojenia.

    Kształt DOM-u FB zmienia się, więc gdy po dołączeniu do grupy wpisy renderują
    się bez permalinków, ten parser trzeba dopasować do realnego markdownu
    (materiał: data/raw/<dzień>/facebook-raw-<n>.md).

    Treścią wpisu jest tekst od jego permalinka do początku następnego wpisu —
    bez tego słowa kluczowe z sąsiednich postów „zarażałyby" cały wątek.
    """
    matches = post_permalinks(markdown)
    items, seen = [], set()
    for index, match in enumerate(matches):
        post_url = _post_url(match.group(2))
        key = _norm_url(post_url)
        if key in seen:
            continue
        end = (matches[index + 1].start() if index + 1 < len(matches)
               else match.end() + 800)
        text = _clean(match.group(1))
        raw_body = markdown[match.end():end]
        cut = CHROME.search(raw_body)
        if cut:
            raw_body = raw_body[:cut.start()]
        body = _clean(raw_body)
        hay = f"{text} {body}".strip()
        if not hay or not keyword_matches(hay, keywords):
            continue
        seen.add(key)
        items.append({
            "title": (body or text)[:120],
            "url": post_url,
            "source": source,
            "published": "",
            "snippet": _snippet(hay, keywords),
        })
    return items


# --------------------------------------------------------- sesja przeglądarki

def opencli_bin() -> str | None:
    return shutil.which(OPENCLI)


def _run(args: list[str]) -> str:
    proc = subprocess.run([opencli_bin() or OPENCLI, "browser", SESSION, *args],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=COMMAND_TIMEOUT_S,
                          cwd=str(REPO_ROOT))
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0 and not out.strip():
        raise RuntimeError(f"{OPENCLI} browser {' '.join(args[:2])} "
                           f"exit {proc.returncode}")
    return out


def _json_from(output: str) -> dict:
    """opencli wypisuje JSON, czasem poprzedzony logami — bierzemy od pierwszej '{'."""
    start = output.find("{")
    if start < 0:
        return {}
    try:
        return json.loads(output[start:])
    except json.JSONDecodeError:
        return {}


def extract_source(url: str) -> str:
    """Otwiera URL w sesji OpenCLI i składa markdown (do MAX_CHUNKS porcji)."""
    _run(["open", url, "--window", "background"])
    _run(["wait", "time", "5"])
    chunks, start, seen = [], 0, set()
    for _ in range(MAX_CHUNKS):
        data = _json_from(_run(["extract", "--chunk-size", str(CHUNK_CHARS),
                                "--start", str(start)]))
        content = data.get("content") or ""
        if content:
            chunks.append(content)
        nxt = data.get("next_start_char")
        if not nxt or nxt in seen:
            break
        seen.add(nxt)
        start = int(nxt)
        _run(["scroll", "down"])
        _run(["wait", "time", "3"])
    return "\n".join(chunks)


def close_session() -> None:
    try:
        _run(["close"])
    except Exception as exc:  # zwolnienie leasingu zakładki ≠ błąd krytyczny
        print(f"[facebook] nie udało się zwolnić sesji: {exc}", file=sys.stderr)


# ------------------------------------------------------------------ zbieranie

def collect(keywords: list[str], sources: list[str], out_dir: Path,
            extract=None) -> tuple[list[dict], list[str], int]:
    """Źródło po źródle; awaria jednego nie przerywa reszty."""
    extract = extract or extract_source
    keywords = [k.lower() for k in keywords]
    items, errors, seen, ok_sources = [], [], set(), 0
    for index, url in enumerate(sources, 1):
        try:
            markdown = extract(url)
        except Exception as exc:
            print(f"[facebook] pomijam źródło ({exc})", file=sys.stderr)
            errors.append(f"{url}: {exc}")
            continue
        if not markdown.strip():
            errors.append(f"{url}: brak treści w DOM (grupa wymaga członkostwa?)")
            continue
        ok_sources += 1
        stamp = datetime.now(timezone.utc).isoformat()
        (out_dir / f"facebook-raw-{index}.md").write_text(
            f"<!-- source: {url}\n     pobrano: {stamp} -->\n\n" + markdown,
            encoding="utf-8")
        label = _label_from_markdown(markdown, url)
        found = posts_from_markdown(markdown, keywords, label)
        new = 0
        for item in found:
            key = _norm_url(item["url"]) + "|" + item["snippet"][:60].lower()
            if key in seen:
                continue
            seen.add(key)
            items.append(item)
            new += 1
        if not found:
            links = post_permalinks(markdown)
            if links:
                errors.append(f"{url}: {len(links)} permalinków, 0 trafień na słowa "
                              f"kluczowe (config.json: facebook.keywords)")
            else:
                errors.append(f"{url}: brak permalinków do wpisów — układ FB do "
                              f"dostrojenia (surowy markdown: facebook-raw-{index}.md)")
        print(f"[facebook] {new} nowych wpisów: {label}", file=sys.stderr)
    items.sort(key=lambda i: i["published"], reverse=True)
    return items, errors, ok_sources


def build_payload(sources: list[str], items: list[dict], errors: list[str],
                  status: str = "ok") -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "backend": f"{OPENCLI} browser (sesja {SESSION}, krok wyłącznie lokalny)",
        "sources": list(sources),
        "errors": list(errors),
        "items": items,
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Treści z Facebooka przez lokalną sesję OpenCLI (grupy/strony).")
    ap.add_argument("--date", help="Dzień raportu (ISO, domyślnie dziś)")
    ap.add_argument("--out-dir", help="Katalog wyjściowy (domyślnie data/raw/<dzień>)")
    ap.add_argument("--source", action="append",
                    help="URL grupy/strony (można powtórzyć; domyślnie config.json)")
    args = ap.parse_args()

    cfg = load_config()
    fb_cfg = cfg.get("facebook") or {}
    sources = args.source or list(fb_cfg.get("sources") or [])
    # FB dopasowujemy węziej niż media: nazwa gminy występuje w samym chrome grupy,
    # więc domyślnie bierzemy facebook.keywords, a globalne tylko jako fallback.
    keywords = list(fb_cfg.get("keywords") or cfg.get("keywords") or [])
    day = resolve_date(args.date)
    out_dir = Path(args.out_dir) if args.out_dir else raw_dir(day)
    out_dir.mkdir(parents=True, exist_ok=True)

    if not sources:
        print("[facebook] brak źródeł (config.json: facebook.sources)", file=sys.stderr)
        payload = build_payload([], [], ["brak źródeł"], status="skipped")
    elif opencli_bin() is None:
        print(f"[facebook] brak {OPENCLI} w PATH — pomijam (krok lokalny)",
              file=sys.stderr)
        payload = build_payload(sources, [], [f"{OPENCLI}: brak w PATH"],
                                status="skipped")
    else:
        try:
            items, errors, ok_sources = collect(keywords, sources, out_dir)
        finally:
            close_session()
        payload = build_payload(sources, items, errors,
                                status="ok" if ok_sources else "error")

    (out_dir / "facebook.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[facebook] {payload['status']}: {len(payload['items'])} wpisów "
          f"-> {out_dir / 'facebook.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

