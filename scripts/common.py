"""Wspólne narzędzia: config, ścieżki, Evidence, atomowe zapisy JSON."""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent

CONFIDENCE_LEVELS = ("niska", "średnia", "wysoka")

# Dwa niezależne score'y dzienne: północna i południowa strona gminy
# (na północ / na południe od wsi Sobienie-Jeziory). Jedno źródło prawdy
# etykiet dla promptu (assess) i strony (build_site).
SCENARIOS = (
    ("north", "Północ gminy — na północ od wsi Sobienie-Jeziory"),
    ("south", "Południe gminy — na południe od wsi Sobienie-Jeziory"),
)


def load_config() -> dict:
    with open(REPO_ROOT / "config.json", encoding="utf-8") as f:
        return json.load(f)


def today_str() -> str:
    return date.today().isoformat()


def resolve_date(raw: str | None) -> str:
    return raw or today_str()


def is_fresh(published: str, day: str, max_age_days: int = 30) -> bool:
    """Okno świeżości: True, gdy dowód nadaje się do oceny dnia `day`.

    Odrzuca wyłącznie dowody z parsowalną datą publikacji starszą niż
    `max_age_days` od dnia raportu. Pusta/nieprawidłowa data (dowodu lub
    dnia) → True — nie odrzucamy tego, czego wieku nie da się zweryfikować;
    data z przyszłości → True (np. różnice stref czasowych).
    """
    try:
        pub = date.fromisoformat((published or "").strip()[:10])
        run = date.fromisoformat((day or "").strip()[:10])
    except ValueError:
        return True
    return (run - pub).days <= max_age_days


@dataclass
class Evidence:
    title: str
    url: str
    source: str
    published: str = ""
    snippet: str = ""
    stance: str = "neutral"

    def to_dict(self) -> dict:
        return asdict(self)


def scores_path() -> Path:
    return REPO_ROOT / "data" / "scores.json"


def raw_dir(day: str) -> Path:
    return REPO_ROOT / "data" / "raw" / day


def assessments_dir() -> Path:
    return REPO_ROOT / "data" / "assessments"


def load_scores(path: Path | None = None) -> dict:
    p = path or scores_path()
    if not p.exists():
        return {"entries": []}
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("entries", [])
    return data


def upsert_entry(entry: dict, path: Path | None = None) -> dict:
    """Idempotentny upsert wpisu dziennego po 'date' (rerun nie duplikuje)."""
    p = path or scores_path()
    data = load_scores(p)
    entries = [e for e in data["entries"] if e.get("date") != entry["date"]]
    entries.append(entry)
    entries.sort(key=lambda e: e.get("date", ""))
    data["entries"] = entries
    atomic_write_json(p, data)
    return data


def atomic_write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# --------------------------------------------------------------- waga dowodu
#
# Klasa dowodu decyduje, jak mocno może ruszyć score. Bez tego model zmieniał
# ocenę o 3 pkt na podstawie artykułu portalu, a przy braku nowych dowodów
# „utrzymywał" wartość tylko z dobrej woli promptu.
GRADES = ("official", "local_gov", "media", "social")

# Maksymalna zmiana score w jednym dniu (w punktach procentowych).
MAX_DELTA = {
    "official": 20,   # komunikat GDDKiA, DŚU, wniosek do RDOŚ, rozporządzenie
    "local_gov": 6,   # uchwała/stanowisko gminy, powiatu, sejmiku
    "media": 2,       # prasa, portale branżowe i lokalne
    "social": 1,      # Reddit, Facebook, YouTube, X
}

OFFICIAL_HOSTS = (
    "gov.pl", "sejm.gov.pl", "senat.gov.pl", "rdos.gov.pl",
    "isap.sejm.gov.pl", "gddkia.gov.pl", "monitorurzedowy.pl",
    "dziennikustaw.gov.pl", "bip.gov.pl",
)

SOCIAL_HOSTS = (
    "reddit.com", "facebook.com", "x.com", "twitter.com",
    "youtube.com", "youtu.be", "instagram.com", "linkedin.com",
    "tiktok.com", "wykop.pl",
)

LOCAL_GOV_TITLE = re.compile(
    r"uchwa[łl]|stanowisk|zarządzeni|protok[óo][łl]|sesj[ai]|rady gminy|rada gminy",
    re.IGNORECASE,
)


def url_host(url: str) -> str:
    try:
        host = (urlparse(url or "").netloc or "").lower().split(":")[0]
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def evidence_grade(url: str, title: str = "", source: str = "") -> str:
    """Klasyfikuje dowód: official / local_gov / media / social.

    Kolejność jest istotna: `bip.<media>.pl` to nadal BIP (local_gov), a
    `news.google.com` to wrapper — media, dopóki nie rozwiążemy linku.
    """
    host = url_host(url)
    if not host:
        return "media"
    if any(host == h or host.endswith("." + h) for h in SOCIAL_HOSTS):
        return "social"
    if any(host == h or host.endswith("." + h) for h in OFFICIAL_HOSTS):
        return "official"
    if host.startswith("bip.") or ".bip." in host:
        return "local_gov"
    if host.endswith(".pl") and LOCAL_GOV_TITLE.search(f"{title} {source}"):
        return "local_gov"
    return "media"


def best_grade(grades) -> str:
    """Najmocniejsza klasa w zbiorze (official > local_gov > media > social)."""
    present = [g for g in grades if g in GRADES]
    if not present:
        return "media"
    return min(present, key=GRADES.index)


def gate_score(score, prev_score, grade: str) -> tuple[int, bool]:
    """Przycina zmianę score do limitu klasy dowodu.

    Zwraca (score_po_bramce, przycięto). Brak poprzedniej oceny albo
    niepoprawna wartość → score bez zmian (pierwsza ocena).
    """
    try:
        score = int(score)
    except (TypeError, ValueError):
        score = 50
    score = max(0, min(100, score))
    if not isinstance(prev_score, int):
        return score, False
    limit = MAX_DELTA.get(grade, MAX_DELTA["media"])
    delta = score - prev_score
    if abs(delta) <= limit:
        return score, False
    gated = prev_score + (limit if delta > 0 else -limit)
    return max(0, min(100, gated)), True


# ------------------------------------------------------- jakość dowodów: tytuł
JUNK_TITLE_RE = re.compile(r"^(?:[–—\-_*.·\s]{3,}|\d+\.\s|_+\w)")


def is_junk_title(title: str, min_len: int = 12, min_letters: int = 8) -> bool:
    """Tytuł, który nie jest tytułem: ciąg myślników, numerowana lista z PDF-a,
    samo „A50”, puste pole. Takie dowody trafiały do promptu i do uzasadnień."""
    t = (title or "").strip()
    if len(t) < min_len:
        return True
    if sum(1 for ch in t if ch.isalpha()) < min_letters:
        return True
    return bool(JUNK_TITLE_RE.match(t))


# ---------------------------------------------- jakość dowodów: data publikacji
MONTHS_PL = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
    "czerwca": 6, "lipca": 7, "sierpnia": 8, "września": 9,
    "października": 10, "listopada": 11, "grudnia": 12,
}
DATE_ISO_RE = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
DATE_DOTTED_RE = re.compile(r"\b(\d{1,2})[.\-](\d{1,2})[.\-](20\d{2})\b")
DATE_PL_RE = re.compile(
    r"\b(\d{1,2})\s+(" + "|".join(MONTHS_PL) + r")\s+(20\d{2})\b", re.IGNORECASE)


def parse_date_hint(text: str, today: str | None = None) -> str:
    """Data publikacji z treści dowodu (tytuł/skrót), gdy RSS jej nie podał.

    44% dowodów w tym repo nie miało daty, więc omijały okno świeżości.
    Akceptujemy wyłącznie daty kalendarzowo poprawne, nie z przyszłości
    (poza marginesem jednego dnia) i nie starsze niż 10 lat.
    """
    if not text:
        return ""
    limit = None
    try:
        limit = date.fromisoformat((today or date.today().isoformat())[:10])
    except ValueError:
        limit = date.today()
    candidates = []
    for pattern, order in ((DATE_ISO_RE, "ymd"), (DATE_DOTTED_RE, "dmy")):
        for m in pattern.finditer(text):
            a, b, c = m.groups()
            try:
                if order == "ymd":
                    candidates.append(date(int(a), int(b), int(c)))
                else:
                    candidates.append(date(int(c), int(b), int(a)))
            except ValueError:
                continue
    for m in DATE_PL_RE.finditer(text):
        day_, month_, year_ = m.groups()
        try:
            candidates.append(date(int(year_), MONTHS_PL[month_.lower()], int(day_)))
        except (ValueError, KeyError):
            continue
    for d in sorted(candidates):
        if d > limit + timedelta(days=1):
            continue
        if limit.year - d.year > 10:
            continue
        return d.isoformat()
    return ""


# ------------------------------------------------- skala prawdopodobieństwa
# Jedno miejsce, w którym zdefiniowana jest skala. Etykieta na stronie
# (badge przy score), legenda skali i archiwum muszą brać ją stąd — wcześniej
# 38% miało etykietę „umiarkowane” przy zegarze, a w archiwum stało obok
# „(niska)”, bo w nawiasie drukowano pewność oceny, nie przedział.
SCORE_BANDS = (
    (75, "wysokie", "R", (75, 100)),
    (50, "podwyższone", "O", (50, 74)),
    (25, "umiarkowane", "Y", (25, 49)),
    (0, "niskie", "G", (0, 24)),
)


def score_band(score) -> str:
    """Etykieta przedziału prawdopodobieństwa dla score 0-100."""
    try:
        value = int(round(float(score)))
    except (TypeError, ValueError):
        value = 0
    for threshold, label, _css, _range in SCORE_BANDS:
        if value >= threshold:
            return label
    return SCORE_BANDS[-1][1]


def score_band_range(label: str) -> str:
    for _threshold, name, _css, (low, high) in SCORE_BANDS:
        if name == label:
            return f"{low}–{high}%"
    return ""
