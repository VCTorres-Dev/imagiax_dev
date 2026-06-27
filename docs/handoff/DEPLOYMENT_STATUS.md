# Deployment Status

> Leads with the operational truth: production is **live** but is **not** running the source-of-truth commit `c0482551b`. Understand the mismatch and the exact next decision before deploying anything.

## Current status

| Item | State |
|------|------|
| Production host | `imagiax.dev` — **live** |
| Source-of-truth commit | `c0482551ba1dae1d8d74ffa92138f06f617e06ba` (tip of `main`) |
| Production running `c0482551b`? | **No** — production runs an older runtime (see below) |
| Confirmed remediations in production | Tenant scoping, branding backend, security hardening, rate-limit tuning, report/PHI access guards |
| Deploy performed by this handoff? | **No** — docs-only change, no deploy, no release published |

## The source / runtime mismatch (critical)

Production runs a **different application factory** than the one packaged in this commit's Dockerfile.

| Layer | In this commit (`c0482551b`) | In production at imagiax.dev |
|-------|------------------------------|------------------------------|
| Dockerfile `CMD` | `uvicorn app.main:create_app --factory` | `uvicorn api.main:create_app --factory` |
| Runtime source root | `app/` | `src/api/` (uncommitted, lives on the server) |
| Health check | `GET /salud` | `GET /salud` (same path, different process tree) |

> The `src/api/` tree that production actually runs is **not part of the committed repository**. It lives only on the production host. Deploying `c0482551b` as-is would start `app.main:create_app`, which is not the surface production currently depends on. **Do not assume repo == production.**

### Shadow routes / split-brain risk

Evidence shows legacy and modern routes coexisted during the refactor, which can produce incoherent behavior between UI, auth, and persistence. Always verify the authoritative runtime before editing a route.

## Health endpoints

| Endpoint | Auth | Purpose |
|----------|------|---------|
| `GET /salud` | None | Lightweight liveness (Docker HEALTHCHECK + load-balancer probes) |
| `GET /salud/detalle` | Admin | Detailed diagnostics: DB, model, Orthanc, Gemini |

```bash
# From the production host
curl -fsS https://imagiax.dev/api/salud
# Admin diagnostics (requires valid admin session/token)
curl -fsS https://imagiax.dev/api/salud/detalle
```

## Production topology

| Container | Role |
|-----------|------|
| `fastapi-imagiax` | FastAPI backend |
| `postgres-imagiax` | PostgreSQL 16 |
| `orthanc-imagiax` | Orthanc PACS |
| `gotenberg-imagiax` | PDF generation |
| `ohif-imagiax` | OHIF viewer |
| `nginx-imagiax` | Reverse proxy / SSL |

- Production compose: `/root/imagiax/OHIF Medical Imaging Viewer/orthanc-imagiax/docker-compose.prod.yml`
- Production does **not** use bind mounts for backend; backend code is baked into the Docker image. Applying a backend change normally requires copying files to the source tree, rebuilding `fastapi`, and restarting only `fastapi`.

## Exact next deploy-source decision (P0)

Before any deployment, the receiving team **must** resolve:

1. **Choose the authoritative entry point.** Is the future runtime `app.main:create_app` (the refactor in the repo) or `api.main:create_app` (what production runs today)?
   - If `app.main`: confirm the `app/` tree is feature-complete vs production's `src/api/` surface, then update production's compose/Dockerfile to match and rebuild.
   - If `api.main`: commit `src/api/` into the repo so the source of truth matches the runtime, or document it as an operational exception.
2. **Reconcile migrations 006–010** before running `alembic upgrade head` or any schema automation — see [`MIGRATION_GAP.md`](./MIGRATION_GAP.md).
3. **Verify** with logs, DB inspection, and `/salud` after the change. Compare local vs production with `sha256sum` markers — do not assume.

## Rollback references

- Rollback of this handoff change: delete the draft release/assets/tag (if created), close the docs branch/PR, keep `main` at `c0482551b`. This change deploys no code.
- Runtime rollback (out of scope here): production restore scripts and DB backups exist on the host. Always take a backup and have a clear rollback before touching production.

## Blocked by

Automated deployment from `c0482551b` is blocked until:

- [ ] The deploy-source entry-point decision is made and documented.
- [ ] Migrations 006–010 are recovered or formally documented as an operational gap.
- [ ] A fresh build of the chosen runtime is smoke-tested against `/salud` and `/salud/detalle`.

## Next step

Read [`MIGRATION_GAP.md`](./MIGRATION_GAP.md) to understand the database blocker, then [`CONTINUATION_CHECKLIST.md`](./CONTINUATION_CHECKLIST.md) for the full action list.