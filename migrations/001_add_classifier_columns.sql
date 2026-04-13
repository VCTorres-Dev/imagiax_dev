-- ============================================================
-- Migration: 001_add_classifier_columns.sql
-- Change:    prompt-orchestrator
-- Purpose:   Add exam classification audit columns to informes
-- ============================================================
--
-- ROLLOUT:
--   Run BEFORE deploying the new prompt_orchestrator code.
--   Safe on live DB: all columns are nullable, no reads are locked.
--   Each ALTER is independent — partial failure is fully resumable.
--
--   SSH command:
--     ssh -i ~/.ssh/do_imagiax root@imagiax.dev \
--       "psql \$POSTGRES_URL -f /app/migrations/001_add_classifier_columns.sql"
--
-- ROLLBACK:
--   Columns are nullable and ignored by existing queries.
--   No rollback SQL required for code rollback.
--   To permanently remove (only if change is fully reverted):
--     ALTER TABLE informes DROP COLUMN IF EXISTS exam_region;
--     ALTER TABLE informes DROP COLUMN IF EXISTS exam_modality;
--     ALTER TABLE informes DROP COLUMN IF EXISTS classification_source;
--     ALTER TABLE informes DROP COLUMN IF EXISTS exam_confidence;
--     DROP INDEX IF EXISTS idx_informes_exam_region;
-- ============================================================

-- exam_region: classified body region (e.g. 'chest', 'knee', 'brain')
ALTER TABLE informes ADD COLUMN IF NOT EXISTS exam_region VARCHAR(50) DEFAULT NULL;

-- exam_modality: effective modality (e.g. 'xray', 'ct', 'mri')
ALTER TABLE informes ADD COLUMN IF NOT EXISTS exam_modality VARCHAR(20) DEFAULT NULL;

-- classifier_source: how classification was determined
--   values: 'rules' | 'llm_fallback' | 'generic_fallback'
ALTER TABLE informes ADD COLUMN IF NOT EXISTS classification_source VARCHAR(20) DEFAULT NULL;

-- exam_confidence: confidence score 0.0-1.0
--   1.0 = high certainty from rules engine
--   0.0 = unknown / generic fallback
ALTER TABLE informes ADD COLUMN IF NOT EXISTS exam_confidence FLOAT DEFAULT NULL;

-- Index on exam_region for future analytics and filtering queries
CREATE INDEX IF NOT EXISTS idx_informes_exam_region ON informes(exam_region);
