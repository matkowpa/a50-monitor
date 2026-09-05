"""Wspólne narzędzia: config, ścieżki, Evidence, atomowe zapisy JSON."""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

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
