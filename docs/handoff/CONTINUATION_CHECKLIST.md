# Continuation Checklist

> Immediate next steps for the receiving maintainer team. Each item has a priority, owner-ready description, and acceptance criterion. Work top-down; do not start a lower priority until its P0 dependencies are clear.

## How to use this

- One front at a time. Do not mix deploy, bootstrap, branding, security, and new features in the same batch.
- Golden rule: **data first, code second.** Inspect logs, DB, and health checks before changing code.
- Never rename routes, functions, or variables "for cleanup." Never touch Gemini prompts without explicit approval.

## Access prerequisites

- [ ] SSH access to `root@imagiax.dev` (production host).
- [ ] GitHub access to `VCTorres-Dev/imagiax_dev` (to publish the release).
- [ ] Production DB access (`psql` on `postgres-imagiax`).
- [ ] A populated `.env.production` on the host (never commit it).
- [ ] Access to GitHub Releases for `handoff-2026-06-27`.
- [ ] Ability to create and verify PostgreSQL backups under `/root/imagiax/_deploy_backups/`.

## Required environment matrix

Use `.env.example` as the baseline and never copy real values into git.

| Variable | Environment | Required | Purpose |
|----------|-------------|----------|---------|
| `APP_ENV` | production | Yes | Must be `production` in production. |
| `POSTGRES_URL` | all | Yes | Async database connection for FastAPI. |
| `POSTGRES_PASSWORD` | docker compose | Yes | PostgreSQL container password. |
| `JWT_SECRET` | all | Yes | JWT signing secret, minimum 32 chars. |
| `JWT_EXPIRE_HOURS` / `JWT_REMEMBER_HOURS` | all | Recommended | Token/session lifetime. |
| `IMAGIAX_BOOTSTRAP_MODE` | production | Yes | Must be absent or `0` in production. |
| `IMAGIAX_PILOT_CLINIC_ID` | production | Yes for current backfill path | Must be non-zero until startup backfill is redesigned. |
| `IMAGIAX_PAYMENT_MODE` | production | Yes | Must be `production` when `APP_ENV=production`. |
| `CORS_ORIGINS` | production | Yes | Explicit origins only, no wildcard. |
| `ORTHANC_BASE_URL`, `ORTHANC_USER`, `ORTHANC_PASSWORD` | all | Yes if Orthanc auth enabled | PACS access. |
| `GEMINI_API_KEY`, `GEMINI_MODEL`, `GEMINI_PRO_MODEL` | AI flows | Yes for AI report generation | Gemini LLM integration. |
| `MP_*`, `TBK_*` | payments | Required if payments enabled | MercadoPago and Transbank. |

## P0 — deploy blockers (must resolve before any automated deploy)

### P0.1 Choose the deploy-source runtime
- **Why:** Production runs `api.main:create_app` from an uncommitted `src/api/`; the repo Dockerfile runs `app.main:create_app`. They diverge.
- **Action:** Decide authoritative entry point. Reconcile the repo and production.
- **Acceptance:** The chosen runtime is committed to the repo, the production Dockerfile/compose matches it, and a rebuild passes `/salud` + `/salud/detalle`.

### P0.2 Reconcile migrations 006–010
- **Why:** Production applied schema (analysis_jobs, clinic_scoping, safe_bootstrap, backfill_clinic_data, verify_gate) that is **not** in the repo. Running schema automation blind may corrupt data.
- **Action:** Recover migrations from production schema, or formally document the gap.
- **Acceptance:** Either reviewed SQL migrations 006–010 exist in `migrations/`, or `MIGRATION_GAP.md` is updated with an accepted operational-gap record. See [`MIGRATION_GAP.md`](./MIGRATION_GAP.md).
- **Status:** Pending

### P0.3 Close the clinic pilot bootstrap safely
- **Why:** `IMAGIAX_BOOTSTRAP_MODE=1` relaxes fail-closed tenant scoping. It must not stay on, but must not be removed until `clinic_id` consistency is confirmed.
- **Action:** Validate new users and reports carry `clinic_id`; confirm no cross-tenant leaks; then remove bootstrap mode and re-verify all logins.
- **Acceptance:** Bootstrap mode off; zero `clinic_id=NULL` for active non-superadmin users and new reports over a monitoring window.

## P1 — stability (1–2 weeks)

### P1.1 Stabilize the analysis pipeline
- **Action:** Confirm `/generar/informe` has no residual 500s; monitor `analysis_jobs` for stale `pending` orphans; plan `auditor-json-resilience` if JSON parse failures recur. Validate with a fresh session.
- **Acceptance:** No orphan `pending` jobs accumulating; no unexpected 500s over a monitoring window.

### P1.2 Make branding operable
- **Action:** Branding backend endpoints and `branding_config` table exist, but the admin panel has no branding UI. For the first pilot clinic, configure via API; plan a visual surface for scale.
- **Acceptance:** At least one clinic has branding applied and visible end-to-end.

### P1.3 Package and publish the GitHub Release
- **Action:** Maintain release `handoff-2026-06-27`, attach standalone docs and `CHECKSUMS_SHA256.txt`, and verify the GitHub-generated source archive. Do not build workspace tarballs.
- **Acceptance:** Release assets are present, checksums match, tag points to `7f960427a`, and release notes state `c0482551b` as the application baseline. See [`MANIFEST.md`](./MANIFEST.md).

## PR / issue reference map

| Ref | Purpose | Why it matters |
|-----|---------|----------------|
| Issue #11 | Tenant isolation for MVP readiness | Approved feature scope for tenant validation. |
| PR #14 | Clean MVP branding and tenant integration | Main application baseline. |
| Issue #15 | Production remediation schema alignment | Approved hotfix scope for safe tenant remediation. |
| PR #16 | Tenant remediation schema hotfix | Adds migration 005 and soft-dedup support. |
| Issue #17 | Initial handoff package | Created first handoff docs. |
| PR #18 | Handoff continuation package | Adds `docs/handoff/`. |
| Issue #19 | Handoff release/package corrections | Tracks this release/package hardening pass. |

## P2 — medium term (1–3 months)

- [ ] Onboard 3–5 real pilot clinics.
- [ ] Branding complete (backend + admin UI).
- [ ] Production monitoring and alerting active.
- [ ] Automated, verified backup policy.
- [ ] Secret rotation (Gemini key, JWT secret, admin password).
- [ ] CI/CD for automated deploy (only after P0.1 + P0.2 resolved).

## Done (context, not actions)

These were completed and deployed in production prior to the handoff — verify, do not redo:

- [x] Tenant scoping + branding backend deployed (PRs #14, #16).
- [x] Security hardening: input limits, brute-force protection, security headers, `/salud/db` without exception leak.
- [x] Report/PHI access guards: cross-tenant mutation protection, superadmin scope restriction.
- [x] Remediation schema (soft dedup + deferred uniqueness enforcement).

## Next step

Resolve P0.1, then P0.2 in parallel. Keep the handoff release assets/checksums current as docs evolve.
