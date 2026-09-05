"""Krok 2: rubryka oceny → OpenRouter → assessment + update scores.json.

Użycie:
    python scripts/assess.py [--date YYYY-MM-DD]

Czyta data/raw/<dzień>/report.json (silnik) i feeds.json (RSS),
buduje prompt z rubryką PL, wywołuje OpenRouter i zapisuje:
  - data/assessments/<dzień>.json  (pełny assessment)
  - data/scores.json               (historia, idempotentny upsert)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (CONFIDENCE_LEVELS, Evidence, assessments_dir, load_config,  # noqa: E402
                    load_scores, raw_dir, resolve_date, upsert_entry)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


# ---------------------------------------------------------------- ekstrakcja

def _clean(text) -> str:
    """Silnik potrafi zwracać literał 'None' w polach tekstowych."""
    if text is None:
        return ""
    text = str(text).strip()
    return "" if text.lower() == "none" else text


def _norm_url(url: str) -> str:
    return url.split("?")[0].rstrip("/").lower()


def _keyword_matches(text: str, keywords: list[str]) -> bool:
    low = text.lower()
    return any(k in low for k in keywords)


def extract_engine_items(raw: dict, cfg: dict) -> list[Evidence]:
    keywords = [k.lower() for k in cfg.get("keywords", [])]
    out, seen = [], set()
    for cand in raw.get("ranked_candidates") or []:
        if not isinstance(cand, dict):
            continue
        url = _clean(cand.get("url"))
        title = _clean(cand.get("title"))
        if not url or _norm_url(url) in seen:
            continue
        snippet = _clean(cand.get("snippet"))
        if not snippet:
            for si in cand.get("source_items") or []:
                snippet = _clean(si.get("body")) or _clean(si.get("snippet"))
                if snippet:
                    break
        published = ""
        for si in cand.get("source_items") or []:
            published = _clean(si.get("published_at"))[:10] or published
            if published:
                break
        source = _clean(cand.get("source")) or ",".join(cand.get("sources") or []) or "web"
        if not _keyword_matches(f"{title} {snippet} {url}", keywords):
            continue
        seen.add(_norm_url(url))
        out.append(Evidence(title=title or "(bez tytułu)", url=url, source=source,
                            published=published, snippet=snippet[:400]))
    out.sort(key=lambda e: e.published, reverse=True)
    return out


def extract_feed_items(feeds: dict, cfg: dict) -> list[Evidence]:
    keywords = [k.lower() for k in cfg.get("keywords", [])]
    out, seen = [], set()
    for item in feeds.get("items") or []:
        url = _clean(item.get("url"))
        if not url or _norm_url(url) in seen:
            continue
        title = _clean(item.get("title"))
        snippet = _clean(item.get("snippet"))
        if not _keyword_matches(f"{title} {snippet} {url}", keywords):
            continue
        seen.add(_norm_url(url))
        out.append(Evidence(title=title or "(bez tytułu)", url=url,
                            source=_clean(item.get("source")) or "rss",
                            published=_clean(item.get("published"))[:10],
                            snippet=snippet[:400]))
    out.sort(key=lambda e: e.published, reverse=True)
    return out


def merge_evidence(*groups: list[Evidence], cap: int) -> list[Evidence]:
    seen, merged = set(), []
    for group in groups:
        for ev in group:
            if _norm_url(ev.url) in seen:
                continue
            seen.add(_norm_url(ev.url))
            merged.append(ev)
    merged.sort(key=lambda e: e.published, reverse=True)
    return merged[:cap]


# ------------------------------------------------------------------- prompt

def build_prompt(evidence: list[Evidence], prev_entry: dict | None, cfg: dict) -> str:
    lines = [
        "Jesteś analitykiem oceniającym prawdopodobieństwo, że finalny przebieg "
        "planowanej południowej obwodnicy autostradowej Warszawy (A50) przetnie "
        f"teren gminy {cfg.get('focus', 'Sobienie-Jeziory')} "
        "(woj. mazowieckie, powiat otwocki).",
        "",
        "DOWODY z ostatnich 30 dni (media, Reddit, YouTube, RSS):",
    ]
    for i, ev in enumerate(evidence, 1):
        lines.append(f"[{i}] {ev.title} | {ev.source} | {ev.published or 'b.d.'} | "
                     f"{ev.url} | {ev.snippet[:200]}")
    lines.append("")
    if prev_entry:
        lines.append(f"POPRZEDNIA OCENA ({prev_entry.get('date')}): "
                     f"score={prev_entry.get('score')}, "
                     f"confidence={prev_entry.get('confidence')}. "
                     f"Uzasadnienie: {prev_entry.get('rationale', '')[:600]}")
    else:
        lines.append("POPRZEDNIA OCENA: brak (pierwsza ocena).")
    lines.append("""
RUBRYKA:
- score 0-100 = Twoja ocena prawdopodobieństwa (w %), że finalny przebieg A50 przetnie teren gminy Sobienie-Jeziory.
- Wagi dowodów (od najsilniejszych): oficjalne komunikaty GDDKiA / ministerstw / rządu > uchwały i stanowiska samorządów (gminnych, powiatowych, marszałkowskich) > główne media ogólnopolskie > media lokalne > social media / sentyment.
- Kluczowe ogniwo: wariant przebiegu. Oficjalne potwierdzenie wariantu omijającego gminę => score niski. Oficjalny proces wskazujący wariant przez gminę (studium, raport OOŚ, decyzja środowiskowa, przetarg) => score wyższy.
- Dowody sprzeczne lub nieliczne => obniż confidence i trzymaj score blisko poprzedniego.
- Brak nowych, istotnych dowodów => score = poprzedni, confidence = "niska", wyraźnie zaznacz brak nowych sygnałów.
- KAŻDY claim w key_findings MUSI mieć evidence_urls wyłącznie z listy dowodów powyżej. Nie wymyślaj URL-i.
- Pisz po polsku.

ODPOWIEDŹ: wyłącznie JSON dokładnie wg schematu:
{"score": <int 0-100>, "confidence": "<niska|średnia|wysoka>", "summary": "<1-2 zdania>", "rationale": "<pełne uzasadnienie, 3-6 zdań>", "key_findings": [{"claim": "<...>", "evidence_urls": ["<url z dowodów>"]}]}""")
    return "\n".join(lines)


# --------------------------------------------------------------- OpenRouter

def call_openrouter(prompt: str, api_key: str, model: str, timeout: int = 180) -> str:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
        "max_tokens": 2000,
    }
    req = urllib.request.Request(
        os.environ.get("OPENROUTER_BASE_URL", OPENROUTER_URL),
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/a50-monitor",
            "X-Title": "a50-monitor",
        },
        method="POST",
    )
    last_err: Exception = RuntimeError("OpenRouter: brak prób")
    for delay in (0, 5, 15, 30):
        if delay:
            time.sleep(delay)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"]
            if not content:
                raise RuntimeError("OpenRouter: pusta odpowiedź")
            return content
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:500]
            if exc.code in (401, 402):
                raise RuntimeError(f"OpenRouter HTTP {exc.code}: {body}") from exc
            last_err = RuntimeError(f"OpenRouter HTTP {exc.code}: {body}")
        except Exception as exc:  # noqa: BLE001 — retry każdej awarii sieciowej
            last_err = exc
    raise last_err


# --------------------------------------------------------------- parsowanie

def parse_assessment(data: dict, prev_entry: dict | None) -> dict:
    score = int(round(float(data.get("score", 0))))
    score = max(0, min(100, score))
    confidence = data.get("confidence") or "niska"
    if confidence not in CONFIDENCE_LEVELS:
        confidence = "niska"
    prev_score = prev_entry.get("score") if prev_entry else None
    trend = (score - prev_score) if isinstance(prev_score, int) else 0
    findings = []
    for f in data.get("key_findings") or []:
        if isinstance(f, dict) and _clean(f.get("claim")):
            urls = [u for u in (f.get("evidence_urls") or []) if _clean(u)]
            findings.append({"claim": _clean(f["claim"]), "evidence_urls": urls})
    return {
        "score": score,
        "confidence": confidence,
        "summary": _clean(data.get("summary")) or "Brak opisu.",
        "rationale": _clean(data.get("rationale")) or "Brak uzasadnienia.",
        "key_findings": findings,
        "trend_vs_prev": trend,
    }


def prev_entry_before(entries: list[dict], day: str) -> dict | None:
    earlier = [e for e in entries if e.get("date", "") < day]
    return earlier[-1] if earlier else None


def no_evidence_entry(day: str, prev: dict | None, status: str) -> dict:
    if prev:
        summary = ("Brak nowych dowodów w podglądanych źródłach — utrzymuję "
                   f"poprzednią ocenę ({prev.get('score')}%).")
        score, confidence = prev.get("score"), "niska"
    else:
        summary = ("Brak danych w pierwszym uruchomieniu monitoringu. "
                   "Ocena neutralna do czasu napływu dowodów.")
        score, confidence = 50, "niska"
    trend = (score - prev.get("score")) if prev and isinstance(prev.get("score"), int) else 0
    return {
        "date": day, "score": score, "confidence": confidence,
        "summary": summary, "trend_vs_prev": trend,
        "rationale": summary,
        "key_findings": [], "evidence": [], "sources_found": 0,
        "engine_status": status, "assessment_path": "",
    }


# -------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="Ocena prawdopodobieństwa A50 przez Sobienie-Jeziory.")
    ap.add_argument("--date", help="Dzień raportu (ISO, domyślnie dziś)")
    args = ap.parse_args()

    day = resolve_date(args.date)
    cfg = load_config()
    raw_path = raw_dir(day) / "report.json"
    feeds_path = raw_dir(day) / "feeds.json"

    engine_items: list[Evidence] = []
    feed_items: list[Evidence] = []
    if raw_path.exists():
        with open(raw_path, encoding="utf-8") as f:
            engine_items = extract_engine_items(json.load(f), cfg)
    if feeds_path.exists():
        with open(feeds_path, encoding="utf-8") as f:
            feed_items = extract_feed_items(json.load(f), cfg)

    evidence = merge_evidence(engine_items, feed_items,
                              cap=int(cfg.get("max_evidence", 40)))
    prev = prev_entry_before(load_scores()["entries"], day)

    if evidence:
        prompt = build_prompt(evidence, prev, cfg)
        api_key = os.environ.get("OPENROUTER_API_KEY", "")
        if not api_key:
            print("[assess] FAIL: brak OPENROUTER_API_KEY w środowisku", file=sys.stderr)
            return 2
        model = cfg.get("openrouter_model", "google/gemini-2.5-flash")
        content = call_openrouter(prompt, api_key, model)
        assessment = parse_assessment(json.loads(content), prev)
        entry = {
            "date": day, **assessment,
            "evidence": [e.to_dict() for e in evidence[:15]],
            "sources_found": len(evidence),
            "engine_status": "ok",
        }
    else:
        status = "no-data" if not (raw_path.exists() or feeds_path.exists()) else "no-evidence"
        entry = no_evidence_entry(day, prev, status)

    entry["assessment_path"] = f"data/assessments/{day}.json"
    upsert_entry(entry)

    full = dict(entry)
    full["evidence"] = [e.to_dict() for e in evidence]
    assessments_dir().mkdir(parents=True, exist_ok=True)
    (assessments_dir() / f"{day}.json").write_text(
        json.dumps(full, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"[assess] {day}: score={entry['score']}% ({entry['confidence']}), "
          f"dowody={entry['sources_found']}, status={entry['engine_status']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

