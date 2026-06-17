# Delta for Infrastructure

## ADDED Requirements

### Requirement: Bootstrap Mode Configuration

The system MUST read `IMAGIAX_BOOTSTRAP_MODE` from environment at startup. When absent or `0`, bootstrap admin creation MUST be fail-closed (tenant checks enforced). When `1` and `APP_ENV=development`, tenant checks MAY be relaxed for initial setup.

#### Scenario: Production bootstrap is fail-closed

- GIVEN `IMAGIAX_BOOTSTRAP_MODE` is absent or `0`
- WHEN the application starts
- THEN tenant validation MUST be enforced on all startup operations
- AND bootstrap admin creation MUST require valid `clinic_id`

#### Scenario: Dev bootstrap relaxes tenant checks

- GIVEN `IMAGIAX_BOOTSTRAP_MODE=1` AND `APP_ENV=development`
- WHEN the application starts
- THEN bootstrap admin creation MAY proceed without `clinic_id`
- AND a startup warning MUST be logged indicating relaxed tenant mode

#### Scenario: Bootstrap mode=1 in production is rejected

- GIVEN `IMAGIAX_BOOTSTRAP_MODE=1` AND `APP_ENV=production`
- WHEN the application starts
- THEN the system MUST refuse to start with a clear error message
- AND MUST log that bootstrap mode is not permitted in production

### Requirement: Startup Tenant Validation

The system MUST run a startup validation that detects any `usuarios` or `informes` rows with `clinic_id=NULL` and report them as warnings.

#### Scenario: Startup detects NULL tenant rows

- GIVEN the database contains `usuarios` rows with `clinic_id=NULL`
- WHEN the application starts
- THEN the validation MUST log a WARNING with the count of affected rows
- AND the application MUST still start (non-blocking warning)

#### Scenario: Startup passes when all rows have clinic_id

- GIVEN all `usuarios` and `informes` rows have non-NULL `clinic_id`
- WHEN the application starts
- THEN the validation MUST log a PASS message
- AND startup MUST proceed normally

### Requirement: Database Backfill for Existing NULL Tenants

Before enforcing NOT NULL constraints, the system MUST provide an idempotent backfill that assigns a pilot clinic UUID to all existing `usuarios` and `informes` rows with `clinic_id=NULL`.

#### Scenario: Backfill assigns clinic to NULL usuarios

- GIVEN existing `usuarios` rows with `clinic_id=NULL` (non-superadmin)
- WHEN the backfill migration runs
- THEN all affected rows MUST be updated with the pilot clinic UUID
- AND superadmin rows MUST remain unchanged

#### Scenario: Backfill assigns clinic to NULL informes

- GIVEN existing `informes` rows with `clinic_id=NULL`
- WHEN the backfill migration runs
- THEN all affected rows MUST be updated with the clinic_id from the associated `usuario_id`
- AND if the user has no clinic, the pilot clinic UUID MUST be used

#### Scenario: Backfill is idempotent

- GIVEN all rows already have non-NULL `clinic_id`
- WHEN the backfill migration runs again
- THEN zero rows MUST be modified
- AND no error MUST be raised

### Requirement: Admin Flows Are Clinic-Aware

Admin endpoints for user management and informe listing MUST support clinic filtering and MUST display `clinic_id` in all list responses.

#### Scenario: Admin creates user with clinic assignment

- GIVEN an admin sends `POST /admin/api/usuarios` with `email`, `nombre`, `rol`, and `clinic_id`
- WHEN the request is processed
- THEN the new user MUST be created with the specified `clinic_id`
- AND the response MUST include the assigned `clinic_id`

#### Scenario: Admin lists informes with clinic filter

- GIVEN an admin sends `GET /admin/api/informes?clinic_id=X`
- WHEN the request is processed
- THEN only informes with `clinic_id=X` MUST be returned
- AND each result MUST include its `clinic_id`

#### Scenario: Admin lists informes without filter

- GIVEN an admin sends `GET /admin/api/informes` without a clinic filter
- WHEN the request is processed
- THEN all informes MUST be returned grouped or tagged by `clinic_id`
- AND no informe with `clinic_id=NULL` MUST appear (backfill ensures this)

## REMOVED Requirements

### Requirement: Reports Listing Access Control (VER-003) — Admin Unfiltered Access

(Reason: The previous spec allowed admin to see all reports without clinic context. The new requirement mandates that every result includes `clinic_id` and that filtering by clinic is supported. This is superseded by the MODIFIED requirement in the auth delta spec.)
