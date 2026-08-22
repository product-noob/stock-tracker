-- =============================================================================
-- Stock Market Insights Engine — PostgreSQL Schema v1
-- =============================================================================
-- Conventions:
--   * One DB, one schema per plane (ingestion / documents / intelligence /
--     market / delivery / core). A service owns its schema; cross-schema reads
--     only via the owning service's exposed views (added later if needed).
--   * Append-only where marked: no UPDATE/DELETE in normal operation.
--   * All timestamps: TIMESTAMPTZ (IST conversions at the edges).
--   * Money: NUMERIC, never float.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS ingestion;
CREATE SCHEMA IF NOT EXISTS documents;
CREATE SCHEMA IF NOT EXISTS intelligence;
CREATE SCHEMA IF NOT EXISTS market;
CREATE SCHEMA IF NOT EXISTS delivery;

-- =============================================================================
-- CORE — whitelist, security master, aliases
-- =============================================================================

CREATE TABLE core.companies (
    company_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    canonical_name  TEXT NOT NULL,
    nse_symbol      TEXT UNIQUE,
    bse_scripcode   TEXT UNIQUE,
    isin            TEXT UNIQUE CHECK (isin ~ '^[A-Z]{2}[A-Z0-9]{9}[0-9]$'),
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'paused', 'removed')),
    notes           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- The whitelist: 20–25 rows. Uniqueness on each exchange key lets the
-- identity resolver do Tier-A exact matches with a plain index lookup.

CREATE TABLE core.company_aliases (
    alias_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company_id      BIGINT NOT NULL REFERENCES core.companies(company_id),
    alias_value     TEXT NOT NULL,          -- e.g. "Tata Elxsi Ltd", "TATAELXSI", "ताता एलएक्सआई"
    alias_kind      TEXT NOT NULL DEFAULT 'name_variant'
                    CHECK (alias_kind IN ('name_variant', 'ticker', 'isin', 'scripcode', 'manual')),
    source          TEXT,                   -- who added it: 'seed', 'resolver_learning', 'human'
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (alias_kind, alias_value)
);
CREATE INDEX idx_aliases_value ON core.company_aliases (lower(alias_value));

-- =============================================================================
-- INGESTION — raw feed items (append-only), normalized records, resolution log
-- =============================================================================

CREATE TABLE ingestion.raw_feed_items (
    item_id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source          TEXT NOT NULL CHECK (source IN ('nse_api', 'bse_api', 'nse_rss', 'bse_rss', 'google_news')),
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at    TIMESTAMPTZ,            -- as claimed by the source, raw
    external_id     TEXT,                   -- seq_id / scripcode+ts / google token
    document_url    TEXT,
    title           TEXT,
    description     TEXT,
    raw_payload     JSONB NOT NULL,         -- the untouched item, replay guarantee
    fetch_meta      JSONB                   -- http status, headers of interest, cookie state id
);
-- Append-only. Dedup of fetches:
CREATE UNIQUE INDEX uq_raw_dedupe ON ingestion.raw_feed_items (source, md5(raw_payload::text));
CREATE INDEX idx_raw_url ON ingestion.raw_feed_items (document_url) WHERE document_url IS NOT NULL;

CREATE TABLE ingestion.normalized_records (
    record_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    raw_item_id     BIGINT NOT NULL REFERENCES ingestion.raw_feed_items(item_id),
    exchange        TEXT NOT NULL CHECK (exchange IN ('NSE', 'BSE')),
    company_name_raw TEXT NOT NULL,         -- as it appeared in the feed
    nse_symbol      TEXT,
    bse_scripcode   TEXT,
    isin            TEXT,                   -- from feed metadata only; NULL for BSE (pilot §7)
    subject         TEXT,
    description     TEXT,
    category_raw    TEXT,                   -- exchange's own category: NEVER trusted for classification (pilot §4)
    document_url    TEXT NOT NULL,
    published_at    TIMESTAMPTZ NOT NULL,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_record_dedupe UNIQUE (exchange, document_url, published_at)
);
CREATE INDEX idx_records_url ON ingestion.normalized_records (document_url);

CREATE TABLE ingestion.identity_resolutions (
    resolution_id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    record_id       BIGINT NOT NULL REFERENCES ingestion.normalized_records(record_id),
    company_id      BIGINT REFERENCES core.companies(company_id),   -- NULL = unresolved
    tier            TEXT NOT NULL CHECK (tier IN ('A_symbol', 'B_scripcode', 'C_isin',
                                                  'D_exact_name', 'E_alias_name', 'F_manual')),
    confidence      NUMERIC(4,3) CHECK (confidence BETWEEN 0 AND 1),
    resolved        BOOLEAN NOT NULL,
    needs_review    BOOLEAN NOT NULL DEFAULT false,
    attempted_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_resolutions_record ON ingestion.identity_resolutions (record_id);
CREATE INDEX idx_resolutions_unresolved ON ingestion.identity_resolutions (needs_review) WHERE needs_review;

-- =============================================================================
-- DOCUMENTS — append-only doc registry, M:N links, extracted text
-- =============================================================================

CREATE TABLE documents.documents (
    doc_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sha256          CHAR(64) NOT NULL UNIQUE,   -- hash-tier dedup: the pilot's 58→28 collapse
    document_url    TEXT NOT NULL,
    first_seen_via  TEXT NOT NULL CHECK (first_seen_via IN ('NSE', 'BSE')),
    storage_path    TEXT NOT NULL,          -- object store key: s3://... or ./data/pdfs/<sha256>.pdf
    byte_size       BIGINT,
    page_count      INT,
    is_valid_pdf    BOOLEAN NOT NULL DEFAULT false,  -- magic-byte check result (pilot §6)
    downloaded_at   TIMESTAMPTZ,
    download_status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (download_status IN ('pending', 'ok', 'failed', 'invalid_content'))
);
-- Append-only: a hash never changes once seen. Re-downloads just re-confirm.

CREATE TABLE documents.document_records (
    doc_id          BIGINT NOT NULL REFERENCES documents.documents(doc_id),
    record_id       BIGINT NOT NULL REFERENCES ingestion.normalized_records(record_id),
    PRIMARY KEY (doc_id, record_id)
);
-- M:N on purpose: pilot §5 — one PDF appeared as 4 feed records across both
-- exchanges. Never assume 1:1.

CREATE TABLE documents.document_texts (
    doc_id              BIGINT PRIMARY KEY REFERENCES documents.documents(doc_id),
    text                TEXT NOT NULL,
    char_count          INT NOT NULL,
    extraction_method   TEXT NOT NULL DEFAULT 'pdfplumber'
                        CHECK (extraction_method IN ('pdfplumber', 'ocr_tesseract', 'manual')),
    extraction_ok       BOOLEAN NOT NULL,
    extracted_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- One text per doc (re-extraction replaces the row; old text lives in object store).

-- =============================================================================
-- INTELLIGENCE — extractions, events, insights, reactions
-- =============================================================================

CREATE TABLE intelligence.extractions (
    extraction_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    doc_id              BIGINT NOT NULL REFERENCES documents.documents(doc_id),
    model               TEXT NOT NULL,              -- 'gpt-4o-mini'
    prompt_version      TEXT NOT NULL,              -- schema evolution tracking (pilot §17)
    document_kind       TEXT NOT NULL,              -- LLM-assigned; router input, never sole signal
    summary             TEXT NOT NULL,
    quote               TEXT,                       -- NULL if suppressed after failed verification
    quote_verified      BOOLEAN NOT NULL,           -- code-level normalized substring check
    verify_attempts     SMALLINT NOT NULL DEFAULT 0,
    quote_suppressed    BOOLEAN NOT NULL DEFAULT false,
    facts               JSONB NOT NULL DEFAULT '{}', -- full facts payload; evolves with prompt_version
    cost_usd            NUMERIC(8,4),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_extractions_doc ON intelligence.extractions (doc_id);
CREATE INDEX idx_extractions_kind ON intelligence.extractions (document_kind);
-- No self-reported confidence column by design — pilot §10 proved it carries
-- zero signal. Quality = quote_verified + verify_attempts, both code-computed.

CREATE TABLE intelligence.events (
    event_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company_id      BIGINT NOT NULL REFERENCES core.companies(company_id),
    event_kind      TEXT NOT NULL,              -- dominant kind after clustering
    window_start    TIMESTAMPTZ NOT NULL,
    window_end      TIMESTAMPTZ NOT NULL,
    cluster_method  TEXT NOT NULL DEFAULT 'manual'  -- 'manual' | 'v1_similarity'
                    CHECK (cluster_method IN ('manual', 'v1_similarity')),
    title           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_events_company_window ON intelligence.events (company_id, window_start DESC);

CREATE TABLE intelligence.event_documents (
    event_id        BIGINT NOT NULL REFERENCES intelligence.events(event_id),
    doc_id          BIGINT NOT NULL REFERENCES documents.documents(doc_id),
    role            TEXT,                       -- e.g. 'results_pdf', 'press_release', 'presentation'
    PRIMARY KEY (event_id, doc_id)
);
-- M:N on purpose: the pilot's 6-documents-one-results-event case.

CREATE TABLE intelligence.insights (
    insight_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id        BIGINT NOT NULL REFERENCES intelligence.events(event_id),
    severity        SMALLINT NOT NULL CHECK (severity BETWEEN 1 AND 5),
    severity_basis  TEXT NOT NULL CHECK (severity_basis IN ('category_rule', 'red_flag_override', 'manual')),
    red_flag_hits   TEXT[],                     -- which red-flag regexes fired (empty = none)
    headline        TEXT NOT NULL,
    rationale       TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_insights_event ON intelligence.insights (event_id);
CREATE INDEX idx_insights_severity ON intelligence.insights (severity DESC, created_at DESC);

CREATE TABLE intelligence.reaction_signals (
    signal_id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id            BIGINT NOT NULL UNIQUE REFERENCES intelligence.events(event_id),
    reaction_pct        NUMERIC(8,4),           -- next-session close-to-close %
    return_zscore       NUMERIC(8,3),
    volume_zscore       NUMERIC(8,3),
    attribution_safe    BOOLEAN,                -- false => digest must hedge/suppress causal framing (pilot §16)
    anomaly_context     JSONB,                  -- the competing unexplained anomalies found in the window
    computed_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- =============================================================================
-- MARKET — price bars and cached statistical baselines
-- =============================================================================

CREATE TABLE market.price_bars (
    company_id      BIGINT NOT NULL REFERENCES core.companies(company_id),
    trade_date      DATE NOT NULL,
    open            NUMERIC(14,2) NOT NULL,
    high            NUMERIC(14,2) NOT NULL,
    low             NUMERIC(14,2) NOT NULL,
    close           NUMERIC(14,2) NOT NULL,
    volume          BIGINT NOT NULL,
    source          TEXT NOT NULL DEFAULT 'yfinance',
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (company_id, trade_date)
);

CREATE TABLE market.price_baselines (
    company_id      BIGINT NOT NULL REFERENCES core.companies(company_id),
    as_of_date      DATE NOT NULL,
    window_days     SMALLINT NOT NULL DEFAULT 63,   -- 3mo trailing baseline
    mean_return     NUMERIC(10,6),
    std_return      NUMERIC(10,6),
    mean_ln_volume  NUMERIC(12,6),
    std_ln_volume   NUMERIC(12,6),
    n_days          SMALLINT,
    computed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (company_id, as_of_date, window_days)
);
-- Cached so the anomaly cross-check (pilot §16) can scan the whole window
-- without recomputing stats per event.

-- =============================================================================
-- DELIVERY — digests and their contents
-- =============================================================================

CREATE TABLE delivery.digests (
    digest_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    digest_date     DATE NOT NULL UNIQUE,
    status          TEXT NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft', 'sent', 'failed')),
    sent_at         TIMESTAMPTZ,
    telegram_message_id BIGINT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE delivery.digest_items (
    digest_id       BIGINT NOT NULL REFERENCES delivery.digests(digest_id),
    insight_id      BIGINT NOT NULL REFERENCES intelligence.insights(insight_id),
    rank_in_digest  SMALLINT NOT NULL,
    flood_capped    BOOLEAN NOT NULL DEFAULT false, -- dropped/merged by per-stock cap (post-clustering, pilot §12)
    PRIMARY KEY (digest_id, insight_id)
);

-- =============================================================================
-- Seed helper: nothing auto-inserted; whitelist is loaded via script.
-- =============================================================================
