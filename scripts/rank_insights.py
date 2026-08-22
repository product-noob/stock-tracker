"""Collate extractions into events, apply deterministic severity (§8) and ranking (§9), render a digest."""
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # Windows cp1252 console can't print ₹/unicode quotes

sys.path.insert(0, str(Path(__file__).parent))
from companies import get

co = get(sys.argv[1] if len(sys.argv) > 1 else "tataelxsi")
DATA = Path(__file__).parent.parent / "data" / co.slug

CATEGORY_WEIGHT = {
    "governance": 1.5,
    "insider": 1.4,
    "results": 1.2,
    "debt_capital": 1.1,
    "growth_ops": 1.0,
    "corporate_action": 0.9,
    "news_colour": 0.4,
}

# Red-flag keyword safety net: scans facts/summary regardless of document_kind,
# because document_kind (LLM-assigned) is demonstrably unreliable on this sample
# (e.g. an income-tax demand notice was classified "compliance_certificate").
RED_FLAG_PATTERNS = [
    (r"auditor.{0,20}resign", 5, "governance"),
    (r"adverse opinion|qualified opinion", 5, "governance"),
    (r"forensic audit", 5, "governance"),
    (r"insolvency", 5, "governance"),
    (r"sebi order|regulatory penalty|penalty.{0,20}sebi", 5, "governance"),
    (r"\b(md|ceo|cfo)\b.{0,20}(resign|exit|step)", 4, "governance"),
    (r"rating downgrade", 4, "debt_capital"),
    (r"credit rating.{0,15}downgrad", 4, "debt_capital"),
]


def classify_severity(doc: dict) -> tuple[int, str, str]:
    """Returns (severity, category, reason)."""
    haystack = f"{doc['summary_one_line']} {json.dumps(doc['facts'])}".lower()

    for pattern, sev, category in RED_FLAG_PATTERNS:
        if re.search(pattern, haystack):
            return sev, category, f"red-flag keyword match: /{pattern}/"

    kind = doc["document_kind"]
    facts = doc["facts"]

    if kind == "results":
        if facts.get("audit_opinion") and facts["audit_opinion"].lower() not in ("unmodified", "unqualified", "clean"):
            return 5, "governance", "non-clean audit opinion on results"
        return 3, "results", "results filed"

    if kind == "corporate_action":
        if any(k in haystack for k in ["dividend", "buyback", "bonus", "split"]):
            return 3, "corporate_action", "dividend/buyback/split/bonus"
        return 2, "corporate_action", "corporate action, non-dividend (e.g. share allotment)"

    if kind == "agm_egm":
        if "dividend" in haystack:
            return 3, "corporate_action", "AGM item states a dividend figure"
        return 1, "corporate_action", "routine AGM/EGM notice or voting result"

    if kind == "board_meeting":
        return 1, "growth_ops", "board meeting intimation"

    if kind == "compliance_certificate":
        # Safety net gap observed live: income-tax demand/dispute content sometimes
        # lands here instead of "governance" because the LLM's document_kind enum
        # conflates "certificate" filings with genuine tax/legal disputes.
        if any(k in haystack for k in ["demand notice", "penalty", "income tax", "commissioner"]):
            return 2, "governance", "tax/legal notice misfiled under compliance_certificate — needs its own document_kind"
        return 1, "governance", "routine compliance certificate"

    if kind == "governance":
        if "trading window" in haystack:
            return 1, "governance", "routine trading window closure"
        return 2, "governance", "governance-adjacent filing, no red-flag keyword hit"

    if kind == "press_release":
        return 2, "growth_ops", "press release: order/partnership/product news"

    return 1, "news_colour", "unclassified / other"


def freshness_factor(published_at: str, now: datetime) -> float:
    days_old = (now - datetime.fromisoformat(published_at.replace("Z", "+00:00"))).days
    return max(0.1, 1.0 - (days_old / 90))  # linear decay over the 3-month window


def main():
    extractions = json.loads((DATA / "raw_meta" / "llm_extractions.json").read_text())
    now = datetime.now(timezone.utc)

    events = []
    for doc in extractions:
        published_at = min(r["published_at"] for r in doc["source_records"])
        # normalize to ISO if needed
        try:
            dt = datetime.fromisoformat(published_at.replace("Z", ""))
        except ValueError:
            dt = datetime.strptime(published_at.split("T")[0], "%Y-%m-%d")
        published_at_iso = dt.replace(tzinfo=timezone.utc).isoformat()

        severity, category, reason = classify_severity(doc)
        weight = CATEGORY_WEIGHT[category]
        fresh = freshness_factor(published_at_iso, now)
        score = severity * weight * fresh

        events.append({
            "sha256": doc["sha256"],
            "document_kind": doc["document_kind"],
            "severity": severity,
            "category": category,
            "severity_reason": reason,
            "score": round(score, 3),
            "published_at": published_at_iso,
            "summary": doc["summary_one_line"],
            "quote": doc["supporting_quote"],
            "exchanges": sorted({r["exchange"] for r in doc["source_records"]}),
            "quote_verified": doc.get("quote_verified", None),
        })

    events.sort(key=lambda e: e["score"], reverse=True)
    (DATA / "raw_meta" / "ranked_events.json").write_text(json.dumps(events, indent=2))

    print(f"{len(events)} events ranked -> {DATA / 'raw_meta' / 'ranked_events.json'}\n")
    print("Severity distribution:", {s: sum(1 for e in events if e["severity"] == s) for s in range(1, 6)})
    print()

    # render a digest mirroring product_note.html §9's mock format
    high = [e for e in events if e["severity"] >= 4]
    moderate = [e for e in events if e["severity"] == 3]
    routine = [e for e in events if e["severity"] <= 2]

    print("=" * 60)
    print(f"MORNING BRIEFING (pilot) · {co.name} · {len(events)} events, 3-month backfill")
    print("=" * 60)
    print(f"\nHIGH ({len(high)})\n")
    for i, e in enumerate(high, 1):
        print(f"{i}. sev {e['severity']} · {e['category']} · score {e['score']}")
        print(f"   {e['summary']}")
        print(f"   \"{e['quote'][:120]}...\"")
        print(f"   {'/'.join(e['exchanges'])} · {e['published_at'][:10]}\n")

    print(f"MODERATE ({len(moderate)})")
    for e in moderate:
        print(f"  - sev {e['severity']} · {e['summary'][:90]}")

    print(f"\nROUTINE ({len(routine)}) — collapsed")


if __name__ == "__main__":
    main()
