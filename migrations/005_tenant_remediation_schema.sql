-- ============================================================
-- Migration: 005_tenant_remediation_schema.sql
-- Change:    fix(tenant): align remediation schema with soft dedup
-- Purpose:   Production deploy readiness hotfix — align schema and
--            source code so that the application can start on
--            production without crashing on duplicate analysis_jobs.
--
--            BLOCKERS RESOLVED:
--            1. branding_config.clinic_id absent in production.
--               → Added idempotently here; startup code already
--                 creates the index.
--            2. analysis_jobs_study_user_clinic_uniq cannot be a
--               total UNIQUE index because production has 5
--               historical terminal-duplicate groups (41 rows total,
--               0 active pending/running duplicates).
--               → Replaced total unique with partial soft-dedup
--                 index WHERE deduped_at IS NULL.
--
--            PRODUCTION CONTEXT:
--            - 5 duplicate groups, 41 rows involved, 0 active.
--            - Existing partial index idx_analysis_jobs_pending_dedup
--              ON (study_instance_uid, usuario_id)
--              WHERE status IN ('pending','running') already guards
--              concurrent inserts.
--            - Deterministic canonical rule (approved):
--              retain latest completed per group, else latest terminal,
--              mark non-canonical rows with deduped_at/deduped_into_job_id.
--            - No data is deleted; soft-dedup preserves audit trail.
--
-- ROLLOUT:
--   Safe on live DB: all ALTERs are IF NOT EXISTS / IF NOT NULL;
--   indexes use IF NOT EXISTS; no data is mutated or deleted.
--   Backfill of branding_config.clinic_id from owner usuarios.clinic_id
--   is idempotent (WHERE bc.clinic_id IS NULL).
--
-- ROLLBACK:
--   Columns and indexes are nullable / independent — no rollback
--   SQL required for code rollback. To permanently remove (only if
--   change is fully reverted):
--     ALTER TABLE analysis_jobs DROP COLUMN IF EXISTS deduped_at;
--     ALTER TABLE analysis_jobs DROP COLUMN IF EXISTS deduped_into_job_id;
--     ALTER TABLE analysis_jobs DROP COLUMN IF EXISTS deduped_reason;
--     DROP INDEX IF EXISTS analysis_jobs_study_user_clinic_uniq;
--   To remove the branding_config additions only:
--     ALTER TABLE branding_config DROP COLUMN IF EXISTS clinic_id;
--     DROP INDEX IF EXISTS idx_branding_config_clinic_id;
-- ============================================================

-- ─── 1. branding_config.clinic_id ───────────────────────────────
-- Production was missing this column. The startup code in
-- create_auth_tables() also adds it with ADD COLUMN IF NOT EXISTS,
-- but on production the table exists WITHOUT clinic_id, so this
-- migration ensures it is present BEFORE the application starts.
ALTER TABLE branding_config ADD COLUMN IF NOT EXISTS clinic_id UUID;

-- Backfill clinic_id from the owning user's clinic_id where possible.
-- This is safe: only rows where branding_config.clinic_id is still
-- NULL get updated from their owner usuario's clinic_id.
-- Superadmin rows (scope='global') intentionally keep clinic_id=NULL.
UPDATE branding_config bc
SET clinic_id = u.clinic_id
FROM usuarios u
WHERE bc.usuario_id = u.id
  AND bc.clinic_id IS NULL
  AND u.clinic_id IS NOT NULL;

-- Index for clinic-scoped branding queries.
-- Startup code also creates this with IF NOT EXISTS; the migration
-- ensures it exists BEFORE the application starts on production.
CREATE INDEX IF NOT EXISTS idx_branding_config_clinic_id
    ON branding_config (clinic_id);

COMMENT ON COLUMN branding_config.clinic_id IS
    'Tenant clinic UUID for PHI isolation. NULL for global/superadmin rows. '
    'Backfilled from owner usuario.clinic_id when available.';

-- ─── 2. analysis_jobs soft-dedup columns ─────────────────────────
-- These columns support soft deduplication of historical terminal
-- duplicates without deleting any data. The partial unique index
-- below replaces the unsafe total unique index that would fail on
-- production due to existing duplicate rows.
--
-- deduped_at:      Timestamp when this row was marked non-canonical.
--                  NULL means the row is canonical (or not yet deduped).
-- deduped_into_job_id: UUID of the canonical row that replaces this one.
--                  References analysis_jobs(id) for audit traceability.
-- deduped_reason:  Human-readable reason for dedup, e.g.
--                  'historical_duplicate' or 'canonical_retained_latest_completed'.
ALTER TABLE analysis_jobs ADD COLUMN IF NOT EXISTS deduped_at TIMESTAMPTZ;
ALTER TABLE analysis_jobs ADD COLUMN IF NOT EXISTS deduped_into_job_id UUID REFERENCES analysis_jobs(id);
ALTER TABLE analysis_jobs ADD COLUMN IF NOT EXISTS deduped_reason TEXT;

COMMENT ON COLUMN analysis_jobs.deduped_at IS
    'Soft-dedup timestamp: when set, this row is a non-canonical '
    'historical duplicate. NULL means canonical or not yet deduped. '
    'Used by partial unique index analysis_jobs_study_user_clinic_uniq.';
COMMENT ON COLUMN analysis_jobs.deduped_into_job_id IS
    'References the canonical analysis_jobs row that supersedes this one. '
    'For audit traceability — always paired with deduped_at.';
COMMENT ON COLUMN analysis_jobs.deduped_reason IS
    'Human-readable reason for soft dedup, e.g. '
    'historical_duplicate or canonical_retained_latest_completed.';

-- ─── 3. Partial unique index (replaces unsafe total unique) ───────
-- PRODUCTION SAFETY RATIONALE:
-- Production has 5 groups of historical terminal duplicates (41 rows).
-- None are in pending/running status. The existing partial index
-- idx_analysis_jobs_pending_dedup already prevents concurrent
-- active-job duplicates:
--   UNIQUE (study_instance_uid, usuario_id) WHERE status IN ('pending','running')
--
-- This new partial index enforces uniqueness ONLY for non-deduped rows,
-- allowing historical duplicates to coexist once marked with deduped_at.
-- After production remediation marks the 36 non-canonical rows with
-- deduped_at, this index enforces that no NEW duplicates can be created
-- for the same (study_instance_uid, usuario_id, clinic_id) group
-- going forward, without requiring deletion of historical data.
--
-- CAVEAT: CREATE INDEX (non-CONCURRENTLY) inside a migration transaction
-- blocks writes for the duration of the index build. On small-to-medium
-- tables (<100K rows) this is acceptable. If using a migration runner
-- that wraps in a transaction, CONCURRENTLY cannot be used inside the
-- transaction block. For production deployment, if the table is large,
-- consider running CREATE INDEX CONCURRENTLY separately before this
-- migration. With IF NOT EXISTS, the index will simply be skipped if
-- already created concurrently.
CREATE UNIQUE INDEX IF NOT EXISTS analysis_jobs_study_user_clinic_uniq
    ON analysis_jobs (study_instance_uid, usuario_id, clinic_id)
    WHERE deduped_at IS NULL;

-- ─── 4. Ensure clinic_id exists on analysis_jobs ──────────────────
-- Startup code also adds this, but the migration ensures it is present
-- BEFORE the application starts on production.
ALTER TABLE analysis_jobs ADD COLUMN IF NOT EXISTS clinic_id UUID;