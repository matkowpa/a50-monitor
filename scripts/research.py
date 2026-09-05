"""Krok 1: uruchomienie silnika last30days i zapis surowego raportu dnia.

Użycie:
    python scripts/research.py [--date YYYY-MM-DD] [--mock] [--out-dir PATH]

Wyjście: data/raw/<dzień>/report.json (+ engine.log). Sukces oceniamy po
istnieniu pliku raportu, nie po kodzie wyjścia silnika — silnik zwraca 1,
gdy część źródeł nie odpowie, a raport i tak jest użyteczny.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import REPO_ROOT, load_config, raw_dir, resolve_date  # noqa: E402

ENGINE = REPO_ROOT / "skill" / "last30days" / "scripts" / "last30days.py"
TIMEOUT_S = 20 * 60


def run_engine(cfg: dict, day: str, out_dir: Path, mock: bool = False) -> tuple[Path, bool]:
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / "report.json"
    cmd = [
        sys.executable, str(ENGINE),
        cfg["topic"],
        "--emit=json", "--json-profile=raw",
        "--quick",
        "--days", str(cfg.get("lookback_days", 30)),
        "--search", cfg.get("search_sources", "web,reddit,youtube"),
        "--max-results", str(cfg.get("max_results", 30)),
        "--max-per-source", str(cfg.get("max_per_source", 10)),
        "--output", str(report_path),
        "--save-dir", str(out_dir),
    ]
    if mock:
        cmd.append("--mock")

    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=TIMEOUT_S, cwd=str(REPO_ROOT), env=env,
        )
        log = (proc.stdout or "")[-20000:] + "\n--- STDERR ---\n" + (proc.stderr or "")[-20000:]
        (out_dir / "engine.log").write_text(log, encoding="utf-8")
    except subprocess.TimeoutExpired:
        (out_dir / "engine.log").write_text("TIMEOUT po %d s" % TIMEOUT_S, encoding="utf-8")
        return report_path, False

    ok = report_path.exists() and report_path.stat().st_size > 100
    return report_path, ok


def main() -> int:
    ap = argparse.ArgumentParser(description="Uruchom silnik last30days dla tematu A50.")
    ap.add_argument("--date", help="Dzień raportu (ISO, domyślnie dziś)")
    ap.add_argument("--mock", action="store_true", help="Tryb mock silnika (bez sieci)")
    ap.add_argument("--out-dir", help="Katalog wyjściowy (domyślnie data/raw/<dzień>)")
    args = ap.parse_args()

    day = resolve_date(args.date)
    out_dir = Path(args.out_dir) if args.out_dir else raw_dir(day)
    cfg = load_config()
    report_path, ok = run_engine(cfg, day, out_dir, mock=args.mock)

    if ok:
        print(f"[research] OK: {report_path}")
        return 0
    print(f"[research] FAIL: silnik nie wygenerował raportu w {out_dir} "
          f"(szczegóły: {out_dir / 'engine.log'})")
    return 1


if __name__ == "__main__":
    sys.exit(main())
