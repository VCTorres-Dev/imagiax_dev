-- Migration: 002_pipeline_v1_1.sql
-- Change: ai-pipeline-v1.1 (T-03)
-- Purpose: Add nullable columns to `informes` table for v1.1 pipeline features.
--          All columns are nullable / have defaults — zero-downtime, backward compatible.
-- Run BEFORE deploying v1.1 code.

-- Clinical note entered by the physician before report generation
ALTER TABLE informes
    ADD COLUMN IF NOT EXISTS nota_clinica VARCHAR(500) NULL;

-- Quality Gate result from Phase 0.5 (Gemini Flash image quality assessment)
-- Stores: {verdict, reason, confidence}
ALTER TABLE informes
    ADD COLUMN IF NOT EXISTS quality_gate_result JSONB NULL;

-- Auditor result from Phase 2 (Gemini Pro second-pass review)
-- Stores: {verdict, observations, severity, recommendation, model_used}
ALTER TABLE informes
    ADD COLUMN IF NOT EXISTS audit_result JSONB NULL;

-- Which pipeline version was used to generate this report
ALTER TABLE informes
    ADD COLUMN IF NOT EXISTS pipeline_version VARCHAR(10) NULL DEFAULT '1.0';

-- Full structured trace of all pipeline phases with timing and verdicts
-- Stores: {pipeline_id, version, started_at, phases: {...}, quality_gate, audit_result}
ALTER TABLE informes
    ADD COLUMN IF NOT EXISTS pipeline_trace JSONB NULL;

-- Index for quick lookup by pipeline_id (for debug trace endpoint)
CREATE INDEX IF NOT EXISTS idx_informes_pipeline_version
    ON informes (pipeline_version)
    WHERE pipeline_version IS NOT NULL;

-- Comment columns for documentation
COMMENT ON COLUMN informes.nota_clinica IS 'Sanitized clinical note from physician (max 500 chars), injected into v1.1 LLM prompt';
COMMENT ON COLUMN informes.quality_gate_result IS 'v1.1 Phase 0.5: Gemini Flash quality verdict {verdict, reason, confidence}';
COMMENT ON COLUMN informes.audit_result IS 'v1.1 Phase 2: Gemini Pro audit verdict {verdict, observations, severity, recommendation, model_used}';
COMMENT ON COLUMN informes.pipeline_version IS 'Pipeline version used: 1.0 (legacy) or 1.1 (with quality gate + auditor)';
COMMENT ON COLUMN informes.pipeline_trace IS 'Full JSON trace of all pipeline phases with timing, model, tokens, verdicts';
