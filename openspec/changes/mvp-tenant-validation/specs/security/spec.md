# Delta for Security

## ADDED Requirements

### Requirement: Tenant Data Isolation for PHI

All queries that return protected health information (informes, analysis results, DICOM metadata) MUST filter by the authenticated user's `clinic_id`. Cross-clinic data access MUST be denied with an opaque 403 response.

#### Scenario: Cross-clinic informe access denied

- GIVEN a medico with `clinic_id=A`
- WHEN they request an informe where `clinic_id=B` (B != A)
- THEN the system MUST return 403 Forbidden
- AND no data from clinic B MUST be included in the response

#### Scenario: User cannot access informe by guessing ID across clinics

- GIVEN a medico with `clinic_id=A`
- WHEN they request an informe by short_id belonging to `clinic_id=B`
- THEN the system MUST return 403 Forbidden
- AND the response MUST NOT reveal whether the informe exists

#### Scenario: Superadmin can access any clinic's data

- GIVEN a superadmin with `scope=global`
- WHEN they request an informe from any clinic
- THEN the system MUST return the requested data
- AND the `clinic_id` MUST be present in the response

### Requirement: NULL Tenant Rejection

The system MUST reject any operation that would create or persist a row with `clinic_id=NULL` for non-superadmin entities.

#### Scenario: Informe creation without clinic_id rejected

- GIVEN a medico with a valid JWT
- WHEN they attempt to create an informe and `clinic_id` is NULL or missing
- THEN the system MUST reject the operation with a 400-level error
- AND no row with `clinic_id=NULL` MUST be inserted

#### Scenario: Analysis job creation without clinic_id rejected

- GIVEN a user with a valid JWT
- WHEN they attempt to create an analysis_job and `clinic_id` is NULL or missing
- THEN the system MUST reject the operation
- AND no row with `clinic_id=NULL` MUST be inserted into `analysis_jobs`

### Requirement: Analysis Job Idempotency Includes Clinic

The `analysis_jobs` table UNIQUE constraint MUST include `clinic_id` to prevent cross-clinic idempotency collisions.

#### Scenario: Same study UID in different clinics creates separate jobs

- GIVEN clinic A and clinic B each submit the same `study_instance_uid`
- WHEN `create_analysis_job()` is called for each
- THEN two separate job rows MUST be created (one per clinic)
- AND no UNIQUE constraint violation MUST occur

#### Scenario: Duplicate submission within same clinic is idempotent

- GIVEN clinic A submits `study_instance_uid=X`
- WHEN the same user from clinic A submits `study_instance_uid=X` again
- THEN the system MUST return the existing job (idempotent)
- AND no duplicate row MUST be created

### Requirement: Branding Clinic Scoping

The `branding_config` table MUST be scoped by `clinic_id`. Branding queries MUST filter by the user's `clinic_id`.

#### Scenario: Branding is shared within a clinic

- GIVEN a user from clinic A with branding configured
- WHEN another user from clinic A requests branding
- THEN the system MUST return the same clinic A branding configuration

#### Scenario: Branding is isolated between clinics

- GIVEN clinic A has branding configured and clinic B does not
- WHEN a user from clinic B requests branding
- THEN the system MUST NOT return clinic A's branding
- AND MUST return default or clinic B's own configuration

### Requirement: Dead Tenant Code Resurrection

All references to `current_user.clinic_id` and `current_user.scope` in `src/api/ai/router.py` MUST resolve to valid attributes on `UserPayload` without raising `AttributeError`.

#### Scenario: AI router clinic check executes without crash

- GIVEN a user with `clinic_id=X` and `scope=clinic`
- WHEN a request reaches `src/api/ai/router.py` code that reads `current_user.clinic_id`
- THEN the attribute access MUST succeed (no AttributeError)
- AND the clinic scoping logic MUST execute with the correct `clinic_id`

## MODIFIED Requirements

### Requirement: Report Generation Auth Guards (VER-009)

The `/generar/informe` and `/informes/guardar` endpoints MUST require authentication, RBAC, and MUST persist `clinic_id` from the authenticated user's JWT on every created informe and analysis_job.

(Previously: required auth and RBAC only; no tenant scoping on created records)

#### Scenario: Unauthenticated access to report endpoints

- GIVEN an unauthenticated user
- WHEN the user sends a `POST` request to `/generar/informe` or `/informes/guardar`
- THEN the system MUST return a 401 Unauthorized status

#### Scenario: Informe creation persists clinic_id

- GIVEN an authenticated medico with `clinic_id=X`
- WHEN they send a valid `POST` to `/generar/informe`
- THEN the created informe row MUST have `clinic_id=X`
- AND the associated analysis_job MUST have `clinic_id=X`
