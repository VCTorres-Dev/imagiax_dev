# Proposal: MVP Tenant Validation

## Intent

Fix the first P0 MVP blocker: tenant identity is unreliable. JWT/UserPayload omit `clinic_id`/`scope`, users can lack clinics, PHI queries are inconsistently scoped, and old production-facing AI code references tenant fields that would crash.

## Scope

### In Scope
- No-debt tenant schema/backfill for users, informes, jobs, and clinic metadata.
- JWT, `UserPayload`, login response, CRUD, and query scoping updates.
- Bootstrap shutdown plan: dev-only toggle, fail-closed defaults, NULL-tenant validation.

### Out of Scope
- Broad RBAC rewrite, frontend redesign, unrelated AI pipeline refactor, payment refactor.
- Dual-entrypoint cleanup beyond required tenant fixes.

## Capabilities

### New Capabilities
- None.

### Modified Capabilities
- `auth`: sessions and user creation MUST carry clinic context.
- `security`: PHI access MUST enforce tenant isolation.
- `infrastructure`: report/job listing and bootstrap startup MUST be tenant-safe.

## Approach

Use the recommended no-debt path: schema/backfill first, then JWT/UserPayload propagation, query scoping, and bootstrap shutdown. Existing sessions may require re-login.

## Delivery Plan

| Phase | Goal | Safety |
|---|---|---|
| 1 | Add schema + backfill NULL tenants | Validate rows before constraints |
| 2 | Add JWT/UserPayload tenant fields | Force re-login; fail closed |
| 3 | Scope user/admin/informe/job queries | Test cross-clinic denial |
| 4 | Gate bootstrap to dev | Warn before hard startup block |

## Affected Areas

| Area | Impact | Description |
|---|---|---|
| `app/auth/*` | Modified | Models, token creation/decoding, user CRUD |
| `app/admin/router.py` | Modified | Clinic-aware admin flows |
| `app/informes/*` | Modified | Clinic-scoped informes, jobs, branding metadata |
| `src/api/ai/router.py` | Modified | Stop dead tenant attribute paths |
| `app/core/config.py`, `app/main.py` | Modified | Bootstrap shutdown |

## Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| PHI leak from partial fix | High | Ship schema + JWT + scoping together |
| Wrong clinic backfill | Medium | Review affected rows first |
| JWT change logs users out | High | Low-traffic deploy; expected re-login |

## Rollback Plan

Revert code and restore previous JWT decoding. Leave additive columns/backfill in place during emergency rollback; clean up only after stability is confirmed.

## Review Forecast

Estimated 300–500 changed lines, under the 800-line budget. Chained PR likely not needed; split if forecast grows.

## Dependencies

- Staging/prod schema inventory and reviewed pilot clinic backfill mapping.

## Success Criteria

- [ ] JWT/UserPayload always carry `clinic_id` and `scope`.
- [ ] Non-superadmin users and PHI rows have non-NULL `clinic_id`.
- [ ] Report listing, generation, admin views, and analysis jobs are clinic-scoped.
- [ ] Bootstrap mode is dev-only/fail-closed with operational validation.
