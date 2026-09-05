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
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (CONFIDENCE_LEVELS, SCENARIOS, Evidence, assessments_dir,  # noqa: E402
                    load_config, load_scores, raw_dir, resolve_date, upsert_entry)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

ANALIZY_DIR = Path(__file__).resolve().parent.parent / "analizy"
ANALIZY_CAP = 8000


def load_analyses() -> str:
    """Tekst analiz eksperckich (analizy/*.md) — punkt wyjścia ocen.

    Analizy żyją w repo, więc prompt zawsze ma ich aktualne ustalenia
    (bez duplikowania treści w kodzie). Każdy plik skracany do ANALIZY_CAP.
    """
    parts = []
    for md in sorted(ANALIZY_DIR.glob("*.md")):
        text = md.read_text(encoding="utf-8-sig").strip()
        if len(text) > ANALIZY_CAP:
            text = text[:ANALIZY_CAP] + "\n…(skrócono)"
        parts.append(f"### {md.stem}\n{text}")
    return "\n\n".join(parts)


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

def build_prompt(evidence: list[Evidence], prev_entry: dict | None, cfg: dict,
                 analyses: str = "") -> str:
    prev_scores = ""
    if prev_entry:
        prev_s = prev_entry.get("scores") or {}
        prev_scores = (
            f"score północ={prev_s.get('north', {}).get('score')}, "
            f"południe={prev_s.get('south', {}).get('score')}, "
            f"confidence północ={prev_s.get('north', {}).get('confidence')}, "
            f"południe={prev_s.get('south', {}).get('confidence')}"
        )
    lines = [
        "Jesteś analitykiem oceniającym planowaną południową obwodnicę "
        "autostradową Warszawy (A50/OAW) i szansę, że jej finalny przebieg "
        f"przetnie teren gminy {cfg.get('focus', 'Sobienie-Jeziory')} "
        "(woj. mazowieckie, powiat otwocki). Ocenasz DWA niezależne "
        "scenariusze przebiegu:",
        "1) SCENARIUSZ PÓŁNOC: trasa prowadzi przez północną część gminy — "
        "na północ od wsi Sobienie-Jeziory (pas między wsią a doliną Wisły: "
        "tereny zalewowe, Natura 2000 Dolina Środkowej Wisły), łącznie "
        "z nowym śladem przez środkowo-północny pas gminy.",
        "2) SCENARIUSZ POŁUDNIE: trasa prowadzi przez południową część "
        "gminy — na południe od wsi Sobienie-Jeziory (rejon m.in. "
        "Śniadkowa Dolnego i Górnego; otwarty płaskowyż rolniczy ku "
        "południowej granicy gminy).",
        "Kontekst geograficzny: wieś Sobienie-Jeziory leży na drogach "
        "wojewódzkich 801 i 739, w środkowej części gminy. DK50 biegnie "
        "NA PÓŁNOC od gminy — od Góry Kalwarii (nowy most Nadwiślańskiego "
        "Urzecza) przez Ostrówek i Piotrowice (gmina Karczew) oraz Tabor "
        "i Regut (gmina Celestynów) do Kołbiela — i nie przecina gminy. "
        "Północną granicę gminy wyznacza dolina Wisły (tereny zalewowe, "
        "Natura 2000); na południe od wsi leżą m.in. Śniadków Dolny "
        "i Górny. UWAGA: teksty analiz eksperckich poniżej zakładają "
        "starszy, nieaktualny przebieg DK50 przez gminę — w razie "
        "sprzeczności stosuj niniejszy kontekst.",
        "",
        "DOWODY z ostatnich 30 dni (media, Reddit, YouTube, RSS):",
    ]
    for i, ev in enumerate(evidence, 1):
        lines.append(f"[{i}] {ev.title} | {ev.source} | {ev.published or 'b.d.'} | "
                     f"{ev.url} | {ev.snippet[:200]}")
    lines.append("")
    baseline = cfg.get("baseline_scores") or {}
    lines.append("PUNKT WYJŚCIA — ANALIZY EKSPERCKIE:")
    lines.append(f"- Score bazowy PÓŁNOC: {baseline.get('north', '?')}% "
                 "(analiza nr 2: obwodnica wsi po stronie północnej ~10–12% "
                 "+ nowy ślad przez środkowo-północny pas gminy ~30–35%).")
    lines.append(f"- Score bazowy POŁUDNIE: {baseline.get('south', '?')}% "
                 "(analiza nr 2: korytarz DK50 z obwodnicą wsi po stronie "
                 "południowej ~25–30%).")
    if analyses:
        lines.append("- Pełne teksty analiz:")
        lines.append(analyses)
    lines.append("")
    if prev_entry:
        lines.append(f"POPRZEDNIA OCENA ({prev_entry.get('date')}): "
                     f"{prev_scores}. "
                     f"Uzasadnienie północ: {(prev_s.get('north') or {}).get('rationale', '')[:400]}. "
                     f"Uzasadnienie południe: {(prev_s.get('south') or {}).get('rationale', '')[:400]}")
    else:
        lines.append("POPRZEDNIA OCENA: brak (pierwsza ocena).")
    lines.append("""
RUBRYKA (osobno dla PÓŁNOC i POŁUDNIE):
- score 0-100 = Twoja ocena prawdopodobieństwa (w %), że finalny przebieg A50 przetnie DANĄ stronę gminy Sobienie-Jeziory (północną lub południową względem wsi Sobienie-Jeziory).
- Punktem wyjścia są score bazowe z analiz eksperckich (oraz poprzednia ocena). Codzienne dowody modyfikują score; wnioski obu analiz są kotwicą — wyraźne odstępstwo od nich wymaga mocnych, oficjalnych dowodów (komunikat GDDKiA, wskazanie wariantu, DŚU, przetarg). Brak nowych dowodów → utrzymanie poprzednich wartości.
- Wagi dowodów (od najsilniejszych): oficjalne komunikaty GDDKiA / ministerstw / rządu > uchwały i stanowiska samorządów (gminnych, powiatowych, marszałkowskich) > główne media ogólnopolskie > media lokalne > social media / sentyment.
- Kluczowe ogniwo: wariant przebiegu. Oficjalne potwierdzenie wariantu omijającego gminę albo prowadzącego przez jej środek/inną stronę => score danego scenariusza niski. Oficjalny proces wskazujący korytarz przez daną stronę gminy (studium, raport OOŚ, decyzja środowiskowa, przetarg) => score tego scenariusza wyższy.
- Dowody mogą mówić o jednej stronie gminy i nic nie wnosić o drugiej — wtedy oceniaj samodzielnie geometrycznie (Wisła/Natura 2000 na północy, DK50 środkiem, otwarte tereny rolnicze na południu) i nisko ustaw confidence danej strony.
- Dowody sprzeczne lub nieliczne => obniż confidence i trzymaj score blisko poprzedniego.
- Brak nowych, istotnych dowodów => score = poprzedni (osobno dla północy i południa), confidence = "niska", wyraźnie zaznacz brak nowych sygnałów.
- KAŻDY claim w key_findings MUSI mieć evidence_urls wyłącznie z listy dowodów powyżej. Nie wymyślaj URL-i.
- Pisz po polsku.

ODPOWIEDŹ: wyłącznie JSON dokładnie wg schematu (bez ogrodzeń markdown; max 5 pozycji w key_findings per scenariusz; summary ≤ 2 zdania; rationale ≤ 4 zdania):
{"scores": {"north": {"score": <int 0-100>, "confidence": "<niska|średnia|wysoka>", "summary": "<1-2 zdania>", "rationale": "<pełne uzasadnienie, 3-6 zdań>", "key_findings": [{"claim": "<...>", "evidence_urls": ["<url z dowodów>"]}]}, "south": {...analogicznie...}}}""")
    return "\n".join(lines)


def _repair_json(text: str) -> str:
    """Naprawia typowe błędy LLM w JSON: nieescapowane cudzysłowy wewnątrz
    wartości, surowe znaki nowej linii w stringach, przecinki wiszące."""
    out: list[str] = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        ch = text[i]
        if in_str:
            if ch == "\\" and i + 1 < n:
                out.append(ch)
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                j = i + 1
                while j < n and text[j] in " \t\r\n":
                    j += 1
                nxt = text[j] if j < n else ""
                if nxt in ",:}]":
                    in_str = False
                    out.append(ch)
                else:
                    out.append('\\"')  # cudzysłów w środku wartości
            elif ch in "\r\n":
                out.append("\\n")
            else:
                out.append(ch)
        else:
            if ch == '"':
                in_str = True
            out.append(ch)
        i += 1
    return re.sub(r",\s*([}\]])", r"\1", "".join(out))


def extract_json(content: str) -> dict:
    """Parsuje JSON z odpowiedzi LLM, tolerując ogrodzenia ```json
    i drobne błędy składni (naprawiane heurystycznie)."""
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        return json.loads(_repair_json(text.strip()))


# --------------------------------------------------------------- OpenRouter

def call_openrouter(prompt: str, api_key: str, model: str, timeout: int = 180) -> str:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
        # Modele reasoningowe (np. GLM) zużywają budżet na rozumowanie —
        # za mały limit skutkuje pustym content (finish_reason=length).
        "max_tokens": 20000,
        "reasoning": {"effort": "low", "exclude": True},
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
    prev_scores = (prev_entry.get("scores") or {}) if prev_entry else {}
    out = {}
    for key, _label in SCENARIOS:
        data_sc = data.get("scores", {}).get(key)
        if not isinstance(data_sc, dict):
            raise ValueError(f"brak sekcji scores.{key} w odpowiedzi LLM")
        score = int(round(float(data_sc.get("score", 0))))
        score = max(0, min(100, score))
        confidence = data_sc.get("confidence") or "niska"
        if confidence not in CONFIDENCE_LEVELS:
            confidence = "niska"
        prev_score = (prev_scores.get(key) or {}).get("score")
        trend = (score - prev_score) if isinstance(prev_score, int) else 0
        findings = []
        for f in data_sc.get("key_findings") or []:
            if isinstance(f, dict) and _clean(f.get("claim")):
                urls = [u for u in (f.get("evidence_urls") or []) if _clean(u)]
                findings.append({"claim": _clean(f["claim"]), "evidence_urls": urls})
        out[key] = {
            "score": score,
            "confidence": confidence,
            "summary": _clean(data_sc.get("summary")) or "Brak opisu.",
            "rationale": _clean(data_sc.get("rationale")) or "Brak uzasadnienia.",
            "key_findings": findings,
            "trend_vs_prev": trend,
        }
    return {"scores": out}


def prev_entry_before(entries: list[dict], day: str) -> dict | None:
    earlier = [e for e in entries if e.get("date", "") < day]
    return earlier[-1] if earlier else None


def no_evidence_entry(day: str, prev: dict | None, status: str,
                      cfg: dict | None = None) -> dict:
    baseline = (cfg or {}).get("baseline_scores") or {}
    prev_scores = (prev.get("scores") or {}) if prev else {}
    scores = {}
    for key, _label in SCENARIOS:
        p = prev_scores.get(key) or {}
        if isinstance(p.get("score"), int):
            score, confidence = p["score"], "niska"
            summary = ("Brak nowych dowodów w podglądanych źródłach — "
                       f"utrzymuję poprzednią ocenę ({p['score']}%).")
        else:
            score = baseline.get(key) if isinstance(baseline.get(key), int) else 50
            confidence = "niska"
            summary = ("Brak nowych dowodów — utrzymuję score bazowy "
                       f"z analiz eksperckich ({score}%).")
        prev_score = p.get("score")
        trend = (score - prev_score) if isinstance(prev_score, int) else 0
        scores[key] = {
            "score": score, "confidence": confidence,
            "summary": summary, "trend_vs_prev": trend,
            "rationale": summary,
            "key_findings": [],
        }
    return {
        "date": day, "scores": scores,
        "sources_found": 0,
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
        prompt = build_prompt(evidence, prev, cfg, load_analyses())
        api_key = os.environ.get("OPENROUTER_API_KEY", "")
        if not api_key:
            print("[assess] FAIL: brak OPENROUTER_API_KEY w środowisku", file=sys.stderr)
            return 2
        model = cfg.get("openrouter_model", "google/gemini-2.5-flash")
        assessment = None
        for attempt in (1, 2, 3):
            content = call_openrouter(prompt, api_key, model)
            try:
                assessment = parse_assessment(extract_json(content), prev)
                break
            except json.JSONDecodeError as exc:
                print(f"[assess] ostrzeżenie: JSON LLM niepoprawny "
                      f"(próba {attempt}/3): {exc}", file=sys.stderr)
        if assessment is None:
            raise RuntimeError("OpenRouter: 3× niepoprawny JSON w odpowiedzi")
        entry = {
            "date": day, **assessment,
            "evidence": [e.to_dict() for e in evidence[:15]],
            "sources_found": len(evidence),
            "engine_status": "ok",
        }
    else:
        status = "no-data" if not (raw_path.exists() or feeds_path.exists()) else "no-evidence"
        entry = no_evidence_entry(day, prev, status, cfg)

    entry["assessment_path"] = f"data/assessments/{day}.json"
    upsert_entry(entry)

    full = dict(entry)
    full["evidence"] = [e.to_dict() for e in evidence]
    assessments_dir().mkdir(parents=True, exist_ok=True)
    (assessments_dir() / f"{day}.json").write_text(
        json.dumps(full, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"[assess] {day}: score północ={entry['scores']['north']['score']}% "
          f"({entry['scores']['north']['confidence']}), "
          f"południe={entry['scores']['south']['score']}% "
          f"({entry['scores']['south']['confidence']}), "
          f"dowody={entry['sources_found']}, status={entry['engine_status']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

