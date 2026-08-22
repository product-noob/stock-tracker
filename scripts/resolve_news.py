"""Resolve Google News RSS redirect links (news.google.com/rss/articles/<token>) to
real publisher URLs, so news can be deduped by URL and fetched for extraction.

Two strategies, tried in order:
  1. Direct decode: older tokens are base64url protobuf with the target URL inside.
  2. batchexecute: newer tokens wrap an opaque inner signature token. Fetch the
     article page, read data-n-a-id / data-n-a-sg / data-n-a-ts off the c-wiz div,
     then POST a Fbv4je request to Google's batchexecute endpoint.

Results are cached in data/<slug>/news/resolved_links.json keyed by the original
token — re-runs only resolve new items. Be polite: ~0.5s delay between resolutions.

Usage: python resolve_news.py <company_slug> [--limit N]
"""
import base64
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from companies import get

try:
    from curl_cffi import requests as cr
except ImportError:
    sys.exit("pip install curl_cffi")

co = get(sys.argv[1] if len(sys.argv) > 1 else "tataelxsi")
limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else None

NEWS_DIR = Path(__file__).parent.parent / "data" / co.slug / "news"
RSS_PATH = NEWS_DIR / "google_news_raw.xml"
OUT_PATH = NEWS_DIR / "resolved_links.json"
DELAY = 0.5


def load_items():
    xml = RSS_PATH.read_text()
    items = []
    for chunk in re.findall(r"<item>(.*?)</item>", xml, re.S):
        title = re.search(r"<title>(.*?)</title>", chunk, re.S)
        link = re.search(r"<link>(https://news\.google\.com/rss/articles/[^<]+)</link>", chunk)
        src = re.search(r'<source[^>]*>(.*?)</source>', chunk, re.S)
        pub = re.search(r"<pubDate>(.*?)</pubDate>", chunk)
        if not link:
            continue
        items.append({
            "title": title.group(1).strip() if title else None,
            "source": re.sub(r"<[^>]+>", "", src.group(1)).strip() if src else None,
            "published": pub.group(1).strip() if pub else None,
            "news_url": link.group(1),
            "token": link.group(1).rsplit("/", 1)[1].split("?")[0],
        })
    return items


def decode_direct(token: str):
    """Old-style tokens: protobuf contains a plain https URL string."""
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except Exception:
        return None
    m = re.search(rb"https?://[\x20-\x7e]+", raw)
    if not m:
        return None
    url = m.group(0).decode()
    if b"\xc2" in m.group(0):  # protobuf field boundary artifacts
        url = url.split("�")[0]
    if "news.google.com" in url or "googleusercontent" in url:
        return None
    return url.rstrip()


def resolve_batchexecute(session, token: str):
    r = session.get(f"https://news.google.com/rss/articles/{token}", timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"article page HTTP {r.status_code}")
    nid = re.search(r'data-n-a-id="([^"]+)"', r.text)
    sg = re.search(r'data-n-a-sg="([^"]+)"', r.text)
    ts = re.search(r'data-n-a-ts="([^"]+)"', r.text)
    if not (nid and sg and ts):
        raise RuntimeError("c-wiz signature attrs not found")
    inner = json.dumps([
        "garturlreq",
        [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None,
          None, None, 0, 1], "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
        nid.group(1), int(ts.group(1)), sg.group(1),
    ])
    freq = json.dumps([[["Fbv4je", inner]]])
    r2 = session.post(
        "https://news.google.com/_/DotsSplashUi/data/batchexecute",
        data={"f.req": freq},
        headers={"content-type": "application/x-www-form-urlencoded;charset=UTF-8"},
        timeout=30,
    )
    if r2.status_code != 200:
        raise RuntimeError(f"batchexecute HTTP {r2.status_code}")
    urls = [u for u in re.findall(r'https?://[^"\\]+', r2.text)
            if "google.com" not in u and "googleusercontent" not in u and "gstatic" not in u]
    if not urls:
        raise RuntimeError("no publisher URL in batchexecute response")
    return urls[0]


def main():
    items = load_items()
    print(f"RSS items with google-news links: {len(items)}")

    cache = {}
    if OUT_PATH.exists():
        cache = json.loads(OUT_PATH.read_text())

    todo = [i for i in items if i["token"] not in cache]
    if limit:
        todo = todo[:limit]
    print(f"to resolve: {len(todo)} (cached: {len(items) - len([i for i in items if i['token'] not in cache])})")
    session = cr.Session(impersonate="chrome")

    ok = fail = 0
    for n, item in enumerate(todo, 1):
        token = item["token"]
        try:
            url = decode_direct(token)
            method = "direct-decode"
            if not url:
                url = resolve_batchexecute(session, token)
                method = "batchexecute"
            cache[token] = {"resolved_url": url, "method": method}
            ok += 1
            print(f"[{n}/{len(todo)}] {method}: {url[:100]}")
        except Exception as e:
            cache[token] = {"resolved_url": None, "error": str(e)}
            fail += 1
            print(f"[{n}/{len(todo)}] FAILED: {e}")
        time.sleep(DELAY)

    OUT_PATH.write_text(json.dumps(cache, indent=2))
    print(f"\ndone: {ok} resolved, {fail} failed -> {OUT_PATH}")

    # join back onto items for a quick view
    resolved = [{**i, **cache.get(i["token"], {})} for i in items]
    by_src = {}
    for r_ in resolved:
        if r_.get("resolved_url"):
            host = re.match(r"https?://(?:www\.)?([^/]+)", r_["resolved_url"])
            host = host.group(1) if host else "?"
            by_src[host] = by_src.get(host, 0) + 1
    print("publishers:", dict(sorted(by_src.items(), key=lambda kv: -kv[1])))


if __name__ == "__main__":
    main()
