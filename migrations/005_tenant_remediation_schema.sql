-- ============================================================
-- Migration: 005_tenant_remediation_schema.sql
-- Change:    fix(tenant): defer remediation uniqueness enforcement
-- Purpose:   Production deploy readiness hotfix — add schema
--            columns needed for tenant remediation WITHOUT
--            enforcing uniqueness until after deterministic
--            data remediation marks historical duplicates.
--
--            DESIGN PRINCIPLE: schema-enabling + source-alignment
--            only. No unique constraint or index is created that
--            could fail on existing production data.
--
--            BLOCKERS RESOLVED:
--            1. branding_config.clinic_id absent in production.
--               → Added idempotently here; startup code already
--                 creates the index.
--            2. analysis_jobs.clinic_id must exist BEFORE any
--               references to it (soft-dedup columns, indexes).
--               → Moved to section 2, before soft-dedup columns.
--            3. analysis_jobs_study_user_clinic_uniq CANNOT be
--               created yet — even as a partial index with
--               WHERE deduped_at IS NULL, because production has
--               5 historical terminal-duplicate groups whose rows
--               still have deduped_at = NULL. Creating the index
--               now would fail with duplicate-key violations.
--               → DEFERRED: this index must be created by a SEPARATE
--                 post-remediation migration AFTER deterministic
--                 soft-dedup marks all non-canonical rows with
--                 deduped_at. See POST-REMEDIATION NOTE below.
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
--            POST-REMEDIATION NOTE:
--            After the production data remediation script marks all
--            non-canonical rows with deduped_at, a FUTURE migration
--            should create the partial unique index:
--
--              CREATE UNIQUE INDEX IF NOT EXISTS analysis_jobs_study_user_clinic_uniq
--                  ON analysis_jobs (study_instance_uid, usuario_id, clinic_id)
--                  WHERE deduped_at IS NULL;
--
--            This index CANNOT be created now because the 41 existing
--            duplicate rows have deduped_at = NULL and would violate
--            uniqueness. It must be a separate step after remediation.
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
--     ALTER TABLE analysis_jobs DROP COLUMN IF EXISTS clinic_id;
--     ALTER TABLE analysis_jobs DROP COLUMN IF EXISTS deduped_at;
--     ALTER TABLE analysis_jobs DROP COLUMN IF EXISTS deduped_into_job_id;
--     ALTER TABLE analysis_jobs DROP COLUMN IF EXISTS deduped_reason;
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

-- ─── 2. analysis_jobs.clinic_id (MUST come before soft-dedup references) ──
-- This column is referenced by future indexes and constraints. It MUST
-- exist before any soft-dedup columns that might form part of a unique
-- index on (study_instance_uid, usuario_id, clinic_id). Adding it
-- first ensures startup code and future migrations find it available.
ALTER TABLE analysis_jobs ADD COLUMN IF NOT EXISTS clinic_id UUID;

-- ─── 3. analysis_jobs soft-dedup columns ─────────────────────────
-- These columns support soft deduplication of historical terminal
-- duplicates without deleting any data. They enable the deterministic
-- canonical rule (retain latest completed, mark others) WITHOUT
-- enforcing uniqueness yet.
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
    'Post-remediation: a partial unique index (WHERE deduped_at IS NULL) '
    'will enforce uniqueness on (study_instance_uid, usuario_id, clinic_id).';
COMMENT ON COLUMN analysis_jobs.deduped_into_job_id IS
    'References the canonical analysis_jobs row that supersedes this one. '
    'For audit traceability — always paired with deduped_at.';
COMMENT ON COLUMN analysis_jobs.deduped_reason IS
    'Human-readable reason for soft dedup, e.g. '
    'historical_duplicate or canonical_retained_latest_completed.';

-- ─── 4. REMOVED: analysis_jobs_study_user_clinic_uniq ──────────────
-- DO NOT CREATE analysis_jobs_study_user_clinic_uniq IN THIS MIGRATION.
-- See POST-REMEDIATION NOTE in the header. The partial unique index:
--
--   CREATE UNIQUE INDEX IF NOT EXISTS analysis_jobs_study_user_clinic_uniq
--       ON analysis_jobs (study_instance_uid, usuario_id, clinic_id)
--       WHERE deduped_at IS NULL;
--
-- CANNOT be created until AFTER production data remediation marks all
-- non-canonical rows with deduped_at. Creating it now would fail with
-- duplicate-key violations on the 41 existing rows that still have
-- deduped_at = NULL. This must be a SEPARATE post-remediation step.