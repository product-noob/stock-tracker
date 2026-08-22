-- =============================================================================
-- Stock Market Insights Engine — PostgreSQL Schema v1.1
-- =============================================================================
-- v1.1 (architect review):
--   * Dedup keys moved to source-native IDs (#1, #2); no content-hash uniques
--   * Growing vocabularies moved to reference tables: ref_sources,
--     ref_event_kinds (#3, #13); closed state machines keep CHECKs
--   * Added ingestion_cursors + pipeline_runs for completeness tracking (#4, #5)
--   * Denormalized company_id onto normalized_records for fast-path joins (#7)
--   * price_bars.is_adjusted added (#6); price_baselines dropped (#10)
--   * rulepack_version on insights (#8); published_at made nullable (#9)
--   * document_records renamed record_documents (#12)
--
-- Conventions:
--   * Schemas group by plane = organization + future-split seams of the
--     modular monolith. NOT hard service boundaries: cross-schema FKs flow
--     one way (downstream -> upstream) along contract direction.
--   * Append-only where marked: no UPDATE/DELETE in normal operation.
--     (Single sanctioned exception: back-fill of normalized_records.company_id
--      by the identity resolver.)
--   * All timestamps: TIMESTAMPTZ (IST conversions at the edges).
--   * Money/prices: NUMERIC, never float.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS ingestion;
CREATE SCHEMA IF NOT EXISTS documents;
CREATE SCHEMA IF NOT EXISTS intelligence;
CREATE SCHEMA IF NOT EXISTS market;
CREATE SCHEMA IF NOT EXISTS delivery;

-- =============================================================================
-- CORE — whitelist, aliases, shared reference vocabularies
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
-- Known limitation (documented): companies with multiple listed securities
-- (e.g. DVR share classes) model poorly here. Fine for a 20-25 large/mid-cap
-- whitelist; split into a listings table if ever needed.

CREATE TABLE core.company_aliases (
    alias_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company_id      BIGINT NOT NULL REFERENCES core.companies(company_id),
    alias_value     TEXT NOT NULL,
    alias_kind      TEXT NOT NULL DEFAULT 'name_variant',   -- open vocabulary; no CHECK
    source          TEXT,                   -- who added it: 'seed', 'resolver_learning', 'human'
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (alias_kind, alias_value)
);
CREATE INDEX idx_aliases_value ON core.company_aliases (lower(alias_value));

-- Shared vocabulary of data sources. Adding a source = INSERT, not DDL. (#3)
CREATE TABLE core.ref_sources (
    source_code     TEXT PRIMARY KEY,       -- 'nse_api', 'bse_api', 'nse_rss', 'bse_rss', 'google_news', ...
    description     TEXT NOT NULL,
    poll_interval_sec INT,                  -- advisory; scheduler reads this
    is_active       BOOLEAN NOT NULL DEFAULT true
);

-- =============================================================================
-- INGESTION — raw feed items (append-only), normalized records, resolution
-- =============================================================================

CREATE TABLE ingestion.raw_feed_items (
    item_id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source          TEXT NOT NULL REFERENCES core.ref_sources(source_code),
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at    TIMESTAMPTZ,            -- as claimed by the source, raw
    external_id     TEXT,                   -- source-native id: seq_id / scripcode+ts / news token
    document_url    TEXT,
    title           TEXT,
    description     TEXT,
    raw_payload     JSONB NOT NULL,         -- untouched item; replay guarantee
    fetch_meta      JSONB                   -- http status, headers of interest, cookie state id
);
-- Append-only. Refetch dedup uses the source-native id when the source has
-- one (#2). Items without an external id simply repeat — harmless; true dedup
-- happens downstream at the document hash. No content-hash unique: feeds
-- embed volatile fields and byte-comparison produces false both ways.
CREATE UNIQUE INDEX uq_raw_native ON ingestion.raw_feed_items (source, external_id)
    WHERE external_id IS NOT NULL;
CREATE INDEX idx_raw_url ON ingestion.raw_feed_items (document_url) WHERE document_url IS NOT NULL;

CREATE TABLE ingestion.normalized_records (
    record_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    raw_item_id     BIGINT NOT NULL REFERENCES ingestion.raw_feed_items(item_id),
    exchange        TEXT NOT NULL CHECK (exchange IN ('NSE', 'BSE')),   -- closed: two exchanges exist
    external_id     TEXT,                   -- carried from raw; the real dedup key (#1)
    company_name_raw TEXT NOT NULL,
    nse_symbol      TEXT,
    bse_scripcode   TEXT,
    isin            TEXT,                   -- feed metadata only; NULL for BSE pending master join
    subject         TEXT,
    description     TEXT,
    category_raw    TEXT,                   -- exchange's own tag: quarantined, never for classification
    document_url    TEXT NOT NULL,
    published_at    TIMESTAMPTZ,            -- nullable (#9): unparseable dates must not lose rows
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    company_id      BIGINT REFERENCES core.companies(company_id)  -- denormalized fast path (#7),
                                                            -- set once by identity resolver
);
-- Same exchange + same native id = same announcement. Distinct records that
-- happen to share a PDF URL survive (pilot §5: NSE tagged one PDF as both
-- 'Record Date' and 'Updates'). Cross-exchange duplication resolves at the
-- sha256 layer, not here.
CREATE UNIQUE INDEX uq_record_native ON ingestion.normalized_records (exchange, external_id)
    WHERE external_id IS NOT NULL;
CREATE INDEX idx_records_url ON ingestion.normalized_records (document_url);
CREATE INDEX idx_records_company ON ingestion.normalized_records (company_id) WHERE company_id IS NOT NULL;

CREATE TABLE ingestion.identity_resolutions (
    resolution_id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    record_id       BIGINT NOT NULL REFERENCES ingestion.normalized_records(record_id),
    company_id      BIGINT REFERENCES core.companies(company_id),   -- NULL = unresolved attempt
    tier            TEXT NOT NULL CHECK (tier IN ('A_symbol', 'B_scripcode', 'C_isin',
                                                  'D_exact_name', 'E_alias_name', 'F_manual')),
                                                    -- closed ladder from product note §7
    confidence      NUMERIC(4,3) CHECK (confidence BETWEEN 0 AND 1),
    resolved        BOOLEAN NOT NULL,
    needs_review    BOOLEAN NOT NULL DEFAULT false,
    attempted_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_resolutions_record ON ingestion.identity_resolutions (record_id);
CREATE INDEX idx_resolutions_unresolved ON ingestion.identity_resolutions (needs_review) WHERE needs_review;

-- Completeness tracking (#4): rolling-window feeds need a remembered position
-- so we can detect polling gaps and prove nothing was missed.
CREATE TABLE ingestion.ingestion_cursors (
    source              TEXT PRIMARY KEY REFERENCES core.ref_sources(source_code),
    last_external_id    TEXT,               -- newest item id successfully processed
    last_seen_published TIMESTAMPTZ,        -- its claimed timestamp
    last_success_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    consecutive_failures SMALLINT NOT NULL DEFAULT 0,
    notes               TEXT
);

-- Per-run funnel observability (#5): fetched -> new -> normalized -> docs.
-- When counts diverge between runs you want to know WHEN, not debug rows.
CREATE TABLE ingestion.pipeline_runs (
    run_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source          TEXT NOT NULL REFERENCES core.ref_sources(source_code),
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    status          TEXT NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running', 'ok', 'failed')),   -- closed state machine
    items_fetched   INT NOT NULL DEFAULT 0,
    items_new       INT NOT NULL DEFAULT 0,
    records_created INT NOT NULL DEFAULT 0,
    docs_new        INT NOT NULL DEFAULT 0,
    errors          INT NOT NULL DEFAULT 0,
    notes           TEXT
);
CREATE INDEX idx_runs_source_time ON ingestion.pipeline_runs (source, started_at DESC);

-- =============================================================================
-- DOCUMENTS — append-only doc registry, links, extracted text
-- =============================================================================

CREATE TABLE documents.documents (
    doc_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sha256          CHAR(64) NOT NULL UNIQUE,   -- THE dedup layer: pilot 58->28 collapse
    document_url    TEXT NOT NULL,
    first_seen_via  TEXT NOT NULL REFERENCES core.ref_sources(source_code),  -- no enum CHECK (#3)
    storage_path    TEXT NOT NULL,
    byte_size       BIGINT,
    page_count      INT,
    is_valid_pdf    BOOLEAN NOT NULL DEFAULT false,  -- magic-byte check result
    downloaded_at   TIMESTAMPTZ,
    download_status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (download_status IN ('pending', 'ok', 'failed', 'invalid_content'))
);
-- Append-only: a hash never changes once seen.

CREATE TABLE documents.record_documents (
    doc_id          BIGINT NOT NULL REFERENCES documents.documents(doc_id),
    record_id       BIGINT NOT NULL REFERENCES ingestion.normalized_records(record_id),
    PRIMARY KEY (doc_id, record_id)
);
-- M:N on purpose: pilot §5 — one PDF arrived as 4 feed records across both
-- exchanges (and 2+ records within one exchange). Never assume 1:1.

CREATE TABLE documents.document_texts (
    doc_id              BIGINT PRIMARY KEY REFERENCES documents.documents(doc_id),
    text                TEXT NOT NULL,
    char_count          INT NOT NULL,
    extraction_method   TEXT NOT NULL DEFAULT 'pdfplumber',  -- open vocabulary; no CHECK (#3)
    extraction_ok       BOOLEAN NOT NULL,
    extracted_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Re-extraction replaces the row; prior text remains recoverable from the
-- object store snapshot. Single-text-per-doc is a deliberate simplification.

-- =============================================================================
-- INTELLIGENCE — taxonomy, extractions, events, insights, reactions
-- =============================================================================

-- Event/document taxonomy as DATA, single source of truth (#13): the LLM
-- prompt, severity rules, and stored values all read from here. Pilot §11's
-- governance split (red_flag vs routine) lives as rows, not prose.
CREATE TABLE intelligence.ref_event_kinds (
    kind_code       TEXT PRIMARY KEY,       -- 'financial_results', 'governance_red_flag', ...
    plane           TEXT NOT NULL,          -- coarse grouping: 'results', 'governance', 'corp_action', ...
    description     TEXT NOT NULL,
    is_active       BOOLEAN NOT NULL DEFAULT true
);

CREATE TABLE intelligence.extractions (
    extraction_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    doc_id              BIGINT NOT NULL REFERENCES documents.documents(doc_id),
    model               TEXT NOT NULL,
    prompt_version      TEXT NOT NULL,      -- pairs with facts-shape evolution
    document_kind       TEXT NOT NULL REFERENCES intelligence.ref_event_kinds(kind_code),
                        -- writer normalizes unknown LLM output to 'other' and logs it
    summary             TEXT NOT NULL,
    quote               TEXT,               -- NULL if suppressed after failed verification
    quote_verified      BOOLEAN NOT NULL,
    verify_attempts     SMALLINT NOT NULL DEFAULT 0,
    quote_suppressed    BOOLEAN NOT NULL DEFAULT false,
    facts               JSONB NOT NULL DEFAULT '{}',
    cost_usd            NUMERIC(8,4),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_extractions_doc ON intelligence.extractions (doc_id);
CREATE INDEX idx_extractions_kind ON intelligence.extractions (document_kind);
CREATE INDEX idx_extractions_qa ON intelligence.extractions (created_at)
    WHERE NOT quote_verified OR quote_suppressed;   -- QA dashboard scan path
-- No self-reported confidence column by design — pilot §10 proved it carries
-- zero signal.

CREATE TABLE intelligence.events (
    event_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company_id      BIGINT NOT NULL REFERENCES core.companies(company_id),
    event_kind      TEXT NOT NULL REFERENCES intelligence.ref_event_kinds(kind_code),
    window_start    TIMESTAMPTZ NOT NULL,
    window_end      TIMESTAMPTZ NOT NULL,
    cluster_method  TEXT NOT NULL DEFAULT 'manual',  -- open vocabulary; no CHECK (#3)
    title           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_events_company_window ON intelligence.events (company_id, window_start DESC);

CREATE TABLE intelligence.event_documents (
    event_id        BIGINT NOT NULL REFERENCES intelligence.events(event_id),
    doc_id          BIGINT NOT NULL REFERENCES documents.documents(doc_id),
    role            TEXT,                       -- e.g. 'results_pdf', 'press_release'
    PRIMARY KEY (event_id, doc_id)
);
-- M:N on purpose: pilot §12 — six distinct documents described one results event.

CREATE TABLE intelligence.insights (
    insight_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id        BIGINT NOT NULL REFERENCES intelligence.events(event_id),
    severity        SMALLINT NOT NULL CHECK (severity BETWEEN 1 AND 5),  -- closed scale
    severity_basis  TEXT NOT NULL CHECK (severity_basis IN ('category_rule', 'red_flag_override', 'manual')),
    rulepack_version TEXT NOT NULL DEFAULT 'v1',   -- which regex/ruleset produced this (#8);
                                                    -- without it, past severities are unreproducible
    red_flag_hits   TEXT[],
    headline        TEXT NOT NULL,
    rationale       TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_insights_event ON intelligence.insights (event_id);
CREATE INDEX idx_insights_severity ON intelligence.insights (severity DESC, created_at DESC);

CREATE TABLE intelligence.reaction_signals (
    signal_id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id            BIGINT NOT NULL UNIQUE REFERENCES intelligence.events(event_id),
    reaction_pct        NUMERIC(8,4),
    return_zscore       NUMERIC(8,3),
    volume_zscore       NUMERIC(8,3),
    attribution_safe    BOOLEAN,            -- false => digest must hedge causal framing (pilot §16)
    anomaly_context     JSONB,             -- competing unexplained anomalies found in the window
    computed_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- v1: one signal per event (next-session reaction). If multi-day events ever
-- need per-session reactions, this becomes (event_id, trade_date) — deferred.

-- =============================================================================
-- MARKET — price bars only (#10: baselines table dropped; trailing stats are
-- computed on the fly — 25 stocks x daily bars is trivially cheap, and the
-- anomaly cross-check does one indexed scan either way).
-- =============================================================================

CREATE TABLE market.price_bars (
    company_id      BIGINT NOT NULL REFERENCES core.companies(company_id),
    trade_date      DATE NOT NULL,
    open            NUMERIC(14,2) NOT NULL,
    high            NUMERIC(14,2) NOT NULL,
    low             NUMERIC(14,2) NOT NULL,
    close           NUMERIC(14,2) NOT NULL,
    volume          BIGINT NOT NULL,
    is_adjusted     BOOLEAN NOT NULL DEFAULT true,  -- MUST match how z-scores are computed (#6);
                                                    -- mixing adjusted/unadjusted across a split
                                                    -- turns every anomaly into garbage
    source          TEXT NOT NULL DEFAULT 'yfinance',
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (company_id, trade_date)
);

-- =============================================================================
-- DELIVERY — digests and their contents
-- =============================================================================

CREATE TABLE delivery.digests (
    digest_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    digest_date     DATE NOT NULL UNIQUE,
    status          TEXT NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft', 'sent', 'failed')),   -- closed state machine
    sent_at         TIMESTAMPTZ,
    telegram_message_id BIGINT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE delivery.digest_items (
    digest_id       BIGINT NOT NULL REFERENCES delivery.digests(digest_id),
    insight_id      BIGINT NOT NULL REFERENCES intelligence.insights(insight_id),
    rank_in_digest  SMALLINT NOT NULL,
    flood_capped    BOOLEAN NOT NULL DEFAULT false,
    PRIMARY KEY (digest_id, insight_id)
);

-- =============================================================================
-- SEED — reference vocabularies (idempotent; safe to re-run)
-- =============================================================================

INSERT INTO core.ref_sources (source_code, description) VALUES
    ('nse_api',      'NSE corporate-announcements API (Akamai cookie warm-up required)'),
    ('bse_api',      'BSE AnnSubCategoryGetData API'),
    ('nse_rss',      'NSE RSS announcement feeds'),
    ('bse_rss',      'BSE beta RSS announcement feeds'),
    ('google_news',  'Google News RSS (forward-looking only, ~100-item cap)'),
    ('yfinance',     'OHLCV price history')
ON CONFLICT (source_code) DO NOTHING;

INSERT INTO intelligence.ref_event_kinds (kind_code, plane, description) VALUES
    ('financial_results',      'results',      'Quarterly/annual audited or unaudited results'),
    ('investor_presentation',  'results',      'Results-day presentation material'),
    ('press_release',          'results',      'Results-day press release'),
    ('board_meeting_outcome',  'results',      'Outcome of board meeting intimation'),
    ('agm_egm_notice',         'shareholders', 'AGM/EGM notice and postal ballot material'),
    ('record_date',            'corp_action',  'Record date determination'),
    ('corporate_action',       'corp_action',  'Dividend, bonus, split, buyback actions'),
    ('insider_trading_sast',   'insider',      'Insider trading / SAST disclosures'),
    ('shareholding_pattern',   'disclosure',   'Shareholding pattern filings'),
    ('trading_window',         'compliance',   'Trading window closure/opening intimation'),
    ('compliance_certificate', 'compliance',   'Routine compliance certificates and BRSR'),
    ('governance_red_flag',    'governance',   'Auditor exit, adverse opinion, SEBI order, insolvency, KMP exit'),
    ('governance_routine',     'governance',   'BRSR submissions, KMP authorizations'),
    ('newspaper_publication',  'disclosure',   'Newspaper publication of notices'),
    ('credit_rating',          'disclosure',   'Rating actions/affirmations'),
    ('esop',                   'corp_action',  'ESOP allotments and related'),
    ('other',                  'other',        'Fallback for unrecognized kinds (writer normalizes here)')
ON CONFLICT (kind_code) DO NOTHING;
