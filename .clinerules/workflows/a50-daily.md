# Codzienny monitoring A50 — ręczny run ($ARGUMENTS: opcjonalna data ISO)

Uruchamia lokalnie ten sam pipeline, co GitHub Actions (workflow `daily-monitor`).
Wymagane: `OPENROUTER_API_KEY` w środowisku. Kroki wykonuj po kolei, komendy
uruchamiaj z katalogu repo (ścieżki względne):

1. Research (silnik last30days, ~2–10 min, wykorzystuje OpenRouter):

   ```
   python scripts/research.py
   ```

   Jeśli zwraca FAIL — sprawdź `data/raw/<dzień>/engine.log`; przejdź dalej
   mimo to (RSS fallback pokryje media).

2. Fallback RSS (Google News + GDDKiA):

   ```
   python scripts/fetch_feeds.py
   ```

3. Ocena + score (OpenRouter, JSON):

   ```
   python scripts/assess.py
   ```

   Wynik wypisz użytkownikowi: score %, confidence, podsumowanie (polski),
   trend względem poprzedniego dnia. Plik: `data/assessments/<dzień>.json`.

4. Budowa strony:

   ```
   python scripts/build_site.py
   ```

   Otwórz `site/index.html` w przeglądarce do podglądu (katalog `site/` jest
   w `.gitignore` — NIE commituj go).

5. Commit + push danych historycznych (tylko `data/`, nigdy `site/`):

   ```
   git add data
   git commit -m "daily data <dzień>"
   git push
   ```

   Jeśli repo nie ma jeszcze remote — poinformuj użytkownika i pomiń push.

6. Podsumuj: score, kluczowe ustalenia z linkami (2–4 pozycje), zmiany
   względem wczoraj. Nie redaguj treści assessmentu — relacjonuj plik.
