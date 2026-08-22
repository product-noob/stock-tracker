"""Throwaway HTML report: everything downloaded/derived for one company.

Usage: python make_report.py <company_slug>   (writes data/<slug>/report.html)
"""
import html
import json
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from companies import get
from price_reaction import load_prices

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

co = get(sys.argv[1] if len(sys.argv) > 1 else "hdfcbank")
DATA = Path(__file__).parent.parent / "data" / co.slug
e = html.escape


def esc(x):
    return e(str(x)) if x is not None else "—"


norm = json.loads((DATA / "raw_meta" / "normalized_documents.json").read_text(encoding="utf-8"))
manifest = json.loads((DATA / "raw_meta" / "pdf_manifest.json").read_text(encoding="utf-8"))
extractions = {x["sha256"]: x for x in json.loads((DATA / "raw_meta" / "llm_extractions.json").read_text(encoding="utf-8"))}
ranked = json.loads((DATA / "raw_meta" / "ranked_events.json").read_text(encoding="utf-8"))
prices = load_prices()

# join: hash -> manifest rows (source records per unique doc)
by_hash = {}
for r in manifest:
    if r.get("downloaded"):
        by_hash.setdefault(r["sha256"], []).append(r)

sev_color = {5: "#d32f2f", 4: "#ef6c00", 3: "#f9a825", 2: "#9e9e9e", 1: "#bdbdbd"}

# ---- price sparkline (SVG) ----
dates = sorted(prices)
closes = [prices[d]["close"] for d in dates]
lo, hi = min(closes), max(closes)
W, H = 900, 160
pts = " ".join(
    f"{W * i / (len(closes) - 1):.1f},{H - 10 - (H - 30) * (c - lo) / (hi - lo or 1):.1f}"
    for i, c in enumerate(closes)
)
spark = f'<svg viewBox="0 0 {W} {H}" style="width:100%;background:#fafafa;border:1px solid #eee"><polyline points="{pts}" fill="none" stroke="#1565c0" stroke-width="2"/></svg>'

# ---- events table ----
ev_rows = []
for ev in ranked:
    qv = "✅" if ev.get("quote_verified") else "⚠️"
    ev_rows.append(f"""<tr>
<td><span class="badge" style="background:{sev_color[ev['severity']]}">S{ev['severity']}</span></td>
<td>{esc(ev['document_kind'])}</td>
<td>{esc(ev['category'])}</td>
<td>{esc(ev['score'])}</td>
<td>{esc(ev['published_at'][:10])}</td>
<td class="sum">{esc(ev['summary'])}</td>
<td>{'/'.join(ev['exchanges'])}</td>
<td>{qv}</td></tr>""")

# ---- documents table ----
doc_rows = []
for h, recs in sorted(by_hash.items(), key=lambda kv: kv[1][0]["published_at"] or "", reverse=True):
    x = extractions.get(h, {})
    srcs = " + ".join(f"{r['exchange']}:{r['subject']}" for r in recs)
    local = Path(recs[0]["local_path"]) if recs[0].get("local_path") else None
    pdf_link = f'<a href="file:///{local.as_posix()}">pdf</a>' if local and local.exists() else "—"
    urls = "<br>".join(f'<a href="{e(r["document_url"])}">{r["exchange"]} orig</a>' for r in recs if r.get("document_url"))
    doc_rows.append(f"""<tr>
<td><code>{h[:12]}</code></td>
<td>{recs[0]['published_at'][:10]}</td>
<td class="sum">{esc(srcs)}</td>
<td>{esc(x.get('document_kind', '—'))}</td>
<td class="sum">{esc(x.get('summary_one_line', ''))}</td>
<td>{x.get('n_pages','—')}</td>
<td>{pdf_link}<br>{urls}</td></tr>""")

# ---- news ----
root = ET.parse(DATA / "news" / "google_news_raw.xml").getroot()
news_rows = []
for it in root.findall(".//item")[:100]:
    title = it.findtext("title") or ""
    link = it.findtext("link") or ""
    date = it.findtext("pubDate") or ""
    news_rows.append(f'<li><a href="{e(link)}">{e(title)}</a> <span class="muted">{e(date[:16])}</span></li>')

page = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>{co.name} — pipeline dump</title>
<style>
body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:1200px;margin:24px auto;padding:0 16px;color:#222}}
h1{{margin-bottom:2px}} h2{{margin-top:36px;border-bottom:2px solid #1565c0;padding-bottom:4px;color:#1565c0}}
table{{border-collapse:collapse;width:100%;font-size:13px}} th,td{{border:1px solid #ddd;padding:6px 8px;text-align:left;vertical-align:top}}
th{{background:#f5f5f5;position:sticky;top:0}} tr:nth-child(even){{background:#fafafa}}
.badge{{color:#fff;border-radius:10px;padding:2px 8px;font-size:11px;font-weight:bold}}
.sum{{max-width:420px}} .muted{{color:#888;font-size:12px}} code{{font-size:12px}}
.stats{{display:flex;gap:16px;margin:16px 0;flex-wrap:wrap}}
.stat{{background:#f5f7fa;border:1px solid #e0e5ec;border-radius:8px;padding:10px 18px;text-align:center}}
.stat b{{display:block;font-size:26px;color:#1565c0}}
ul{{columns:2;font-size:13px}}
</style></head><body>

<h1>{co.name} <span class="muted">NSE {co.nse_symbol} · BSE {co.bse_scrip}</span></h1>
<p class="muted">Throwaway report — generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} · window: last ~3 months</p>

<div class="stats">
<div class="stat"><b>{len(norm)}</b>feed records<br><span class="muted">{sum(1 for r in norm if r['exchange']=='NSE')} NSE / {sum(1 for r in norm if r['exchange']=='BSE')} BSE</span></div>
<div class="stat"><b>{len(by_hash)}</b>unique docs<br><span class="muted">from {sum(len(v) for v in by_hash.values())} PDFs</span></div>
<div class="stat"><b>{len(extractions)}</b>LLM extracts<br><span class="muted">{sum(1 for x in extractions.values() if x.get('quote_verified'))}/{len(extractions)} quotes verified</span></div>
<div class="stat"><b>{len(ranked)}</b>ranked events</div>
<div class="stat"><b>{len(root.findall('.//item'))}</b>news items<br><span class="muted">Google News RSS (capped)</span></div>
<div class="stat"><b>{len(prices)}</b>price days</div>
</div>

<h2>Price · close, 3 months ({esc(round(lo,1))} → {esc(round(hi,1))})</h2>
{spark}

<h2>Ranked events <span class="muted">(severity × category weight × freshness)</span></h2>
<table><tr><th>Sev</th><th>Kind</th><th>Category</th><th>Score</th><th>Date</th><th>Summary</th><th>Exch</th><th>Quote✓</th></tr>
{''.join(ev_rows)}</table>

<h2>All unique documents <span class="muted">(post hash-dedup; click pdf to open locally)</span></h2>
<table><tr><th>SHA-256</th><th>Date</th><th>Sources (exchange: subject)</th><th>Kind</th><th>LLM summary</th><th>Pages</th><th>Links</th></tr>
{''.join(doc_rows)}</table>

<h2>News <span class="muted">(Google News RSS — forward-looking feed only)</span></h2>
<ul>{''.join(news_rows)}</ul>

</body></html>"""

out = DATA / "report.html"
out.write_text(page, encoding="utf-8")
print(f"wrote {out} ({out.stat().st_size // 1024} KB)")
