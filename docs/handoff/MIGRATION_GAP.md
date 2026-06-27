# Migration Gap

> Documents missing database migrations 006–010, the risk they pose, and the recovery path. No migration SQL is fabricated here — this is a disclosure and recovery guide, not a reconstruction.

## Quick facts

| Field | Value |
|-------|-------|
| Migrations committed to repo | 001–005 |
| Migrations applied to production but **not** in repo | 006–010 |
| Repo migration style | Raw SQL files in `migrations/` + startup auto-DDL (no Alembic `versions/`) |
| Deploy risk | **High** — schema automation blind-run may corrupt data |

## Committed migration inventory (in repo at `c0482551b`)

| File | Subject |
|------|---------|
| `001_add_classifier_columns.sql` | Classifier columns |
| `002_pipeline_v1_1.sql` | Pipeline v1.1 |
| `003_add_patient_age.sql` | Patient age |
| `004_add_patient_id_to_pagos.sql` | Patient ID on payments |
| `005_tenant_remediation_schema.sql` | Tenant remediation (soft dedup, deferred uniqueness) |

## The missing set (006–010)

The production runtime applied additional schema changes that are referenced by name in operational notes but are **not** present in the committed repository. They correspond to the tenant/clinic-pilot rollout:

| Migration (by name, from ops notes) | Purpose |
|-------------------------------------|---------|
| `006_analysis_jobs.sql` | `analysis_jobs` table for the async analysis pipeline |
| `007_clinic_scoping.sql` | `clinic_id` columns + `clinicas` table |
| `008_safe_bootstrap.sql` | `IMAGIAX_BOOTSTRAP_MODE` safe-fail-open support |
| `009_backfill_clinic_data.sql` | Backfill `clinic_id` on existing users/reports |
| `010_verify_gate.sql` | Verification gate for tenant scoping |

> The exact SQL content of 006–010 is **not recoverable from the repo**. Do not invent it. The names and purposes above come from operational documentation; the authoritative version lives only in the production database schema.

## Why this is a deploy blocker

- The repo's Dockerfile runs `app.main:create_app`, which may trigger startup auto-DDL. If the production schema is ahead of the repo's migration history, the auto-DDL assumptions and a blind `migrate` can collide with real tables/columns.
- Running any schema automation against a production DB whose provenance is unknown risks **data loss or corruption**.
- Even read-only `alembic upgrade head` checks assume a continuous migration chain that does not exist here.

## Recovery path (at least one required)

Choose in priority order:

1. **Recover from production schema (preferred).** With approved production DB access, export the affected tables' DDL (`\d+` / `pg_dump --schema-only`) and author reviewed, idempotent SQL migrations 006–010 that recreate the observed state. Review each in a PR; never apply unreviewed SQL to production.
2. **Reconstruct from git history of the runtime tree.** If the `src/api/` runtime source on the host has git history or local artifacts, mine the `CREATE TABLE` / `ALTER TABLE` statements and turn them into reviewed migrations.
3. **Document as an operational gap requiring manual intervention.** If neither source is available, record 006–010 as "applied-to-production, source-unrecoverable" and treat the repo as out of sync with production schema permanently. Any future deploy must hand-reconcile schema, not automate it.

## Acceptance for unblocking

- [ ] 006–010 either exist as reviewed SQL migrations, **or** are formally recorded as an accepted operational gap in this file.
- [ ] A schema-diff between production and the reconciled migrations is reviewed and signed off.
- [ ] `MIGRATION_STATUS` in [`MANIFEST.md`](./MANIFEST.md) reflects the chosen state: `recovered` or `documented-gap`.

## Verification before deploy

```bash
# Inspect production schema (read-only). Never run write migrations blind.
docker exec postgres-imagiax psql -U imagiax -c "\dt+"
docker exec postgres-imagiax psql -U imagiax -c "\d analysis_jobs"
docker exec postgres-imagiax psql -U imagiax -c "\d clinicas"

# Record the current migration state BEFORE any change
# (no alembic version table exists — capture manually)
```

## Next step

Resolve P0.1 (deploy-source) and P0.2 (this gap) together, then proceed to [`MANIFEST.md`](./MANIFEST.md) for release packaging.