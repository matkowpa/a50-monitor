# A50 Monitor

Codzienny, automatyczny monitoring mediów i social mediów dotyczący decyzji
o przebiegu **południowej obwodnicy autostradowej Warszawy (A50)** — ze
szczególnym uwzględnieniem ryzyka, że finalna trasa przetnie teren
**gminy Sobienie-Jeziory** (powiat otwocki, woj. mazowieckie).

Strona z raportami (GitHub Pages) publikuje **dwa dzienne, niezależne score
prawdopodobieństwa 0–100%** — dla północnej i południowej strony gminy
względem wsi Sobienie-Jeziory — wraz z uzasadnieniem i linkami do wszystkich źródeł.

🌐 **Strona:** <https://matkowpa.github.io/a50-monitor/> — codzienne raporty
oraz dłuższe analizy eksperckie z folderu [`analizy/`](analizy/).

## Jak to działa

```
GitHub Actions (cron 6:30 PL) lub lokalnie z Cline (/a50-daily)
  → scripts/research.py      # silnik last30days (Reddit, YouTube, HN, web)
  → scripts/fetch_feeds.py   # fallback RSS: Google News, GDDKiA
  → scripts/agent_reach.py   # wyszukiwanie semantyczne agent-reach (Exa przez mcporter)
  → scripts/facebook.py      # treści z Facebooka (tylko lokalnie, sesja OpenCLI)
  → scripts/assess.py        # rubryka PL → OpenRouter → 2 score (północ/południe) + dowody
  → scripts/build_site.py    # statyczny HTML (zero JS, czyste SVG)
  → deploy na gh-pages + commit data/ (historia w repo)
```

- **Silnik**: [last30days](https://github.com/mvanhorn/last30days-skill) (MIT),
  zwendoryzowany w `skill/last30days/` — działa w trybie headless/cron,
  planowanie zapytań przez OpenRouter.
- **Okno świeżości**: do oceny trafiają wyłącznie dowody opublikowane
  w ciągu ostatnich 30 dni od dnia raportu (`lookback_days` w
  `config.json`) — starsze artykuły są odfiltrowywane przed oceną
  (pełne, nieprzefiltrowane zebranie zostaje w `data/raw/` jako audyt),
  a dowody bez daty publikacji zostają, bo ich wieku nie da się
  zweryfikować.
- **Wyszukiwanie agent-reach**: `scripts/agent_reach.py` dopytuje
  semantyczną wyszukiwarkę Exa (serwer MCP `exa` przez `mcporter` —
  definicja w `config/mcporter.json`, bez klucza API) zapytaniami
  z `config.json` → `agent_reach.queries`. Wynik ląduje w
  `data/raw/<dzień>/agent_reach.json` w tym samym formacie co RSS,
  więc ocena traktuje go jak każdy inny dowód. Gdy `mcporter` nie ma
  w PATH (np. runner CI bez Node), krok jest pomijany ze statusem
  `skipped` i **nie przerywa** pipeline'u.
- **Facebook (tylko lokalnie)**: `scripts/facebook.py` czyta grupy/strony
  z `config.json` → `facebook.sources` w Twojej sesji OpenCLI
  (`opencli browser <sesja> open/extract`), zapisuje wynik jako
  `data/raw/<dzień>/facebook.json` (ten sam format co RSS) oraz surowy
  markdown do `data/raw/<dzień>/facebook-raw-<n>.md`. Wymaga Chrome
  z rozszerzeniem OpenCLI i zalogowanego `facebook.com` — Facebook pokazuje
  treść dyskusji tylko członkom grupy, więc krok **nie działa w cronie CI**:
  brak `opencli` ⇒ status `skipped`, pipeline leci dalej. Wpisy dopasowujemy
  węziej niż media (`config.json` → `facebook.keywords`), bo sama nazwa gminy
  występuje w opisie i statystykach grupy. Dowody z FB
  wpływają na score tylko w dniach, gdy uruchomisz `/a50-daily` lokalnie.
- **Ocena**: model wskazany w `config.json` (`openrouter_model`,
  obecnie `deepseek/deepseek-v4.1-flash`) ocenia dowody wg sztywnej rubryki,
  **osobno dla dwóch scenariuszy** — trasa przez północną część gminy
  (na północ od wsi, kierunek Wisły/Natura 2000) lub przez południową
  (na południe od wsi, rejon Śniadków). Wagi dowodów:
  oficjalne komunikaty GDDKiA/ministerstw > uchwały samorządów > media
  ogólnopolskie > media lokalne > social media. Faktograficznie: wieś
  Sobienie-Jeziory leży na DW801/DW739, a DK50 biegnie na północ od
  gminy (przez Karczew/Celestynów do Kołbiela) i jej nie przecina.
- **Brak nowych dowodów danego dnia** → oba score pozostają bez zmian,
  confidence spada do „niska”.

**Skrypty vs model — gdzie co działa.** Pipeline uruchamia się na dwa
sposoby: automatycznie na **GitHub Actions** (cron powyżej lub ręczne
„Run workflow” w zakładce Actions) albo **lokalnie z Cline**
(`/a50-daily`, sekcja niżej) — kod jest identyczny, bo silnik
last30days jest zwendoryzowany w repo. Sam model LLM **nigdy nie działa
ani lokalnie, ani na runnerze GitHuba**: skrypty wysyłają prompt przez
HTTPS do API OpenRouter (`openrouter.ai`) i otrzymują ocenę jako JSON.
„Lokalnie” oznacza wyłącznie to, że skrypt wywołujący działa na Twojej
maszynie (z lokalną zmienną `OPENROUTER_API_KEY`); oceniający model
zawsze jest w chmurze — nie ma tu żadnego lokalnego LLM.

## Struktura repo

| Ścieżka | Opis |
|---|---|
| `scripts/` | pipeline (research, fetch_feeds, agent_reach, facebook, assess, build_site, common) |
| `skill/last30days/` | zwendoryzowany silnik badawczy |
| `templates/` | szablony strony (string.Template) |
| `data/scores.json` | historia score'ów (committowana) |
| `data/assessments/` | pełne dzienne oceny z dowodami |
| `data/raw/` | surowe raporty silnika + RSS (audyt) |
| `analizy/` | analizy eksperckie (markdown), publikowane na stronie |
| `feeds.txt` | kanały RSS fallback (dodaj własne liniami `URL\|Etykieta`) |
| `config.json` | temat, słowa kluczowe, źródła, model |
| `config/mcporter.json` | serwer MCP Exa (wyszukiwanie agent-reach) |
| `tests/` | testy jednostkowe (`python -m unittest discover -s tests`) |

## Uruchomienie lokalne (lub przez Cline: `/a50-daily`)

Wymagane: Python 3.12+, `OPENROUTER_API_KEY` w środowisku.

```powershell
python scripts/research.py       # silnik last30days (kilka minut)
python scripts/fetch_feeds.py    # RSS fallback
python scripts/agent_reach.py    # wyszukiwanie agent-reach (Exa; wymaga mcporter)
python scripts/facebook.py       # Facebook: grupy/strony z configu (lokalnie; Chrome + OpenCLI + login)
python scripts/assess.py         # dwa score (północ/południe) + zapis do data/
python scripts/build_site.py     # strona w site/ (podgląd lokalny)
```

Po lokalnym runie: wyniki lądują w `data/` — historię commitujesz
i wypychasz ręcznie (`git add data && git commit -m "daily data" &&
git push`), a strona w `site/` służy tylko do podglądu (katalog jest
w `.gitignore` i nie jest commitowany). Publikację na GitHub Pages robi
wyłącznie workflow `daily-monitor` — ręcznie (Actions → Run workflow)
albo czekając na cron. Klucz API: lokalnie ze zmiennej środowiskowej,
na GitHubie z secretu repo.

## Setup GitHub (jednorazowo)

1. Utwórz **publiczne** repo `a50-monitor` na github.com (bez README).
2. `git remote add origin https://github.com/<USER>/a50-monitor.git`
   i `git push -u origin main`.
3. **Settings → Secrets and variables → Actions**: dodaj secret
   `OPENROUTER_API_KEY`.
4. **Settings → Pages**: Source = *Deploy from a branch*, Branch = `gh-pages`
   (pojawi się po pierwszym uruchomieniu workflow) / *(root)*.
5. Uruchom workflow ręcznie: **Actions → daily-monitor → Run workflow**.

Strona będzie dostępna pod `https://<USER>.github.io/a50-monitor/`.

## Zastrzeżenia

Score jest **oceną ekspercką modelu językowego** opartą na publicznie
dostępnych dowodach — nie jest informacją oficjalną ani prognozą ekspercką
człowieka. Decyzje o przebiegu dróg podejmuje GDDKiA i administracja
publiczna; źródłem prawdy są ich oficjalne komunikaty. Linki do źródeł są
publikowane przy każdym ustaleniu, by ocenę można było zweryfikować.

## Licencja

MIT (patrz `LICENSE`). Skrypty skilla last30days — MIT (c) mvanhorn.
