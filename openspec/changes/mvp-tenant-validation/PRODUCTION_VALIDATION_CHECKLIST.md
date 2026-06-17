# Production Read-Only Validation Checklist

**Change**: mvp-tenant-validation
**Purpose**: Pre-deployment verification that tenant columns, indexes, and constraints are in place and no cross-clinic data leakage exists.
**When to run**: Before code deploy, after backfill completes, on a live or staging PostgreSQL connection.

## Quick path

1. Run each SELECT query below against the production/staging database.
2. Record results in the checklist.
3. All items MUST pass before proceeding to deploy.

## 1. NULL Count Checks

These queries count rows with missing tenant data. All MUST return 0 before constraints are added.

```sql
-- Non-superadmin usuarios with NULL clinic_id
SELECT COUNT(*) AS usuarios_null_clinic
FROM usuarios
WHERE clinic_id IS NULL AND rol != 'superadmin';
-- Expected: 0

-- Informes with NULL clinic_id
SELECT COUNT(*) AS informes_null_clinic
FROM informes
WHERE clinic_id IS NULL;
-- Expected: 0

-- Analysis jobs with NULL clinic_id
SELECT COUNT(*) AS analysis_jobs_null_clinic
FROM analysis_jobs
WHERE clinic_id IS NULL;
-- Expected: 0

-- Branding config with NULL clinic_id
SELECT COUNT(*) AS branding_config_null_clinic
FROM branding_config
WHERE clinic_id IS NULL;
-- Expected: 0
```

- [ ] `usuarios_null_clinic = 0`
- [ ] `informes_null_clinic = 0`
- [ ] `analysis_jobs_null_clinic = 0`
- [ ] `branding_config_null_clinic = 0`

## 2. Index and Constraint Presence

```sql
-- Verify tenant indexes exist
SELECT indexname FROM pg_indexes
WHERE indexname IN (
    'idx_usuarios_clinic_id',
    'idx_branding_config_clinic_id',
    'idx_informes_clinic_id',
    'analysis_jobs_study_user_clinic_uniq'
)
ORDER BY indexname;
-- Expected: 4 rows
```

- [ ] `idx_usuarios_clinic_id` exists
- [ ] `idx_branding_config_clinic_id` exists
- [ ] `idx_informes_clinic_id` exists
- [ ] `analysis_jobs_study_user_clinic_uniq` exists

```sql
-- Verify column existence
SELECT table_name, column_name
FROM information_schema.columns
WHERE column_name IN ('clinic_id', 'scope')
  AND table_schema = 'public'
ORDER BY table_name, column_name;
-- Expected: clinic_id on usuarios, informes, analysis_jobs, branding_config
--           scope on usuarios
```

- [ ] `usuarios.clinic_id` exists
- [ ] `usuarios.scope` exists
- [ ] `informes.clinic_id` exists
- [ ] `analysis_jobs.clinic_id` exists
- [ ] `branding_config.clinic_id` exists

## 3. Cross-Clinic SELECT-Only Probes

These SELECT queries verify that data isolation works at the SQL level. Replace `CLINIC_A_UUID` and `CLINIC_B_UUID` with real clinic IDs from the database.

```sql
-- Verify clinic A cannot see clinic B informes via the same query
-- used by get_todos_informes
SELECT COUNT(*) FROM informes
WHERE clinic_id = 'CLINIC_A_UUID'
  AND clinic_id = 'CLINIC_B_UUID';
-- Expected: 0 (no row can have two clinic_ids)

-- Verify no informes exist that belong to multiple clinics
-- (sanity check — should always be 0)
SELECT COUNT(*) FROM informes
WHERE clinic_id IS NOT NULL
GROUP BY id HAVING COUNT(DISTINCT clinic_id) > 1;
-- Expected: 0 rows returned

-- Verify analysis job idempotency: same study UID in different clinics
-- creates separate rows
SELECT study_instance_uid, COUNT(DISTINCT clinic_id) AS clinic_count
FROM analysis_jobs
WHERE clinic_id IS NOT NULL
GROUP BY study_instance_uid
HAVING COUNT(DISTINCT clinic_id) > 1;
-- Expected: rows may exist (same study submitted to different clinics — that's correct)
```

- [ ] Cross-clinic overlap query returns 0
- [ ] Analysis job idempotency uses clinic_id correctly (same study UID across clinics is allowed)

## 4. Data Integrity Spot Checks

```sql
-- Verify superadmin users have scope='global'
SELECT COUNT(*) AS superadmin_wrong_scope
FROM usuarios
WHERE rol = 'superadmin' AND scope != 'global';
-- Expected: 0

-- Verify non-superadmin users have non-NULL clinic_id
SELECT COUNT(*) AS non_superadmin_null_clinic
FROM usuarios
WHERE rol != 'superadmin' AND clinic_id IS NULL;
-- Expected: 0

-- Verify informes clinic_id matches their owner's clinic_id
SELECT COUNT(*) AS mismatched_clinic
FROM informes i
JOIN usuarios u ON i.usuario_id = u.id
WHERE u.clinic_id IS NOT NULL
  AND i.clinic_id IS NOT NULL
  AND i.clinic_id != u.clinic_id;
-- Expected: 0 (or very few — backfill may have used pilot fallback for orphans)
```

- [ ] Superadmin scope is 'global'
- [ ] Non-superadmin clinic_id is not NULL
- [ ] Informes clinic_id matches owner clinic (0 mismatches or documented exceptions)

## 5. Configuration Checks

- [ ] `IMAGIAX_BOOTSTRAP_MODE=0` or absent in production environment
- [ ] `APP_ENV=production` is set
- [ ] `IMAGIAX_PILOT_CLINIC_ID` is set to the reviewed UUID (not the zero-UUID default)

## Next step

All checks pass → proceed with code deployment. Any non-zero NULL counts → run `backfill_tenant_columns()` again and re-validate.
