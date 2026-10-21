"""Krok 2: rubryka oceny → OpenRouter → assessment + update scores.json.

Użycie:
    python scripts/assess.py [--date YYYY-MM-DD]

Czyta data/raw/<dzień>/report.json (silnik), feeds.json (RSS),
agent_reach.json (wyszukiwanie agent-reach/Exa) i facebook.json
(LOKALNY krok OpenCLI — w CI go nie ma),
buduje prompt z rubryką PL (tylko dowody NOWE względem poprzednich ocen —
delta dnia; cichy dzień utrzymuje score bez wywołania LLM), wywołuje
OpenRouter i zapisuje:
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
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (CONFIDENCE_LEVELS, SCENARIOS, Evidence, assessments_dir,  # noqa: E402
                    is_fresh, load_config, load_scores, raw_dir, resolve_date,
                    upsert_entry)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

ANALIZY_DIR = Path(__file__).resolve().parent.parent / "analizy"
ANALIZY_CAP = 8000

# Ile dni wstecz dowody uznajemy za "już widziane" (0 = cała historia).
# Bez okna pula nowych URL-i wyczerpuje się i score staje na zawsze.
DEFAULT_SEEN_WINDOW_DAYS = 7


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


def pick_evidence(evidence: list[Evidence], seen: set[str],
                  prev_entry: dict | None) -> tuple[list[Evidence], bool]:
    """Delta dnia (tylko dowody nieobecne w poprzednich ocenach) + decyzja,
    czy w ogóle wywoływać LLM. Pierwsza ocena (brak prev) używa pełnego
    zbioru — nawet gdy assessments coś mają, a scores.json jest pusty."""
    if not evidence:
        return [], False
    new = [e for e in evidence if _norm_url(e.url) not in seen]
    if new or prev_entry is None:
        return (new or evidence), True
    return [], False


def seen_evidence_urls(day: str, directory: Path | None = None,
                       window_days: int = DEFAULT_SEEN_WINDOW_DAYS) -> set[str]:
    """Znormalizowane URL-e dowodów z ocen z ostatnich `window_days` dni.

    Pomija plik dnia bieżącego: rerun tego samego dnia nie może potraktować
    własnych, wcześniej zapisanych dowodów jako 'widziane'.

    Okno jest konieczne: przy wąskim temacie pula artykułów jest mała, więc
    „widziane raz na zawsze” wyczerpywało się po kilku tygodniach i pipeline
    zamarzał w statusie `no-new-evidence` (score nigdy się nie zmieniał).
    Po `window_days` dniach dowód wraca do oceny jako kontekst odświeżający.

    `window_days <= 0` → bez okna (cała historia; zachowanie sprzed zmiany).
    """
    out: set[str] = set()
    d = directory or assessments_dir()
    if not d.is_dir():
        return out
    try:
        run_day = date.fromisoformat((day or "").strip()[:10])
    except ValueError:
        run_day = None
    for path in sorted(d.glob("*.json")):
        if path.stem == day:
            continue
        if run_day is not None and window_days > 0:
            try:
                file_day = date.fromisoformat(path.stem[:10])
            except ValueError:
                continue  # nazwa nie jest datą — nie wiemy, czy w oknie
            if (run_day - file_day).days > window_days or file_day > run_day:
                continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue  # uszkodzony plik nie blokuje pipeline'u
        for ev in data.get("evidence") or []:
            url = _clean(ev.get("url")) if isinstance(ev, dict) else ""
            if url:
                out.add(_norm_url(url))
    return out


# ------------------------------------------------------------------- prompt

SCENARIO_GEOMETRY = {
    "north": (
        "SCENARIUSZ PÓŁNOC: trasa prowadzi przez północną część gminy — pas "
        "między wsią Sobienie-Jeziory a doliną Wisły (tereny zalewowe, "
        "Natura 2000 Dolina Środkowej Wisły), łącznie z nowym śladem przez "
        "środkowo-północny pas gminy."
    ),
    "south": (
        "SCENARIUSZ POŁUDNIE: trasa prowadzi przez południową część gminy — "
        "na południe od wsi Sobienie-Jeziory (rejon Śniadkowa Dolnego "
        "i Górnego), otwarty płaskowyż rolniczy ku południowej granicy gminy."
    ),
}

# Skąd bierze się score bazowy danego scenariusza (analiza nr 2).
SCENARIO_BASELINE_NOTE = {
    "north": ("analiza nr 2: obwodnica wsi po stronie północnej ~10–12% "
              "+ nowy ślad przez środkowo-północny pas gminy ~30–35%"),
    "south": "analiza nr 2: korytarz DK50 z obwodnicą wsi po stronie południowej ~25–30%",
}


def build_prompt(evidence: list[Evidence], prev_entry: dict | None, cfg: dict,
                 analyses: str = "", scenario: str = "north") -> str:
    """Prompt oceny JEDNEGO scenariusza (północ albo południe).

    Każdy scenariusz oceniamy osobnym wywołaniem modelu: wspólny prompt
    z dwoma score'ami kończył się kopiowaniem delty (korelacja 1,0), bo model
    traktował oba jak jedną ocenę z przesunięciem. Osobny kontekst i osobny
    punkt wyjścia wymuszają osobne uzasadnienie.
    """
    if scenario not in SCENARIO_GEOMETRY:
        raise ValueError(f"nieznany scenariusz: {scenario!r}")
    baseline = cfg.get("baseline_scores") or {}
    base = baseline.get(scenario)
    prev_sc = ((prev_entry or {}).get("scores") or {}).get(scenario) or {}
    lines = [
        "Jesteś analitykiem oceniającym planowaną południową obwodnicę "
        "autostradową Warszawy (A50/OAW) i szansę, że jej finalny przebieg "
        f"przetnie teren gminy {cfg.get('focus', 'Sobienie-Jeziory')} "
        "(woj. mazowieckie, powiat otwocki). Oceniasz WYŁĄCZNIE jeden "
        "scenariusz przebiegu:",
        SCENARIO_GEOMETRY[scenario],
        "",
        "Kontekst geograficzny (obowiązuje nawet gdy źródła mówią inaczej): "
        "wieś Sobienie-Jeziory leży na drogach wojewódzkich 801 i 739, "
        "w środkowej części gminy. DK50 biegnie NA PÓŁNOC od gminy — od Góry "
        "Kalwarii (nowy most Nadwiślańskiego Urzecza) przez Ostrówek "
        "i Piotrowice (gmina Karczew) oraz Tabor i Regut (gmina Celestynów) "
        "do Kołbiela — i nie przecina gminy. Północną granicę gminy wyznacza "
        "dolina Wisły (tereny zalewowe, Natura 2000); na południe od wsi leżą "
        "m.in. Śniadków Dolny i Górny. UWAGA: teksty analiz eksperckich "
        "poniżej zakładają starszy, nieaktualny przebieg DK50 przez gminę — "
        "w razie sprzeczności stosuj niniejszy kontekst.",
        "",
        "NOWE DOWODY (nieobecne w ocenach z ostatnich dni; media, Reddit, "
        "YouTube, RSS):",
    ]
    for i, ev in enumerate(evidence, 1):
        lines.append(f"[{i}] {ev.title} | {ev.source} | {ev.published or 'b.d.'} | "
                     f"{ev.url} | {ev.snippet[:200]}")
    lines.append("")
    lines.append("PUNKT WYJŚCIA — ANALIZY EKSPERCKIE:")
    lines.append(f"- Score bazowy dla tego scenariusza: "
                 f"{base if base is not None else '?'}% ({SCENARIO_BASELINE_NOTE[scenario]}).")
    if analyses:
        lines.append("- Pełne teksty analiz:")
        lines.append(analyses)
    lines.append("")
    if prev_entry:
        lines.append(f"POPRZEDNIA OCENA TEGO SCENARIUSZA ({prev_entry.get('date')}): "
                     f"score={prev_sc.get('score')}, "
                     f"confidence={prev_sc.get('confidence')}. "
                     f"Uzasadnienie: {(prev_sc.get('rationale') or '')[:400]}")
    else:
        lines.append("POPRZEDNIA OCENA: brak (pierwsza ocena).")
    lines.append(f"""
RUBRYKA (dotyczy wyłącznie scenariusza {scenario.upper()}):
- score 0-100 = Twoja ocena prawdopodobieństwa (w %), że finalny przebieg A50 przetnie tą stronę gminy Sobienie-Jeziory, którą opisuje scenariusz powyżej.
- Punktem wyjścia jest score bazowy z analiz eksperckich oraz poprzednia ocena TEGO scenariusza. Codzienne dowody modyfikują score; wnioski analiz są kotwicą — wyraźne odstępstwo wymaga mocnych, oficjalnych dowodów (komunikat GDDKiA, wskazanie wariantu, DŚU, przetarg).
- NIE oceniasz drugiego scenariusza i nie próbuj zachować jakiejkolwiek relacji między ich wartościami. Nie stosuj tej samej delty do obu stron; każda strona ma własne prawdopodobieństwo i własne uzasadnienie.
- Dowód dotyczący wyłącznie przeciwnej strony gminy nie zmienia tego score (może co najwyżej obniżyć confidence).
- Wagi dowodów (od najsilniejszych): oficjalne komunikaty GDDKiA / ministerstw / rządu > uchwały i stanowiska samorządów (gminnych, powiatowych, marszałkowskich) > główne media ogólnopolskie > media lokalne > social media / sentyment.
- Kluczowe ogniwo: wariant przebiegu. Oficjalne potwierdzenie wariantu omijającego gminę albo prowadzącego przez jej środek/inną stronę => score tego scenariusza niski. Oficjalny proces wskazujący korytarz przez tę stronę gminy (studium, raport OOŚ, decyzja środowiskowa, przetarg) => score wyższy.
- Dowody sprzeczne lub nieliczne => obniż confidence i trzymaj score blisko poprzedniego.
- Score modyfikuj WYŁĄCZNIE w oparciu o NOWE dowody z listy powyżej — wcześniejsze ustalenia są już odzwierciedlone w poprzedniej ocenie. Dowody starsze niż 30 dni są odfiltrowane przed oceną.
- Brak nowych, istotnych dowodów => score = poprzedni (lub bazowy), confidence = "niska", wyraźnie zaznacz brak nowych sygnałów.
- KAŻDY claim w key_findings MUSI mieć evidence_urls wyłącznie z listy dowodów powyżej. Nie wymyślaj URL-i.
- Pisz po polsku.

ODPOWIEDŹ: wyłącznie JSON dokładnie wg schematu (bez ogrodzeń markdown; wewnątrz wartości tekstowych NIE używaj znaku " — cytuj polskimi cudzysłowami „…”; max 5 pozycji w key_findings; summary ≤ 2 zdania; rationale ≤ 4 zdania):
{{"score": <int 0-100>, "confidence": "<niska|średnia|wysoka>", "summary": "<1-2 zdania>", "rationale": "<pełne uzasadnienie, 3-6 zdań>", "key_findings": [{{"claim": "<...>", "evidence_urls": ["<url z dowodów>"]}}]}}""")
    return "\n".join(lines)


def parse_scenario(data: dict, prev_sc: dict | None) -> dict:
    """Waliduje odpowiedź modelu dla jednego scenariusza."""
    if not isinstance(data, dict):
        raise ValueError("odpowiedź LLM nie jest obiektem JSON")
    score = data.get("score")
    try:
        score = int(round(float(score)))
    except (TypeError, ValueError):
        raise ValueError("brak/nieliczbowy score w odpowiedzi LLM") from None
    score = max(0, min(100, score))
    confidence = data.get("confidence") or "niska"
    if confidence not in CONFIDENCE_LEVELS:
        confidence = "niska"
    prev_score = (prev_sc or {}).get("score")
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
        "trend_vs_prev": (score - prev_score) if isinstance(prev_score, int) else 0,
    }


def _json_continues(text: str, j: int) -> bool:
    """Czy cudzysłów na pozycji j-1 zamyka string, czy jest tylko treścią?

    Polskie zdania w wartościach często mają cudzysłów tuż przed
    przecinkiem: 'wariant "południowy", który…'. Po przecinku idzie wtedy
    zwykły wyraz — a w JSON po przecinku musi nastąpić wartość, klucz
    lub koniec struktury, więc taki cudzysłów to treść, nie koniec.
    """
    n = len(text)
    while j < n and text[j] in " \t\r\n":
        j += 1
    if j >= n:
        return True  # koniec tekstu — traktuj jako zamykający
    if text[j] in ":}]":
        return True
    if text[j] != ",":
        return False
    j += 1
    while j < n and text[j] in " \t\r\n":
        j += 1
    if j >= n:
        return True
    if text[j] in '"{[-' or text[j].isdigit():
        return True  # po przecinku zaczyna się wartość/klucz/element tablicy
    for word in ("true", "false", "null"):
        if text.startswith(word, j):
            after = text[j + len(word):].lstrip()
            return not after or after[0] in ",:}]"
    return False  # zwykły wyraz — cudzysłów był treścią


def _repair_json(text: str) -> str:
    """Naprawia typowe błędy LLM w JSON: nieescapowane cudzysłowy wewnątrz
    wartości, surowe znaki nowej linii w stringach, przecinki wiszące
    oraz pomylony/niedomknięty ogon nawiasów (LLM gubi głębię na końcu)."""
    out: list[str] = []
    stack: list[str] = []
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
                if _json_continues(text, i + 1):
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
            elif ch in "{[":
                stack.append(ch)
                out.append(ch)
            elif ch in "}]":
                if not stack:
                    i += 1
                    continue  # nadmiarowe domknięcie — pomiń
                want = "}" if stack[-1] == "{" else "]"
                stack.pop()
                if ch != want:
                    out.append(want)  # zły znak domykający — popraw
                    i += 1
                    continue
                out.append(ch)
            else:
                out.append(ch)
        i += 1
    if in_str:
        out.append('"')  # string ucięty przez limit tokenów
    while stack:
        out.append("}" if stack.pop() == "{" else "]")  # dokończ ogon
    return re.sub(r",\s*([}\]])", r"\1", "".join(out))


def extract_json(content: str) -> dict:
    """Parsuje JSON z odpowiedzi LLM, tolerując ogrodzenia ```json
    i drobne błędy składni (naprawiane heurystycznie)."""
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    repaired = _repair_json(text)
    try:
        return json.loads(repaired)
    except json.JSONDecodeError as exc:
        # Fragment wokół błędu trafia do komunikatu — bez tego diagnoza
        # w CI ("Expecting ',' delimiter, char 4723") to zgadywanie.
        frag = repaired[max(0, exc.pos - 120):exc.pos + 120]
        raise json.JSONDecodeError(
            f"{exc.msg} (kontekst: {frag!r})", exc.doc, exc.pos) from None


# --------------------------------------------------------------- OpenRouter

def call_openrouter(prompt: str, api_key: str, model: str, timeout: int = 180) -> str:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
        # Modele reasoningowe (np. DeepSeek V4.1 Flash) zużywają budżet na
        # rozumowanie — za mały limit skutkuje pustym content (finish_reason=length).
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
            choice = data["choices"][0]
            content = choice["message"]["content"]
            finish = choice.get("finish_reason")
            usage = data.get("usage") or {}
            print(f"[assess] OpenRouter: finish_reason={finish}, "
                  f"completion_tokens={usage.get('completion_tokens')}",
                  file=sys.stderr)
            if not content:
                raise RuntimeError(
                    f"OpenRouter: pusta odpowiedź (finish_reason={finish})")
            return content, finish
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:500]
            if exc.code in (401, 402):
                raise RuntimeError(f"OpenRouter HTTP {exc.code}: {body}") from exc
            last_err = RuntimeError(f"OpenRouter HTTP {exc.code}: {body}")
        except Exception as exc:  # noqa: BLE001 — retry każdej awarii sieciowej
            last_err = exc
    raise last_err


# --------------------------------------------------------------- parsowanie

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


def assess_scenarios(evidence: list[Evidence], prev: dict | None, cfg: dict,
                     api_key: str, analyses: str) -> tuple[dict, list[str]]:
    """Ocenia każdy scenariusz osobnym wywołaniem modelu.

    Zwraca (scores, błędy). Awaria jednego scenariusza nie zabija raportu:
    ta strona dostaje poprzednią wartość z confidence „niska”, a powód ląduje
    w `assess_errors`. Gdy padną oba — wyjątek (workflow ma być czerwony).
    """
    model = cfg.get("openrouter_model", "google/gemini-2.5-flash")
    baseline = cfg.get("baseline_scores") or {}
    prev_scores = (prev or {}).get("scores") or {}
    scores: dict = {}
    errors: list[str] = []
    for key, _label in SCENARIOS:
        prev_sc = prev_scores.get(key) or {}
        prompt = build_prompt(evidence, prev, cfg, analyses, scenario=key)
        parsed = None
        for attempt in (1, 2, 3):
            content, finish = call_openrouter(prompt, api_key, model)
            try:
                parsed = parse_scenario(extract_json(content), prev_sc)
                break
            except (json.JSONDecodeError, ValueError) as exc:
                print(f"[assess] ostrzeżenie ({key}): JSON LLM niepoprawny "
                      f"(próba {attempt}/3, finish_reason={finish}): {exc}",
                      file=sys.stderr)
        if parsed is None:
            fallback = prev_sc.get("score")
            if not isinstance(fallback, int):
                fallback = baseline.get(key) if isinstance(baseline.get(key), int) else 50
            errors.append(f"{key}: brak poprawnej odpowiedzi modelu — "
                          f"utrzymano poprzednią ocenę ({fallback}%)")
            print(f"[assess] BŁĄD ({key}): 3× niepoprawny JSON — "
                  f"utrzymuję {fallback}%", file=sys.stderr)
            parsed = {
                "score": fallback, "confidence": "niska",
                "summary": ("Ocena modelu nie powiodła się — utrzymuję "
                            f"poprzednią wartość ({fallback}%)."),
                "rationale": ("Brak poprawnej odpowiedzi modelu dla tego "
                              "scenariusza; score niezmieniony."),
                "key_findings": [],
                "trend_vs_prev": ((fallback - prev_sc["score"])
                                  if isinstance(prev_sc.get("score"), int) else 0),
            }
        scores[key] = parsed
    if len(errors) == len(SCENARIOS):
        raise RuntimeError("OpenRouter: żaden scenariusz nie został oceniony")
    return {"scores": scores}, errors


# -------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="Ocena prawdopodobieństwa A50 przez Sobienie-Jeziory.")
    ap.add_argument("--date", help="Dzień raportu (ISO, domyślnie dziś)")
    args = ap.parse_args()

    day = resolve_date(args.date)
    cfg = load_config()
    raw_path = raw_dir(day) / "report.json"
    feeds_path = raw_dir(day) / "feeds.json"
    reach_path = raw_dir(day) / "agent_reach.json"
    facebook_path = raw_dir(day) / "facebook.json"

    engine_items: list[Evidence] = []
    feed_items: list[Evidence] = []
    reach_items: list[Evidence] = []
    facebook_items: list[Evidence] = []
    if raw_path.exists():
        with open(raw_path, encoding="utf-8") as f:
            engine_items = extract_engine_items(json.load(f), cfg)
    if feeds_path.exists():
        with open(feeds_path, encoding="utf-8") as f:
            feed_items = extract_feed_items(json.load(f), cfg)
    if reach_path.exists():
        with open(reach_path, encoding="utf-8") as f:
            # agent-reach zapisuje {"items": [...]} — ten sam kształt co feeds.json
            reach_items = extract_feed_items(json.load(f), cfg)
    if facebook_path.exists():
        with open(facebook_path, encoding="utf-8") as f:
            # krok lokalny (OpenCLI) — w CI pliku nie ma; kształt jak feeds.json
            facebook_items = extract_feed_items(json.load(f), cfg)

    # Okno świeżości: do oceny trafiają tylko dowody z ostatnich
    # lookback_days dni od dnia raportu (bez daty → zostaje). Stare
    # publikacje nie wchodzą do assessmentu; pełne zebranie zostaje
    # w data/raw/ jako audyt. Filtr przed merge, by cap zapełniały
    # świeże pozycje.
    max_age_days = int(cfg.get("lookback_days", 30))
    engine_items = [e for e in engine_items if is_fresh(e.published, day, max_age_days)]
    feed_items = [e for e in feed_items if is_fresh(e.published, day, max_age_days)]
    reach_items = [e for e in reach_items if is_fresh(e.published, day, max_age_days)]
    facebook_items = [e for e in facebook_items
                      if is_fresh(e.published, day, max_age_days)]

    evidence = merge_evidence(engine_items, feed_items, reach_items, facebook_items,
                              cap=int(cfg.get("max_evidence", 40)))
    prev = prev_entry_before(load_scores()["entries"], day)
    seen_window = int(cfg.get("seen_window_days", DEFAULT_SEEN_WINDOW_DAYS))
    seen = seen_evidence_urls(day, window_days=seen_window)
    to_assess, assess_needed = pick_evidence(evidence, seen, prev)

    if assess_needed:
        api_key = os.environ.get("OPENROUTER_API_KEY", "")
        if not api_key:
            print("[assess] FAIL: brak OPENROUTER_API_KEY w środowisku", file=sys.stderr)
            return 2
        assessment, errors = assess_scenarios(to_assess, prev, cfg, api_key,
                                              load_analyses())
        entry = {
            "date": day, **assessment,
            "evidence": [e.to_dict() for e in to_assess[:15]],
            "sources_found": len(to_assess),
            "engine_status": "ok",
        }
        if errors:
            entry["assess_errors"] = errors
    elif evidence:
        # Cichy dzień: nic nowego od ostatniej oceny — utrzymanie bez LLM.
        entry = no_evidence_entry(day, prev, "no-new-evidence", cfg)
    else:
        have_raw = (raw_path.exists() or feeds_path.exists() or reach_path.exists()
                    or facebook_path.exists())
        status = "no-evidence" if have_raw else "no-data"
        entry = no_evidence_entry(day, prev, status, cfg)

    entry["assessment_path"] = f"data/assessments/{day}.json"
    upsert_entry(entry)

    full = dict(entry)
    full["evidence"] = [e.to_dict() for e in to_assess]
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

