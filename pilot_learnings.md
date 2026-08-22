# Pilot Learnings — Tata Elxsi (NSE: TATAELXSI, BSE: 500408)

**Date:** 22 August 2026
**Purpose:** Manually walk one company through source discovery → download → normalize → dedup, before committing to pipeline requirements in `product_note.html`. This file captures what actually worked, what broke, and what it implies for the real build.

Scripts: `scripts/normalize.py`, `scripts/download_pdfs.py`. Data: `data/tataelxsi/`.

---

## 1. Source access — what worked, mechanism by mechanism

| Source | Works? | Mechanism | Gotcha |
|---|---|---|---|
| NSE corporate-announcements API | Yes | Must first GET a normal nseindia.com page with a browser UA to acquire Akamai cookies (`bm_sz`, `_abck`, `AKA_A2`, `nsit`), then call `/api/corporate-announcements?symbol=…&from_date=DD-MM-YYYY&to_date=DD-MM-YYYY` with those cookies + a `Referer` header | Two-step (cookie warm-up, then API call). No cookie = blocked. Confirms product note §5's "Akamai-protected, manual gap-filling" framing — this needs a scheduled cookie-refresh step if ever automated. |
| BSE `AnnSubCategoryGetData` API | Yes | `api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w?...&strScrip=500408&strPrevDate=YYYYMMDD&strToDate=YYYYMMDD` — no cookie warm-up needed, just UA + Referer + Origin headers | Response is `{"Table": [...], "Table1": [...]}`, not a bare array — code must unwrap `Table`. |
| BSE PDF download | Yes, but non-obvious | Filing PDFs live at `bseindia.com/xml-data/corpfiling/AttachHis/{filename}`, **not** `AttachLive/{filename}` once a filing is more than a few days old | `AttachLive` returns HTTP **200** with an HTML error page (not a clean 404) when the file isn't there — a naive "status == 200 → success" check silently saves garbage. Must verify magic bytes (`%PDF`) after download, not just HTTP status. |
| NSE PDF download | Yes | Direct GET on the `attchmntFile` URL with a `Referer: nseindia.com` header — no separate cookie warm-up needed beyond UA | Straightforward once the announcement JSON is in hand. |
| Google News RSS | Partial | `news.google.com/rss/search?q="Tata Elxsi" OR TATAELXSI&hl=en-IN&gl=IN&ceid=IN:en` returns live results, no auth | **Capped at ~100 items regardless of date range** — not a 3-month window, just "most recent/relevant 100." Our result spanned back to Feb 2026 (5+ months) inconsistently, so it cannot be used as a reliable historical backfill tool, only a forward-looking feed. Confirms product note's framing as Tier-2, not a backfill source. |
| Google News article links | Not resolved | `<link>` values are `news.google.com/rss/articles/…` opaque redirect tokens | Requires a follow-redirect step before the article is fetchable or dedup-able against Tier-1 filings by URL. Not attempted this pass — flagged as open work. |
| yfinance (prices) | Yes, after a fix | `yf.download("TATAELXSI.NS", period="3mo")` | Failed initially with `SSLError: self-signed certificate in certificate chain` — this machine sits behind a corporate MITM proxy (Flipkart network). Fix: set `REQUESTS_CA_BUNDLE` / `SSL_CERT_FILE` env vars to `~/.mac-ca-roots` (the corporate root CA bundle) before running. **This is an environment-specific gotcha, not a code bug** — will not reproduce on a personal Linux server, but must be documented so it isn't re-debugged from scratch later. |
| Screener.in fundamentals | Yes (manual only) | Plain `curl` with a browser UA returned the real page (200, correct title, real data) — no blocking observed in this one-off pull | Product note already defers this to Phase 4 due to ToS restricting *automated* access. This pilot did a single manual snapshot, consistent with that — not evidence that scheduled scraping is safe. |

---

## 2. Volume observed (3-month window, one mid-cap company)

- NSE announcements: **28**
- BSE announcements: **30**
- Google News items returned: **100** (capped, not date-bounded)
- Price history: **67** trading days
- PDFs downloaded: **58** (28 NSE + 30 BSE)
- **Unique documents by exact SHA-256 hash: 28** — i.e. cross-exchange duplication is the norm, not the exception, for a company that discloses to both exchanges simultaneously (as required by SEBI LODR).

---

## 3. Dedup — validated, not just theorized

**Result: 58 downloaded PDFs collapse to 28 unique documents by exact SHA-256 hash alone.** No simhash or embedding tier was needed to resolve any of the 27 duplicate groups in this sample — every cross-exchange duplicate was a byte-identical PDF.

This directly validates product note §6/§7's three-tier dedup design (hash → simhash → embedding) and suggests, for at least this filing pattern, **hash-tier dedup does the overwhelming majority of the work for free.** Simhash/embedding tiers exist for genuine paraphrase cases (e.g. two different news outlets covering the same story in their own words) — none of that was tested this pass since only Tier-1 filings were deduped.

**Implication for requirements:** don't under-invest in the hash tier assuming it's a formality before "the real dedup" — for filings specifically, it may be sufficient on its own most of the time. Simhash/embedding remains necessary for Tier-2 news dedup, untested here.

---

## 4. Category/subject fields are not trustworthy classification — confirmed with evidence

Two concrete cases where NSE and BSE tag the **identical, byte-for-byte PDF** with unrelated category strings:

| Same PDF (by hash) | NSE subject | BSE subject |
|---|---|---|
| Newspaper publication (AGM notice) | `Copy of Newspaper Publication` | `Company Update` |
| Quarterly results filing | `Outcome of Board Meeting` | `Result` |
| Insider trading window notice | `Trading Window` | `Insider Trading / SAST` |

**Implication for requirements:** product note §8 already specifies the LLM (not feed metadata) determines `document_kind`. This pilot confirms that's necessary, not just cleaner — the exchange's own category field is inconsistent across exchanges and cannot be used as a shortcut for event typing even as a fallback/cheap-path heuristic.

---

## 5. One document can map to multiple event rows — not previously modeled

One PDF group (a corporate-action / AGM bundle) had **4 separate feed records** pointing at the same content hash: NSE tagged it both `Record Date` and `Updates`; BSE tagged it both `Corp. Action` and `AGM/EGM`. A second group had 3 records (NSE `Shareholders meeting`; BSE `Others` and `AGM/EGM`).

**Implication for requirements:** the pipeline's data model implicitly assumes "one document → one event" (§8's event taxonomy talks about "every event carries... affected ISIN(s)" as if one document maps to one row). This pilot shows a single filing can legitimately trigger **multiple event types simultaneously** (e.g. a combined AGM notice + record-date + corp-action announcement). The `documents` → `intelligence` relationship should be modeled as one-to-many, not one-to-one, or the LLM extraction schema needs a way to emit multiple `document_kind`/event entries from one input document.

---

## 6. Practical fetch-reliability lessons (for the ingestion layer, not just this pilot)

- **HTTP 200 is not proof of success.** BSE's `AttachLive` path returns 200 with an HTML error page for missing files. Every downloader must verify content type/magic bytes, not just status code.
- **NSE requires a cookie warm-up step; BSE does not.** These are structurally different auth models per exchange — the "adapter" abstraction in §6 (uniform FETCH stage) needs to accommodate per-source session state, not assume a stateless GET.
- **Corporate/proxy SSL interception will break naive HTTPS libraries** (`yfinance`/`requests`) on this specific machine (Flipkart-issued laptop) unless `REQUESTS_CA_BUNDLE` is pointed at the local root CA bundle. Irrelevant on the eventual personal Linux server target, but worth a one-line note in setup docs so it isn't re-discovered as a mystery bug later.

---

## 7. Gaps this pilot did not close

- **Google News redirect resolution** — not attempted; needed before Tier-1/Tier-2 dedup-by-URL is possible, and before articles are fetchable for LLM extraction.
- **PDF text-layer extraction** — PDFs are downloaded but not yet parsed into text/tables. Next logical step.
- **Identity resolution ladder (§7, Tiers C–F)** — this pilot used a single known company end-to-end (symbol/scripcode supplied manually), so it never exercised the actual resolution problem (name-only NSE titles, collision risk, alias table). A true test needs either a batch of unresolved raw feed items or a deliberately ambiguous company name.
- **Severity/ranking rules (§8/§9)** — no LLM extraction has been run yet, so the deterministic severity ladder hasn't been tested against real facts.
- **BSE ISIN gap** — BSE's announcement API returns `scripcode` but no ISIN field directly; the normalizer currently leaves `isin: null` for all BSE-origin records pending either (a) a scripcode→ISIN join against a security master, or (b) extracting ISIN from PDF text per §7 Tier C — neither built yet.

---

## 8. Suggested next pilot steps (in priority order) — superseded, see Part 2 below

1. ~~PDF text extraction~~ — done, see §9.
2. Google News redirect resolution + a real cross-Tier dedup test (filing vs. news covering the same event) — still open.
3. ~~One LLM extraction pass~~ — done, see §10.
4. Deliberately test identity resolution against a company with real collision risk (e.g. a Bajaj or Kotak entity) rather than a clean single-company pull — still open.

---

# Part 2 — Extraction, Collation & Insight Generation

**Date:** 22 August 2026 (same-day continuation). Scripts: `scripts/extract_text.py`, `scripts/extract_facts.py`, `scripts/rank_insights.py`.

## 9. PDF text extraction — 28/28 usable, zero OCR needed (this sample)

Ran `pdfplumber` over all 28 unique documents. **Every single one had a usable text layer — zero image-only PDFs.** Page counts ranged from 1 (trading window notice) to 152 (AGM notice bundle, 644KB of text — likely includes the full annual report as an attachment).

**Implication for requirements:** this is one data point, not a verdict, but it argues against urgency on Phase-4 OCR (§16 Q2) for routine large/mid-cap NSE-BSE filings specifically. The risk is concentrated in scanned/legacy filings from smaller companies, not in this filing pattern. Worth measuring across a few more companies before deciding OCR priority.

**Noise, not a defect:** pdfplumber logs high-volume `Cannot set gray non-stroke color` warnings to stderr from pdfminer's colour-space parser. Harmless but must be filtered from pipeline logs or it will drown real errors.

## 10. LLM extraction — cost is trivial, but the "verbatim quote" guarantee failed at high rate and needed real diagnosis

Ran `gpt-4o-mini` (bulk tier) over all 28 documents against the exact JSON schema in §8 (facts/summary/quote, no severity). **Cost: $0.0107 (~₹0.94) for 28 documents** — confirms the ₹1,500/month budget ceiling (§8) is generous headroom even before hitting the whitelist's full 20–25 stocks.

**The important finding is in quote verification.** §8 states: "every insight carries a verbatim supporting quote... an insight whose quote does not contain the claimed fact is a bug." A naive substring check (`quote in source_text`) failed on **25/28 (89%)** of documents. Root-caused in three layers, each a distinct fix:

1. **Whitespace/line-wrap differences (22/25 of the failures):** the PDF text extractor preserves the source's visual line breaks (e.g. a numbered list splits across lines); the LLM naturally renders its "verbatim" quote as flowing text. Normalizing both sides (`re.sub(r'\s+', ' ', s)`) resolved 22 of the 25 failures immediately — these were never real problems, just a verification bug.
2. **Mid-word hyphenation artifacts (1/25):** the PDF extractor sometimes preserves a source line-wrap hyphen as `"co- developed"` (with a stray space) where the LLM correctly reconstructed `"co-developed"`. A real extraction artifact, not a model error — normalized comparison needs hyphenation-aware handling, not just whitespace collapsing.
3. **Genuine clause-dropping by the model (2/25 — the real finding):** on two documents, the LLM's "verbatim" quote silently omitted a middle clause from the source sentence while still presenting it as one continuous quote. Example — source: *"...Thiruvananthapuram, **an Institution of National Importance under the Department of Science and Technology (DST), Government of India,** has signed a Memorandum of Understanding..."* — the model's quote deleted the bolded clause and stitched the sentence back together seamlessly. The underlying *fact* was still accurate; the *verbatim guarantee* was violated in a way a human spot-check would likely miss, because the result still reads as fluent, plausible prose.

**Critical secondary finding:** the model's self-reported `extraction_confidence` was 0.95–1.00 on every one of the 28 documents — including both documents with genuine clause-dropping. **Self-reported confidence has zero correlation with actual verbatim compliance in this sample.** It cannot be used as a signal for routing to a stronger model or a review queue.

**Implications for requirements (this changes §8's design, not just its testing plan):**
- The pipeline needs a **post-hoc, code-level verbatim verification step** (whitespace + hyphenation normalized substring match) run automatically after every extraction — not as an occasional audit, but as a gate before an insight is allowed to ship. This should have been in scope from the start, not discovered by hand-checking a pilot.
- On verification failure, **re-prompt once** with an explicit instruction to copy the exact substring (cheap — one extra call), and if it still fails, **suppress the quote and downgrade confidence automatically** rather than trusting the model's self-reported score.
- `extraction_confidence` as currently specified is decorative. Either drop it from the schema or replace it with something code can actually validate (e.g. did the quote verify, is a required fact null when the document type usually has it).

## 11. Severity classification — plausible results, but exposed two real design gaps

Applied deterministic severity rules (§8) to the 28 extracted documents via keyword/document_kind rules (`scripts/rank_insights.py`). Result: **0 sev-5, 0 sev-4, 6 sev-3, 12 sev-2, 10 sev-1** — no governance red flags in three months for a stable large-cap, which is plausible and reassuring rather than suspicious.

Two gaps surfaced by building the rule engine against real data:

- **`document_kind` (LLM-assigned) is not reliable enough to be the sole severity router.** An income-tax demand-notice dispute landed under `compliance_certificate` in one filing and under `governance` in a near-identical filing about the same underlying tax matter. Routing severity purely off `document_kind` would have silently under-weighted a genuine (if minor) litigation-adjacent event in one case and correctly caught it in the other, by chance of phrasing.
  **Fix implemented and recommended:** run a **red-flag keyword safety net over the extracted facts/summary text directly**, independent of `document_kind` — regex for auditor resignation, adverse/qualified opinion, forensic audit, insolvency, SEBI order, rating downgrade, MD/CEO/CFO exit — and let that override category-based routing. This pilot's rule engine does this and it is the right general pattern: **trust document_kind for category weighting, never trust it alone for red-flag detection.**
- **The `governance` bucket in the §8 event taxonomy is too coarse.** It currently has to hold everything from an auditor resignation (genuinely severity 5) down to a routine BRSR sustainability report submission (severity 1) and a KMP materiality-determination authorization (severity 1). Real data suggests `governance` should split into at least `governance_red_flag` (auditor/regulatory/insolvency) vs. `governance_routine` (BRSR, KMP authorizations, trading window) as distinct `document_kind` values, so severity assignment doesn't depend entirely on keyword-matching within one overloaded bucket.

## 12. Collation/ranking — the real problem is event clustering, not document dedup

This is the most significant finding of the whole pilot. Building the ranked digest surfaced that **one quarterly results announcement produced 6 separate severity-3 entries** in the ranked output: the audited results PDF, the investor presentation, the press release, the conference-call intimation, the board-meeting outcome notice, and (implicitly) the cover letter — each a **genuinely distinct document** (different SHA-256, correctly passed the Part 1 dedup stage) describing **the same underlying business event**.

Content-hash/simhash/embedding dedup (§6) solves "is this the same document." It does **not** solve "are these different documents about the same event" — and that second problem is what actually determines whether a digest reads as concise-and-ranked or repetitive-and-noisy. §9's "per-stock flood cap of 2 entries per digest" is a blunt mitigation for a problem that a proper fix (event clustering) would eliminate at the source, and the flood cap alone risks arbitrarily dropping the better-quality document (e.g. keeping the press release, capping out the audited results) if entries aren't clustered first.

**Implication for requirements:** the pipeline needs an explicit **event-clustering stage between ENRICH and the INSIGHT ENGINE** (§4's pipeline diagram doesn't currently have one) — group documents by (company, tight date window, semantic similarity of summaries/facts) into a single `event` with multiple linked source documents, before scoring and ranking run. This is a materially different component from the dedup already validated in Part 1, and it's not yet designed anywhere in the product note.

## 13. Recommended concrete changes to the product note, in priority order

1. **Add an event-clustering stage** to the pipeline (§4, §6) between enrichment and the insight engine — the single highest-value gap this pilot found.
2. **Add automated, code-level verbatim quote verification** (normalized substring match, retry-once-then-downgrade) as a required pipeline step, not an occasional audit — §8's "show-your-work" rule currently has no enforcement mechanism specified.
3. **Drop or redefine `extraction_confidence`** in the LLM output schema — it did not correlate with actual quote reliability in this sample and cannot be trusted for routing decisions as currently specified.
4. **Split the `governance` category/document_kind into red-flag vs. routine subtypes**, and make severity routing rely on a keyword/pattern safety net over extracted facts text rather than `document_kind` alone.
5. Still open from Part 1: Google News redirect resolution, identity resolution stress-test against a real name-collision company, BSE ISIN backfill via security master join.

## 14. Suggested next pilot steps — superseded for item 1, see Part 3 below

1. ~~Build the event-clustering stage~~ — not yet built as a general stage, but manually proven for one cluster; see §15.
2. Re-run LLM extraction with the retry-on-verification-failure loop and confirm it drives the clause-dropping failure rate toward zero — still open.
3. Pull a second company with a real governance red flag in its recent history (if one exists in the whitelist candidates) to test whether the severity-5 path actually fires correctly — this pilot's company had no red flags to test against — still open.
4. Resume the still-open Part 1 gaps (Google News redirects, identity collision test) — still open.

---

# Part 3 — Price-Reaction Join Prototype

**Date:** 22 August 2026 (same-day continuation). Script: `scripts/price_reaction_prototype.py`. Cluster hardcoded manually (the 6 Q1 FY27 results documents from §12) since the general event-clustering stage isn't built yet — the goal here was to prove the *join* is worth building before investing in clustering automation.

## 15. The join works and produces a genuinely new insight type

Collapsing the 6 results-day documents into one event and joining against `ohlcv_3mo.csv`:

- **Reported:** revenue ₹1,021.1 Cr, **+14.5% YoY**, unmodified (clean) audit opinion. Filed after market close on 14 July.
- **Market reaction (15 July, the next session):** **−4.78%** on **4.2σ volume** versus the stock's own 3-month baseline. The daily-return z-score was **−2.28** — a statistically unusual move, not just background noise.
- **Synthesized insight:** *"Revenue grew 14.5% YoY, but the market sold off hard the next session on abnormal volume — an expectations gap, not just noise."* This is a claim no single document or event-severity score currently produces; it requires the join.

This directly validates the idea proposed at the end of the previous conversation: **the insight engine's real value-add is connecting a filing to how the market actually responded**, not just classifying the filing in isolation.

## 16. But the join needed a second check to avoid a false causal claim — this is the more important finding

Before trusting the z-score as an "insight," it was sanity-checked against another large move in the same window: **1 July had an even more extreme anomaly (−6.3%, return z-score −3.05, volume z-score 4.0)** with no matching filing of any real substance — the only disclosure that day was a trivial 2,497-share ESOP allotment. The stock moved harder on a day with essentially no news than it did on results day.

This means a naive rule — "flag any event whose next-session return z-score exceeds ~1.5–2σ as a market surprise" — would have **equal statistical confidence in a real, explainable signal (results day) and a spurious one (1 July, cause unknown to our sources)**. The z-score alone cannot distinguish "this move happened because of the event we're looking at" from "this stock was just noisy that week for reasons outside our data (sector rotation, broad market, an analyst note we don't ingest, etc.)."

**Implication for requirements — this is a real design requirement the product note doesn't currently have:** any price-reaction insight needs a **second gate before being presented as a claim**, not just a statistical threshold on the move itself:
- Check whether a comparable-or-larger unexplained anomaly occurred elsewhere in the lookback window for the same stock. If so, either suppress the causal framing or explicitly flag the ambiguity ("stock also moved N% on an unrelated day this quarter — treat with caution").
- This is a stronger, evidence-based version of §9's existing "context, not attributed causation" rule for price labels — it's not enough to *label* price as context; the pipeline should actively detect when attribution is unsafe, not just phrase it cautiously.
- A cheap first version of this check: flag any event-reaction pairing where the z-score gap between the event day and the *next-largest unexplained anomaly* in the window is small (e.g. within 1σ of each other) — that's the "this could easily be something else" signal.

## 17. Recommended concrete changes to the product note (adds to §13's list)

6. **Add a price-reaction join as a defined insight type** in the intelligence/signals schema — not just "same-day close move" as passive display context (as currently specified in §9), but an active derived signal: `reaction_pct`, `reaction_zscore`, `volume_zscore` computed against the stock's own trailing baseline, attached to the *clustered event*, not to individual documents.
7. **Require an "unexplained anomaly" cross-check before a price-reaction insight can carry causal framing.** Compute the same z-scores across the full lookback window for the stock; if a comparable or larger unexplained move exists nearby with no corresponding event, suppress or hedge the causal narrative. This is a genuine gap in §9 as currently written, not an implementation nicety.
8. **Add a `revenue_yoy_pct` (or general `growth_pct`) field to the §8 facts schema.** The only reason this prototype could produce "+14.5% YoY" was a regex pulled from free-text summary/quote — the structured `facts` schema has no field for a stated growth percentage at all, only absolute amounts (`amount_inr`) and two hardcoded percent fields (`pledge_pct_after`, `holding_delta_pct`) that don't apply to results filings.

## 18. Suggested next steps

1. Generalize the event-clustering stage (still the top blocking item from Part 2) so this join runs automatically on any cluster, not just this hand-picked one.
2. Implement the anomaly cross-check (§16) as a real function and re-run against the full 3-month window to see how many of the 28 events would actually survive it as safe-to-attribute.
3. Widen the facts schema per §17 item 8 and re-run LLM extraction to capture growth percentages structurally instead of via regex.
4. Everything still open from Parts 1 and 2 (Google News redirects, identity collision test, quote-verification retry loop, governance category split).

---

# Part 4 — Schema v1.2 Architecture Resolution

**Status:** design resolution after the pilot critique. Historical measurements above are unchanged; recommendations superseded below now point to the executable v1.2 model in `db/schema.sql` and `docs/database-schema.md`.

## 19. Pilot finding → durable schema decision

| Pilot finding | v1.2 resolution |
|---|---|
| Exact cross-exchange PDF hashing collapsed 58 downloads to 28 contents | `documents.content_objects` is immutable and SHA-256 unique; source records link M:N through `record_contents`. |
| One PDF can appear through several feed records | Canonical records retain all source sightings through `record_raw_items`; records and contents are M:N. |
| One document can express several event meanings | One extraction can create multiple `extracted_event_mentions`; an event snapshot links exact mentions and contents. |
| Six distinct results-day documents describe one business event | Stable `events` plus immutable `event_versions` separate business-event clustering from byte deduplication. |
| Exchange category labels are unreliable | Raw category remains quarantined metadata; document kind and deterministic business-event kind use separate reference tables. |
| Naive quote verification failed because of whitespace/hyphenation, while two quotes genuinely dropped clauses | Extractions record verifier version, attempts, verified/suppressed state and exact text-version input; unverified evidence is a shipping gate. |
| Model self-confidence was uniformly high and useless | No self-reported extraction-confidence column exists. Quality is represented by observable execution and verification fields. |
| Price reaction needs exact event timing and an anomaly gate | Reactions retain event cutoff, listing-level price series, exact bars, pre-event baseline, method/input versions, benchmark values, unexplained anomaly and attribution reason. |
| Adjusted/raw prices must not mix | Adjustment mode/version is fixed on `market.price_series`, not chosen independently per bar. |
| Google News produces RSS/XML, redirects and HTML rather than exchange PDFs | The content lifecycle supports arbitrary media types and text versions; `exchange`/content URL are not universally required. |
| Rolling feeds can silently lose items | Every feed/symbol/query is a source partition with overlap checkpoints, exact fetch history and reconciliation-based coverage measurement. |
| Async retries can strand work or duplicate outputs | PostgreSQL durable jobs plus transactional outbox; every derived stage has an idempotency key. |
| A later audit may prove an accepted identity wrong | Immutable resolution decisions are preserved; an explicit current-assignment pointer and resolver-controlled normalized cache can be corrected atomically. |

## 20. Corrections to earlier wording

- **Completeness:** cursors and feed-depth telemetry detect risk but do not prove no loss. The product reports measured coverage from reconciliation.
- **Identity:** the durable identity chain is company → instrument/share class → exchange listing. ISIN is an instrument key, not a universal company/listing key.
- **Reproducibility:** prompt/rulepack labels alone are insufficient. v1.2 retains exact text inputs, event snapshots, fingerprints, price bars and method versions.
- **Append-only:** evidence and derived snapshots are now database-guarded. Mutable polling/job/delivery state remains explicitly mutable.
- **Embeddings:** exact hash remains proven for exchange filings. Paraphrase embedding dedup is deferred until Phase-2 news data can validate it.

## 21. Still requires empirical validation

Schema correction does not close the remaining product experiments:

1. identity collision testing across ambiguous company families;
2. Google News redirect resolution and article extraction;
3. normalized verifier retry performance across more companies;
4. automatic cluster quality against a hand-labelled event fixture set;
5. governance severity paths on real adverse filings;
6. no-look-ahead, benchmark-aware reaction calculations beyond the Tata Elxsi example;
7. feed reconciliation through results-season volume.

---

# Part 4 — Second Company Run: HDFC Bank (NSE: HDFCBANK, BSE: 500180)

**Date:** 22 August 2026. Scripts: `scripts/companies.py` (new registry), `fetch_announcements.py`, `price_reaction.py`; all Part-1/2 scripts generalized to take a company slug.

## 19. The pipeline generalizes cleanly — with three new environment findings

Ran the full chain end-to-end on a second, very different company (mega-cap bank vs mid-cap tech): **38 NSE + 42 BSE announcements fetched → 80 PDFs downloaded → 38 unique by hash (36 cross-exchange dup groups) → 38/38 text-usable → LLM extraction (~$0.02) → 37/38 quotes verified after normalization → ranked digest → automated price-reaction join.** Cross-exchange byte-identical duplication held at the same ~95% rate as Tata Elxsi, further validating hash-tier dedup.

New findings from running on a different machine/network (Windows, corporate proxy):

1. **Akamai now defeats plain HTTP clients on nseindia.com entirely** (403 even on the homepage for curl and python-requests). Fix: `curl_cffi` with `impersonate="chrome"` — TLS-fingerprint impersonation — passes warm-up and API calls. This supersedes §1's "cookie warm-up" note: cookies are necessary but no longer sufficient; the client's TLS handshake must look like a browser too.
2. **Both exchange APIs changed contracts since Part 1.** NSE's `/api/corporate-announcements` now rejects queries without `index=equities` (returns `Missing index`). BSE replaced `strCategory/strSubCategory/strPageSize` with `strCat/strType/subcategory` and wants `strSearch=P` for scrip-code lookups (reverse-engineered from ann.html's Angular bundle). Confirms the product note's adapter-with-per-source-session-state requirement — these feeds change without notice.
3. **Windows/console encoding is a real portability bug class**: default cp1252 choked on `₹` in PDF text (`write_text`) and stdout prints. All text I/O needs explicit `encoding="utf-8"`; scripts that print unicode need `sys.stdout.reconfigure(encoding="utf-8")`.

## 20. Quote verification fix confirmed at scale

Applied the whitespace+hyphenation normalization from §10 to HDFC Bank's extractions: **3/38 naive verbatim matches → 37/38 after normalization (34 recovered)**. Same failure profile as the pilot (89% naive failures were never real). One genuine model quote-fabrication remains in 38 docs (~2.6%), consistent with the 2/28 rate in Part 2 — the retry-then-downgrade loop stays a required pipeline component.

## 21. Event-cluster anchoring: the subtle half of clustering nobody scoped

Generalizing the price-reaction join forced automating cluster→event-date mapping, which surfaced a failure mode distinct from anything in Parts 1–3: **which trading session an event "reacts on" depends on per-document publication timestamps, and both simple rules get it wrong.**

- Anchoring on the *latest* filing in the cluster breaks when a follow-up doc (con-call update filed 3 days later, tagged `results` by the LLM) drags the anchor past the real reaction.
- Anchoring on the *earliest* breaks when pre-event noise (a notice filed 2 days before results) masks the real post-publication session.

Rule that validates against both companies: **each document maps to the first trading session strictly after its publication timestamp; the event's reaction session is the modal such session across the cluster, tie-broken earliest.** Reproduces Tata Elxsi exactly (−4.78%, z≈−2.3 on 15 Jul) and correctly lands HDFC Bank on Mon 20 Jul after its Saturday board outcome.

This means event clustering has *two* coupled sub-problems, not one: grouping documents (semantic) AND choosing the event's market-time anchor (temporal). The product note's event-clustering stage spec should cover both.

## 22. The anomaly cross-check works — and immediately earned its keep

The §16 gate, implemented as code, re-ran against Tata Elxsi's full window and **automatically reproduced the manual finding from §16: it flagged 1 July (−6.3%, z=−3.05, unexplained) plus two other comparable anomalies, downgrading the results-day insight to hedged framing.** What took manual spreadsheet work in Part 3 is now one function call.

HDFC Bank Q1 FY27 results, by contrast, passed clean: **−5.12% next session (Mon 20 Jul), z=−3.79 on 1.35% daily stdev, volume z=+1.51, zero comparable-or-larger unexplained anomalies anywhere in the window** — a stronger causal candidate than anything in the Tata Elxsi sample. Notably the drop came despite the filings themselves being routine-looking (board outcome + presentation + con-call intimation), reinforcing that the reaction signal lives in the join, not in any single document.

## 23. State of the pipeline after two companies

Working end-to-end, parameterized by company slug: fetch (NSE/BSE/news/prices) → normalize → download+hash-dedup → text extraction → LLM extraction → normalized quote verification → severity/ranking digest → auto-clustered price-reaction join with anomaly gate.

Still open (unchanged): Google News redirect resolution, identity-resolution stress test, retry-on-quote-failure loop (verification exists, retry doesn't), growth% facts field, governance kind split, full event-clustering stage (the modal-anchor rule above is its temporal half).
