"""Krok 3: budowa statycznej strony HTML z data/scores.json.

Użycie:
    python scripts/build_site.py [--out site] [--scores PATH]

Generuje: index.html, day-YYYY-MM-DD.html, about.html, style.css.
Zero zależności — szablony string.Template + czyste SVG.
"""
from __future__ import annotations

import argparse
import html
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from string import Template

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import REPO_ROOT, load_scores, scores_path  # noqa: E402

TEMPLATES = REPO_ROOT / "templates"

BANDS = (
    (75, "wysokie", "r", "var(--red)"),
    (50, "podwyższone", "o", "var(--orange)"),
    (25, "umiarkowane", "y", "var(--yellow)"),
    (-1, "niskie", "g", "var(--green)"),
)


def band(score: int) -> tuple[str, str, str]:
    for threshold, label, css, color in BANDS:
        if score >= threshold:
            return label, css, color
    return BANDS[-1][1], BANDS[-1][2], BANDS[-1][3]


def esc(text) -> str:
    return html.escape(str(text or ""), quote=True)


def render_gauge_svg(score: int, color: str) -> str:
    """Półokrągły zegar 0-100% (czysty SVG)."""
    import math
    cx, cy, r = 110, 100, 80
    frac = max(0.0, min(1.0, score / 100.0))
    angle = math.pi * (1 - frac)  # 180° (lewo) -> 0° (prawo)
    x = cx + r * math.cos(angle)
    y = cy - r * math.sin(angle)
    large = 0
    return f'''<svg viewBox="0 0 220 125" width="220" height="125" role="img"
     aria-label="Score prawdopodobieństwa: {score}%">
  <path d="M {cx - r} {cy} A {r} {r} 0 0 1 {cx + r} {cy}"
        fill="none" stroke="var(--border)" stroke-width="16" stroke-linecap="round"/>
  <path d="M {cx - r} {cy} A {r} {r} 0 {large} 1 {x:.1f} {y:.1f}"
        fill="none" stroke="{color}" stroke-width="16" stroke-linecap="round"/>
  <text x="{cx}" y="{cy - 12}" text-anchor="middle" fill="var(--text)"
        font-size="34" font-weight="800">{score}%</text>
  <text x="{cx - r}" y="{cy + 20}" text-anchor="middle" fill="var(--muted)" font-size="11">0%</text>
  <text x="{cx + r}" y="{cy + 20}" text-anchor="middle" fill="var(--muted)" font-size="11">100%</text>
</svg>'''


def render_trend_svg(entries: list[dict]) -> str:
    """Polyline score w czasie (czysty SVG)."""
    if len(entries) < 2:
        return '<p class="meta">Wykres trendu pojawi się po zebraniu co najmniej 2 dni danych.</p>'
    w, h, pad = 720, 180, 34
    n = len(entries)
    xs = [pad + i * (w - 2 * pad) / (n - 1) for i in range(n)]
    ys = [h - pad - (e.get("score", 0) / 100.0) * (h - 2 * pad) for e in entries]
    pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
    dots = " ".join(
        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="var(--accent)">'
        f'<title>{esc(e.get("date"))}: {e.get("score")}%</title></circle>'
        for x, y, e in zip(xs, ys, entries))
    labels = " ".join(
        f'<text x="{x:.1f}" y="{h - 8}" text-anchor="middle" fill="var(--muted)" '
        f'font-size="10">{esc(e.get("date", "")[5:])}</text>'
        for x, e in zip(xs, entries))
    return (f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" '
            f'aria-label="Trend score\'a w czasie">'
            f'<line x1="{pad}" y1="{h - pad}" x2="{w - pad}" y2="{h - pad}" '
            f'stroke="var(--border)"/>'
            f'<line x1="{pad}" y1="{pad}" x2="{pad}" y2="{h - pad}" stroke="var(--border)"/>'
            f'<polyline points="{pts}" fill="none" stroke="var(--accent)" stroke-width="2.5"/>'
            f'{dots}{labels}</svg>')


# ----------------------------------------------------------- fragmenty HTML

def findings_html(entry: dict) -> str:
    items = []
    for f in entry.get("key_findings") or []:
        links = ", ".join(
            f'<a href="{esc(u)}" rel="noopener" target="_blank">źródło</a>'
            for u in f.get("evidence_urls") or [])
        claim = esc(f.get("claim"))
        items.append(f"<li>{claim}" + (f" [{links}]" if links else "") + "</li>")
    if not items:
        return '<p class="meta">Brak kluczowych ustaleń.</p>'
    return '<ul class="findings">' + "".join(items) + "</ul>"


def evidence_table_html(evidence: list[dict]) -> str:
    rows = []
    for ev in evidence:
        title = esc(ev.get("title") or "(bez tytułu)")
        snippet = esc((ev.get("snippet") or "")[:220])
        date_ = esc(ev.get("published") or "b.d.")
        rows.append(
            f"<tr><td><a href=\"{esc(ev.get('url'))}\" rel=\"noopener\" "
            f"target=\"_blank\">{title}</a><br><span class=\"src\">"
            f"{esc(ev.get('source'))} · {date_}</span>"
            + (f"<br><span class=\"src\">{snippet}…</span>" if snippet else "")
            + "</td></tr>")
    if not rows:
        return '<p class="meta">Brak dowodów.</p>'
    return ("<table><thead><tr><th>Dowody (linki do źródeł)</th></tr></thead>"
            "<tbody>" + "".join(rows) + "</tbody></table>")


def archive_html(entries: list[dict]) -> str:
    items = "".join(
        f'<li><a href="day-{esc(e.get("date"))}.html">{esc(e.get("date"))}</a> '
        f'— score {e.get("score")}% ({esc(e.get("confidence"))})</li>'
        for e in reversed(entries))
    return f'<ul class="archive">{items}</ul>'


def render_page(content_name: str, title: str, content: str) -> str:
    content_tpl = (TEMPLATES / content_name).read_text(encoding="utf-8")
    body = Template(content_tpl).safe_substitute(content=content)
    base_tpl = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    return Template(base_tpl).safe_substitute(title=title, content=body)


# ------------------------------------------------------------------- build

def build(scores_file: Path, out_dir: Path) -> None:
    entries = sorted(load_scores(scores_file)["entries"],
                     key=lambda e: e.get("date", ""))
    out_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(TEMPLATES / "style.css", out_dir / "style.css")

    # --- strona dnia (każdy wpis) ---
    for e in entries:
        day = e.get("date", "brak-daty")
        label, css, color = band(e.get("score", 0))
        content = f"""
<h1>Raport z dnia {esc(day)}</h1>
<div class="panel">
  <p class="score-label">Prawdopodobieństwo, że przebieg A50 przetnie teren
     gminy Sobienie-Jeziory:</p>
  <p class="score-big" style="color:{color}">{e.get('score', 0)}%
     <span class="badge {css}">{label}</span></p>
  <p class="meta">Pewność oceny: {esc(e.get('confidence'))} ·
     trend: {e.get('trend_vs_prev', 0):+d} p.p. ·
     dowody: {e.get('sources_found', 0)} ·
     status silnika: {esc(e.get('engine_status'))}</p>
</div>
<div class="panel"><h2>Podsumowanie</h2><p>{esc(e.get('summary'))}</p>
<h2>Uzasadnienie</h2><p>{esc(e.get('rationale'))}</p>
<h2>Kluczowe ustalenia</h2>{findings_html(e)}</div>
<div class="panel">{evidence_table_html(e.get('evidence') or [])}</div>
<p><a href="index.html">← Wróć do aktualnego raportu</a></p>
"""
        (out_dir / f"day-{day}.html").write_text(
            render_page("day.html", f"Raport {day}", content), encoding="utf-8")

    # --- index ---
    if entries:
        latest = entries[-1]
        label, css, color = band(latest.get("score", 0))
        gauge = render_gauge_svg(latest.get("score", 0), color)
        trend = render_trend_svg(entries)
        status_note = "" if latest.get("engine_status") == "ok" else (
            '<p class="disclaimer">Uwaga: najnowszy raport nie ma nowych '
            "dowodów — ocena utrzymana z poprzedniego dnia.</p>")
        content = f"""
<h1>Aktualny score: przebieg A50 przez gminę Sobienie-Jeziory</h1>
{status_note}
<div class="panel" style="display:flex;gap:24px;align-items:center;flex-wrap:wrap">
  {gauge}
  <div>
    <p class="score-label">Prawdopodobieństwo
       <span class="badge {css}">{label}</span></p>
    <p class="score-big" style="color:{color}">{latest.get('score', 0)}%</p>
    <p class="meta">Dzień: {esc(latest.get('date'))} ·
       pewność: {esc(latest.get('confidence'))} ·
       trend: {latest.get('trend_vs_prev', 0):+d} p.p.</p>
  </div>
</div>
<div class="panel"><h2>Podsumowanie dnia</h2><p>{esc(latest.get('summary'))}</p>
<h2>Kluczowe ustalenia</h2>{findings_html(latest)}
<p><a href="day-{esc(latest.get('date'))}.html">Pełny raport z uzasadnieniem i wszystkimi dowodami →</a></p></div>
<div class="panel"><h2>Trend score'a</h2>{trend}</div>
<div class="panel"><h2>Dowody z ostatniego dnia</h2>{evidence_table_html((latest.get('evidence') or [])[:10])}</div>
<div class="panel"><h2>Archiwum raportów</h2>{archive_html(entries)}</div>
<p class="meta">Ostatnia generacja: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</p>
"""
    else:
        content = ("<h1>A50 Monitor</h1>"
                   '<div class="panel"><p>Brak danych — uruchom pipeline '
                   "(research → fetch_feeds → assess → build_site).</p></div>")
    (out_dir / "index.html").write_text(
        render_page("index.html", "Aktualny score", content), encoding="utf-8")

    # --- about ---
    about_content = """
<h1>Metodologia i zastrzeżenia</h1>
<div class="panel">
<h2>Jak to działa</h2>
<p>Codzienny automat (GitHub Actions) uruchamia silnik badawczy
<a href="https://github.com/mvanhorn/last30days-skill" rel="noopener" target="_blank">last30days</a>
(Reddit, YouTube, Hacker News, web) uzupełniony o kanały RSS mediów
polskich (Google News, GDDKiA). Zebrane dowody są oceniane przez model
językowy (OpenRouter), który przydziela score prawdopodobieństwa.</p>
<h2>Co oznacza score</h2>
<p>Score 0–100% to ocena ekspercka LLM: prawdopodobieństwo, że finalny
przebieg A50 przetnie teren gminy Sobienie-Jeziory. Wagi dowodów:
oficjalne komunikaty GDDKiA/ministerstw &gt; uchwały samorządów &gt;
media ogólnopolskie &gt; media lokalne &gt; social media.</p>
<h2>Zastrzeżenia</h2>
<p class="disclaimer">Score <strong>nie jest</strong> informacją oficjalną ani
prognozą ekspercką człowieka. Decyzje o przebiegu dróg podejmuje
GDDKiA i administracja publiczna — źródłem prawdy są zawsze ich
oficjalne komunikaty. Model może się mylić; linki do źródeł są
udostępnione, by każdy mógł zweryfikować dowody samodzielnie.</p>
<h2>Brak nowych dowodów</h2>
<p>Gdy danego dnia nie ma nowych, istotnych dowodów, score pozostaje
bez zmian, a pewność oceny spada do poziomu „niska”.</p>
</div>
"""
    (out_dir / "about.html").write_text(
        render_page("about.html", "Metodologia", about_content), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="Budowa statycznej strony A50 Monitor.")
    ap.add_argument("--out", default="site", help="Katalog wyjściowy (domyślnie site/)")
    ap.add_argument("--scores", help="Ścieżka scores.json (domyślnie data/scores.json)")
    args = ap.parse_args()

    scores_file = Path(args.scores) if args.scores else scores_path()
    out_dir = Path(args.out)
    build(scores_file, out_dir)
    print(f"[build] OK: {out_dir.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
