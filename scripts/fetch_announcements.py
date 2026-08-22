"""Fetch raw NSE + BSE announcements + Google News RSS + prices for a company.

Usage: python fetch_announcements.py <company_slug> [from_date DD-MM-YYYY] [to_date]

NSE requires Akamai cookie warm-up; curl_cffi's Chrome TLS impersonation gets past
the bot check that plain requests/curl fail (403) from some networks (see
pilot_learnings.md §1 — mechanism validated on the Tata Elxsi pilot).
"""
import json
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from companies import get

from curl_cffi import requests as cr

ROOT = Path(__file__).parent.parent
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def nse_session() -> cr.Session:
    s = cr.Session(impersonate="chrome")
    r = s.get("https://www.nseindia.com/", timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"NSE warm-up failed: HTTP {r.status_code}")
    return s


def fetch_nse(s: cr.Session, symbol: str, out: Path, frm: str, to: str):
    # NOTE: NSE started rejecting this endpoint with 'Missing index' unless
    # index=equities is supplied (worked without it during the Tata Elxsi pilot).
    r = s.get(
        "https://www.nseindia.com/api/corporate-announcements",
        params={"symbol": symbol, "from_date": frm, "to_date": to, "index": "equities"},
        headers={"Referer": "https://www.nseindia.com/all-corporate-announcements",
                 "Accept": "application/json, text/plain, */*",
                 "X-Requested-With": "XMLHttpRequest"},
        timeout=30,
    )
    print(f"NSE API: HTTP {r.status_code}")
    if r.status_code != 200:
        print(f"  body head: {r.text[:200]}")
        return 0
    data = r.json()
    rows = data.get("data", data) if isinstance(data, dict) else data
    out.write_text(json.dumps(rows, indent=2))
    print(f"  {len(rows)} announcements -> {out}")
    return len(rows)


def fetch_bse(scrip: str, out: Path, frm: str, to: str):
    # BSE changed this endpoint's params since the Tata Elxsi pilot (Aug 2026):
    # old strCategory/strSubCategory/strPageSize -> new strCat/strType/subcategory,
    # and strSearch now takes "P" for scrip-code lookups. Reverse-engineered from
    # bseindia.com/corporates/ann.html's Angular bundle.
    f = "".join(frm.split("-")[::-1])
    t = "".join(to.split("-")[::-1])
    url = (
        "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w?"
        f"pageno=1&strCat=-1&strPrevDate={f}&strScrip={scrip}&strSearch=P"
        f"&strToDate={t}&strType=C&subcategory=-1"
    )
    s = cr.Session(impersonate="chrome")
    r = s.get(url, headers={
        "Referer": "https://www.bseindia.com/corporates/ann.html",
        "Origin": "https://www.bseindia.com",
        "Accept": "application/json, text/plain, */*",
    }, timeout=30)
    print(f"BSE API: HTTP {r.status_code}")
    if r.status_code != 200:
        print(f"  body head: {r.text[:200]}")
        return 0
    payload = r.json()
    rows = payload.get("Table", [])
    out.write_text(json.dumps(payload, indent=2))
    print(f"  {len(rows)} announcements -> {out}")
    return len(rows)


def fetch_news(query: str, out: Path):
    q = query.replace('"', "%22").replace(" ", "+")
    url = f"https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"
    r = cr.get(url, impersonate="chrome", timeout=30)
    out.write_bytes(r.content)
    print(f"Google News RSS: HTTP {r.status_code}, {len(r.content)} bytes -> {out}")


def fetch_prices(yahoo_symbol: str, out: Path, ca_bundle: str | None):
    env = ""
    script = f'import yfinance as yf; df = yf.download("{yahoo_symbol}", period="3mo"); df.to_csv("{out.as_posix()}")'
    cmd = [sys.executable, "-c", script]
    subprocess.run(cmd, check=True, env={"REQUESTS_CA_BUNDLE": ca_bundle} if ca_bundle else None)
    print(f"prices -> {out}")


def main():
    slug = sys.argv[1] if len(sys.argv) > 1 else "hdfcbank"
    co = get(slug)
    frm = sys.argv[2] if len(sys.argv) > 2 else (date.today() - timedelta(days=92)).strftime("%d-%m-%Y")
    to = sys.argv[3] if len(sys.argv) > 3 else date.today().strftime("%d-%m-%Y")

    d = ROOT / "data" / co.slug
    for sub in ("nse", "bse", "news", "prices", "raw_meta"):
        (d / sub).mkdir(parents=True, exist_ok=True)

    print(f"=== {co.name} (NSE {co.nse_symbol} / BSE {co.bse_scrip}) window {frm} .. {to} ===\n")

    s = nse_session()
    n_nse = fetch_nse(s, co.nse_symbol, d / "nse" / "announcements_raw.json", frm, to)
    n_bse = fetch_bse(co.bse_scrip, d / "bse" / "announcements_raw.json", frm, to)
    fetch_news(" OR ".join(co.aliases), d / "news" / "google_news_raw.xml")

    try:
        fetch_prices(co.yahoo_symbol, d / "prices" / "ohlcv_3mo.csv", None)
    except Exception as e:
        print(f"yfinance failed: {e}")

    print(f"\nDone. NSE={n_nse} BSE={n_bse}")


if __name__ == "__main__":
    main()
