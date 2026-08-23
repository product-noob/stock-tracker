#!/usr/bin/env python3
"""Generate data/dashboard.html — a self-contained visual overview of everything
ingested so far, straight from PostgreSQL. Throwaway-friendly: re-run any time.

Usage: venv/bin/python scripts/make_dashboard.py [--out data/dashboard.html]
"""
from __future__ import annotations

import argparse
import html
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).parent))
import jobs  # db_url_from_env

ROOT = Path(__file__).resolve().parent.parent


def q(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def spark(points: list[float], w=110, h=26) -> str:
    if len(points) < 2:
        return ""
    lo, hi = min(points), max(points)
    rng = (hi - lo) or 1.0
    step = w / (len(points) - 1)
    coords = " ".join(f"{i*step:.1f},{h - (v-lo)/rng*(h-4) - 2:.1f}"
                      for i, v in enumerate(points))
    color = "#c0392b" if points[-1] < points[0] else "#1a7f37"
    return (f'<svg width="{w}" height="{h}" class="spark">'
            f'<polyline fill="none" stroke="{color}" stroke-width="1.4" points="{coords}"/></svg>')


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "data" / "dashboard.html"))
    ap.add_argument("--spark-days", type=int, default=90)
    args = ap.parse_args()

    url = jobs.db_url_from_env()
    with psycopg.connect(url) as conn:
        totals = q(conn, """
            select
              (select count(*) from core.companies) companies,
              (select count(*) from ingestion.source_partitions) partitions,
              (select count(*) from ingestion.fetch_runs) fetch_runs,
              (select count(*) from ingestion.raw_artifacts) artifacts,
              (select count(*) from ingestion.raw_feed_items) raw_items,
              (select count(*) from ingestion.normalized_records) records,
              (select count(*) from ingestion.identity_resolutions
                 where outcome='resolved') resolutions_ok,
              (select count(*) from documents.content_objects
                 where media_type='application/pdf') pdfs,
              (select pg_size_pretty(sum(byte_size)) from documents.content_objects) store_size,
              (select count(*) from market.price_bars) price_bars,
              (select max(trade_date) from market.price_bars) last_bar""")[0]

        per_source = q(conn, """
            select p.source, count(distinct p.partition_id) parts,
                   count(r.fetch_run_id) runs,
                   count(*) filter (where r.status='ok') ok,
                   count(*) filter (where r.status='not_modified') nomod,
                   count(*) filter (where r.status='failed') failed
            from ingestion.source_partitions p
            left join ingestion.fetch_runs r using (partition_id)
            group by 1 order by 1""")

        stocks = q(conn, """
            with nse_c as (
              select ia.company_id, count(*) n
              from ingestion.normalized_records n
              join ingestion.record_raw_items rri using (record_id)
              join ingestion.raw_feed_items ri using (item_id)
              join ingestion.source_partitions sp using (partition_id)
              join ingestion.identity_assignments ia on ia.record_id = n.record_id
              where sp.source='nse_api' group by 1),
            bse_c as (
              select ia.company_id, count(*) n
              from ingestion.normalized_records n
              join ingestion.record_raw_items rri using (record_id)
              join ingestion.raw_feed_items ri using (item_id)
              join ingestion.source_partitions sp using (partition_id)
              join ingestion.identity_assignments ia on ia.record_id = n.record_id
              where sp.source='bse_api' group by 1),
            pdfs as (
              select c2.company_id, count(*) n
              from ingestion.raw_artifacts a
              join ingestion.fetch_runs r using (fetch_run_id)
              join ingestion.source_partitions p using (partition_id)
              join core.companies c2 on c2.company_id = p.company_id
              where a.artifact_kind='filing_pdf'
                and coalesce((a.metadata->>'magic_valid')::boolean, true)
              group by 1),
            px as (
              select l.company_id,
                     array_agg(b.close order by b.trade_date) closes,
                     max(b.trade_date) last_dt
              from market.price_bars b
              join market.price_series s using (series_id)
              join core.listings l using (listing_id)
              where b.trade_date > current_date - %s::int
              group by 1)
            select c.canonical_name, c.company_id,
                   coalesce(nse_c.n,0) nse, coalesce(bse_c.n,0) bse,
                   coalesce(pdfs.n,0) pdfs,
                   px.closes, px.last_dt
            from core.companies c
            left join nse_c using (company_id)
            left join bse_c using (company_id)
            left join pdfs using (company_id)
            left join px using (company_id)
            order by (coalesce(nse_c.n,0)+coalesce(bse_c.n,0)) desc""", (args.spark_days,))

        recent = q(conn, """
            with pdfx as (
              select a.artifact_id, a.storage_path, a.byte_size,
                     a.metadata->>'url' src_url,
                     'nse_ann:' || x.native_id AS item_key
              from ingestion.raw_artifacts a,
                   jsonb_array_elements_text(coalesce(a.metadata->'seq_ids','[]'::jsonb)) x(native_id)
              where a.artifact_kind='filing_pdf'
              union all
              select a.artifact_id, a.storage_path, a.byte_size,
                     a.metadata->>'url',
                     'bse_ann:' || x.native_id
              from ingestion.raw_artifacts a,
                   jsonb_array_elements_text(coalesce(a.metadata->'news_ids','[]'::jsonb)) x(native_id)
              where a.artifact_kind='filing_pdf')
            select distinct
              c.canonical_name,
              coalesce(n.title, n.description) as title_or_desc,
              coalesce(n.published_at, n.first_seen_at) as published_at,
              sp.source, n.content_url, n.dedup_key,
              px.storage_path as pdf_path, px.src_url as pdf_src_url, px.byte_size as pdf_bytes
            from ingestion.normalized_records n
            join ingestion.identity_assignments ia on ia.record_id = n.record_id
            join core.companies c on c.company_id = ia.company_id
            left join ingestion.record_raw_items rri on rri.record_id = n.record_id
            left join ingestion.raw_feed_items ri on ri.item_id = rri.item_id
            left join ingestion.source_partitions sp on sp.partition_id = ri.partition_id
            left join pdfx px on px.item_key = ri.idempotency_key
            where n.dedup_scope !~ '^(identtest|test)'
            order by coalesce(n.published_at, n.first_seen_at) desc nulls last
            limit 25""")

        filings = q(conn, """
            select a.storage_path, a.byte_size, a.metadata->>'url' src_url,
                   a.fetched_at, p.partition_key, c.canonical_name,
                   coalesce((a.metadata->>'magic_valid')::boolean, true) magic_ok
            from ingestion.raw_artifacts a
            join ingestion.fetch_runs r using (fetch_run_id)
            join ingestion.source_partitions p using (partition_id)
            left join core.companies c on c.company_id = p.company_id
            where a.artifact_kind = 'filing_pdf'
            order by a.fetched_at desc limit 30""")

        review = q(conn, """
            select outcome, count(*) from ingestion.identity_resolutions
            where outcome <> 'resolved' group by 1""")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    esc = html.escape

    def pct(a, b):
        try:
            return (a / b - 1) * 100
        except (TypeError, ZeroDivisionError):
            return None

    rows = []
    for s in stocks:
        cl = s["closes"] or []
        last = cl[-1] if cl else None
        chg1 = pct(cl[-1], cl[-2]) if len(cl) >= 2 else None
        chg5 = pct(cl[-1], cl[-6]) if len(cl) >= 6 else None
        fmt = lambda v: "—" if v is None else f"{v:+.2f}%"
        cls = lambda v: "" if v is None else (" style='color:#1a7f37'" if v >= 0 else " style='color:#c0392b'")
        rows.append(
            f"<tr><td><strong>{esc((s['canonical_name'] or '')[:34])}</strong></td>"
            f"<td class='num'>{s['nse']}</td><td class='num'>{s['bse']}</td>"
            f"<td class='num'>{s['pdfs']}</td>"
            f"<td class='num'>{'—' if last is None else f'{last:,.2f}'}</td>"
            f"<td class='num'{cls(chg1)}>{fmt(chg1)}</td>"
            f"<td class='num'{cls(chg5)}>{fmt(chg5)}</td>"
            f"<td>{spark(cl[-60:])}</td></tr>")

    src_rows = "".join(
        f"<tr><td><code>{esc(r['source'])}</code></td><td class='num'>{r['parts']}</td>"
        f"<td class='num'>{r['runs']}</td><td class='num'>{r['ok']}</td>"
        f"<td class='num'>{r['nomod']}</td><td class='num'>{r['failed']}</td></tr>"
        for r in per_source)

    rev_rows = "".join(f"<li><code>{esc(str(r['outcome']))}</code>: {r['count']}</li>"
                       for r in review) or "<li>none 🎉</li>"

    rec_rows = []
    for r in recent:
        links = []
        if r.get("pdf_path"):
            links.append(f"<a href='objects/{esc(r['pdf_path'])}'>📄 stored pdf</a>")
        src = r.get("pdf_src_url") or r.get("content_url")
        if src:
            links.append(f"<a href='{esc(src)}' target='_blank'>source</a>")
        link = " ".join(links) or "<span class='muted'>—</span>"
        rec_rows.append(
            f"<tr><td>{esc((str(r['published_at']) if r['published_at'] else '')[:16])}</td>"
            f"<td><code>{esc(r['source'] or '?')}</code></td>"
            f"<td>{esc((r['canonical_name'] or '')[:30])}</td>"
            f"<td>{esc((r['title_or_desc'] or '')[:90])}</td><td>{link}</td></tr>")

    fil_rows = []
    for f in filings:
        size_kb = f["byte_size"] / 1024
        name = (f["src_url"] or "").rsplit("/", 1)[-1][:52] or f["storage_path"][-16:]
        flag = "" if f["magic_ok"] else " ⚠️"
        fil_rows.append(
            f"<tr><td>{f['fetched_at']:%m-%d %H:%M}</td>"
            f"<td>{esc((f['canonical_name'] or f['partition_key'] or '')[:28])}</td>"
            f"<td><a href='objects/{esc(f['storage_path'])}'>{esc(name)}</a>{flag}</td>"
            f"<td class='num'>{size_kb:,.0f} KB</td>"
            f"<td><a href='{esc(f['src_url'] or '#')}' target='_blank'>origin</a></td></tr>")

    page = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<title>Pipeline Dashboard</title>
<style>
 body {{ font-family:'Segoe UI',system-ui,sans-serif; margin:24px auto; max-width:1080px;
        padding:0 20px; color:#1a2332; line-height:1.5; }}
 h1 {{ border-bottom:3px solid #0d6e6e; padding-bottom:8px; font-size:1.5rem; }}
 h2 {{ font-size:1.15rem; margin-top:1.8em; border-bottom:1px solid #dde4ec; }}
 .cards {{ display:flex; gap:12px; flex-wrap:wrap; margin:14px 0; }}
 .card {{ background:#f7f9fc; border:1px solid #dde4ec; border-radius:10px;
          padding:10px 16px; min-width:130px; }}
 .card .v {{ font-size:1.35rem; font-weight:700; color:#0d6e6e; }}
 .card .k {{ font-size:.75rem; color:#5b6b7f; text-transform:uppercase; letter-spacing:.05em; }}
 table {{ border-collapse:collapse; width:100%; font-size:.85rem; margin:10px 0; }}
 th,td {{ border:1px solid #dde4ec; padding:5px 9px; text-align:left; }}
 th {{ background:#f7f9fc; }} .num {{ text-align:right; }}
 .spark {{ vertical-align:middle; }}
 .meta {{ color:#5b6b7f; font-size:.82rem; }}
</style></head><body>
<h1>📊 Stock Tracker — Pipeline Dashboard</h1>
<p class="meta">generated {now} · bars through {totals['last_bar']} · store {totals['store_size']}</p>

<div class="cards">
 <div class="card"><div class="v">{totals['companies']}</div><div class="k">companies</div></div>
 <div class="card"><div class="v">{totals['partitions']}</div><div class="k">partitions</div></div>
 <div class="card"><div class="v">{totals['fetch_runs']}</div><div class="k">fetch runs</div></div>
 <div class="card"><div class="v">{totals['artifacts']}</div><div class="k">raw artifacts</div></div>
 <div class="card"><div class="v">{totals['records']}</div><div class="k">canonical records</div></div>
 <div class="card"><div class="v">{totals['pdfs']}</div><div class="k">filings stored</div></div>
 <div class="card"><div class="v">{totals['price_bars']:,}</div><div class="k">price bars</div></div>
</div>

<h2>Capture health by source</h2>
<table><tr><th>source</th><th>partitions</th><th>runs</th><th>ok</th><th>not_modified</th><th>failed</th></tr>
{src_rows}</table>

<h2>Per-stock coverage &amp; 90-day close</h2>
<table><tr><th>company</th><th>NSE items</th><th>BSE items</th><th>PDFs</th>
<th>last close ₹</th><th>1d</th><th>~1w</th><th>trend</th></tr>
{''.join(rows)}</table>

<h2>Stored filings (latest {len(filings)})</h2>
<p class="meta">📄 links serve the exact stored bytes from the local object store (⚠️ = magic-byte check failed, retained as evidence); “origin” opens the exchange source URL.</p>
<table><tr><th>fetched</th><th>company</th><th>file</th><th>size</th><th>origin</th></tr>
{''.join(fil_rows)}</table>

<h2>Identity review queue</h2><ul>{rev_rows}</ul>

<h2>Latest announcements</h2>
<table><tr><th>published</th><th>src</th><th>company</th><th>title</th><th>evidence</th></tr>
{''.join(rec_rows)}</table>
</body></html>"""

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size/1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
