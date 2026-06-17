# Design: MVP Tenant Validation

## Technical Approach

Ship a no-debt tenant spine: persist tenant columns first, backfill/validate, then propagate `clinic_id`/`scope` from `usuarios` through JWT, `UserPayload`, dependencies, and every PHI query. Keep the current raw `asyncpg` + startup-migration pattern; do not introduce an ORM or a separate migration framework in this change.

## Architecture Decisions

| Topic | Rejected shortcut | Decision / rationale |
|---|---|---|
| Tenant source | Derive tenant by DB lookup per request | `usuarios` is source; JWT carries `clinic_id`/`scope` so existing `Depends(get_current_user)` paths stay cheap and `src/api/ai/router.py` tenant code stops crashing. |
| Scope model | Role-only scoping | `scope="global"` only bypasses clinic filters; roles still gate actions. This separates tenancy from permissions. |
| Migration | Default everything blindly | Add columns idempotently, backfill NULLs with reviewed rules, validate counts, then add constraints/indexes. Safer for production drift. |
| Admin data | Admin sees untagged global lists | Admin can list all only with `clinic_id` present on every row; `clinic_id` query filter is supported. |
| Bootstrap | Keep implicit bootstrap | Add explicit `IMAGIAX_BOOTSTRAP_MODE`; production `1` is startup error, dev `1` logs warning, default is fail-closed. |
| Debt | Patch only `listado` | Also cover creation, jobs, branding, viewer/detail, and old `src/api/ai/router.py` attribute paths because partial tenant fixes leak PHI. |

## Data Flow

```text
usuarios.clinic_id/scope
  -> get_user_by_email()
  -> create_access_token(clinic_id, scope)
  -> decode_token()/decode_token_with_renewal_check()
  -> UserPayload(clinic_id, scope)
  -> Depends(get_current_user)
  -> router auth guards
  -> informes/analysis_jobs/branding SQL filters
```

Old tokens decode with `clinic_id=None`, `scope=None`; downstream tenant validation treats that as missing tenant context and returns 401/403 rather than silently widening access. Cookie renewal must preserve tenant claims.

## File Changes

| File | Action | Description |
|---|---|---|
| `app/auth/models.py` | Modify | Add nullable `clinic_id`, `scope` to `UserPayload`. |
| `app/auth/service.py` | Modify | Idempotent DDL/backfill helpers; user CRUD returns tenant fields; JWT encode/decode/renewal carries claims. |
| `app/auth/router.py` | Modify | Login and `/auth/me` responses include tenant fields. |
| `app/auth/dependencies.py` | Modify | Preserve tenant claims during sliding renewal; fail closed on missing tenant for clinic users. |
| `app/admin/router.py`, `app/admin/service.py` | Modify | `clinic_id` in user create/update/list; informe filter and tagged responses. |
| `app/informes/db.py` | Modify | Enforce clinic parameters for informes, jobs, branding; migrate `branding_config`; unique job idempotency includes clinic. |
| `app/informes/router.py`, `app/ai/router.py`, `src/api/ai/router.py` | Modify | Pass `current_user.clinic_id/scope`; deny cross-clinic viewer/list/generation. |
| `app/api/branding/router.py` | Modify | Clinic-scoped branding reads/writes, not per-user only. |
| `app/core/config.py`, `app/main.py`, `src/api/main.py` | Modify | Bootstrap flag validation and startup tenant validation warnings. |
| `tests/...` | Create/modify | Unit + TestClient tenant regression coverage. |

## Interfaces / Contracts

JWT claims: `clinic_id: string | null`, `scope: "clinic" | "global" | null`. Non-superadmin persisted entities MUST have non-NULL `clinic_id`. Superadmin may have `clinic_id=NULL` only with `scope="global"`.

Status codes: missing/invalid auth `401`; valid session without required tenant context `401` if re-login can fix it; cross-clinic/role denial opaque `403`; NULL tenant creation request `400`; duplicate user `409`; internal DB/migration failures `500` except startup validation warnings.

## Migration / Rollout

1. Add columns/indexes with `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` for `usuarios`, `informes`, `analysis_jobs`, `branding_config`; add `scope` default for existing users.
2. Backfill: non-superadmin `usuarios` to reviewed pilot clinic; `informes` from owner user clinic, fallback pilot; `analysis_jobs` from user/report where possible; `branding_config` from owner user.
3. Validate read-only counts for NULL tenant rows before constraints. Add CHECK/NOT NULL-equivalent constraints only after zero unsafe NULLs.
4. Deploy code during low traffic; existing JWTs lacking tenant claims are expected to require re-login. Rollback code only; leave additive columns/backfill in place.

## Testing Strategy

| Layer | What to Test | Approach |
|---|---|---|
| Unit | JWT defaults, CRUD SQL params, migration idempotency, job unique lookup includes clinic | pytest/unittest with mocks/fake records. |
| Integration | `/auth/me`, `/informes/listado`, `/admin/api/informes`, branding routes, cross-clinic denial | FastAPI TestClient with dependency overrides where DB can be mocked. |
| Production read-only | NULL counts, constraints, indexes, sample cross-clinic query plans | Checklist of SELECT-only validation before deploy/rollback. |

## Review-size Forecast / Work Units

Forecast 450-700 changed lines, under 800 but medium risk. Recommended split: (1) schema/backfill helpers + tests, (2) auth/JWT/UserPayload + tests, (3) query scoping for informes/jobs/branding/admin + tests, (4) bootstrap validation + production checklist.

## Open Questions

- [ ] Confirm pilot clinic UUID and reviewed production backfill mapping before implementation.
