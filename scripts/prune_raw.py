"""Retencja surowych danych: `data/raw/` rośnie ~220 KB dziennie (~80 MB/rok).

Usuwamy tylko surowe zbiory z dawnych dni (feeds, agent-reach, official,
silnik). Zostają: `data/assessments/` (ślad decyzji — co model dostał i co
odpowiedział), `data/events.json`, `data/scores.json`, `data/official_state.json`.

Domyślnie dry-run: pokazuje, co by usunął. `--apply` faktycznie usuwa.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import REPO_ROOT  # noqa: E402

RAW_DIR = REPO_ROOT / "data" / "raw"


def old_days(raw: Path, keep_days: int, today: date | None = None) -> list[Path]:
    """Katalogi dni starsze niż `keep_days` (nazwa katalogu = data ISO)."""
    today = today or date.today()
    cutoff = today - timedelta(days=keep_days)
    out = []
    if not raw.exists():
        return out
    for entry in sorted(raw.iterdir()):
        if not entry.is_dir():
            continue
        try:
            day = date.fromisoformat(entry.name)
        except ValueError:
            continue          # nie-data w nazwie: nie ruszamy
        if day < cutoff:
            out.append(entry)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Retencja data/raw.")
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    targets = old_days(RAW_DIR, args.days)
    size = sum(f.stat().st_size for d in targets for f in d.rglob("*") if f.is_file())
    print(f"[prune] dni starsze niż {args.days}: {len(targets)} "
          f"({size / 1024:.0f} KB)")
    for d in targets:
        print("  ", d.name)
        if args.apply:
            shutil.rmtree(d)
    if targets and not args.apply:
        print("[prune] dry-run — dodaj --apply, żeby usunąć")
    return 0


if __name__ == "__main__":
    sys.exit(main())
