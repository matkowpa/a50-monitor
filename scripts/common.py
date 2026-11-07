"""Wspólne narzędzia: config, ścieżki, Evidence, atomowe zapisy JSON."""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from datetime import date
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
