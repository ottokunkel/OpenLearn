-- Enable UUID generation
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ============================================================
-- Job queue table for PDF-to-DoclingDocument conversion jobs
-- ============================================================
CREATE TABLE IF NOT EXISTS conversion_jobs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'processing', 'completed', 'failed')),
    storage_bucket  TEXT NOT NULL,
    storage_path    TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    error_message   TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    max_attempts    INTEGER NOT NULL DEFAULT 3,
    worker_id       TEXT
);

-- Partial index for fast polling of pending jobs
CREATE INDEX IF NOT EXISTS idx_conversion_jobs_pending
    ON conversion_jobs (created_at ASC)
    WHERE status = 'pending';

-- ============================================================
-- Lightweight metadata for converted documents.
-- Full DoclingDocument JSON lives in Supabase Storage.
-- ============================================================
CREATE TABLE IF NOT EXISTS converted_documents (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id          UUID NOT NULL UNIQUE REFERENCES conversion_jobs(id),
    doc_name        TEXT,
    page_count      INTEGER,
    storage_path    TEXT NOT NULL,
    markdown_preview TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- Atomic job claiming with row-level locking.
-- SKIP LOCKED ensures concurrent workers never block or double-claim.
-- ============================================================
CREATE OR REPLACE FUNCTION claim_next_conversion_job(p_worker_id TEXT)
RETURNS SETOF conversion_jobs
LANGUAGE sql
AS $$
    UPDATE conversion_jobs
    SET status     = 'processing',
        started_at = now(),
        updated_at = now(),
        attempts   = attempts + 1,
        worker_id  = p_worker_id
    WHERE id = (
        SELECT id FROM conversion_jobs
        WHERE status = 'pending'
          AND attempts < max_attempts
        ORDER BY created_at ASC
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING *;
$$;

-- ============================================================
-- Reset jobs stuck in 'processing' after a timeout (crash recovery).
-- ============================================================
CREATE OR REPLACE FUNCTION reset_stale_conversion_jobs(stale_minutes INTEGER DEFAULT 30)
RETURNS INTEGER
LANGUAGE sql
AS $$
    WITH stale AS (
        UPDATE conversion_jobs
        SET status     = 'pending',
            updated_at = now(),
            worker_id  = NULL
        WHERE status = 'processing'
          AND started_at < now() - (stale_minutes || ' minutes')::INTERVAL
          AND attempts < max_attempts
        RETURNING id
    )
    SELECT count(*)::INTEGER FROM stale;
$$;
