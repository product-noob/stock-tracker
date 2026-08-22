"""Prototype: join the Q1 FY27 results event-cluster with its price reaction.

Manual cluster for now (event-clustering stage isn't built yet — this hardcodes
the 6 known results-day document hashes to prove the join is worth building).
"""
import csv
import json
import re
import statistics
from pathlib import Path

DATA = Path(__file__).parent.parent / "data" / "tataelxsi"

RESULTS_CLUSTER_HASHES = [
    "1a82381d6a0d",  # investor presentation
    "e03aae4a7267",  # publication of audited results
    "f2b56b6ebc1d",  # con-call audio intimation
    "16ff2c055612",  # press release (has the "14.5% YoY" language)
    "6b6cabc5821c",  # press release, revenue restated
    "54abf3a35e39",  # audited results + auditor's report (audit_opinion: unmodified)
]


def load_prices():
    rows = list(csv.reader(open(DATA / "prices" / "ohlcv_3mo.csv")))
    prices = {}
    for r in rows[3:]:
        if not r or not r[0] or not r[2]:
            continue
        prices[r[0]] = {"close": float(r[2]), "volume": int(float(r[6]))}
    return prices


def load_cluster_facts():
    extractions = json.loads((DATA / "raw_meta" / "llm_extractions.json").read_text())
    cluster = [e for e in extractions if e["sha256"][:12] in RESULTS_CLUSTER_HASHES]

    revenue_cr = next((d["facts"]["amount_inr"] for d in cluster if d["facts"]["amount_inr"]), None)
    audit_opinion = next((d["facts"]["audit_opinion"] for d in cluster if d["facts"]["audit_opinion"]), None)

    # YoY growth isn't in the current extraction schema at all (gap noted in pilot_learnings.md) —
    # pull it out of summary/quote text as a stopgap to prove the concept.
    yoy_pct = None
    for d in cluster:
        m = re.search(r"(\d+(?:\.\d+)?)\s*%\s*YoY", d["summary_one_line"] + " " + d["supporting_quote"], re.I)
        if m:
            yoy_pct = float(m.group(1))
            break

    event_date = min(min(r["published_at"] for r in d["source_records"]) for d in cluster)[:10]

    return {
        "n_documents": len(cluster),
        "revenue_cr": revenue_cr,
        "yoy_growth_pct": yoy_pct,
        "audit_opinion": audit_opinion,
        "event_date": event_date,
        "source_kinds": [d["document_kind"] for d in cluster],
    }


def compute_reaction(prices: dict, event_date: str):
    dates = sorted(prices.keys())
    idx = dates.index(event_date)
    t0, t1 = dates[idx], dates[idx + 1]  # filing came after market close on event_date, so reaction lands on t1
    reaction_pct = (prices[t1]["close"] - prices[t0]["close"]) / prices[t0]["close"] * 100

    # baseline: full-window daily return distribution, to judge if this move is actually unusual
    daily_returns = []
    daily_volumes = []
    for i in range(1, len(dates)):
        prev, cur = prices[dates[i - 1]], prices[dates[i]]
        if prev["close"] and prev["volume"]:
            daily_returns.append((cur["close"] - prev["close"]) / prev["close"] * 100)
            daily_volumes.append(cur["volume"])

    mean_ret, std_ret = statistics.mean(daily_returns), statistics.pstdev(daily_returns)
    mean_vol, std_vol = statistics.mean(daily_volumes), statistics.pstdev(daily_volumes)

    return {
        "reaction_session": t1,
        "close_before": prices[t0]["close"],
        "close_after": prices[t1]["close"],
        "reaction_pct": round(reaction_pct, 2),
        "reaction_zscore": round((reaction_pct - mean_ret) / std_ret, 2),
        "volume_on_reaction_day": prices[t1]["volume"],
        "volume_zscore": round((prices[t1]["volume"] - mean_vol) / std_vol, 2),
        "baseline_daily_move_std_pct": round(std_ret, 2),
    }


def main():
    prices = load_prices()
    facts = load_cluster_facts()
    reaction = compute_reaction(prices, facts["event_date"])

    insight = {**facts, **reaction}

    print(json.dumps(insight, indent=2))
    print()

    # narrative synthesis — this is the actual "insight" a digest would show
    surprise = insight["reaction_zscore"] <= -1.5 and (insight["yoy_growth_pct"] or 0) > 0
    print("=" * 70)
    print("SYNTHESIZED INSIGHT")
    print("=" * 70)
    print(f"Tata Elxsi Q1 FY27 results ({facts['event_date']}): revenue Rs.{facts['revenue_cr']} Cr "
          f"({facts['yoy_growth_pct']}% YoY), audit opinion: {facts['audit_opinion']}.")
    print(f"Market reaction ({reaction['reaction_session']}): {reaction['reaction_pct']}% "
          f"(z-score {reaction['reaction_zscore']} vs 3-month baseline; baseline daily move stdev is "
          f"{reaction['baseline_daily_move_std_pct']}%), on {reaction['volume_zscore']} sigma volume.")
    if surprise:
        print("\n-> FLAG: reported growth was positive but the market reaction was a statistically "
              "unusual negative move (>1.5 sigma) — a genuine 'expectations gap' signal, not just noise.")
    else:
        print("\n-> Move is within/near normal volatility for this stock; not flaggable as a surprise on its own.")

    # collapsed 6-document cluster into 1 event, as the real pipeline should
    print(f"\n(Collapsed {facts['n_documents']} separate documents into 1 event — this is what "
          f"the event-clustering stage needs to do automatically, not just for this hardcoded example.)")


if __name__ == "__main__":
    main()
