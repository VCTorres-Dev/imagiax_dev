# Delta for Auth

## ADDED Requirements

### Requirement: Tenant Identity in JWT Payload

The system MUST include `clinic_id` (UUID) and `scope` (string) claims in every JWT access token issued after login.

#### Scenario: JWT carries clinic context for clinic-scoped user

- GIVEN a user with `clinic_id=X` and `scope=clinic` in the `usuarios` table
- WHEN `create_access_token()` is called for this user
- THEN the JWT payload MUST contain `clinic_id=X` and `scope=clinic` claims
- AND `UserPayload` constructed from the token MUST expose `clinic_id` and `scope` fields

#### Scenario: JWT carries null clinic for superadmin

- GIVEN a superadmin user with `scope=global` and no clinic assignment
- WHEN `create_access_token()` is called
- THEN the JWT payload MUST contain `scope=global` and `clinic_id=null`
- AND `UserPayload` MUST reflect these values without error

#### Scenario: Existing sessions invalidated after JWT schema change

- GIVEN a user with a valid JWT issued before `clinic_id`/`scope` were added
- WHEN the token is decoded by the updated `decode_token()`
- THEN `UserPayload` MUST populate `clinic_id=null` and `scope=null` as defaults
- AND the system MUST treat the session as lacking tenant context (fail-closed)

### Requirement: User Schema Includes Tenant Columns

The `usuarios` table DDL MUST include `clinic_id UUID` and `scope VARCHAR` columns. The `branding_config` table MUST include a `clinic_id UUID` column.

#### Scenario: Table creation includes tenant columns

- GIVEN a fresh database with no existing tables
- WHEN `create_auth_tables()` runs at startup
- THEN the `usuarios` table MUST have `clinic_id` and `scope` columns
- AND the `branding_config` table MUST have a `clinic_id` column

#### Scenario: Schema migration is idempotent

- GIVEN a database where `usuarios` already has `clinic_id` and `scope` columns
- WHEN `create_auth_tables()` runs again
- THEN no error MUST be raised (ALTER IF NOT EXISTS or equivalent)

### Requirement: User CRUD Carries Clinic Context

User creation, retrieval, and listing operations MUST accept, persist, and return `clinic_id` and `scope`.

#### Scenario: Admin creates user with clinic assignment

- GIVEN an admin sends `POST /admin/api/usuarios` with `clinic_id` in the request body
- WHEN `create_user()` executes
- THEN the new row in `usuarios` MUST have `clinic_id` set to the provided value
- AND `clinic_id` MUST NOT be NULL for non-superadmin users

#### Scenario: User retrieval returns tenant fields

- GIVEN a user row with `clinic_id=X` and `scope=clinic`
- WHEN `get_user_by_email()` is called
- THEN the returned record MUST include `clinic_id=X` and `scope=clinic`

#### Scenario: User listing includes tenant fields

- GIVEN multiple users with varying `clinic_id` values
- WHEN `list_users()` is called
- THEN each user in the result MUST include their `clinic_id` and `scope`

### Requirement: Login Response Exposes Tenant Context

The `/auth/me` endpoint MUST return `clinic_id` and `scope` in the response body.

#### Scenario: Login response includes tenant fields

- GIVEN an authenticated user with `clinic_id=X` and `scope=clinic`
- WHEN `GET /auth/me` is called
- THEN the response body MUST include `clinic_id=X` and `scope=clinic`

## MODIFIED Requirements

### Requirement: Reports Listing Access Control (VER-003)

The `/api/informes/listado` endpoint MUST require authentication, enforce RBAC, and apply clinic scoping based on the authenticated user's `clinic_id`.

(Previously: scoped by `usuario_id` only; admin sees all reports without clinic filter)

#### Scenario: Unauthenticated access attempt

- GIVEN an unauthenticated user
- WHEN the user sends a `GET` request to `/api/informes/listado`
- THEN the system MUST return a 401 Unauthorized status

#### Scenario: Authenticated medico sees only clinic reports

- GIVEN an authenticated user with `medico` role and `clinic_id=X`
- WHEN the user sends a `GET` request to `/api/informes/listado`
- THEN the system MUST return only reports where `clinic_id=X`
- AND MUST NOT return reports from any other clinic

#### Scenario: Admin filters by clinic

- GIVEN an authenticated admin user
- WHEN the user sends a `GET` request to `/api/informes/listado?clinic_id=X`
- THEN the system MUST return only reports where `clinic_id=X`
- AND each result MUST include the `clinic_id` field

#### Scenario: Admin without clinic filter sees all with clinic present

- GIVEN an authenticated admin user
- WHEN the user sends a `GET` request to `/api/informes/listado` without a clinic filter
- THEN the system MUST return all reports
- AND every result MUST have a non-NULL `clinic_id`
