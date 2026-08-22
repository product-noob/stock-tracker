-- =============================================================================
-- Stock Market Insights Engine — PostgreSQL Schema v1.2
-- Fresh-install baseline. Run with: psql -v ON_ERROR_STOP=1 -f db/schema.sql
-- =============================================================================
-- Design guarantees:
--   * exact fetched bytes live in immutable object storage and are registered here
--   * source polling is partition-aware; every canonical row has run/source lineage
--   * jobs/outbox make asynchronous hand-offs retryable without dual-write loss
--   * source records, immutable content, text versions, extractions, event snapshots,
--     insights, market calculations, and deliveries are distinct lifecycle objects
--   * document kinds and business-event kinds are separate vocabularies
--   * market bars belong to a listing + immutable series definition, not a company
--   * derived outputs carry input fingerprints and behavioral versions
--
-- This file intentionally contains no DROP statements. It is a baseline for an empty
-- database, not an in-place v1.1 migration. After first integration, use db/migrations/.
-- =============================================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS platform;
CREATE SCHEMA IF NOT EXISTS ingestion;
CREATE SCHEMA IF NOT EXISTS documents;
CREATE SCHEMA IF NOT EXISTS intelligence;
CREATE SCHEMA IF NOT EXISTS market;
CREATE SCHEMA IF NOT EXISTS delivery;

-- =============================================================================
-- CORE — security master, whitelist, aliases, shared source vocabularies
-- =============================================================================

CREATE TABLE core.ref_sources (
    source_code         TEXT PRIMARY KEY,
    description         TEXT NOT NULL,
    source_kind         TEXT NOT NULL,
    default_poll_interval_sec INT CHECK (default_poll_interval_sec > 0),
    is_active           BOOLEAN NOT NULL DEFAULT true,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE core.ref_record_types (
    record_type         TEXT PRIMARY KEY,
    description         TEXT NOT NULL,
    is_active           BOOLEAN NOT NULL DEFAULT true
);

CREATE TABLE core.companies (
    company_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    canonical_name      TEXT NOT NULL CHECK (btrim(canonical_name) <> ''),
    status              TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'paused', 'removed')),
    notes               TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Aliases are unique only within a company. The same normalized alias may belong to
-- multiple companies; that ambiguity must fail closed in the resolver.
CREATE TABLE core.company_aliases (
    alias_id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company_id          BIGINT NOT NULL REFERENCES core.companies(company_id),
    alias_value         TEXT NOT NULL CHECK (btrim(alias_value) <> ''),
    normalized_value    TEXT NOT NULL CHECK (btrim(normalized_value) <> ''),
    alias_kind          TEXT NOT NULL DEFAULT 'name_variant',
    source              TEXT,
    effective_from      DATE,
    effective_to        DATE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from)
);
CREATE INDEX idx_company_aliases_normalized
    ON core.company_aliases (normalized_value);
CREATE UNIQUE INDEX uq_company_alias_normalized_period
    ON core.company_aliases (
        company_id,
        alias_kind,
        normalized_value,
        COALESCE(effective_from, '-infinity'::date)
    );

-- An instrument is the economic security/share class; listings are its exchange venues.
CREATE TABLE core.instruments (
    instrument_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company_id          BIGINT NOT NULL REFERENCES core.companies(company_id),
    security_name       TEXT NOT NULL CHECK (btrim(security_name) <> ''),
    instrument_type     TEXT NOT NULL DEFAULT 'equity',
    isin                TEXT UNIQUE CHECK (isin IS NULL OR isin ~ '^[A-Z]{2}[A-Z0-9]{9}[0-9]$'),
    status              TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'suspended', 'delisted')),
    valid_from          DATE,
    valid_to            DATE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from),
    UNIQUE (instrument_id, company_id)
);
CREATE INDEX idx_instruments_company ON core.instruments (company_id);

CREATE TABLE core.listings (
    listing_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    instrument_id       BIGINT NOT NULL,
    company_id          BIGINT NOT NULL,
    exchange            TEXT NOT NULL CHECK (exchange IN ('NSE', 'BSE')),
    exchange_symbol     TEXT,
    exchange_code       TEXT,
    currency            CHAR(3) NOT NULL DEFAULT 'INR',
    provider_symbols    JSONB NOT NULL DEFAULT '{}'::jsonb
                        CHECK (jsonb_typeof(provider_symbols) = 'object'),
    is_primary_reaction_listing BOOLEAN NOT NULL DEFAULT false,
    valid_from          DATE,
    valid_to            DATE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (exchange_symbol IS NOT NULL OR exchange_code IS NOT NULL),
    CHECK (valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from),
    FOREIGN KEY (instrument_id, company_id)
        REFERENCES core.instruments(instrument_id, company_id),
    UNIQUE (listing_id, company_id),
    UNIQUE (exchange, exchange_symbol, valid_from),
    UNIQUE (exchange, exchange_code, valid_from)
);
CREATE INDEX idx_listings_instrument ON core.listings (instrument_id);
CREATE INDEX idx_listings_company ON core.listings (company_id);
CREATE UNIQUE INDEX uq_primary_reaction_listing
    ON core.listings (instrument_id) WHERE is_primary_reaction_listing;
CREATE UNIQUE INDEX uq_current_exchange_symbol
    ON core.listings (exchange, exchange_symbol)
    WHERE valid_to IS NULL AND exchange_symbol IS NOT NULL;
CREATE UNIQUE INDEX uq_current_exchange_code
    ON core.listings (exchange, exchange_code)
    WHERE valid_to IS NULL AND exchange_code IS NOT NULL;

CREATE TABLE core.whitelist_memberships (
    membership_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company_id          BIGINT NOT NULL REFERENCES core.companies(company_id),
    added_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    removed_at           TIMESTAMPTZ,
    reason               TEXT,
    CHECK (removed_at IS NULL OR removed_at >= added_at)
);
CREATE INDEX idx_whitelist_company ON core.whitelist_memberships (company_id);
CREATE UNIQUE INDEX uq_active_whitelist_company
    ON core.whitelist_memberships (company_id) WHERE removed_at IS NULL;

-- =============================================================================
-- PLATFORM — schema ledger, durable work queue and transactional outbox
-- =============================================================================

CREATE TABLE platform.schema_migrations (
    version             TEXT PRIMARY KEY,
    description         TEXT NOT NULL,
    applied_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE platform.processing_jobs (
    job_id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_kind            TEXT NOT NULL,
    idempotency_key     TEXT NOT NULL,
    payload             JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    priority            SMALLINT NOT NULL DEFAULT 100,
    status              TEXT NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'dead')),
    attempts            SMALLINT NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts        SMALLINT NOT NULL DEFAULT 5 CHECK (max_attempts > 0),
    available_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    locked_at           TIMESTAMPTZ,
    locked_by           TEXT,
    last_error          TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at         TIMESTAMPTZ,
    CHECK (attempts <= max_attempts),
    CHECK ((status = 'running') = (locked_at IS NOT NULL)),
    CHECK (status NOT IN ('succeeded', 'dead') OR finished_at IS NOT NULL),
    UNIQUE (job_kind, idempotency_key)
);
CREATE INDEX idx_jobs_claim
    ON platform.processing_jobs (priority, available_at, job_id)
    WHERE status IN ('queued', 'failed');

CREATE TABLE platform.outbox_events (
    outbox_id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_key           TEXT NOT NULL UNIQUE,
    event_type          TEXT NOT NULL,
    aggregate_type      TEXT NOT NULL,
    aggregate_id        TEXT NOT NULL,
    payload             JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    status              TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'published', 'failed')),
    attempts            SMALLINT NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    available_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at        TIMESTAMPTZ,
    last_error          TEXT,
    CHECK ((status = 'published') = (published_at IS NOT NULL))
);
CREATE INDEX idx_outbox_pending
    ON platform.outbox_events (available_at, outbox_id)
    WHERE status IN ('pending', 'failed');

-- =============================================================================
-- INGESTION — partitions, fetch lineage, exact raw artifacts, normalized records
-- =============================================================================

CREATE TABLE ingestion.source_partitions (
    partition_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source              TEXT NOT NULL REFERENCES core.ref_sources(source_code),
    partition_key       TEXT NOT NULL,
    description         TEXT,
    company_id          BIGINT REFERENCES core.companies(company_id),
    poll_interval_sec   INT CHECK (poll_interval_sec > 0),
    config              JSONB NOT NULL DEFAULT '{}'::jsonb
                        CHECK (jsonb_typeof(config) = 'object'),
    is_active           BOOLEAN NOT NULL DEFAULT true,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source, partition_key)
);
CREATE INDEX idx_source_partitions_company ON ingestion.source_partitions (company_id);

CREATE TABLE ingestion.fetch_runs (
    fetch_run_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    partition_id        BIGINT NOT NULL REFERENCES ingestion.source_partitions(partition_id),
    idempotency_key     TEXT NOT NULL UNIQUE,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at         TIMESTAMPTZ,
    status              TEXT NOT NULL DEFAULT 'running'
                        CHECK (status IN ('running', 'ok', 'not_modified', 'partial', 'failed')),
    http_status         SMALLINT CHECK (http_status BETWEEN 100 AND 599),
    items_seen          INT NOT NULL DEFAULT 0 CHECK (items_seen >= 0),
    items_new           INT NOT NULL DEFAULT 0 CHECK (items_new >= 0),
    oldest_published_at TIMESTAMPTZ,
    newest_published_at TIMESTAMPTZ,
    response_etag       TEXT,
    response_last_modified TEXT,
    error_summary       TEXT,
    CHECK ((status = 'running' AND finished_at IS NULL) OR
           (status <> 'running' AND finished_at IS NOT NULL)),
    CHECK (finished_at IS NULL OR finished_at >= started_at),
    CHECK (items_new <= items_seen),
    UNIQUE (fetch_run_id, partition_id),
    UNIQUE (partition_id, fetch_run_id)
);
CREATE INDEX idx_fetch_runs_partition_time
    ON ingestion.fetch_runs (partition_id, started_at DESC);
CREATE INDEX idx_fetch_runs_failures
    ON ingestion.fetch_runs (started_at DESC) WHERE status IN ('partial', 'failed');

-- One row per exact HTTP/feed artifact. raw_payload on feed items is a parsed aid;
-- storage_path + sha256 are the replay source of truth.
CREATE TABLE ingestion.raw_artifacts (
    artifact_id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    fetch_run_id        BIGINT NOT NULL REFERENCES ingestion.fetch_runs(fetch_run_id),
    idempotency_key     TEXT NOT NULL UNIQUE,
    artifact_kind       TEXT NOT NULL,
    content_type        TEXT,
    sha256              TEXT NOT NULL CHECK (sha256 ~ '^[0-9a-fA-F]{64}$'),
    storage_path        TEXT NOT NULL,
    byte_size           BIGINT NOT NULL CHECK (byte_size >= 0),
    fetched_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb
                        CHECK (jsonb_typeof(metadata) = 'object'),
    UNIQUE (artifact_id, fetch_run_id),
    UNIQUE (fetch_run_id, artifact_kind, sha256)
);
CREATE INDEX idx_raw_artifacts_run ON ingestion.raw_artifacts (fetch_run_id);
CREATE INDEX idx_raw_artifacts_sha ON ingestion.raw_artifacts (sha256);

CREATE TABLE ingestion.raw_feed_items (
    item_id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    partition_id        BIGINT NOT NULL REFERENCES ingestion.source_partitions(partition_id),
    first_seen_run_id   BIGINT NOT NULL,
    artifact_id         BIGINT NOT NULL,
    idempotency_key     TEXT NOT NULL,
    external_id         TEXT,
    fetched_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at        TIMESTAMPTZ,
    title               TEXT,
    description         TEXT,
    content_url         TEXT,
    raw_payload         JSONB,
    raw_fragment        TEXT,
    CHECK (raw_payload IS NOT NULL OR raw_fragment IS NOT NULL),
    CHECK (raw_payload IS NULL OR jsonb_typeof(raw_payload) IN ('object', 'array')),
    FOREIGN KEY (first_seen_run_id, partition_id)
        REFERENCES ingestion.fetch_runs(fetch_run_id, partition_id),
    FOREIGN KEY (artifact_id, first_seen_run_id)
        REFERENCES ingestion.raw_artifacts(artifact_id, fetch_run_id),
    UNIQUE (partition_id, idempotency_key)
);
CREATE INDEX idx_raw_items_artifact ON ingestion.raw_feed_items (artifact_id);
CREATE INDEX idx_raw_items_first_seen_run ON ingestion.raw_feed_items (first_seen_run_id);
CREATE INDEX idx_raw_items_published ON ingestion.raw_feed_items (partition_id, published_at DESC);
CREATE INDEX idx_raw_items_url ON ingestion.raw_feed_items (content_url) WHERE content_url IS NOT NULL;

CREATE TABLE ingestion.ingestion_cursors (
    partition_id        BIGINT PRIMARY KEY REFERENCES ingestion.source_partitions(partition_id),
    checkpoint          JSONB NOT NULL DEFAULT '{}'::jsonb
                        CHECK (jsonb_typeof(checkpoint) = 'object'),
    high_watermark_at   TIMESTAMPTZ,
    overlap_from        TIMESTAMPTZ,
    last_success_at     TIMESTAMPTZ,
    last_fetch_run_id   BIGINT,
    consecutive_failures SMALLINT NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (partition_id, last_fetch_run_id)
        REFERENCES ingestion.fetch_runs(partition_id, fetch_run_id)
);

CREATE TABLE ingestion.normalized_records (
    record_id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    record_type         TEXT NOT NULL REFERENCES core.ref_record_types(record_type),
    dedup_scope         TEXT NOT NULL,
    dedup_key           TEXT NOT NULL,
    exchange            TEXT CHECK (exchange IN ('NSE', 'BSE')),
    external_id         TEXT,
    company_name_raw    TEXT,
    exchange_symbol_raw TEXT,
    exchange_code_raw   TEXT,
    isin_raw            TEXT,
    title               TEXT,
    subject             TEXT,
    description         TEXT,
    category_raw        TEXT,
    content_url         TEXT,
    canonical_url       TEXT,
    published_at        TIMESTAMPTZ,
    first_seen_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    company_id          BIGINT REFERENCES core.companies(company_id),
    CHECK (record_type <> 'exchange_announcement' OR exchange IS NOT NULL),
    UNIQUE (dedup_scope, dedup_key)
);
CREATE INDEX idx_records_company_time
    ON ingestion.normalized_records (company_id, published_at DESC)
    WHERE company_id IS NOT NULL;
CREATE INDEX idx_records_content_url
    ON ingestion.normalized_records (content_url) WHERE content_url IS NOT NULL;
CREATE INDEX idx_records_unresolved
    ON ingestion.normalized_records (first_seen_at) WHERE company_id IS NULL;

-- A canonical record may have appeared in several category feeds; every raw sighting survives.
CREATE TABLE ingestion.record_raw_items (
    record_id           BIGINT NOT NULL REFERENCES ingestion.normalized_records(record_id),
    item_id             BIGINT NOT NULL REFERENCES ingestion.raw_feed_items(item_id),
    linked_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (record_id, item_id)
);
CREATE INDEX idx_record_raw_items_item ON ingestion.record_raw_items (item_id);

CREATE TABLE ingestion.identity_resolutions (
    resolution_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    record_id           BIGINT NOT NULL REFERENCES ingestion.normalized_records(record_id),
    company_id          BIGINT REFERENCES core.companies(company_id),
    listing_id          BIGINT,
    tier                TEXT NOT NULL CHECK (tier IN (
                            'A_scripcode', 'B_cross_exchange', 'C_document_identifier',
                            'D_exact_name', 'E_alias_name', 'F_manual')),
    confidence          NUMERIC(4,3) CHECK (confidence BETWEEN 0 AND 1),
    outcome             TEXT NOT NULL CHECK (outcome IN ('resolved', 'unresolved', 'rejected')),
    needs_review        BOOLEAN NOT NULL DEFAULT false,
    method_version      TEXT NOT NULL,
    reason              TEXT,
    decided_by          TEXT NOT NULL DEFAULT 'system',
    supersedes_resolution_id BIGINT UNIQUE,
    attempted_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((outcome = 'resolved' AND company_id IS NOT NULL) OR
           (outcome = 'unresolved' AND company_id IS NULL) OR
           outcome = 'rejected'),
    CHECK (NOT needs_review OR outcome = 'unresolved'),
    CHECK (listing_id IS NULL OR company_id IS NOT NULL),
    UNIQUE (resolution_id, record_id),
    UNIQUE (resolution_id, record_id, company_id),
    FOREIGN KEY (listing_id, company_id)
        REFERENCES core.listings(listing_id, company_id),
    FOREIGN KEY (supersedes_resolution_id, record_id)
        REFERENCES ingestion.identity_resolutions(resolution_id, record_id)
);
CREATE INDEX idx_resolutions_record_time
    ON ingestion.identity_resolutions (record_id, attempted_at DESC);
CREATE INDEX idx_resolutions_company ON ingestion.identity_resolutions (company_id)
    WHERE company_id IS NOT NULL;
CREATE INDEX idx_resolutions_listing ON ingestion.identity_resolutions (listing_id)
    WHERE listing_id IS NOT NULL;
CREATE INDEX idx_resolutions_review
    ON ingestion.identity_resolutions (attempted_at) WHERE needs_review;

-- Mutable current pointer; every decision it points to remains immutable history.
CREATE TABLE ingestion.identity_assignments (
    record_id           BIGINT PRIMARY KEY REFERENCES ingestion.normalized_records(record_id),
    resolution_id       BIGINT NOT NULL UNIQUE,
    company_id          BIGINT NOT NULL REFERENCES core.companies(company_id),
    assigned_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (resolution_id, record_id, company_id)
        REFERENCES ingestion.identity_resolutions(resolution_id, record_id, company_id)
);
CREATE INDEX idx_identity_assignments_company ON ingestion.identity_assignments (company_id);

CREATE VIEW ingestion.current_identity_resolutions AS
SELECT r.*
FROM ingestion.identity_assignments a
JOIN ingestion.identity_resolutions r ON r.resolution_id = a.resolution_id;

CREATE TABLE ingestion.stage_runs (
    stage_run_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    stage_name          TEXT NOT NULL,
    idempotency_key     TEXT NOT NULL,
    fetch_run_id        BIGINT REFERENCES ingestion.fetch_runs(fetch_run_id),
    code_version        TEXT NOT NULL,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at         TIMESTAMPTZ,
    status              TEXT NOT NULL DEFAULT 'running'
                        CHECK (status IN ('running', 'ok', 'partial', 'failed')),
    items_in            INT NOT NULL DEFAULT 0 CHECK (items_in >= 0),
    items_out           INT NOT NULL DEFAULT 0 CHECK (items_out >= 0),
    errors              INT NOT NULL DEFAULT 0 CHECK (errors >= 0),
    metrics             JSONB NOT NULL DEFAULT '{}'::jsonb
                        CHECK (jsonb_typeof(metrics) = 'object'),
    CHECK ((status = 'running' AND finished_at IS NULL) OR
           (status <> 'running' AND finished_at IS NOT NULL)),
    CHECK (finished_at IS NULL OR finished_at >= started_at),
    UNIQUE (stage_name, idempotency_key)
);
CREATE INDEX idx_stage_runs_status_time ON ingestion.stage_runs (status, started_at DESC);
CREATE INDEX idx_stage_runs_fetch ON ingestion.stage_runs (fetch_run_id)
    WHERE fetch_run_id IS NOT NULL;

CREATE TABLE ingestion.stage_errors (
    error_id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    stage_run_id        BIGINT NOT NULL REFERENCES ingestion.stage_runs(stage_run_id),
    entity_type         TEXT,
    entity_id           TEXT,
    error_code          TEXT,
    message             TEXT NOT NULL,
    detail              JSONB NOT NULL DEFAULT '{}'::jsonb
                        CHECK (jsonb_typeof(detail) = 'object'),
    occurred_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_stage_errors_run ON ingestion.stage_errors (stage_run_id);

-- =============================================================================
-- DOCUMENTS — content-addressed objects, fetch attempts, text versions
-- =============================================================================

CREATE TABLE documents.content_objects (
    content_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sha256              TEXT NOT NULL UNIQUE CHECK (sha256 ~ '^[0-9a-fA-F]{64}$'),
    storage_path        TEXT NOT NULL UNIQUE,
    media_type          TEXT NOT NULL,
    byte_size           BIGINT NOT NULL CHECK (byte_size >= 0),
    magic_valid         BOOLEAN,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb
                        CHECK (jsonb_typeof(metadata) = 'object')
);

-- Attempts are inserted when complete. Pending/retry state belongs to processing_jobs.
CREATE TABLE documents.content_fetch_attempts (
    attempt_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    record_id           BIGINT NOT NULL REFERENCES ingestion.normalized_records(record_id),
    job_id              BIGINT REFERENCES platform.processing_jobs(job_id),
    requested_url       TEXT NOT NULL,
    resolved_url        TEXT,
    attempt_no          SMALLINT NOT NULL CHECK (attempt_no > 0),
    status              TEXT NOT NULL CHECK (status IN ('ok', 'failed', 'invalid_content')),
    http_status         SMALLINT CHECK (http_status BETWEEN 100 AND 599),
    response_media_type TEXT,
    content_id          BIGINT REFERENCES documents.content_objects(content_id),
    error_message       TEXT,
    started_at          TIMESTAMPTZ NOT NULL,
    finished_at         TIMESTAMPTZ NOT NULL,
    CHECK (finished_at >= started_at),
    CHECK (status <> 'ok' OR content_id IS NOT NULL),
    UNIQUE (record_id, requested_url, attempt_no)
);
CREATE INDEX idx_content_attempts_record ON documents.content_fetch_attempts (record_id, attempt_no DESC);
CREATE INDEX idx_content_attempts_job ON documents.content_fetch_attempts (job_id)
    WHERE job_id IS NOT NULL;
CREATE INDEX idx_content_attempts_content ON documents.content_fetch_attempts (content_id)
    WHERE content_id IS NOT NULL;
CREATE INDEX idx_content_attempts_failures ON documents.content_fetch_attempts (finished_at DESC)
    WHERE status <> 'ok';

CREATE TABLE documents.record_contents (
    record_id           BIGINT NOT NULL REFERENCES ingestion.normalized_records(record_id),
    content_id          BIGINT NOT NULL REFERENCES documents.content_objects(content_id),
    relation            TEXT NOT NULL DEFAULT 'primary',
    source_url          TEXT,
    linked_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (record_id, content_id, relation)
);
CREATE INDEX idx_record_contents_content ON documents.record_contents (content_id);

CREATE TABLE documents.text_versions (
    text_version_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    content_id          BIGINT NOT NULL REFERENCES documents.content_objects(content_id),
    idempotency_key     TEXT NOT NULL UNIQUE,
    extractor           TEXT NOT NULL,
    extractor_version   TEXT NOT NULL,
    config              JSONB NOT NULL DEFAULT '{}'::jsonb
                        CHECK (jsonb_typeof(config) = 'object'),
    status              TEXT NOT NULL CHECK (status IN ('ok', 'failed', 'image_only', 'unsupported')),
    text                TEXT,
    text_sha256         TEXT CHECK (text_sha256 IS NULL OR text_sha256 ~ '^[0-9a-fA-F]{64}$'),
    char_count          INT NOT NULL DEFAULT 0 CHECK (char_count >= 0),
    page_count          INT CHECK (page_count >= 0),
    low_confidence_pages INT CHECK (low_confidence_pages >= 0),
    language            TEXT,
    error_message       TEXT,
    extracted_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (low_confidence_pages IS NULL OR page_count IS NULL OR low_confidence_pages <= page_count),
    CHECK ((text IS NULL AND char_count = 0) OR
           (text IS NOT NULL AND char_count = char_length(text))),
    CHECK (status <> 'ok' OR (text IS NOT NULL AND text_sha256 IS NOT NULL))
);
CREATE INDEX idx_text_versions_content_time
    ON documents.text_versions (content_id, extracted_at DESC);
CREATE INDEX idx_text_versions_attention
    ON documents.text_versions (extracted_at DESC) WHERE status <> 'ok';

-- =============================================================================
-- INTELLIGENCE — separate document/event taxonomies, immutable event snapshots
-- =============================================================================

CREATE TABLE intelligence.ref_event_categories (
    category_code       TEXT PRIMARY KEY,
    description         TEXT NOT NULL,
    default_weight      NUMERIC(5,3) NOT NULL CHECK (default_weight > 0),
    is_active           BOOLEAN NOT NULL DEFAULT true
);

CREATE TABLE intelligence.ref_document_kinds (
    kind_code           TEXT PRIMARY KEY,
    description         TEXT NOT NULL,
    is_active           BOOLEAN NOT NULL DEFAULT true
);

CREATE TABLE intelligence.ref_event_kinds (
    kind_code           TEXT PRIMARY KEY,
    category_code       TEXT NOT NULL REFERENCES intelligence.ref_event_categories(category_code),
    description         TEXT NOT NULL,
    is_active           BOOLEAN NOT NULL DEFAULT true
);

CREATE TABLE intelligence.extractions (
    extraction_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    text_version_id     BIGINT NOT NULL REFERENCES documents.text_versions(text_version_id),
    idempotency_key     TEXT NOT NULL UNIQUE,
    status              TEXT NOT NULL CHECK (status IN ('ok', 'failed')),
    model_provider      TEXT NOT NULL,
    model               TEXT NOT NULL,
    model_revision      TEXT,
    prompt_version      TEXT NOT NULL,
    schema_version      TEXT NOT NULL,
    verifier_version    TEXT NOT NULL,
    document_kind       TEXT REFERENCES intelligence.ref_document_kinds(kind_code),
    summary             TEXT,
    supporting_quote    TEXT,
    quote_verified      BOOLEAN NOT NULL DEFAULT false,
    quote_suppressed    BOOLEAN NOT NULL DEFAULT false,
    verify_attempts     SMALLINT NOT NULL DEFAULT 0 CHECK (verify_attempts >= 0),
    facts               JSONB NOT NULL DEFAULT '{}'::jsonb
                        CHECK (jsonb_typeof(facts) = 'object'),
    input_truncated     BOOLEAN NOT NULL DEFAULT false,
    input_chars         INT CHECK (input_chars >= 0),
    prompt_tokens       INT CHECK (prompt_tokens >= 0),
    completion_tokens   INT CHECK (completion_tokens >= 0),
    cost_usd            NUMERIC(12,6) CHECK (cost_usd >= 0),
    error_message       TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (NOT quote_verified OR (supporting_quote IS NOT NULL AND NOT quote_suppressed)),
    CHECK (NOT quote_suppressed OR NOT quote_verified),
    CHECK (status <> 'ok' OR (document_kind IS NOT NULL AND summary IS NOT NULL))
);
CREATE INDEX idx_extractions_text ON intelligence.extractions (text_version_id, created_at DESC);
CREATE INDEX idx_extractions_kind ON intelligence.extractions (document_kind);
CREATE INDEX idx_extractions_qa ON intelligence.extractions (created_at DESC)
    WHERE status <> 'ok' OR NOT quote_verified OR quote_suppressed OR input_truncated;

-- One extraction may yield several business-event mentions. Event typing is produced
-- by versioned deterministic classification over extracted facts, not trusted directly
-- from a model category label.
CREATE TABLE intelligence.extracted_event_mentions (
    mention_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    extraction_id       BIGINT NOT NULL REFERENCES intelligence.extractions(extraction_id),
    ordinal             SMALLINT NOT NULL CHECK (ordinal > 0),
    event_kind          TEXT NOT NULL REFERENCES intelligence.ref_event_kinds(kind_code),
    classifier_version  TEXT NOT NULL,
    direction           TEXT NOT NULL DEFAULT 'neutral'
                        CHECK (direction IN ('positive', 'negative', 'neutral', 'mixed', 'unknown')),
    headline            TEXT,
    effective_date      DATE,
    facts               JSONB NOT NULL DEFAULT '{}'::jsonb
                        CHECK (jsonb_typeof(facts) = 'object'),
    evidence_quote      TEXT,
    evidence_verified   BOOLEAN NOT NULL DEFAULT false,
    evidence_page       INT CHECK (evidence_page > 0),
    evidence_start      INT CHECK (evidence_start >= 0),
    evidence_end        INT CHECK (evidence_end >= 0),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (evidence_end IS NULL OR evidence_start IS NULL OR evidence_end >= evidence_start),
    CHECK (NOT evidence_verified OR evidence_quote IS NOT NULL),
    UNIQUE (extraction_id, ordinal)
);
CREATE INDEX idx_mentions_kind ON intelligence.extracted_event_mentions (event_kind);

-- events is stable identity; event_versions are immutable cluster snapshots.
CREATE TABLE intelligence.events (
    event_id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company_id          BIGINT NOT NULL REFERENCES core.companies(company_id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_events_company ON intelligence.events (company_id, created_at DESC);

CREATE TABLE intelligence.event_versions (
    event_version_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id            BIGINT NOT NULL REFERENCES intelligence.events(event_id),
    version_no          INT NOT NULL CHECK (version_no > 0),
    idempotency_key     TEXT NOT NULL UNIQUE,
    event_kind          TEXT NOT NULL REFERENCES intelligence.ref_event_kinds(kind_code),
    direction           TEXT NOT NULL DEFAULT 'neutral'
                        CHECK (direction IN ('positive', 'negative', 'neutral', 'mixed', 'unknown')),
    title               TEXT,
    announced_at_start  TIMESTAMPTZ,
    announced_at_end    TIMESTAMPTZ,
    effective_date      DATE,
    cluster_window_start TIMESTAMPTZ NOT NULL,
    cluster_window_end  TIMESTAMPTZ NOT NULL,
    cluster_method      TEXT NOT NULL,
    cluster_version     TEXT NOT NULL,
    cluster_confidence  NUMERIC(4,3) CHECK (cluster_confidence BETWEEN 0 AND 1),
    input_fingerprint   TEXT NOT NULL CHECK (input_fingerprint ~ '^[0-9a-fA-F]{64}$'),
    supersedes_event_version_id BIGINT UNIQUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (cluster_window_end >= cluster_window_start),
    CHECK (announced_at_end IS NULL OR announced_at_start IS NULL OR announced_at_end >= announced_at_start),
    UNIQUE (event_id, version_no),
    UNIQUE (event_version_id, event_id),
    FOREIGN KEY (supersedes_event_version_id, event_id)
        REFERENCES intelligence.event_versions(event_version_id, event_id)
);
CREATE INDEX idx_event_versions_event ON intelligence.event_versions (event_id, version_no DESC);
CREATE INDEX idx_event_versions_window ON intelligence.event_versions (event_kind, cluster_window_start DESC);

CREATE VIEW intelligence.current_event_versions AS
SELECT v.*
FROM intelligence.event_versions v
WHERE NOT EXISTS (
    SELECT 1 FROM intelligence.event_versions newer
    WHERE newer.supersedes_event_version_id = v.event_version_id
);

CREATE TABLE intelligence.event_version_mentions (
    event_version_id    BIGINT NOT NULL REFERENCES intelligence.event_versions(event_version_id),
    mention_id          BIGINT NOT NULL REFERENCES intelligence.extracted_event_mentions(mention_id),
    role                TEXT NOT NULL DEFAULT 'supporting',
    is_primary          BOOLEAN NOT NULL DEFAULT false,
    PRIMARY KEY (event_version_id, mention_id)
);
CREATE INDEX idx_event_version_mentions_mention
    ON intelligence.event_version_mentions (mention_id);
CREATE UNIQUE INDEX uq_event_version_primary_mention
    ON intelligence.event_version_mentions (event_version_id) WHERE is_primary;

CREATE TABLE intelligence.event_version_contents (
    event_version_id    BIGINT NOT NULL REFERENCES intelligence.event_versions(event_version_id),
    content_id          BIGINT NOT NULL REFERENCES documents.content_objects(content_id),
    role                TEXT NOT NULL DEFAULT 'supporting',
    PRIMARY KEY (event_version_id, content_id, role)
);
CREATE INDEX idx_event_version_contents_content
    ON intelligence.event_version_contents (content_id);

CREATE TABLE intelligence.event_version_instruments (
    event_version_id    BIGINT NOT NULL REFERENCES intelligence.event_versions(event_version_id),
    instrument_id       BIGINT NOT NULL REFERENCES core.instruments(instrument_id),
    role                TEXT NOT NULL DEFAULT 'affected',
    PRIMARY KEY (event_version_id, instrument_id, role)
);
CREATE INDEX idx_event_version_instruments_instrument
    ON intelligence.event_version_instruments (instrument_id);

CREATE TABLE intelligence.insights (
    insight_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_version_id    BIGINT NOT NULL REFERENCES intelligence.event_versions(event_version_id),
    insight_type        TEXT NOT NULL DEFAULT 'event_summary',
    idempotency_key     TEXT NOT NULL UNIQUE,
    severity            SMALLINT NOT NULL CHECK (severity BETWEEN 1 AND 5),
    severity_basis      TEXT NOT NULL
                        CHECK (severity_basis IN ('category_rule', 'red_flag_override', 'manual')),
    rulepack_version    TEXT NOT NULL,
    input_fingerprint   TEXT NOT NULL CHECK (input_fingerprint ~ '^[0-9a-fA-F]{64}$'),
    red_flag_hits       TEXT[] NOT NULL DEFAULT '{}'::text[],
    headline            TEXT NOT NULL,
    rationale           TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (severity_basis <> 'red_flag_override' OR cardinality(red_flag_hits) > 0),
    UNIQUE (event_version_id, insight_type, rulepack_version)
);
CREATE INDEX idx_insights_event_version ON intelligence.insights (event_version_id);
CREATE INDEX idx_insights_severity_time ON intelligence.insights (severity DESC, created_at DESC);

-- =============================================================================
-- MARKET — immutable listing-level series and reproducible event reactions
-- =============================================================================

CREATE TABLE market.price_series (
    series_id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    listing_id          BIGINT NOT NULL REFERENCES core.listings(listing_id),
    source              TEXT NOT NULL REFERENCES core.ref_sources(source_code),
    provider_symbol     TEXT NOT NULL,
    interval_code       TEXT NOT NULL DEFAULT '1d',
    adjustment_mode     TEXT NOT NULL
                        CHECK (adjustment_mode IN ('raw', 'split_adjusted', 'total_return_adjusted')),
    adjustment_version TEXT NOT NULL,
    currency            CHAR(3) NOT NULL DEFAULT 'INR',
    is_active           BOOLEAN NOT NULL DEFAULT true,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (listing_id, source, provider_symbol, interval_code, adjustment_mode, adjustment_version)
);
CREATE INDEX idx_price_series_listing ON market.price_series (listing_id);

-- Provider corrections append a new observation; current_price_bars selects the latest.
CREATE TABLE market.price_bars (
    bar_id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    series_id           BIGINT NOT NULL REFERENCES market.price_series(series_id),
    idempotency_key     TEXT NOT NULL UNIQUE,
    trade_date          DATE NOT NULL,
    open                NUMERIC(18,6) NOT NULL CHECK (open > 0),
    high                NUMERIC(18,6) NOT NULL CHECK (high > 0),
    low                 NUMERIC(18,6) NOT NULL CHECK (low > 0),
    close               NUMERIC(18,6) NOT NULL CHECK (close > 0),
    volume              BIGINT NOT NULL CHECK (volume >= 0),
    observed_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    fetch_run_id        BIGINT REFERENCES ingestion.fetch_runs(fetch_run_id),
    CHECK (high >= low AND high >= open AND high >= close),
    CHECK (low <= open AND low <= close),
    UNIQUE (series_id, trade_date, observed_at)
);
CREATE INDEX idx_price_bars_series_date ON market.price_bars (series_id, trade_date DESC, observed_at DESC);
CREATE INDEX idx_price_bars_fetch_run ON market.price_bars (fetch_run_id)
    WHERE fetch_run_id IS NOT NULL;

CREATE VIEW market.current_price_bars AS
SELECT DISTINCT ON (series_id, trade_date) *
FROM market.price_bars
ORDER BY series_id, trade_date, observed_at DESC, bar_id DESC;

CREATE TABLE intelligence.reaction_signals (
    signal_id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_version_id    BIGINT NOT NULL REFERENCES intelligence.event_versions(event_version_id),
    series_id           BIGINT NOT NULL REFERENCES market.price_series(series_id),
    idempotency_key     TEXT NOT NULL UNIQUE,
    method_version      TEXT NOT NULL,
    input_fingerprint   TEXT NOT NULL CHECK (input_fingerprint ~ '^[0-9a-fA-F]{64}$'),
    event_cutoff_at     TIMESTAMPTZ NOT NULL,
    reaction_trade_date DATE NOT NULL,
    before_bar_id       BIGINT NOT NULL REFERENCES market.price_bars(bar_id),
    reaction_bar_id     BIGINT NOT NULL REFERENCES market.price_bars(bar_id),
    benchmark_series_id BIGINT REFERENCES market.price_series(series_id),
    benchmark_before_bar_id BIGINT REFERENCES market.price_bars(bar_id),
    benchmark_reaction_bar_id BIGINT REFERENCES market.price_bars(bar_id),
    baseline_start      DATE NOT NULL,
    baseline_end        DATE NOT NULL,
    baseline_sessions   SMALLINT NOT NULL CHECK (baseline_sessions > 1),
    baseline_return_mean NUMERIC(12,6),
    baseline_return_std NUMERIC(12,6) CHECK (baseline_return_std >= 0),
    baseline_volume_mean NUMERIC(20,4),
    baseline_volume_std NUMERIC(20,4) CHECK (baseline_volume_std >= 0),
    reaction_pct        NUMERIC(10,4),
    return_zscore       NUMERIC(10,4),
    volume_zscore       NUMERIC(10,4),
    benchmark_return_pct NUMERIC(10,4),
    abnormal_return_pct NUMERIC(10,4),
    attribution_status  TEXT NOT NULL
                        CHECK (attribution_status IN ('safe', 'unsafe', 'insufficient_data')),
    attribution_reason  TEXT NOT NULL,
    next_largest_unexplained_zscore NUMERIC(10,4),
    anomaly_context     JSONB NOT NULL DEFAULT '{}'::jsonb
                        CHECK (jsonb_typeof(anomaly_context) = 'object'),
    computed_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (baseline_end >= baseline_start),
    CHECK (baseline_end < reaction_trade_date),
    CHECK ((benchmark_series_id IS NULL AND benchmark_before_bar_id IS NULL AND
            benchmark_reaction_bar_id IS NULL AND benchmark_return_pct IS NULL) OR
           (benchmark_series_id IS NOT NULL AND benchmark_before_bar_id IS NOT NULL AND
            benchmark_reaction_bar_id IS NOT NULL AND benchmark_return_pct IS NOT NULL)),
    UNIQUE (event_version_id, series_id, method_version, reaction_trade_date)
);
CREATE INDEX idx_reactions_event_version ON intelligence.reaction_signals (event_version_id, computed_at DESC);
CREATE INDEX idx_reactions_series ON intelligence.reaction_signals (series_id, reaction_trade_date DESC);
CREATE INDEX idx_reactions_before_bar ON intelligence.reaction_signals (before_bar_id);
CREATE INDEX idx_reactions_reaction_bar ON intelligence.reaction_signals (reaction_bar_id);
CREATE INDEX idx_reactions_benchmark_series ON intelligence.reaction_signals (benchmark_series_id)
    WHERE benchmark_series_id IS NOT NULL;
CREATE INDEX idx_reactions_benchmark_before_bar ON intelligence.reaction_signals (benchmark_before_bar_id)
    WHERE benchmark_before_bar_id IS NOT NULL;
CREATE INDEX idx_reactions_benchmark_reaction_bar ON intelligence.reaction_signals (benchmark_reaction_bar_id)
    WHERE benchmark_reaction_bar_id IS NOT NULL;

CREATE TABLE intelligence.reaction_baseline_bars (
    signal_id           BIGINT NOT NULL REFERENCES intelligence.reaction_signals(signal_id),
    bar_id              BIGINT NOT NULL REFERENCES market.price_bars(bar_id),
    ordinal             SMALLINT NOT NULL CHECK (ordinal > 0),
    PRIMARY KEY (signal_id, bar_id),
    UNIQUE (signal_id, ordinal)
);
CREATE INDEX idx_reaction_baseline_bars_bar ON intelligence.reaction_baseline_bars (bar_id);

CREATE TABLE intelligence.insight_reactions (
    insight_id          BIGINT NOT NULL REFERENCES intelligence.insights(insight_id),
    signal_id           BIGINT NOT NULL REFERENCES intelligence.reaction_signals(signal_id),
    PRIMARY KEY (insight_id, signal_id)
);
CREATE INDEX idx_insight_reactions_signal ON intelligence.insight_reactions (signal_id);

-- =============================================================================
-- DELIVERY — digest composition, immutable rendered notifications, send attempts
-- =============================================================================

CREATE TABLE delivery.digests (
    digest_id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    idempotency_key     TEXT NOT NULL UNIQUE,
    digest_date         DATE NOT NULL,
    timezone            TEXT NOT NULL DEFAULT 'Asia/Kolkata',
    coverage_start      TIMESTAMPTZ NOT NULL,
    coverage_end        TIMESTAMPTZ NOT NULL,
    audience_key        TEXT NOT NULL DEFAULT 'owner',
    template_version    TEXT NOT NULL,
    rulepack_version    TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft', 'finalized', 'superseded')),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    finalized_at        TIMESTAMPTZ,
    CHECK (coverage_end > coverage_start),
    CHECK ((status = 'draft' AND finalized_at IS NULL) OR
           (status <> 'draft' AND finalized_at IS NOT NULL))
);
CREATE INDEX idx_digests_date ON delivery.digests (digest_date DESC, audience_key);

CREATE TABLE delivery.digest_items (
    digest_id           BIGINT NOT NULL REFERENCES delivery.digests(digest_id),
    insight_id          BIGINT NOT NULL REFERENCES intelligence.insights(insight_id),
    signal_id           BIGINT,
    rank_in_digest      SMALLINT NOT NULL CHECK (rank_in_digest > 0),
    ranking_score       NUMERIC(12,6) NOT NULL,
    selection_reason    TEXT NOT NULL,
    PRIMARY KEY (digest_id, insight_id),
    UNIQUE (digest_id, rank_in_digest),
    FOREIGN KEY (insight_id, signal_id)
        REFERENCES intelligence.insight_reactions(insight_id, signal_id)
);
CREATE INDEX idx_digest_items_insight ON delivery.digest_items (insight_id);

CREATE TABLE delivery.notifications (
    notification_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    idempotency_key     TEXT NOT NULL UNIQUE,
    notification_kind   TEXT NOT NULL CHECK (notification_kind IN ('digest', 'red_flag', 'system')),
    digest_id           BIGINT REFERENCES delivery.digests(digest_id),
    insight_id          BIGINT REFERENCES intelligence.insights(insight_id),
    channel             TEXT NOT NULL DEFAULT 'telegram',
    recipient_key       TEXT NOT NULL DEFAULT 'owner',
    template_version    TEXT NOT NULL,
    rendered_message    TEXT NOT NULL,
    rendered_sha256     TEXT NOT NULL CHECK (rendered_sha256 ~ '^[0-9a-fA-F]{64}$'),
    status              TEXT NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued', 'sent', 'failed', 'cancelled')),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at             TIMESTAMPTZ,
    CHECK ((notification_kind = 'digest' AND digest_id IS NOT NULL AND insight_id IS NULL) OR
           (notification_kind = 'red_flag' AND insight_id IS NOT NULL AND digest_id IS NULL) OR
           (notification_kind = 'system' AND digest_id IS NULL AND insight_id IS NULL)),
    CHECK ((status = 'sent') = (sent_at IS NOT NULL))
);
CREATE INDEX idx_notifications_status ON delivery.notifications (status, created_at);
CREATE INDEX idx_notifications_digest ON delivery.notifications (digest_id)
    WHERE digest_id IS NOT NULL;
CREATE INDEX idx_notifications_insight ON delivery.notifications (insight_id)
    WHERE insight_id IS NOT NULL;

CREATE TABLE delivery.delivery_attempts (
    delivery_attempt_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    notification_id     BIGINT NOT NULL REFERENCES delivery.notifications(notification_id),
    attempt_no          SMALLINT NOT NULL CHECK (attempt_no > 0),
    status              TEXT NOT NULL CHECK (status IN ('sent', 'failed')),
    provider_message_id TEXT,
    provider_response   JSONB,
    error_message       TEXT,
    attempted_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (provider_response IS NULL OR jsonb_typeof(provider_response) = 'object'),
    CHECK (status <> 'sent' OR provider_message_id IS NOT NULL),
    UNIQUE (notification_id, attempt_no)
);

-- =============================================================================
-- IMMUTABILITY GUARDS — database-enforced for evidence and derived snapshots
-- =============================================================================

CREATE OR REPLACE FUNCTION platform.reject_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION '% is append-only; attempted %', TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME, TG_OP;
END;
$$;

CREATE OR REPLACE FUNCTION ingestion.guard_normalized_record_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'ingestion.normalized_records cannot be deleted';
    END IF;
    IF (to_jsonb(NEW) - 'company_id') IS DISTINCT FROM (to_jsonb(OLD) - 'company_id') THEN
        RAISE EXCEPTION 'only the resolver-controlled company_id cache may change';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION ingestion.check_identity_assignment_consistency()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    target_record_id BIGINT;
    cached_company_id BIGINT;
    assigned_company_id BIGINT;
BEGIN
    target_record_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.record_id ELSE NEW.record_id END;
    SELECT company_id INTO cached_company_id
      FROM ingestion.normalized_records WHERE record_id = target_record_id;
    SELECT company_id INTO assigned_company_id
      FROM ingestion.identity_assignments WHERE record_id = target_record_id;
    IF cached_company_id IS DISTINCT FROM assigned_company_id THEN
        RAISE EXCEPTION 'normalized company cache and current identity assignment disagree for record %', target_record_id;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION intelligence.check_event_version_evidence()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM intelligence.event_version_mentions
        WHERE event_version_id = NEW.event_version_id
    ) AND NOT EXISTS (
        SELECT 1 FROM intelligence.event_version_contents
        WHERE event_version_id = NEW.event_version_id
    ) THEN
        RAISE EXCEPTION 'event version % has no evidence membership', NEW.event_version_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION intelligence.check_reaction_signal_bars()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    target_signal_id BIGINT;
    signal_row intelligence.reaction_signals%ROWTYPE;
    baseline_count INT;
    invalid_count INT;
BEGIN
    target_signal_id := NEW.signal_id;
    SELECT * INTO signal_row
      FROM intelligence.reaction_signals WHERE signal_id = target_signal_id;
    IF NOT FOUND THEN
        RETURN NEW;
    END IF;

    SELECT count(*), count(*) FILTER (
        WHERE b.series_id <> signal_row.series_id
           OR b.trade_date < signal_row.baseline_start
           OR b.trade_date > signal_row.baseline_end
           OR b.trade_date >= signal_row.reaction_trade_date
    )
    INTO baseline_count, invalid_count
    FROM intelligence.reaction_baseline_bars rb
    JOIN market.price_bars b ON b.bar_id = rb.bar_id
    WHERE rb.signal_id = target_signal_id;

    IF baseline_count <> signal_row.baseline_sessions OR invalid_count <> 0 THEN
        RAISE EXCEPTION 'reaction signal % baseline membership is inconsistent', target_signal_id;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM market.price_bars before_bar, market.price_bars reaction_bar
        WHERE before_bar.bar_id = signal_row.before_bar_id
          AND reaction_bar.bar_id = signal_row.reaction_bar_id
          AND before_bar.series_id = signal_row.series_id
          AND reaction_bar.series_id = signal_row.series_id
          AND before_bar.trade_date < signal_row.reaction_trade_date
          AND reaction_bar.trade_date = signal_row.reaction_trade_date
    ) THEN
        RAISE EXCEPTION 'reaction signal % before/reaction bars are inconsistent', target_signal_id;
    END IF;
    IF signal_row.benchmark_series_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM market.price_bars before_bar, market.price_bars reaction_bar
        WHERE before_bar.bar_id = signal_row.benchmark_before_bar_id
          AND reaction_bar.bar_id = signal_row.benchmark_reaction_bar_id
          AND before_bar.series_id = signal_row.benchmark_series_id
          AND reaction_bar.series_id = signal_row.benchmark_series_id
          AND before_bar.trade_date < signal_row.reaction_trade_date
          AND reaction_bar.trade_date = signal_row.reaction_trade_date
    ) THEN
        RAISE EXCEPTION 'reaction signal % benchmark bars are inconsistent', target_signal_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION platform.guard_job_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'processing jobs cannot be deleted';
    END IF;
    IF (to_jsonb(NEW) - ARRAY['status','attempts','available_at','locked_at','locked_by','last_error','finished_at'])
       IS DISTINCT FROM
       (to_jsonb(OLD) - ARRAY['status','attempts','available_at','locked_at','locked_by','last_error','finished_at']) THEN
        RAISE EXCEPTION 'processing job identity/payload is immutable';
    END IF;
    IF OLD.status IN ('succeeded', 'dead') AND NEW.status <> OLD.status THEN
        RAISE EXCEPTION 'terminal processing job % cannot transition from %', OLD.job_id, OLD.status;
    END IF;
    IF (OLD.status = 'queued' AND NEW.status NOT IN ('queued', 'running', 'dead')) OR
       (OLD.status = 'running' AND NEW.status NOT IN ('running', 'queued', 'succeeded', 'failed', 'dead')) OR
       (OLD.status = 'failed' AND NEW.status NOT IN ('failed', 'queued', 'running', 'dead')) THEN
        RAISE EXCEPTION 'invalid processing job transition % -> %', OLD.status, NEW.status;
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION platform.guard_outbox_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'outbox events cannot be deleted';
    END IF;
    IF (to_jsonb(NEW) - ARRAY['status','attempts','available_at','published_at','last_error'])
       IS DISTINCT FROM
       (to_jsonb(OLD) - ARRAY['status','attempts','available_at','published_at','last_error']) THEN
        RAISE EXCEPTION 'outbox event identity/payload is immutable';
    END IF;
    IF OLD.status = 'published' AND NEW.status <> 'published' THEN
        RAISE EXCEPTION 'published outbox event % is terminal', OLD.outbox_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION delivery.guard_digest_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'digests cannot be deleted';
    END IF;
    IF (to_jsonb(NEW) - ARRAY['status','finalized_at'])
       IS DISTINCT FROM
       (to_jsonb(OLD) - ARRAY['status','finalized_at']) THEN
        RAISE EXCEPTION 'digest identity/composition metadata is immutable';
    END IF;
    IF (OLD.status = 'draft' AND NEW.status NOT IN ('draft', 'finalized')) OR
       (OLD.status = 'finalized' AND NEW.status NOT IN ('finalized', 'superseded')) OR
       (OLD.status = 'superseded' AND NEW.status <> 'superseded') THEN
        RAISE EXCEPTION 'invalid digest transition % -> %', OLD.status, NEW.status;
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION delivery.guard_notification_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'notifications cannot be deleted';
    END IF;
    IF (to_jsonb(NEW) - ARRAY['status','sent_at'])
       IS DISTINCT FROM
       (to_jsonb(OLD) - ARRAY['status','sent_at']) THEN
        RAISE EXCEPTION 'rendered notification identity/payload is immutable';
    END IF;
    IF OLD.status IN ('sent', 'cancelled') AND NEW.status <> OLD.status THEN
        RAISE EXCEPTION 'terminal notification % cannot transition from %', OLD.notification_id, OLD.status;
    END IF;
    IF (OLD.status = 'queued' AND NEW.status NOT IN ('queued', 'sent', 'failed', 'cancelled')) OR
       (OLD.status = 'failed' AND NEW.status NOT IN ('failed', 'queued', 'sent', 'cancelled')) THEN
        RAISE EXCEPTION 'invalid notification transition % -> %', OLD.status, NEW.status;
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION intelligence.guard_event_membership_insert()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM intelligence.insights
        WHERE event_version_id = NEW.event_version_id
    ) OR EXISTS (
        SELECT 1 FROM intelligence.reaction_signals
        WHERE event_version_id = NEW.event_version_id
    ) THEN
        RAISE EXCEPTION 'event version % is already consumed and its membership is sealed', NEW.event_version_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION delivery.guard_digest_item_insert()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM delivery.digests
        WHERE digest_id = NEW.digest_id AND status = 'draft'
    ) THEN
        RAISE EXCEPTION 'digest % is not draft; composition is sealed', NEW.digest_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION delivery.guard_delivery_attempt_insert()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM delivery.notifications
        WHERE notification_id = NEW.notification_id AND status IN ('queued', 'failed')
    ) THEN
        RAISE EXCEPTION 'notification % is not eligible for another attempt', NEW.notification_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_guard_normalized_records
BEFORE UPDATE OR DELETE ON ingestion.normalized_records
FOR EACH ROW EXECUTE FUNCTION ingestion.guard_normalized_record_mutation();

CREATE CONSTRAINT TRIGGER trg_check_record_identity_assignment
AFTER INSERT OR UPDATE ON ingestion.normalized_records
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION ingestion.check_identity_assignment_consistency();

CREATE CONSTRAINT TRIGGER trg_check_current_identity_assignment
AFTER INSERT OR UPDATE OR DELETE ON ingestion.identity_assignments
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION ingestion.check_identity_assignment_consistency();

CREATE CONSTRAINT TRIGGER trg_check_event_version_evidence
AFTER INSERT ON intelligence.event_versions
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION intelligence.check_event_version_evidence();

CREATE CONSTRAINT TRIGGER trg_check_reaction_signal_bars
AFTER INSERT ON intelligence.reaction_signals
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION intelligence.check_reaction_signal_bars();

CREATE CONSTRAINT TRIGGER trg_check_reaction_baseline_bars
AFTER INSERT ON intelligence.reaction_baseline_bars
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION intelligence.check_reaction_signal_bars();

CREATE TRIGGER trg_guard_processing_jobs
BEFORE UPDATE OR DELETE ON platform.processing_jobs
FOR EACH ROW EXECUTE FUNCTION platform.guard_job_mutation();

CREATE TRIGGER trg_guard_outbox_events
BEFORE UPDATE OR DELETE ON platform.outbox_events
FOR EACH ROW EXECUTE FUNCTION platform.guard_outbox_mutation();

CREATE TRIGGER trg_guard_digests
BEFORE UPDATE OR DELETE ON delivery.digests
FOR EACH ROW EXECUTE FUNCTION delivery.guard_digest_mutation();

CREATE TRIGGER trg_guard_notifications
BEFORE UPDATE OR DELETE ON delivery.notifications
FOR EACH ROW EXECUTE FUNCTION delivery.guard_notification_mutation();

CREATE TRIGGER trg_guard_event_version_mentions_insert
BEFORE INSERT ON intelligence.event_version_mentions
FOR EACH ROW EXECUTE FUNCTION intelligence.guard_event_membership_insert();

CREATE TRIGGER trg_guard_event_version_contents_insert
BEFORE INSERT ON intelligence.event_version_contents
FOR EACH ROW EXECUTE FUNCTION intelligence.guard_event_membership_insert();

CREATE TRIGGER trg_guard_event_version_instruments_insert
BEFORE INSERT ON intelligence.event_version_instruments
FOR EACH ROW EXECUTE FUNCTION intelligence.guard_event_membership_insert();

CREATE TRIGGER trg_guard_digest_items_insert
BEFORE INSERT ON delivery.digest_items
FOR EACH ROW EXECUTE FUNCTION delivery.guard_digest_item_insert();

CREATE TRIGGER trg_guard_delivery_attempts_insert
BEFORE INSERT ON delivery.delivery_attempts
FOR EACH ROW EXECUTE FUNCTION delivery.guard_delivery_attempt_insert();

DO $$
DECLARE
    target regclass;
BEGIN
    FOREACH target IN ARRAY ARRAY[
        'platform.schema_migrations'::regclass,
        'ingestion.raw_artifacts'::regclass,
        'ingestion.raw_feed_items'::regclass,
        'ingestion.record_raw_items'::regclass,
        'ingestion.identity_resolutions'::regclass,
        'ingestion.stage_errors'::regclass,
        'documents.content_objects'::regclass,
        'documents.content_fetch_attempts'::regclass,
        'documents.record_contents'::regclass,
        'documents.text_versions'::regclass,
        'intelligence.extractions'::regclass,
        'intelligence.extracted_event_mentions'::regclass,
        'intelligence.events'::regclass,
        'intelligence.event_versions'::regclass,
        'intelligence.event_version_mentions'::regclass,
        'intelligence.event_version_contents'::regclass,
        'intelligence.event_version_instruments'::regclass,
        'intelligence.insights'::regclass,
        'market.price_bars'::regclass,
        'intelligence.reaction_signals'::regclass,
        'intelligence.reaction_baseline_bars'::regclass,
        'intelligence.insight_reactions'::regclass,
        'delivery.digest_items'::regclass,
        'delivery.delivery_attempts'::regclass
    ]
    LOOP
        EXECUTE format(
            'CREATE TRIGGER trg_reject_mutation BEFORE UPDATE OR DELETE ON %s '
            'FOR EACH ROW EXECUTE FUNCTION platform.reject_mutation()', target
        );
    END LOOP;
END;
$$;

-- =============================================================================
-- SEED — idempotent reference vocabularies
-- =============================================================================

INSERT INTO platform.schema_migrations (version, description) VALUES
    ('baseline-1.2', 'Fresh-install schema baseline v1.2')
ON CONFLICT (version) DO NOTHING;

INSERT INTO core.ref_sources
    (source_code, description, source_kind, default_poll_interval_sec)
VALUES
    ('nse_rss',     'NSE corporate filing RSS feeds',              'feed',        300),
    ('bse_rss',     'BSE corporate filing RSS feeds',              'feed',        300),
    ('nse_api',     'NSE corporate-announcements backfill API',    'api',         NULL),
    ('bse_api',     'BSE AnnSubCategoryGetData API',               'api',         NULL),
    ('google_news', 'Google News RSS per-company queries',         'feed',        3600),
    ('moneycontrol','Moneycontrol RSS feeds',                       'feed',        3600),
    ('nse_bhavcopy','NSE official daily market files',              'market_data', 86400),
    ('yfinance',    'Yahoo Finance market data via yfinance',       'market_data', 86400)
ON CONFLICT (source_code) DO NOTHING;

INSERT INTO core.ref_record_types (record_type, description) VALUES
    ('exchange_announcement', 'Official exchange announcement or filing record'),
    ('news_article',          'Publisher or aggregator news item'),
    ('feed_item',             'Generic source item not yet promoted to another type')
ON CONFLICT (record_type) DO NOTHING;

INSERT INTO intelligence.ref_event_categories
    (category_code, description, default_weight)
VALUES
    ('governance',       'Governance, regulatory and red-flag events', 1.500),
    ('ownership',        'Ownership, insider and pledge events',       1.400),
    ('results',          'Earnings, results and guidance',             1.200),
    ('debt_capital',     'Debt, ratings and capital structure',        1.100),
    ('growth_ops',       'Growth and operating developments',          1.000),
    ('corporate_action', 'Corporate actions and shareholder events',   0.900),
    ('compliance',       'Routine compliance disclosures',             0.600),
    ('news',             'News colour not yet confirmed by a filing',  0.400),
    ('other',            'Unclassified event',                         0.500)
ON CONFLICT (category_code) DO NOTHING;

INSERT INTO intelligence.ref_document_kinds (kind_code, description) VALUES
    ('financial_statement',       'Audited or unaudited financial statement'),
    ('auditor_report',            'Independent auditor report'),
    ('investor_presentation',     'Investor or earnings presentation'),
    ('press_release',             'Company or media press release'),
    ('exchange_cover_letter',     'Exchange submission cover letter'),
    ('board_meeting_notice',      'Notice of a forthcoming board meeting'),
    ('board_meeting_outcome',     'Outcome of a board meeting'),
    ('shareholder_notice',        'AGM, EGM or postal ballot notice'),
    ('regulatory_order',          'Order or notice from a regulator or authority'),
    ('credit_rating_letter',      'Credit rating agency communication'),
    ('shareholding_disclosure',   'Shareholding, insider or SAST disclosure'),
    ('compliance_certificate',    'Routine certificate or compliance filing'),
    ('news_article',              'Publisher article or wire story'),
    ('other',                     'Fallback document/content type')
ON CONFLICT (kind_code) DO NOTHING;

INSERT INTO intelligence.ref_event_kinds (kind_code, category_code, description) VALUES
    ('financial_results',     'results',          'Periodic financial results'),
    ('guidance_change',       'results',          'Management guidance introduced or changed'),
    ('dividend',              'corporate_action', 'Dividend declaration or change'),
    ('bonus_issue',           'corporate_action', 'Bonus share issue'),
    ('stock_split',           'corporate_action', 'Stock split or consolidation'),
    ('buyback',               'corporate_action', 'Share buyback'),
    ('agm_egm',               'corporate_action', 'AGM, EGM or postal ballot'),
    ('insider_transaction',   'ownership',        'Promoter, insider or SAST transaction'),
    ('pledge_change',         'ownership',        'Promoter encumbrance or pledge change'),
    ('shareholding_update',   'ownership',        'Shareholding pattern update'),
    ('auditor_change',        'governance',       'Auditor appointment, resignation or report concern'),
    ('executive_change',      'governance',       'KMP, director or senior executive change'),
    ('regulatory_action',     'governance',       'SEBI or other regulatory action'),
    ('litigation',            'governance',       'Material litigation, tax demand or legal notice'),
    ('insolvency',            'governance',       'Insolvency or bankruptcy proceeding'),
    ('credit_rating_change',  'debt_capital',     'Credit rating assignment or change'),
    ('fundraising',           'debt_capital',     'Debt or equity fundraising'),
    ('order_win',             'growth_ops',       'Material order or contract win'),
    ('capex',                 'growth_ops',       'Capital expenditure or capacity development'),
    ('partnership',           'growth_ops',       'Commercial or strategic partnership'),
    ('trading_window',        'compliance',       'Trading-window closure or reopening'),
    ('compliance_update',     'compliance',       'Routine compliance event'),
    ('news_development',      'news',             'News-led development awaiting or not requiring filing confirmation'),
    ('other',                 'other',            'Fallback event kind')
ON CONFLICT (kind_code) DO NOTHING;

COMMIT;
