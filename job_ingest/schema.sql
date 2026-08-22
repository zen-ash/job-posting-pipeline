-- job_ingest schema. Applied idempotently via job_ingest.db.ensure_schema()
-- (CREATE ... IF NOT EXISTS everywhere), so it's safe to run at the start of
-- every ingestion run — no separate migration step to remember.

CREATE TABLE IF NOT EXISTS companies (
    slug         TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    ats          TEXT NOT NULL CHECK (ats IN ('greenhouse', 'lever', 'ashby')),
    board_token  TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Rows are never deleted. A job's lifecycle lives entirely in first_seen_at /
-- last_seen_at / closed_at / notified_at — the posting history itself is data
-- worth keeping (see README "what's next").
CREATE TABLE IF NOT EXISTS jobs (
    source              TEXT NOT NULL,
    company_slug        TEXT NOT NULL REFERENCES companies (slug),
    external_id         TEXT NOT NULL,
    title               TEXT NOT NULL,
    location            TEXT,
    department          TEXT,
    url                 TEXT NOT NULL,
    -- Best-effort timestamp as reported by the source (see job_ingest/models.py).
    -- Descriptive only — never used to decide new/closed, that's done by diffing
    -- against what we already have in this table.
    source_updated_at   TIMESTAMPTZ,
    -- Truncated to ~4000 chars on write (job_ingest/db.py:MAX_DESCRIPTION_CHARS) to
    -- stay well inside Neon's free-tier 0.5GB. content_hash is computed from the
    -- FULL untruncated text fetched each run, so truncation never hides an edit.
    -- `url` always points at the full posting.
    description         TEXT,
    content_hash         TEXT NOT NULL,
    first_seen_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- NULL while open. Set only when a board fetch SUCCEEDS and the posting is
    -- absent from that fetch's results — never inferred from a failed/timed-out
    -- fetch, so one bad request day can't mass-close (and then mass-"reopen") a
    -- company's whole set. Cleared again (reopened) if the posting reappears.
    closed_at            TIMESTAMPTZ,
    -- NULL until the digest email successfully includes this posting. The digest
    -- (step 4) queries `notified_at IS NULL AND closed_at IS NULL` instead of a
    -- time window, so a failed send or a re-run never loses or double-sends one.
    notified_at           TIMESTAMPTZ,
    PRIMARY KEY (source, company_slug, external_id)
);

-- Matches the digest's query shape exactly.
CREATE INDEX IF NOT EXISTS idx_jobs_pending_notification
    ON jobs (company_slug)
    WHERE notified_at IS NULL AND closed_at IS NULL;

-- One row per pipeline execution, so "no new jobs today" is distinguishable from
-- "the run crashed" — both look like silence otherwise.
CREATE TABLE IF NOT EXISTS runs (
    id                    BIGSERIAL PRIMARY KEY,
    started_at            TIMESTAMPTZ NOT NULL,
    finished_at           TIMESTAMPTZ,
    -- 'success': every board fetch succeeded.
    -- 'partial_failure': at least one board failed, at least one succeeded.
    -- 'failure': every board failed, or the run raised before finishing.
    status                TEXT NOT NULL CHECK (status IN ('success', 'partial_failure', 'failure')),
    companies_total       INT NOT NULL DEFAULT 0,
    companies_succeeded   INT NOT NULL DEFAULT 0,
    companies_failed      INT NOT NULL DEFAULT 0,
    jobs_seen             INT NOT NULL DEFAULT 0,
    jobs_new              INT NOT NULL DEFAULT 0,
    jobs_updated          INT NOT NULL DEFAULT 0,
    jobs_reopened         INT NOT NULL DEFAULT 0,
    jobs_closed           INT NOT NULL DEFAULT 0,
    -- Top-level message if the whole run raised before finishing (e.g. couldn't
    -- reach the DB at all). Per-board failures that didn't take the run down are
    -- in board_errors instead.
    error                 TEXT,
    -- [{"company_slug": ..., "ats": ..., "error": "..."}] for boards that failed
    -- this run without failing the run as a whole.
    board_errors          JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- Digest outcome, separate from ingestion `status` above on purpose: a run
    -- can ingest cleanly and still fail to email (or vice versa isn't possible,
    -- but conflating the two in one enum would still blur what actually broke).
    -- pending -> matched -> sent is the full filter funnel for that run, so
    -- "how aggressive is the filter" is answerable from history, not just logs.
    digest_pending_total    INT NOT NULL DEFAULT 0,
    digest_matched_total    INT NOT NULL DEFAULT 0,
    digest_sent             BOOLEAN NOT NULL DEFAULT false,
    digest_postings_sent    INT NOT NULL DEFAULT 0,
    digest_error            TEXT
);

-- Lightweight forward-only "migrations" without a separate framework: new
-- columns get added to the CREATE TABLE above (for a database created fresh
-- from this file, e.g. a new Neon project) AND as an ADD COLUMN IF NOT EXISTS
-- below (for a database — like the local docker-compose one — that already
-- existed before the column was added). Both run every time via ensure_schema()
-- and are no-ops once applied.
ALTER TABLE runs ADD COLUMN IF NOT EXISTS digest_pending_total INT NOT NULL DEFAULT 0;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS digest_matched_total INT NOT NULL DEFAULT 0;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS digest_sent BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS digest_postings_sent INT NOT NULL DEFAULT 0;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS digest_error TEXT;
