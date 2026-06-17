# Tasks: MVP Tenant Validation

## Review Workload Forecast

| Field | Value |
|---|---|
| Estimated changed lines | 450-700 |
| 400-line budget risk | Medium |
| Chained PRs recommended | No |
| Suggested split | Single PR with 4 work units |
| Delivery strategy | auto-forecast |
| Chain strategy | pending |

Decision needed before apply: No
Chained PRs recommended: No
Chain strategy: pending
400-line budget risk: Medium

### Suggested Work Units

| Unit | Goal | Likely PR | Notes |
|---|---|---|---|
| 1 | Schema/backfill + safety checks | PR 1 | Base: main; includes rollback-safe DDL and read-only validation |
| 2 | JWT/UserPayload tenant propagation | PR 1 | Same PR; re-login expected, keep tests with code |
| 3 | Query scoping across PHI/admin flows | PR 1 | Same PR; clinic filters for informes/jobs/branding |
| 4 | Bootstrap/read-only validation | PR 1 | Same PR; prod fail-closed + warning-only startup checks |

## Phase 1: DB foundation

- [x] 1.1 Update `app/auth/service.py:create_auth_tables()` to add `usuarios.clinic_id`, `usuarios.scope`, and `branding_config.clinic_id` with idempotent ALTERs.
- [x] 1.2 Add backfill helpers in `app/auth/service.py` / `app/informes/db.py` for NULL `usuarios`/`informes` rows; keep pilot-clinic fallback and leave additive columns in rollback.
- [x] 1.3 Add read-only validation SQL/checks for NULL counts before constraints; document rollback as code-only revert, no data drop.

## Phase 2: Auth + tenant identity

- [x] 2.1 Extend `app/auth/models.py:UserPayload` with nullable `clinic_id` and `scope`.
- [x] 2.2 Patch `app/auth/service.py:create_access_token()`, `decode_token()`, `decode_token_with_renewal_check()`, `get_user_by_email()`, `create_user()`, and `list_users()` to carry tenant claims.
- [x] 2.3 Update `app/auth/router.py:/auth/login` and `/auth/me` responses; verify old JWTs decode fail-closed.

## Phase 3: Scoping/wiring

- [x] 3.1 Thread `clinic_id` through `app/admin/router.py` `CreateUserRequest`, `create_usuario`, and `admin_informes()` query params/results.
- [x] 3.2 Enforce clinic filters in `app/informes/router.py:listado_informes()` and `app/informes/db.py` (`guardar_informe`, `get_todos_informes`, `create_analysis_job`, branding helpers) plus `src/api/ai/router.py` dead tenant paths.
- [x] 3.3 Add/update `app/api/branding/router.py` and any serialization helpers so branding is clinic-scoped and response payloads include `clinic_id`.

## Phase 4: Startup validation + tests

- [x] 4.1 Add `IMAGIAX_BOOTSTRAP_MODE` to `app/core/config.py` and gate `app/main.py` startup with dev-only relaxed mode + warning.
- [x] 4.2 Add tests for JWT claims, user CRUD, report/admin denial, analysis job uniqueness, branding scoping, and bootstrap mode behavior.
- [x] 4.3 Run production read-only validation steps: NULL counts, constraint/index checks, and cross-clinic SELECT-only probes before rollout.