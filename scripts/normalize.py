"""Normalize NSE + BSE announcement JSON for TATAELXSI into a common schema, per pipeline §6."""
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from companies import get

co = get(sys.argv[1] if len(sys.argv) > 1 else "tataelxsi")
DATA = Path(__file__).parent.parent / "data" / co.slug


def load_nse():
    items = json.loads((DATA / "nse" / "announcements_raw.json").read_text())
    out = []
    for it in items:
        out.append({
            "exchange": "NSE",
            "isin": it.get("sm_isin"),
            "company_name": it.get("sm_name"),
            "symbol": it.get("symbol"),
            "scripcode": None,
            "subject": it.get("desc"),
            "description": it.get("attchmntText"),
            "document_url": it.get("attchmntFile"),
            "published_at": datetime.strptime(it["an_dt"], "%d-%b-%Y %H:%M:%S").isoformat(),
            "source_seq_id": it.get("seq_id"),
        })
    return out


def load_bse():
    payload = json.loads((DATA / "bse" / "announcements_raw.json").read_text())
    items = payload.get("Table", [])
    out = []
    for it in items:
        pdf = it.get("ATTACHMENTNAME")
        out.append({
            "exchange": "BSE",
            "isin": None,  # BSE feed doesn't carry ISIN directly, see product note §7
            "company_name": it.get("SLONGNAME"),
            "symbol": None,
            "scripcode": it.get("SCRIP_CD"),
            "subject": it.get("CATEGORYNAME"),
            "description": it.get("HEADLINE"),
            "document_url": f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{pdf}" if pdf else None,
            "published_at": it.get("NEWS_DT"),
            "source_seq_id": it.get("NEWSID"),
        })
    return out


if __name__ == "__main__":
    nse = load_nse()
    bse = load_bse()
    print(f"NSE normalized: {len(nse)}")
    print(f"BSE normalized: {len(bse)}")

    out_path = DATA / "raw_meta" / "normalized_documents.json"
    out_path.write_text(json.dumps(nse + bse, indent=2))
    print(f"Wrote {len(nse) + len(bse)} records -> {out_path}")

    # naive cross-exchange dedup check: same date (day) + subject/description overlap
    print("\n--- potential cross-exchange dupes (same day) ---")
    from collections import defaultdict
    by_day = defaultdict(list)
    for r in nse + bse:
        day = r["published_at"][:10]
        by_day[day].append(r)
    dupe_days = 0
    for day, recs in by_day.items():
        exchanges = {r["exchange"] for r in recs}
        if len(exchanges) == 2 and len(recs) >= 2:
            dupe_days += 1
    print(f"Days with both NSE and BSE items: {dupe_days} / {len(by_day)} total days with activity")
