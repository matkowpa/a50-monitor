# Implementation Plan — a50-monitor

## Overview

W pełni zautomatyzowany, codzienny monitoring mediów (w tym social mediów) dotyczący decyzji o przebiegu południowej obwodnicy autostradowej Warszawy (A50), ze szczególnym uwzględnieniem ryzyka przeprowadzenia trasy przez gminę Sobienie-Jeziory — z publikacją wyników jako polskojęzyczna statyczna strona HTML na GitHub Pages, zawierająca linki do źródeł oraz wyraźny dzienny score prawdopodobieństwa (0–100%).

**Kontekst i uzasadnienie architektury.** Skill `last30days` (v3.23.0, `C:\Users\alusm\.cline\skills\last30days`) jest silnikiem badawczym z jawnym trybem headless/cron (SKILL.md, l. 1209: auto-resolve to "cron/CI fallback for when no reasoning model is driving"). Silnik potrafi samodzielnie planować podzapytania przez reasoning provider — klasa `OpenRouterClient` w `lib/providers.py`, używana przez `lib/pipeline.py` (l. 2102) — a flaga `--emit=json` zwraca ustrukturyzowany raport z linkami do źródeł. Silnik jest **wyłącznie stdlib Python 3** (zweryfikowano importy — zero zewnętrznych pakietów), więc działa w GitHub Actions bez instalacji zależności. Użytkownik ma ustawiony `OPENROUTER_API_KEY` (jedyny dostępny klucz; brak ScrapeCreators/Brave/Perplexity, więc źródła premium X/TikTok są niedostępne — działają keyless web search, Reddit, YouTube, HN, Bluesky). Nasz własny krok oceny (score) wywołuje bezpośrednio API OpenRouter z ustaloną rubryką — to daje **pełną automatyzację po stronie serwera** (GitHub Actions cron), bez potrzeby agenta LLM w pętli.

**Przepływ dzienny:** cron GitHub Actions → (1) silnik last30days zbiera dane (ostatnie 30 dni, tematy PL) → (2) `assess.py` normalizuje źródła i wywołuje OpenRouter z rubryką oceny → score 0–100% + uzasadnienie + dowody z linkami → (3) `build_site.py` buduje statyczną stronę (SVG gauge + wykres trendu, zero JS/zależności) → (4) deploy na GitHub Pages + commit danych historycznych do main.

Dodatek: workflow Cline `/a50-daily` (`.clinerules/workflows/a50-daily.md`) pozwala uruchomić ten sam pipeline ręcznie lokalnie (przydatne przy problemach z limity Actions oraz do pierwszego zasilenia danych).

## Types

Struktury danych (JSON, w repo, committowane w celu utrwalenia historii):

**`data/scores.json`** — historia score'ów (jedyny plik stanu; odczytywany przez assess i build):
```json
{
  "entries": [
    {
      "date": "2026-09-06",
      "score": 35,
      "confidence": "średnia",
      "summary": "1–2 zdania PL",
      "trend_vs_prev": "+5",
      "rationale": "pełne uzasadnienie PL",
      "key_findings": [
        {"claim": "PL", "evidence_urls": ["https://..."]}
      ],
      "evidence": [
        {"title": "...", "url": "https://...", "source": "naszemiasto.pl",
        "published": "2026-08-30", "snippet": "...",
        "stance": "supporting"}
      ],
      "sources_found": 42,
      "engine_status": "ok",
      "assessment_path": "data/assessments/2026-09-06.json"
    }
  ]
}
```
Idempotencja: wpis z tego samego dnia jest zastępowany (rerun nie duplikuje).

**`data/assessments/YYYY-MM-DD.json`** — pełny dzienny assessment (ten sam schemat co wpis + pełne raw evidence).

**`data/raw/YYYY-MM-DD/`** — surowy wynik silnika (`report.json` z `--emit=json --json-profile=raw`, + pliki pomocnicze silnika). Niezmieniany, jako audyt.

**`config.json`** — konfiguracja (temat, słowa kluczowe, źródła, model, flagi):
```json
{
  "topic": "obwodnica autostradowa Warszawy A50 przebieg trasy",
  "focus": "gmina Sobienie-Jeziory",
  "keywords": ["A50", "obwodnica autostradowa Warszawy", "Sobienie-Jeziory", "przebieg A50", "wariant A50", "autostradowa obwodnica Warszawy południe"],
  "search_sources": "web,reddit,youtube,hn,bluesky",
  "lookback_days": 30,
  "openrouter_model": "google/gemini-2.5-flash",
  "max_evidence": 40
}
```

## Files

**Nowe pliki (wszystkie pod `c:\Users\alusm\OneDrive\Dokumenty\Tata\projekty\a50-monitor\`):**

| Ścieżka | Cel |
|---|---|
| `config.json` | konfiguracja (patrz Types) |
| `scripts/common.py` | wczytywanie configu, ścieżki dat, dataclass `Evidence`, atomowe zapisy JSON (tmp+rename) |
| `scripts/research.py` | uruchamia silnik last30days, zapisuje raw JSON dnia |
| `scripts/assess.py` | normalizuje źródła → rubryka → OpenRouter → assessment + update `scores.json` |
| `scripts/build_site.py` | buduje statyczny site z `data/` |
| `templates/base.html` … | `templates/base.html`, `templates/index.html`, `templates/day.html`, `templates/about.html` — szablony stdlib `string.Template` (zero zależności) |
| `.github/workflows/daily.yml` | cron dzienny + workflow_dispatch + deploy |
| `skill/last30days/…` | vendoryzowany skill: `scripts/` (lib + last30days.py) + `SKILL.md`; BEZ `__pycache__`, `assets/` (mp3/jpeg), skryptów testowych (`test-*.sh`, `evaluate_*`, `verify_*`, `compare.sh`, `build-skill.sh`, `setup-*.sh`); `doctor` zostaje bo użyteczny, `references/` zostaje |
| `.clinerules/workflows/a50-daily.md` | ręczny workflow Cline `/a50-daily` (lokalny pipeline: research→assess→build→commit→push) |
| `README.md` | opis projektu, metodologia score, instrukcja sekretów, lokalne uruchomienie |
| `LICENSE` | MIT |
| `.gitignore` | `__pycache__/`, `*.pyc`, `.env`, `site/` (build lokalny) — uwaga: `data/` JEST committowane |
| `feeds.txt` | (opcjonalny fallback) lista URL RSS/Atom mediów lokalnych/GDDKiA — patrz Testing/fallback |

**Modyfikacje istniejących plików:** brak (puste repo). `TODOs-a50.txt` zostaje jako dokument wymagań (nie usuwać — to plik użytkownika).

**Do skasowania:** nic.

## Functions

**`scripts/common.py`** (nowy):
- `load_config() -> dict` — czyta `config.json` z roota repo.
- `today_str() -> str` — data ISO (spójność nazw plików; w CI ustawimy TZ=Europe/Warsaw).
- `class Evidence` (dataclass): `title, url, source, published, snippet, stance`.
- `atomic_write_json(path, obj)` — zapis tmp + `os.replace` (bezpieczny rerun).
- `scores_path(), raw_dir(date), assessments_dir(date) -> Path` — helpery ścieżek.

**`scripts/research.py`** (nowy):
- `run_engine(cfg, date) -> tuple[Path, bool]` — `subprocess.run([python, "skill/last30days/scripts/last30days.py", topic, "--emit=json", "--json-profile=raw", "--quick", "--days", str(cfg.lookback_days), "--search", cfg.search_sources, "--save-dir", raw_dir(date)], capture stdout)`; stdout zapisywany jako `raw_dir/report.json`. Zwraca (ścieżka, success). Kod wyjścia != 0 → success=False, nie rzuca (workflow kontynuuje z "no data"). Timeout 20 min.
- `main()` — CLI: `python scripts/research.py [--date YYYY-MM-DD]`.

**`scripts/assess.py`** (nowy):
- `extract_sources(raw: dict, cfg) -> list[Evidence]` — normalizuje strukturę raw JSON silnika (iteruje po sekcjach źródeł, wyciąga tytuł/url/datę/snippet/źródło), deduplikuje po URL, filtruje po `cfg.keywords` (case-insensitive), sortuje po dacie malejąco, tnie do `max_evidence`.
- `build_prompt(evidence, prev_entry, cfg) -> str` — rubryka PL, wstrzykuje dowody jako listę `[n] title | source | date | url | snippet` + poprzedni score/uzasadnienie dla ciągłości.
- `call_openrouter(prompt, api_key, model) -> dict` — `urllib.request` POST na `https://openrouter.ai/api/v1/chat/completions` z `response_format=json_object`; retry 3× z backoffem 5/15/30 s; błędy 402/401 → wyjątek z komunikatem (workflow oznaczy dzień jako "stale").
- `parse_assessment(llm_json) -> dict` — walidacja schematu (score 0–100, confidence w {niska, średnia, wysoka}, niepuste pola); domyślne przy brakach.
- `update_scores(entry, scores) -> None` — idempotentny upsert po dacie (ta sama data = replace).
- `main()` — CLI: `python scripts/assess.py [--date YYYY-MM-DD]`; gdy brak raw → wpis z poprzednim score, `engine_status="no-data"`, confidence="niska".

**Rubryka oceny (sztywny prompt, PL)** — kluczowe reguły:
- score = prawdopodobieństwo, że finalny przebieg A50 przetnie teren **gminy Sobienie-Jeziory**;
- wagi dowodów: oficjalne stanowiska GDDKiA/ministerstw/rządu (najsilniejsze) > uchwały władz gminnych/powiatowych/marszałkowskich > raporty głównych mediów > media lokalne > social media/sentiment (najsłabsze);
- każde key_finding MUSI mieć linki do źródeł; zabronione wymyślanie URL-i (tylko z listy dowodów);
- brak nowych dowodów → score = poprzedni, confidence="niska", wyraźna adnotacja o braku sygnałów;
- wyjście: **wyłącznie JSON** zgodny ze schematem wpisu w scores.json.

**`scripts/build_site.py`** (nowy):
- `load_scores() -> list[dict]` (chronologicznie).
- `render_gauge_svg(score) -> str` — półokrągły zegar 0–100% (czysty SVG, inline).
- `render_trend_svg(entries) -> str` — polyline score w czasie (czysty SVG).
- `render_page(template_path, **ctx) -> str` — `string.Template.safe_substitute`.
- `build(out_dir="site")` — generuje: `index.html` (aktualny score + gauge + trend + summary + top findings + archiwum), `day-YYYY-MM-DD.html` per dzień (pełne dowody z linkami), `about.html` (metodologia rubryki, źródła, disclaimer że to ocena ekspercka LLM, nie oficjalna informacja), `style.css` (jednolity plik CSS, mobile-first).
- `main()` — CLI: `python scripts/build_site.py [--out site]`.

**Modyfikowane/usuwane funkcje:** brak (projekt od zera).

## Classes

- `common.Evidence` (dataclass) — nowa, patrz wyżej. Poza tym rozwiązanie funkcyjne (moduły CLI); żadnych dodatkowych klas — zgodnie z zasadą prostoty.

## Dependencies

- **Zero zewnętrznych pakietów Pythona.** Silnik last30days jest czysto stdlib (zweryfikowano); nasze skrypty używają `urllib.request`, `json`, `string`, `datetime`, `dataclasses`. Brak `requirements.txt`.
- **OpenRouter API** (istniejący klucz użytkownika): (a) silnik — planner subqueries (domyślny model engine: gemini flash lite), (b) nasz assess — model z `config.json` (domyślnie `google/gemini-2.5-flash`; tani, dobra jakość PL). Koszt szacunkowy: grosze/dzień.
- **GitHub Actions** (darmowe dla publicznych repo): cron `30 5 * * *` = 6:30/7:30 czasu PL; `permissions: contents: write`; `OPENROUTER_API_KEY` jako repo secret — **wymagane działanie użytkownika w UI GitHub**.
- **GitHub Pages**: serwowane z brancha `gh-pages` (deploy: `peaceiris/actions-gh-pages@v4`); włączenie w Settings → Pages — działanie użytkownika.
- **Brak `gh` CLI** na maszynie — repo zakłada użytkownik ręcznie przez github.com (new public repo, bez README), potem `git remote add origin https://github.com/<USER>/a50-monitor.git && git push -u origin main`. Autoryzacja przez Git Credential Manager Windows.

## Testing

- **Testy jednostkowe** (`tests/test_assess.py`, `tests/test_common.py`, uruchamiane `python -m unittest discover tests`, zero pytest — spójnie z brakiem zależności):
  - `extract_sources`: fixture `tests/fixtures/raw_sample.json` → liczba źródeł, dedup, filtr keyword; pusty raw → pusta lista;
  - `parse_assessment`: poprawny JSON, score poza 0–100 → clamp/reject, brakujące pola → domyślne;
  - `update_scores`: upsert tej samej daty zastępuje, nie duplikuje;
  - `build_prompt`: zawiera focus, poprzedni score, linki dowodów.
- **Testy silnika**: `--mock` flaga silnika pozwala uruchomić pipeline bez sieci — smoke test research.py w CI (osobny workflow `ci.yml` przy push — uruchamia testy + research --mock + build_site na fixture'ach).
- **Walidacja e2e lokalnie** (przed pushem): pełny run `python scripts/research.py && python scripts/assess.py && python scripts/build_site.py` z realnym kluczem → otwarcie `site/index.html` w przeglądarce.
- **Walidacja produkcyjna**: `workflow_dispatch` ręczny → weryfikacja Actions log + strona `https://<user>.github.io/a50-monitor/` + drugi run tego samego dnia (idempotencja — jeden wpis w scores.json).
- **Fallback/odporność**: `research.py` łapie nie-sukces silnika → dzień z `engine_status: no-data`; `feeds.txt` (faza 2, opcjonalne) — prosty RSS fetcher `scripts/fetch_feeds.py` stdlib `xml.etree`, doklejany do evidences gdy silnik zwróci < 3 źródła. W planie implementacyjnym, ale jako ostatni krok — można odłożyć.

## Implementation Order

1. **Init repo** — `git init`, `.gitignore`, `LICENSE` (MIT), `README.md` (szkielet), `config.json` → verify: `git status` czysty, struktura istnieje.
2. **Vendoryzacja skilla** — kopiowanie `scripts/`+`SKILL.md` do `skill/last30days/` (bez `__pycache__`, `assets/`, skryptów testowych) → verify: `python skill/last30days/scripts/last30days.py --help` działa z nowej lokalizacji.
3. **`scripts/common.py`** — config, ścieżki, Evidence, atomic JSON → verify: szybki unittest.
4. **`scripts/research.py`** — run silnika, raw save, obsługa błędów → verify: smoke `--mock` lokalnie + realny run `--days 7` (weryfikacja że keyless web search działa z tego IP; jak nie — korekta `search_sources`).
5. **`scripts/assess.py` + rubryka** — pełna pętla: extract→prompt→OpenRouter→parse→upsert → verify: unittesty na extract/parse/upsert + realne wywołanie OpenRouter (score w scores.json ma sens, dowody mają linki).
6. **`scripts/build_site.py` + szablony + CSS** → verify: strona buduje się z przykładowych danych, linki/diakrytyki/gauge OK.
7. **Testy `tests/`** → verify: `python -m unittest discover tests` zielone.
8. **GitHub** — użytkownik tworzy publiczne repo `a50-monitor`; my: `git remote add` + pierwszy push (main); **użytkownik**: dodaje secret `OPENROUTER_API_KEY` i włącza Pages (branch `gh-pages`) → verify: `git push` OK, remote widoczny.
9. **`.github/workflows/daily.yml` + `ci.yml`** — cron + dispatch + deploy → verify: ręczny `workflow_dispatch` przechodzi end-to-end, strona live, drugi run tego samego dnia nie duplikuje wpisu.
10. **`.clinerules/workflows/a50-daily.md`** — ręczny workflow Cline (komendy kroków 4–6 + commit + push) → verify: uruchomienie `/a50-daily` w Cline wykonuje pełny lokalny run.
11. **Finalizacja README** (metodologia, score, disclaimer, instrukcje) → verify: pełny review plików, status git czysty.

## Ryzyka i założenia (jawne)
- **Założenie**: keyless web search silnika zadziała z IP GitHub Actions; jeśli nie — krokiem 4 sprawdzamy i ewentualnie korygujemy `search_sources`/dodajemy RSS fallback (`feeds.txt`, faza 2).
- **Założenie**: score to **ocena ekspercka LLM na podstawie publicznie dostępnych dowodów**, nie informacja oficjalna — będzie to wyraźnie komunikowane na stronie (`about.html`).
- **Ryzyko**: limity Actions (2100 min/mies free — jeden run ~10–20 min, bezpiecznie) i koszty OpenRouter (grosze/dzień — OK).
- **Nie robię**: alertów e-mail/powiadomień, map przebiegu, przetwarzania pełnej treści artykułów (tylko snippety silnika) — poza zakresem TODO; można dodać później.
