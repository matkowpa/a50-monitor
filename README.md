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
  → scripts/assess.py        # rubryka PL → OpenRouter → 2 score (północ/południe) + dowody
  → scripts/build_site.py    # statyczny HTML (zero JS, czyste SVG)
  → deploy na gh-pages + commit data/ (historia w repo)
```

- **Silnik**: [last30days](https://github.com/mvanhorn/last30days-skill) (MIT),
  zwendoryzowany w `skill/last30days/` — działa w trybie headless/cron,
  planowanie zapytań przez OpenRouter.
- **Ocena**: model wskazany w `config.json` (`openrouter_model`,
  obecnie `z-ai/glm-5.3-flash`) ocenia dowody wg sztywnej rubryki,
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
| `scripts/` | pipeline (research, fetch_feeds, assess, build_site, common) |
| `skill/last30days/` | zwendoryzowany silnik badawczy |
| `templates/` | szablony strony (string.Template) |
| `data/scores.json` | historia score'ów (committowana) |
| `data/assessments/` | pełne dzienne oceny z dowodami |
| `data/raw/` | surowe raporty silnika + RSS (audyt) |
| `analizy/` | analizy eksperckie (markdown), publikowane na stronie |
| `feeds.txt` | kanały RSS fallback (dodaj własne liniami `URL\|Etykieta`) |
| `config.json` | temat, słowa kluczowe, źródła, model |
| `tests/` | testy jednostkowe (`python -m unittest discover -s tests`) |

## Uruchomienie lokalne (lub przez Cline: `/a50-daily`)

Wymagane: Python 3.12+, `OPENROUTER_API_KEY` w środowisku.

```powershell
python scripts/research.py       # silnik last30days (kilka minut)
python scripts/fetch_feeds.py    # RSS fallback
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
