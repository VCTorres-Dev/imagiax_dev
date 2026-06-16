# Exploration: MVP Tenant Validation — IMAGIAX

**Date**: 2026-06-16
**Change**: mvp-tenant-validation
**Status**: EXPLORATION COMPLETE — evidence gathered, code not modified

---

## Executive Summary

IMAGIAX has a **dual-codebase split** between `src/api/` (old, production-facing) and `app/` (newer refactor). The old AI router references `current_user.clinic_id` and `current_user.scope` but the `UserPayload` model in `app/auth/models.py` has **neither field** — making clinic scoping **dead code that would raise `AttributeError` at runtime**. The new `app/` codebase doesn't implement tenant scoping at all: the JWT carries no `clinic_id`, the `usuarios` table DDL omits `clinic_id`/`scope` columns, user creation never sets `clinic_id`, and the informe listing scopes by `usuario_id` only. The `IMAGIAX_BOOTSTRAP_MODE` env var referenced in docs is **not read by any code**. Three distinct data-leak vectors exist and the minimum fix requires spanning JWT, DB schema, user CRUD, and 7 query paths.

---

## 1. Current State — How Tenant Scoping Works Today

### 1.1 Codebase Architecture — TWO Entry Points

| Entry Point | Path | Used For |
|---|---|---|
| `src/api/main.py:create_app` | Production entry point | Tries `api.*` first, falls back to `app.*` |
| `app/main.py:create_app` | Simpler dev entry point | Uses only `app.*` modules |

Production loads:
- **Auth**: from `api.auth.*` → fallback `app.auth.*` (no `api/auth/` exists, so always `app.auth.*`)
- **AI**: from `api.ai.router` = `src/api/ai/router.py` (3130 lines, OLD code with clinic_id refs)
- **Admin**: from `app.admin.router`
- **Informes**: mounted via `api.ai.router`

### 1.2 JWT Payload — No clinic_id

`app/auth/service.py:create_access_token()` (lines 84–123) creates the JWT with these claims:
```
sub, email, nombre, rol, iat, exp, jti, remember, fp, iss, aud
```
**`clinic_id` and `scope` are NOT in the JWT.**

`app/auth/models.py:UserPayload` (lines 12–23):
```python
class UserPayload(BaseModel):
    user_id: str
    email: str
    nombre: str
    rol: str
    jti: str | None = None
    fp: str | None = None
    remember: bool = False
```
**No `clinic_id`, no `scope`.**

`app/auth/service.py:decode_token()` (lines 209–221) constructs `UserPayload` without `clinic_id` or `scope`.

`app/auth/router.py:auth_me()` (lines 181–192) — login response returns only `email`, `nombre`, `rol`.

### 1.3 Old AI Router References `current_user.clinic_id` — DEAD CODE

`src/api/ai/router.py` has 6 call sites that reference `current_user.clinic_id` and `current_user.scope`:

| Line | Expression | Would Crash? |
|---|---|---|
| 187 | `current_user.scope != "global"` | **YES — AttributeError** |
| 189 | `current_user.clinic_id` | **YES — AttributeError** |
| 193 | `current_user.clinic_id, current_user.scope` | **YES — AttributeError** |
| 2573 | `clinic_id=current_user.clinic_id or None` | **YES — AttributeError** |
| 2659 | `clinic_id=current_user.clinic_id or None` | **YES — AttributeError** |
| 2901 | `clinic_id=current_user.clinic_id or None` | **YES — AttributeError** |

Since `UserPayload` has neither `clinic_id` nor `scope`, every one of these lines would raise `AttributeError` at runtime. This means:

1. The clinic scoping code in `src/api/ai/router.py` is **effectively dead** — it cannot execute
2. The `or None` fallback on lines 2573/2659/2901 never triggers because the attribute access crashes first
3. Informes are created/listed with `clinic_id=NULL` because the old code crashes before persisting, OR there's a try/except swallowing the error

### 1.4 New `app/` Codebase — No Tenant Scoping At All

| Layer | Status | Evidence |
|---|---|---|
| **JWT** | No `clinic_id` | `create_access_token()` only writes `sub/email/nombre/rol` |
| **UserPayload** | No `clinic_id`/`scope` | 7 fields total, neither tenant field present |
| **Usuarios table DDL** | No `clinic_id`/`scope` column | `app/auth/service.py:412-423` — CREATE TABLE only has `id, email, password_hash, nombre, rol, activo` |
| **User creation** | No `clinic_id` set | `create_user()` INSERT has no `clinic_id` column |
| **Admin create user** | No `clinic_id` field | `CreateUserRequest` model has only `email, password, nombre, rol` |
| **Login response** | No `clinic_id` | `/auth/me` returns `email, nombre, rol` |
| **Informe listing** | Scoped by `usuario_id` only | `app/informes/router.py:188-198` — filters by `user_id`, not clinic |
| **Branding** | Scoped by `usuario_id` only | `branding_config` table uses `usuario_id` as UNIQUE key, no `clinic_id` column |
| **Analysis jobs** | Accepts `clinic_id` param but never receives it | `create_analysis_job()` has `clinic_id` parameter but callers can't pass it |

### 1.5 Informes DB Layer — Partial Support

`app/informes/db.py` has the infrastructure to support clinic scoping **if callers could provide it**:

- `guardar_informe()` — accepts `clinic_id: str | None = None` ✅
- `get_todos_informes()` — accepts `clinic_id` and `scope` parameters, uses `($5::uuid IS NULL OR clinic_id = $5)` pattern ✅
- `get_study_by_short_id()` — returns `clinic_id` from informes ✅
- `create_analysis_job()` — accepts `clinic_id`, inserts into `analysis_jobs` ✅
- Branding functions — scoped by `usuario_id` only, no clinic concept ❌

The SQL layer is ready for clinic scoping — the problem is upstream: nobody can provide the `clinic_id` value.

### 1.6 Bootstrap Mode — Referenced but Not Implemented

- `IMAGIAX_BOOTSTRAP_MODE=1` is referenced in `ESTADO_REAL_DEL_PROYECTO.txt` (lines 30, 89, 180, 208)
- `IMAGIAX_PROJECT_BRIEF.txt` line 229: "IMAGIAX_BOOTSTRAP_MODE no debe quedar prendido más tiempo del necesario"
- The new `app/core/config.py` does **NOT** read `IMAGIAX_BOOTSTRAP_MODE`
- `app/main.py` bootstrap logic (lines 62–67) runs `ensure_admin_exists` + `upsert_bootstrap_superadmin_from_env` unconditionally
- `upsert_bootstrap_superadmin_from_env()` is guarded by `APP_ENV=development` only (SEC-M02)
- **There is no code path that reads `IMAGIAX_BOOTSTRAP_MODE` and relaxes tenant checks**

This means the bootstrap env var is either:
1. Read by the OLD production code not in this repo (deployed from an older version)
2. A configuration that was intended but never fully implemented in the refactored code

---

## 2. Affected Areas

### 2.1 Critical (P0 — must fix for tenant isolation)

| File | Issue | Impact |
|---|---|---|
| `app/auth/models.py` | `UserPayload` missing `clinic_id`, `scope` | Root cause — no tenant identity flows through the system |
| `app/auth/service.py:84-123` | `create_access_token()` doesn't include `clinic_id` in JWT | JWT carries no tenant context |
| `app/auth/service.py:412-423` | `create_auth_tables()` DDL missing `clinic_id`, `scope` columns | New DB instances won't have tenant columns |
| `app/auth/service.py:507-536` | `create_user()` doesn't set `clinic_id` | New users always get `clinic_id=NULL` |
| `app/auth/service.py:487-494` | `get_user_by_email()` doesn't return `clinic_id` | Login can't fetch tenant info |
| `app/admin/router.py:56-61` | `CreateUserRequest` missing `clinic_id` | Admin UI can't assign clinic |
| `app/auth/router.py:158-166` | Login response missing `clinic_id` | Frontend can't store tenant context |
| `app/informes/router.py:188-198` | `listado_informes` scopes by `usuario_id` only | A clinic user can see another clinic's informes via IDOR |
| `app/admin/router.py:251-262` | `admin_informes` has no clinic filter | Admin can't filter by clinic |
| `src/api/ai/router.py:187-193` | Dead code — `current_user.clinic_id` crashes | Clinic scoping check never executes |
| `src/api/ai/router.py:2573,2659,2901-2902` | Dead code — `current_user.clinic_id` crashes | Informes/analysis_jobs get `clinic_id=NULL` |

### 2.2 Important (P1 — should fix)

| File | Issue | Impact |
|---|---|---|
| `app/informes/db.py:773-809` | `branding_config` table has no `clinic_id` | Branding is per-user, not per-clinic |
| `app/informes/db.py:627-647` | `recover_running_jobs()` only handles `running` status, not stale `pending` | Pending orphan analysis_jobs still block retries |
| `app/main.py:62-67` | Bootstrap admin creation runs unconditionally | No `IMAGIAX_BOOTSTRAP_MODE` guard |
| `app/core/config.py` | Missing `IMAGIAX_BOOTSTRAP_MODE` setting | No way to toggle bootstrap behavior |
| `app/api/branding/router.py` | Branding endpoints scoped by `usuario_id` only | Cross-clinic branding might be accessible |
| `app/informes/router.py:133-161` | `ver_informe` has IDOR check by `usuario_id` but no clinic check | Medico from clinic A can't see clinic B's reports (blocked by user_id), but no defense in depth |

### 2.3 Informational (P2 — awareness)

| File | Issue | Impact |
|---|---|---|
| `Dockerfile` | Entry point is `uvicorn api.main:create_app --factory` | Uses `src/api/main.py` which mixes old+new code |
| `docs/onboarding/ESTADO_REAL_DEL_PROYECTO.txt:30` | Bootstrap mode referenced but not in code | Documentation drift |
| No migration scripts found | Backfill was manual or in deployed-only code | Can't reproduce the backfill locally |

---

## 3. Data Leak Vectors — How Data Can Be Ambiguous Between Clinics

### Vector 1: Informe Listing Without Clinic Scoping (P0)
- **Path**: `GET /listado` → `app/informes/router.py:listado_informes` 
- **Mechanism**: Scoped by `usuario_id` only — no `clinic_id` filter
- **Exploit**: Medico from Clinic A can't see Clinic B's informes directly (blocked by user_id), BUT if admin lists all without clinic filter, data from all clinics is mixed
- **Severity**: HIGH — admin view mixes all clinic data

### Vector 2: Analysis Job Idempotency Without Clinic Scoping (P0)
- **Path**: `POST /generar/informe` → `create_analysis_job()`
- **Mechanism**: UNIQUE constraint on `(study_instance_uid, usuario_id)` but NOT on `(study_instance_uid, usuario_id, clinic_id)`. If two users in different clinics analyze the same study_uid, only one gets a job.
- **Severity**: MEDIUM — edge case, requires same study UID across clinics

### Vector 3: Branding Per-User Without Clinic Grouping (P1)
- **Path**: `/me/branding` endpoints
- **Mechanism**: `branding_config` uses `usuario_id` as unique key with no clinic association
- **Severity**: LOW — branding is visual only, but inconsistent within clinics

### Vector 4: New Users Always Get clinic_id=NULL (P0)
- **Path**: `POST /admin/api/usuarios` → `create_user()`
- **Mechanism**: `create_user()` INSERT has no `clinic_id` column
- **Severity**: CRITICAL — every new user starts with no clinic assignment

---

## 4. Approaches

### Approach A: Full JWT + DB Schema Fix (Recommended)
**Description**: Add `clinic_id` and `scope` to the JWT payload, `UserPayload` model, `usuarios` table DDL, user CRUD, and all query paths. Backfill existing NULLs. Add NOT NULL constraint when safe.

- **Pros**:
  - Complete tenant isolation — fixes root cause
  - JWT carries tenant context for every request
  - Defense in depth: JWT check + DB query filter
  - Aligns with what `src/api/ai/router.py` was trying to do
  - Production-safe if done with backfill before constraint
- **Cons**:
  - Touches auth (high-risk area)
  - All existing JWTs become invalid (users re-login)
  - Requires DB migration with backfill
  - Multiple files affected (~10 files)
- **Effort**: Medium (est. 300-400 lines changed)

### Approach B: DB-Only Scoping (JWT Unchanged)
**Description**: Keep JWT as-is. On every request, look up `clinic_id` from DB using `user_id` from JWT. Apply clinic scoping in query filters. Add NOT NULL column with default clinic.

- **Pros**:
  - No JWT change — existing sessions unaffected
  - Simpler auth change
  - Less risk of breaking login
- **Cons**:
  - Extra DB query on every authenticated request
  - No defense in depth (no JWT-level enforcement)
  - Slower — adds a DB round-trip per request
  - `src/api/ai/router.py` code still dead until rewritten
- **Effort**: Medium (est. 200-300 lines changed)

### Approach C: Minimal Fix — Only Backfill + Constraint
**Description**: Don't change auth flow. Just backfill existing `clinic_id=NULL` rows and add NOT NULL constraint. Accept that clinic scoping is "eventually consistent" via DB queries.

- **Pros**:
  - Smallest code change
  - Fastest to deploy
  - No auth changes
- **Cons**:
  - Doesn't fix the root cause — new users still get NULL
  - `src/api/ai/router.py` clinic checks still dead code
  - No tenant isolation guarantee at application level
  - Just kicks the can down the road
- **Effort**: Low (est. 50-100 lines changed)

---

## 5. Recommendation

**Approach A: Full JWT + DB Schema Fix** with a phased rollout:

### Phase 1: DB Schema + User CRUD (non-breaking)
1. Add `clinic_id` and `scope` columns to `usuarios` table via ALTER (idempotent, safe)
2. Add `clinic_id` to `branding_config` table
3. Update `create_user()` to accept and persist `clinic_id`
4. Update `CreateUserRequest` to include `clinic_id`
5. Update `get_user_by_email()` to return `clinic_id` and `scope`
6. Update `list_users()` to return `clinic_id` and `scope`
7. **Backfill existing NULLs** with the pilot clinic UUID (`a3e1f5c2-7b8d-4e9a-8c3f-2d1b0a9e6f4d` for existing users)

### Phase 2: JWT + Tenant Enforcement (requires re-login)
8. Add `clinic_id`, `scope` to `UserPayload`
9. Add `clinic_id`, `scope` to `create_access_token()` JWT claims
10. Update `decode_token()` to populate new fields
11. Update login response (`/auth/me`) to include `clinic_id`, `scope`
12. Update `app/informes/router.py` to enforce clinic scoping
13. Update `app/admin/router.py` to filter informes by clinic
14. Add NOT NULL constraint to `usuarios.clinic_id` and `informes.clinic_id`

### Phase 3: Dead Code Resurrection or Removal
15. Fix `src/api/ai/router.py` clinic scoping code (was dead, now works with new UserPayload)
16. OR remove the old AI router in favor of `app/ai/router.py` (preferred if feature-complete)
17. Add `IMAGIAX_BOOTSTRAP_MODE` to `app/core/config.py` with fail-closed default

### Phase 4: Defense in Depth
18. Add `clinic_id` to `analysis_jobs` UNIQUE constraint (becomes `study_instance_uid + usuario_id + clinic_id`)
19. Add startup validation script that detects any `clinic_id=NULL` rows
20. Add integration test for cross-clinic data isolation

---

## 6. P0 Acceptance Criteria

- [ ] **AC-1**: `UserPayload` carries `clinic_id` and `scope` from JWT on every authenticated request
- [ ] **AC-2**: `POST /admin/api/usuarios` creates users with non-NULL `clinic_id`
- [ ] **AC-3**: `GET /listado` returns only informes for the user's clinic (medico) or filtered by clinic (admin)
- [ ] **AC-4**: `GET /admin/api/informes` supports clinic filter; without filter, admin sees all but clinic is always present in results
- [ ] **AC-5**: No `usuarios` row has `clinic_id=NULL` for non-superadmin users
- [ ] **AC-6**: No `informes` row has `clinic_id=NULL`
- [ ] **AC-7**: `POST /generar/informe` persists `clinic_id` on the new informe and analysis_job
- [ ] **AC-8**: Existing users with `clinic_id=NULL` are backfilled to the pilot clinic
- [ ] **AC-9**: `IMAGIAX_BOOTSTRAP_MODE=0` (or absent) is the production default; setting it to 1 relaxes clinic checks only in development
- [ ] **AC-10**: Startup validation script reports any `clinic_id=NULL` rows as WARNING (not blocking)
- [ ] **AC-11**: `branding_config` has `clinic_id` column; branding queries are clinic-scoped
- [ ] **AC-12**: `analysis_jobs` UNIQUE constraint includes `clinic_id` to prevent cross-clinic idempotency collisions

---

## 7. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| JWT change invalidates all sessions → mass logout | Certain | Deploy during low-traffic window; notify users; phase rollout |
| DB migration fails on production due to schema drift | Medium | Use `IF NOT EXISTS` / idempotent ALTER; test on staging first |
| Backfill assigns wrong clinic to users | Medium | Run validation query before backfill; review each affected user |
| Old `src/api/ai/router.py` clinic code was crashing silently — fixing it reveals bugs | High | Test all AI pipeline paths with `clinic_id` populated |
| `analysis_jobs` UNIQUE constraint change is destructive | Low | Use migration that drops old constraint, adds new one |
| Bootstrap mode removal locks out legitimate users | Medium | Validate all users have `clinic_id` before removing; keep bootstrap as dev-only toggle |
| Branding becomes invisible after clinic scoping if no clinic_id on branding_config | Medium | Backfill branding_config with clinic_id from associated user |

---

## 8. Suggested Specs

Proposed delta specs for `openspec/specs/auth/` and `openspec/specs/security/`:

### auth/tenant-isolation
- **Scenario: JWT carries clinic context**
  - Given a user with clinic_id=X and scope=clinic
  - When they authenticate
  - Then the JWT MUST contain clinic_id=X and scope=clinic claims
  - And UserPayload MUST expose clinic_id and scope

- **Scenario: User creation assigns clinic**
  - Given an admin from clinic X
  - When they create a new user
  - Then the new user's clinic_id MUST be set to X
  - And clinic_id MUST NOT be NULL

### security/tenant-data-isolation
- **Scenario: Cross-clinic informe access denied**
  - Given a medico from clinic A
  - When they request an informe owned by clinic B
  - Then the response MUST be 403 Forbidden (opaque)
  - And no data MUST be leaked

- **Scenario: Admin clinic filtering**
  - Given an admin
  - When they list informes without clinic filter
  - Then clinic_id MUST be present in every result
  - And filtering by clinic_id MUST work

---

## 9. Ready for Proposal

**Yes**, for `mvp-tenant-validation`. The exploration identifies:
- Root cause: `UserPayload` missing `clinic_id`/`scope` — dead tenant code
- Boundary: ~10 files, 300-400 lines estimated
- Minimum no-debt fix: JWT + DB schema + backfill (Phase 1+2 above)
- What can be deferred: Branding clinic scoping (P1), old AI router cleanup (Phase 3), defense in depth (Phase 4)
- Blockers: None — all information gathered from code inspection only

The orchestrator should proceed to `sdd-propose` for `mvp-tenant-validation`.
