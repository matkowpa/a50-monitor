"""Krok 1c: wyszukiwanie semantyczne przez agent-reach (Exa MCP przez mcporter).

Użycie:
    python scripts/agent_reach.py [--date YYYY-MM-DD] [--out-dir PATH] [--query TEXT]

Zapytania bierzemy z config.json (agent_reach.queries); każde trafia do
semantycznej wyszukiwarki Exa wystawionej jako serwer MCP (definicja serwera
w config/mcporter.json, bez klucza API). Wyjście: data/raw/<dzień>/agent_reach.json
w tym samym formacie co feeds.json ({"items": [...]}) — assess.py czyta oba
pliki tą samą funkcją.

Brak mcporter w PATH (np. runner bez Node) nie jest błędem: skrypt zapisuje
payload ze statusem "skipped" i kończy się kodem 0 — dodatkowe źródło nie może
wywalić dziennego pipeline'u.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import REPO_ROOT, load_config, raw_dir, resolve_date  # noqa: E402

MCPORTER = "mcporter"
MCPORTER_CONFIG = REPO_ROOT / "config" / "mcporter.json"
TOOL = "exa.web_search_exa"
OBJECTIVE = (
    "Zbierz najnowsze, wiarygodne dowody o przebiegu autostrady A50 (Obwodnicy "
    "Aglomeracji Warszawskiej), zwłaszcza o wariantach przecinających gminę "
    "Sobienie-Jeziory: oficjalne komunikaty GDDKiA, uchwały i stanowiska "
    "samorządów oraz artykuły prasowe. Pomiń treści niezwiązane z A50/OAW."
)
CALL_TIMEOUT_S = 180

RECORDS = re.compile(r"(?m)^---\s*$")
FIELDS = {
    "title": re.compile(r"(?m)^Title:\s*(.*)$"),
    "url": re.compile(r"(?m)^URL:\s*(\S+)$"),
    "published": re.compile(r"(?m)^Published:\s*(\S+)$"),
}
ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}")


def keyword_matches(text: str, keywords: list[str]) -> bool:
    low = text.lower()
    return any(k in low for k in keywords)


def _field(pattern: re.Pattern, record: str) -> str:
    match = pattern.search(record)
    return match.group(1).strip() if match else ""


def _clean(text: str) -> str:
    """Skleja fragmenty 'Highlights' (Exa rozdziela je liniami '...')."""
    text = re.sub(r"(?m)^\.\.\.$", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()


def parse_results(text: str) -> list[dict]:
    """Tekst odpowiedzi Exa → elementy {title,url,source,published,snippet}."""
    items = []
    for record in RECORDS.split(text or ""):
        url = _field(FIELDS["url"], record)
        title = _clean(_field(FIELDS["title"], record))
        if not url or not title:
            continue
        parts = record.split("Highlights:", 1)
        snippet = _clean(parts[1]) if len(parts) == 2 else ""
        published = _field(FIELDS["published"], record)
        published = published[:10] if ISO_DAY.match(published) else ""
        items.append({
            "title": title,
            "url": url,
            "source": "exa",
            "published": published,
            "snippet": snippet[:400],
        })
    return items


def mcporter_bin() -> str | None:
    return shutil.which(MCPORTER)


def run_search(query: str, num_results: int) -> str:
    """Jedno zapytanie do Exa przez mcporter; zwraca tekst odpowiedzi."""
    cmd = [
        mcporter_bin() or MCPORTER, "call", TOOL,
        f"query={query}", f"objective={OBJECTIVE}", f"numResults={num_results}",
        "--config", str(MCPORTER_CONFIG),
        "--output", "json", "--timeout", "120000",
    ]
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=CALL_TIMEOUT_S,
                          cwd=str(REPO_ROOT), env=env)
    if proc.returncode != 0:
        raise RuntimeError(f"{MCPORTER} exit {proc.returncode}: "
                           f"{(proc.stderr or '').strip()[:200]}")
    data = json.loads(proc.stdout)
    blocks = [b.get("text") or "" for b in data.get("content") or []
              if isinstance(b, dict) and b.get("type") == "text"]
    return "\n".join(blocks)


def collect_items(cfg: dict, queries: list[str], num_results: int,
                  search=None) -> tuple[list[dict], list[str]]:
    """Zapytanie po zapytaniu; awaria jednego nie przerywa zbierania."""
    search = search or run_search
    keywords = [k.lower() for k in cfg.get("keywords", [])]
    items, errors, seen = [], [], set()
    for query in queries:
        try:
            text = search(query, num_results)
        except Exception as exc:  # pojedyncze zapytanie ≠ błąd krytyczny
            print(f"[agent-reach] pomijam zapytanie ({exc})", file=sys.stderr)
            errors.append(f"{query}: {exc}")
            continue
        found = 0
        for item in parse_results(text):
            key = item["url"].split("?")[0].rstrip("/").lower()
            if key in seen:
                continue
            if not keyword_matches(f"{item['title']} {item['snippet']} {item['url']}",
                                   keywords):
                continue
            seen.add(key)
            items.append(item)
            found += 1
        print(f"[agent-reach] {found} nowych elementów: {query[:60]}", file=sys.stderr)
    items.sort(key=lambda i: i["published"], reverse=True)
    return items, errors


def build_payload(queries: list[str], items: list[dict], errors: list[str],
                  status: str = "ok") -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "backend": f"agent-reach/{TOOL} via {MCPORTER}",
        "queries": list(queries),
        "errors": list(errors),
        "items": items,
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Wyszukiwanie semantyczne agent-reach (Exa) dla monitoringu A50.")
    ap.add_argument("--date", help="Dzień raportu (ISO, domyślnie dziś)")
    ap.add_argument("--out-dir", help="Katalog wyjściowy (domyślnie data/raw/<dzień>)")
    ap.add_argument("--query", action="append",
                    help="Zapytanie (można powtórzyć; domyślnie config.json)")
    args = ap.parse_args()

    cfg = load_config()
    reach_cfg = cfg.get("agent_reach") or {}
    queries = args.query or list(reach_cfg.get("queries") or [])
    num_results = int(reach_cfg.get("results_per_query", 8))
    day = resolve_date(args.date)
    out_dir = Path(args.out_dir) if args.out_dir else raw_dir(day)

    if not queries:
        print("[agent-reach] brak zapytań (config.json: agent_reach.queries)",
              file=sys.stderr)
        payload = build_payload([], [], ["brak zapytań"], status="skipped")
    elif mcporter_bin() is None:
        print(f"[agent-reach] brak {MCPORTER} w PATH — pomijam wyszukiwanie",
              file=sys.stderr)
        payload = build_payload(queries, [], [f"{MCPORTER}: brak w PATH"],
                                status="skipped")
    else:
        items, errors = collect_items(cfg, queries, num_results)
        payload = build_payload(queries, items, errors)

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "agent_reach.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[agent-reach] {payload['status']}: {len(payload['items'])} elementów "
          f"-> {out_dir / 'agent_reach.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

