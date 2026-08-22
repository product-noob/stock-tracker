"""LLM fact extraction per product_note.html §8: facts/summary/quote only, no severity/score.
Bulk-tier model (gpt-4o-mini); long docs (>12k chars) are truncated and flagged for a
stronger-model re-pass rather than silently degraded, per the §8 tiered-routing rule.
"""
import json
import os
import sys
from pathlib import Path

from openai import OpenAI

sys.path.insert(0, str(Path(__file__).parent))
from companies import get

co = get(sys.argv[1] if len(sys.argv) > 1 else "tataelxsi")
DATA = Path(__file__).parent.parent / "data" / co.slug
MODEL = "gpt-4o-mini"
MAX_CHARS = 12000  # bulk-tier cap; see note in pilot_learnings.md on long-document handling

SCHEMA = {
    "type": "object",
    "properties": {
        "document_kind": {
            "type": "string",
            "enum": [
                "insider_trading", "results", "order_win", "governance",
                "corporate_action", "agm_egm", "board_meeting", "press_release",
                "investor_presentation", "compliance_certificate", "other",
            ],
        },
        "facts": {
            "type": "object",
            "properties": {
                "amount_inr": {"type": ["number", "null"]},
                "counterparty": {"type": ["string", "null"]},
                "pledge_pct_after": {"type": ["number", "null"]},
                "holding_delta_pct": {"type": ["number", "null"]},
                "officer_role": {"type": ["string", "null"]},
                "action": {"type": ["string", "null"]},
                "effective_date": {"type": ["string", "null"]},
                "audit_opinion": {"type": ["string", "null"], "description": "e.g. unmodified, qualified, adverse — only for results filings"},
            },
            "required": ["amount_inr", "counterparty", "pledge_pct_after", "holding_delta_pct", "officer_role", "action", "effective_date", "audit_opinion"],
            "additionalProperties": False,
        },
        "summary_one_line": {"type": "string"},
        "supporting_quote": {"type": "string", "description": "verbatim substring from the source text"},
        "extraction_confidence": {"type": "number"},
        "language": {"type": "string"},
    },
    "required": ["document_kind", "facts", "summary_one_line", "supporting_quote", "extraction_confidence", "language"],
    "additionalProperties": False,
}

PROMPT = """You are extracting structured facts from an Indian stock exchange filing for {company} (NSE: {nse}, BSE: {bse}).

Rules:
- Extract ONLY facts explicitly stated in the text. Never infer or estimate a number that isn't written.
- If a fact is not present, use null.
- supporting_quote must be a verbatim substring copied from the text below (not paraphrased).
- Do NOT assign a severity, sentiment, or recommendation — that is out of scope for this task.

Filing text:
---
{text}
---
"""


def load_client() -> OpenAI:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        # fall back to reading .env directly since this session may not have exported it
        env_path = Path(__file__).parent.parent / ".env"
        for line in env_path.read_text().splitlines():
            if line.startswith("OPENAI_API_KEY="):
                key = line.split("=", 1)[1].strip()
    return OpenAI(api_key=key)


def extract_document(client: OpenAI, text: str) -> dict:
    truncated = len(text) > MAX_CHARS
    payload_text = text[:MAX_CHARS]
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": PROMPT.format(text=payload_text, company=co.name, nse=co.nse_symbol, bse=co.bse_scrip)}],
        response_format={"type": "json_schema", "json_schema": {"name": "filing_extraction", "schema": SCHEMA, "strict": True}},
        temperature=0,
    )
    result = json.loads(resp.choices[0].message.content)
    result["_truncated_input"] = truncated
    result["_input_chars_used"] = len(payload_text)
    result["_usage"] = {"prompt_tokens": resp.usage.prompt_tokens, "completion_tokens": resp.usage.completion_tokens}
    return result


def main():
    manifest = json.loads((DATA / "raw_meta" / "extraction_manifest.json").read_text())
    client = load_client()

    results = []
    total_prompt_tokens = 0
    total_completion_tokens = 0

    for doc in manifest:
        if doc.get("error") or doc.get("image_only"):
            continue
        text = Path(doc["text_path"]).read_text(encoding="utf-8")
        if not text.strip():
            continue
        try:
            extraction = extract_document(client, text)
        except Exception as e:
            print(f"FAIL {doc['sha256'][:12]}  {e}")
            continue

        total_prompt_tokens += extraction["_usage"]["prompt_tokens"]
        total_completion_tokens += extraction["_usage"]["completion_tokens"]

        record = {
            "sha256": doc["sha256"],
            "source_records": doc["source_records"],
            "n_pages": doc["n_pages"],
            **{k: v for k, v in extraction.items() if not k.startswith("_")},
            "truncated_input": extraction["_truncated_input"],
        }
        results.append(record)

        # spot-check: does the claimed quote actually appear in the source text? (§8 "show-your-work" rule)
        quote_verified = record["supporting_quote"].strip() in text
        record["quote_verified"] = quote_verified

        flag = "TRUNCATED" if extraction["_truncated_input"] else ""
        quote_flag = "" if quote_verified else " QUOTE-NOT-FOUND"
        print(f"OK  {doc['sha256'][:12]}  {record['document_kind']:14s}  conf={record['extraction_confidence']:.2f}  {flag}{quote_flag}")

    out_path = DATA / "raw_meta" / "llm_extractions.json"
    out_path.write_text(json.dumps(results, indent=2))

    # gpt-4o-mini pricing: $0.15/1M input, $0.60/1M output (at time of writing)
    cost_usd = (total_prompt_tokens / 1_000_000 * 0.15) + (total_completion_tokens / 1_000_000 * 0.60)
    n_unverified = sum(1 for r in results if not r["quote_verified"])
    print(f"\n{len(results)} documents extracted -> {out_path}")
    print(f"Tokens: {total_prompt_tokens} prompt + {total_completion_tokens} completion")
    print(f"Estimated cost: ${cost_usd:.4f} (~₹{cost_usd * 88:.2f})")
    print(f"Quote verification: {len(results) - n_unverified}/{len(results)} quotes found verbatim in source text")


if __name__ == "__main__":
    main()
