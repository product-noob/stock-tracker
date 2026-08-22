"""Price-reaction join, generalized from the Tata Elxsi prototype (§15-17 of pilot_learnings.md).

Improvements over the prototype:
- Event cluster auto-detected (results/earnings-related docs within a +/-3 day window
  around the densest filing date) instead of hardcoded hashes — a lite version of the
  event-clustering stage flagged as the pipeline's top open gap.
- Implements the §16 "unexplained anomaly cross-check": compares the event-day z-score
  against the largest unexplained move elsewhere in the window before allowing causal framing.

Usage: python price_reaction.py <company_slug>
"""
import csv
import json
import re
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from companies import get

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

co = get(sys.argv[1] if len(sys.argv) > 1 else "tataelxsi")
DATA = Path(__file__).parent.parent / "data" / co.slug


def load_prices():
    """Handles both yfinance CSV layouts (old 7-col flat, new multi-header 6-col)."""
    rows = list(csv.reader(open(DATA / "prices" / "ohlcv_3mo.csv")))
    # locate the header row that actually contains 'close'
    h_idx = next(i for i, r in enumerate(rows) if any(c.strip().lower() == "close" for c in r))
    header = [c.strip().lower() for c in rows[h_idx]]
    prices = {}
    for r in rows[h_idx + 1:]:
        if not r or not r[0] or not re.match(r"\d{4}-\d{2}-\d{2}", r[0]):
            continue
        if "volume" in header:
            try:
                close, volume = float(r[header.index("close")]), float(r[header.index("volume")])
            except (ValueError, IndexError):
                continue  # sparse/partial rows in yfinance output
        else:  # old tataelxsi layout: date,,close,...,volume at idx 6
            close, volume = float(r[2]), int(float(r[6]))
        prices[r[0][:10]] = {"close": close, "volume": volume}
    return prices


def load_extractions():
    return json.loads((DATA / "raw_meta" / "llm_extractions.json").read_text())


def parse_dt(s):
    """fromisoformat on py<3.11 rejects non-3/6-digit fractional seconds (BSE emits e.g. .86)."""
    s = s.replace("Z", "+00:00")
    if "." in s:
        head, frac = s.split(".", 1)
        tail = ""
        for ch in ("+", "-"):
            if ch in frac:
                frac, _, tail = frac.partition(ch)
                tail = ch + tail
                break
        s = f"{head}.{frac:<06}{tail}"
    return datetime.fromisoformat(s)


def doc_date(doc):
    dates = sorted(r["published_at"][:10] for r in doc["source_records"])
    return dates[0]


def is_results_related(doc):
    blob = (doc["summary_one_line"] + " " + doc["supporting_quote"]).lower()
    return any(k in blob for k in ["result", "earnings", "financial year", "quarter ended", "q1", "q2", "q3", "q4"])


def detect_cluster(extractions):
    """Group results-related docs into clusters by date proximity (+/-3 days); pick biggest."""
    rel = [(doc_date(d), d) for d in extractions if is_results_related(d)]
    clusters = []
    for date, d in sorted(rel, key=lambda x: x[0]):
        for c in clusters:
            if abs((datetime.fromisoformat(date) - datetime.fromisoformat(c["dates"][0])).days) <= 3:
                c["docs"].append(d)
                c["dates"].append(date)
                break
        else:
            clusters.append({"dates": [date], "docs": [d]})
    clusters.sort(key=lambda c: len(c["docs"]), reverse=True)
    return clusters[0] if clusters else None


def compute_reaction(prices, cluster_docs):
    """Anchor = modal first-tradable session across the cluster's documents (each doc
    maps to the first trading session strictly after its latest publication; the event
    reacts where MOST docs became actionable). Handles both failure modes:
    - ancillary docs published days later (Tata Elxsi con-call update on 17 Jul must not
      shift the anchor away from the 15 Jul reaction to the 14 Jul results),
    - pre-event noise swept into the cluster (HDFC Bank 16 Jul notice must not mask the
      Mon 20 Jul reaction to the Sat 18 Jul board outcome).
    Validated against both pilots."""
    from collections import Counter
    dates = sorted(prices.keys())
    sessions = []
    for d in cluster_docs:
        ts = max(parse_dt(r["published_at"])
                 for r in d["source_records"])
        after = [dt for dt in dates if datetime.fromisoformat(dt).date() > ts.date()]
        if after:
            sessions.append(after[0])
    if not sessions:
        return None
    counts = Counter(sessions)
    top = max(counts.values())
    t1 = min(s for s, n in counts.items() if n == top)  # tie-break: earliest
    prior = [d for d in dates if d < t1]
    if not prior:
        return None
    t0 = prior[-1]
    reaction_pct = (prices[t1]["close"] - prices[t0]["close"]) / prices[t0]["close"] * 100

    daily_returns, daily_volumes = [], []
    for i in range(1, len(dates)):
        prev, cur = prices[dates[i - 1]], prices[dates[i]]
        daily_returns.append((cur["close"] - prev["close"]) / prev["close"] * 100)
        daily_volumes.append(cur["volume"])
    mean_ret, std_ret = statistics.mean(daily_returns), statistics.pstdev(daily_returns)
    mean_vol, std_vol = statistics.mean(daily_volumes), statistics.pstdev(daily_volumes)

    # §16 anomaly cross-check: all days whose |z| >= the event's own |z|,
    # minus the event day itself -> these are "could easily be something else" rivals
    anomalies = []
    for i in range(1, len(dates)):
        d = dates[i]
        ret_z = (daily_returns[i - 1] - mean_ret) / std_ret
        vol_z = (daily_volumes[i - 1] - mean_vol) / std_vol
        anomalies.append({"date": d, "ret_pct": round(daily_returns[i - 1], 2),
                          "ret_z": round(ret_z, 2), "vol_z": round(vol_z, 2)})
    rivals = [a for a in anomalies if abs(a["ret_z"]) >= abs((reaction_pct - mean_ret) / std_ret)
              and a["date"] not in (t0, t1)]

    return {
        "event_session": t0,
        "reaction_session": t1,
        "reaction_pct": round(reaction_pct, 2),
        "reaction_zscore": round((reaction_pct - mean_ret) / std_ret, 2),
        "volume_zscore": round((prices[t1]["volume"] - mean_vol) / std_vol, 2),
        "baseline_daily_move_std_pct": round(std_ret, 2),
        "comparable_or_larger_unexplained_anomalies": rivals,
    }


def main():
    prices = load_prices()
    extractions = load_extractions()

    cluster = detect_cluster(extractions)
    if not cluster:
        print("No results-related cluster found.")
        return

    event_dates = sorted(set(cluster["dates"]))
    n_docs = len(cluster["docs"])
    kinds = [d["document_kind"] for d in cluster["docs"]]
    audit_opinion = next((d["facts"]["audit_opinion"] for d in cluster["docs"] if d["facts"].get("audit_opinion")), None)
    amount = next((d["facts"]["amount_inr"] for d in cluster["docs"] if d["facts"].get("amount_inr")), None)

    print(f"Cluster: {n_docs} docs around {event_dates} · kinds={kinds}")
    print(f"amount_inr={amount} audit_opinion={audit_opinion}\n")

    reaction = compute_reaction(prices, cluster["docs"])
    if not reaction:
        print("No usable price session after event date.")
        return

    print(json.dumps({k: v for k, v in reaction.items()}, indent=2))

    z = reaction["reaction_zscore"]
    rivals = reaction["comparable_or_larger_unexplained_anomalies"]

    print("\n" + "=" * 70)
    print(f"SYNTHESIZED INSIGHT · {co.name}")
    print("=" * 70)
    print(f"Results filed {reaction['event_session']} ({n_docs} documents collapsed into one event).")
    print(f"Market reaction next session ({reaction['reaction_session']}): "
          f"{reaction['reaction_pct']}% (z={z} vs own 3-month baseline; baseline daily stdev "
          f"{reaction['baseline_daily_move_std_pct']}%), volume z={reaction['volume_zscore']}.")
    if abs(z) >= 1.5 and not rivals:
        print(f"\n-> FLAG: statistically unusual move (|z|>=1.5) and NO comparable unexplained "
              f"anomaly elsewhere in the window — causal framing permitted per §16 gate.")
    elif abs(z) >= 1.5 and rivals:
        print(f"\n-> CAUTION: move is statistically unusual, but {len(rivals)} comparable-or-larger "
              f"unexplained anomaly(ies) exist in the same window:")
        for a in rivals:
            print(f"     {a['date']}: {a['ret_pct']}% (z={a['ret_z']}, vol z={a['vol_z']}) — no substantive filing matched")
        print("   Suppress or hedge causal framing per §16.")


if __name__ == "__main__":
    main()
