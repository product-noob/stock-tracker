"""Fetch fundamentals from Screener.in for a company and parse its data tables.

Personal/hobby-use utility — Screener.in ToS restricts automated access at scale,
so keep this to occasional manual runs, one company at a time (see pilot_learnings.md §21).

Usage: python fetch_fundamentals.py <company_slug>

Outputs:
  data/<slug>/fundamentals/screener_raw.html   raw page snapshot
  data/<slug>/fundamentals/fundamentals.json   parsed tables

Parsing uses only the stdlib HTMLParser: every <h2> starts a section; every
<table class="data-table"> under it becomes rows of {row_label: {col_key: value}},
where col_key is the data-date-key attribute (dates) or header text.
"""
import json
import re
import sys
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from companies import get

try:
    from curl_cffi import requests as cr
except ImportError:
    cr = None
    import urllib.request

co = get(sys.argv[1] if len(sys.argv) > 1 else "tataelxsi")
OUT_DIR = Path(__file__).parent.parent / "data" / co.slug / "fundamentals"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def fetch(url: str) -> str:
    if cr is not None:
        r = cr.get(url, headers={"User-Agent": UA}, impersonate="chrome", timeout=30)
        r.raise_for_status()
        return r.text
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", "replace")


# ---------------------------------------------------------------- parsing ---

class TableParser(HTMLParser):
    """Extracts (section, table) pairs: h2 text as section, following data-tables."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.sections = []          # list of {name, tables:[{cols:[...], rows:{label:{col:val}}}]}
        self._h2_buf = None         # collecting h2 text
        self._th_buf = None
        self._td_buf = None
        self._table = None          # current table dict
        self._row = None
        self._cell_kind = None      # 'header' | 'data'
        self._cells = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "h2":
            self._h2_buf = []
        elif tag == "table" and "data-table" in (a.get("class") or ""):
            self._table = {"cols": [], "rows": {}}
            self._pending_headers = None
        elif self._table is not None:
            if tag == "tr" and "hidden" not in (a.get("class") or ""):
                self._row = {"kind": "head" if self._in("thead") else "body"}
                self._cells = []
            elif tag == "th":
                self._cell_kind = "header"
                self._th_buf = []
            elif tag == "td":
                self._cell_kind = "data"
                self._td_buf = []

    def handle_endtag(self, tag):
        if tag == "h2" and self._h2_buf is not None:
            name = re.sub(r"\s+", " ", "".join(self._h2_buf)).strip()
            self._h2_buf = None
            if name:
                self.sections.append({"name": name, "tables": []})
        elif tag == "th" and self._cell_kind == "header":
            txt = re.sub(r"\s+", " ", unescape("".join(self._th_buf or []))).strip()
            key = self._date_key or (txt if txt else f"col{len(self._table['cols'])}")
            self._table["cols"].append(key)
            self._cell_kind = None
        elif tag == "td" and self._cell_kind == "data":
            txt = re.sub(r"\s+", " ", unescape("".join(self._td_buf or []))).strip()
            self._cells.append(txt)
            self._cell_kind = None
        elif tag == "tr" and self._row is not None:
            if self._row["kind"] == "body" and self._cells:
                label = self._cells[0]
                values = {}
                # cols[0] is a placeholder for the label column; values align from cols[1]
                for i, v in enumerate(self._cells[1:]):
                    col = self._table["cols"][i + 1] if i + 1 < len(self._table["cols"]) else f"col{i + 1}"
                    values[col] = v
                if label:
                    self._table["rows"][label] = values
            self._row = None
        elif tag == "table" and self._table is not None:
            if self._table["rows"]:
                sec = self.sections[-1] if self.sections else {"name": "misc", "tables": []}
                if not self.sections or sec is not self.sections[-1]:
                    self.sections.append(sec)
                sec["tables"].append(self._table)
            self._table = None

    def handle_data(self, data):
        if self._h2_buf is not None:
            self._h2_buf.append(data)
        elif self._cell_kind == "header" and self._th_buf is not None:
            # capture data-date-key from the th start tag instead; keep text as fallback
            self._th_buf.append(data)
        elif self._cell_kind == "data" and self._td_buf is not None:
            self._td_buf.append(data)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def _in(self, tag):
        # cheap containment check on the raw get_starttag_text stack we track manually
        return getattr(self, "_stack", []).count(tag) > 0


class DateKeyTableParser(TableParser):
    """Adds data-date-key capture for column keys."""

    def __init__(self):
        super().__init__()
        self._stack = []
        self._date_key = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "th":
            self._date_key = a.get("data-date-key")
        if tag in ("thead", "tbody", "tr", "table"):
            self._stack.append(tag)
        super().handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag in ("thead", "tbody", "tr", "table") and self._stack:
            self._stack.pop()
        super().handle_endtag(tag)


def parse(html: str) -> dict:
    p = DateKeyTableParser()
    p.feed(html)
    return {
        re.sub(r"\s+", " ", s["name"]).strip(): s["tables"] for s in p.sections if s["tables"]
    }


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    urls = [
        f"https://www.screener.in/company/{co.nse_symbol}/consolidated/",
        f"https://www.screener.in/company/{co.nse_symbol}/",
    ]
    html = None
    for url in urls:
        try:
            html = fetch(url)
            print(f"fetched {url} ({len(html)} bytes)")
            break
        except Exception as e:
            print(f"  failed: {e}")
    if html is None:
        sys.exit("could not fetch screener page")

    raw_path = OUT_DIR / "screener_raw.html"
    raw_path.write_text(html)
    print(f"raw snapshot -> {raw_path}")

    sections = parse(html)
    out_path = OUT_DIR / "fundamentals.json"
    out_path.write_text(json.dumps(sections, indent=2))
    print(f"{len(sections)} sections parsed -> {out_path}")
    for name, tables in sections.items():
        n_rows = sum(len(t["rows"]) for t in tables)
        print(f"  - {name}: {n_rows} rows")

    # quick sanity echo
    qr = sections.get("Quarterly Results")
    if qr and qr[0]["rows"]:
        label = next(iter(qr[0]["rows"]))
        last_col = qr[0]["cols"][-1]
        print(f"\nsanity: Quarterly Results / '{label}' latest ({last_col}): "
              f"{qr[0]['rows'][label].get(last_col)}")


if __name__ == "__main__":
    main()
