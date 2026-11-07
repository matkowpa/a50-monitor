"""Dziennik zdarzeń materialnych: co realnie zmienia obraz inwestycji A50/OAW.

Codzienny score jest funkcją procesu inwestycyjnego, a ten zmienia się
skokowo: wybór wariantu, wniosek do RDOŚ, decyzja środowiskowa, przetarg,
uchwała rady gminy. Ten moduł zamienia takie dowody w rekordy w
`data/events.json`, żeby na stronie było widać nie tylko „38%”, ale i
„38% od 4 listopada, bo …”.

Zdarzenie materialne = dowód klasy official albo local_gov (gov.pl, RDOŚ,
Sejm, BIP, uchwała) albo wypowiedź medialna o zdarzeniu procesowym
(wybór wariantu, wniosek, DŚU, przetarg) — ta druga oznaczona jako
niepotwierdzona, bo media wyprzedzają komunikaty.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import REPO_ROOT, Evidence, atomic_write_json, evidence_grade, url_host  # noqa: E402

EVENTS_PATH = REPO_ROOT / "data" / "events.json"

# Kolejność ma znaczenie: pierwszy pasujący wzorzec wygrywa.
EVENT_TYPES = (
    ("decision", r"decyzj\w*\s+środowiskow|decyzj\w*\s+o\s+środowisku|decyzj[ai]\s+DŚU|\bDŚU\b"),
    ("application", r"wniosek\s+do\s+RDOŚ|złożeni\w+\s+wniosku|wniosek\s+o\s+wydanie\s+decyzji"),
    ("variant", r"wyb[óo]r\s+wariantu|wskazan\w+\s+wariant|preferowan\w+\s+wariant|ostateczn\w+\s+przebieg|wariant\w*\s+przebiegu"),
    ("tender", r"przetarg|postępowani\w+\s+przetargow|składani\w+\s+ofert|wyb[óo]r\s+wykonawcy"),
    ("study", r"\bSTEŚ\b|studium\s+techniczno-ekonomiczno-środowiskow"),
    ("consultation", r"konsultacj|spotkani\w+\s+informacyjn|wyłożeni|akcja\s+informacyjna"),
    ("council", r"uchwa[łl]|stanowisk\w+\s+(rady|gminy|powiatu)|sprzeciw|protest"),
    ("statement", r"komunikat|oświadczeni|stanowisko\s+GDDKiA"),
)

# Typy, o których media mogą donieść przed oficjalnym komunikatem.
MEDIA_ALLOWED = {"decision", "application", "variant", "tender"}

# Zdarzenie musi dotyczyć tej inwestycji — inaczej „uchwała w sprawie nazw
# ulic” z dowolnej gminy wchodzi do dziennika jako zdarzenie A50.
TOPIC_RE = re.compile(r"\bA50\b|obwodnic|OAW\b|aglomeracji warszawskiej", re.IGNORECASE)

# Stare dokumenty potrafią wracać w wynikach wyszukiwania — dziennik ma
# pokazywać proces, nie archiwum.
EVENT_MAX_AGE_DAYS = 180

TYPE_LABELS = {
    "decision": "decyzja środowiskowa",
    "application": "wniosek do RDOŚ",
    "variant": "wariant przebiegu",
    "tender": "przetarg",
    "study": "STEŚ",
    "consultation": "konsultacje",
    "council": "uchwała / stanowisko",
    "statement": "komunikat",
}


def classify(title: str, snippet: str = "") -> str | None:
    """Typ zdarzenia z tytułu i skrótu, albo None gdy to nie zdarzenie."""
    text = f"{title} {snippet}"
    for name, pattern in EVENT_TYPES:
        if re.search(pattern, text, re.IGNORECASE):
            return name
    return None


def is_material(grade: str, event_type: str) -> bool:
    if grade in ("official", "local_gov"):
        return True
    return grade == "media" and event_type in MEDIA_ALLOWED


def event_id(url: str) -> str:
    return hashlib.sha1(url.strip().lower().encode("utf-8")).hexdigest()[:12]


JUNK_TITLE_RE = re.compile(r"^(?:[–—\-]{3,}|\d+\.\s|_\w)")
YEAR_RE = re.compile(r"\b(20\d{2})\b")


def usable_title(title: str) -> bool:
    """Tytuł nadaje się do dziennika: ma sens, nie jest listą numerowaną
    ani ciągiem myślników wyciągniętym z PDF-a."""
    t = (title or "").strip()
    letters = sum(1 for ch in t if ch.isalpha())
    return len(t) >= 30 and letters >= 12 and not JUNK_TITLE_RE.match(t)


def extract_events(evidence: list[Evidence], day: str,
                   max_age_days: int = EVENT_MAX_AGE_DAYS) -> list[dict]:
    """Zamienia dowody w zdarzenia materialne (bez duplikatów w obrębie dnia).

    Odrzuca dowody bez związku z A50/OAW oraz takie, których data publikacji
    jest starsza niż `max_age_days` — stare uchwały i artykuły wracają w
    wynikach wyszukiwania i nie są zdarzeniem dnia.
    """
    out, seen = [], set()
    try:
        run_day = date.fromisoformat(day[:10])
    except ValueError:
        run_day = None
    for ev in evidence:
        if not usable_title(ev.title):
            continue
        grade = evidence_grade(ev.url, ev.title, ev.source)
        # Temat musi być w tytule; wyjątkiem są dokumenty urzędowe, których
        # tytuł bywa skrótowy („Interpelacja nr 21813”) — tam wystarczy treść.
        if not TOPIC_RE.search(ev.title):
            if not (grade == "official" and TOPIC_RE.search(ev.snippet)):
                continue
        if run_day is not None:
            if ev.published:
                try:
                    pub = date.fromisoformat(ev.published[:10])
                    if (run_day - pub).days > max_age_days:
                        continue
                except ValueError:
                    pass
            elif any(int(y) < run_day.year for y in YEAR_RE.findall(ev.title)):
                # Dokument bez daty publikacji, ale z rokiem w tytule —
                # archiwalna uchwała, nie zdarzenie dnia.
                continue
        grade = evidence_grade(ev.url, ev.title, ev.source)
        etype = classify(ev.title, ev.snippet)
        if etype is None or not is_material(grade, etype):
            continue
        eid = event_id(ev.url)
        if eid in seen:
            continue
        seen.add(eid)
        out.append({
            "id": eid,
            "date": day,
            "type": etype,
            "type_label": TYPE_LABELS[etype],
            "title": ev.title,
            "url": ev.url,
            "host": url_host(ev.url),
            "grade": grade,
            "confirmed": grade in ("official", "local_gov"),
            "snippet": ev.snippet[:240],
        })
    out.sort(key=lambda e: (not e["confirmed"], e["type"], e["title"]))
    return out


def load_events(path: Path | None = None) -> dict:
    p = path or EVENTS_PATH
    if not p.exists():
        return {"events": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"events": []}
    data.setdefault("events", [])
    return data


def title_key(event: dict) -> str:
    """Ten sam dokument wraca pod różnymi URL-ami (wrapper Google News vs
    kanoniczny link) — drugi klucz deduplikacji po tytule i typie."""
    t = re.sub(r"[^a-z0-9ąćęłńóśźż ]", "", (event.get("title") or "").lower())
    return f"{event.get('type')}|{' '.join(t.split())[:90]}"


def merge_events(new_events: list[dict], path: Path | None = None) -> dict:
    """Dopisuje zdarzenia, deduplikując po id (URL) i po (typ, tytuł)."""
    p = path or EVENTS_PATH
    data = load_events(p)
    known = {e.get("id") for e in data["events"]}
    known_titles = {title_key(e) for e in data["events"]}
    added = []
    for e in new_events:
        if e.get("id") in known or title_key(e) in known_titles:
            continue
        added.append(e)
        known_titles.add(title_key(e))
    if added:
        data["events"].extend(added)
        data["events"].sort(key=lambda e: (e.get("date", ""), e.get("id", "")))
        atomic_write_json(p, data)
    return data


def last_events(limit: int = 6, path: Path | None = None) -> list[dict]:
    data = load_events(path)
    return sorted(data["events"], key=lambda e: e.get("date", ""), reverse=True)[:limit]


if __name__ == "__main__":  # ręczne dopisanie zdarzenia z CLI
    print(json.dumps(last_events(20), ensure_ascii=False, indent=2))
