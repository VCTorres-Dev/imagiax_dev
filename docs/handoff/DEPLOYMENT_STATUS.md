# Deployment Status

> Leads with the operational truth: production is **live** but is **not** running the source-of-truth commit `c0482551b`. Understand the mismatch and the exact next decision before deploying anything.

## Current status

| Item | State |
|------|------|
| Production host | `imagiax.dev` — **live** |
| Source-of-truth commit | `c0482551ba1dae1d8d74ffa92138f06f617e06ba` (tip of `main`) |
| Production running `c0482551b`? | **No** — production runs an older runtime (see below) |
| Confirmed remediations in production | Tenant scoping, branding backend, security hardening, rate-limit tuning, report/PHI access guards |
| Deploy performed by this handoff? | **No** — docs-only release; no production deploy was performed |
| Handoff Release | `handoff-2026-06-27` at `7f960427a4262d78136ea7d4e165d963d4a81b35` |

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

- Production root: `/root/imagiax`
- Production compose observed during remediation: `/root/imagiax/OHIF Medical Imaging Viewer/orthanc-imagiax/docker-compose.prod.PRODUCTION.yml`
- Repository compose file: `OHIF Medical Imaging Viewer/orthanc-imagiax/docker-compose.prod.yml`
- Production does **not** use bind mounts for backend; backend code is baked into the Docker image. Applying a backend change normally requires copying files to the source tree, rebuilding `fastapi`, and restarting only `fastapi`.

## Safe source comparison procedure

Before any backend deploy, capture production without printing secrets:

```bash
# On production host; do not cat .env files.
docker inspect fastapi-imagiax --format '{{.Image}} {{.Config.Cmd}}'
docker ps --filter name=fastapi-imagiax
find /root/imagiax -maxdepth 4 -type d -name api -o -name app
```

Then compare route surfaces and entry points from a safe copy of production source, not in-place editing:

1. Copy only source code needed for comparison to a temporary review directory. Exclude `.env*`, uploads, DICOM, dumps, backups, and caches.
2. Inventory routes from both candidates (`api.main` and `app.main`) using local import/safe static grep.
3. Decide one authoritative runtime and document the decision before rebuilding.
4. Rebuild only `fastapi` after backup and explicit approval.

Do not deploy by copying an entire workstation folder to production.

## Runtime rollback runbook

Before a backend deploy, capture:

| Item | Command / Location |
|------|--------------------|
| Current FastAPI image | `docker inspect fastapi-imagiax --format '{{.Image}}'` |
| Current container status | `docker ps --filter name=fastapi-imagiax` |
| DB backup | `/root/imagiax/_deploy_backups/db-pre-tenant-remediation-20260617T222541Z.dump` already exists; take a fresh backup before new deploys |
| Runtime config backup | `/root/imagiax/_deploy_backups/.env.production.20260617T235619Z.runtime-gates.bak` already exists; take a fresh backup before edits |

Rollback rule: if `/api/salud` or `/api/health/db` fails after deploy, stop feature work, restore the previous FastAPI image/config, and re-run smoke checks before investigating new changes.

## Exact next deploy-source decision (P0)

Before any deployment, the receiving team **must** resolve:

1. **Choose the authoritative entry point.** Is the future runtime `app.main:create_app` (the refactor in the repo) or `api.main:create_app` (what production runs today)?
   - If `app.main`: confirm the `app/` tree is feature-complete vs production's `src/api/` surface, then update production's compose/Dockerfile to match and rebuild.
   - If `api.main`: commit `src/api/` into the repo so the source of truth matches the runtime, or document it as an operational exception.
2. **Reconcile migrations 006–010** before running `alembic upgrade head` or any schema automation — see [`MIGRATION_GAP.md`](./MIGRATION_GAP.md).
3. **Verify** with logs, DB inspection, and `/salud` after the change. Compare local vs production with `sha256sum` markers — do not assume.

## Release rollback references

- Handoff release rollback: replace incorrect assets or delete the release assets; this does not affect production.
- Runtime rollback: use the runbook above. Always take a fresh backup before touching production.

## Blocked by

Automated deployment from `c0482551b` is blocked until:

- [ ] The deploy-source entry-point decision is made and documented.
- [ ] Migrations 006–010 are recovered or formally documented as an operational gap.
- [ ] A fresh build of the chosen runtime is smoke-tested against `/salud` and `/salud/detalle`.

## Next step

Read [`MIGRATION_GAP.md`](./MIGRATION_GAP.md) to understand the database blocker, then [`CONTINUATION_CHECKLIST.md`](./CONTINUATION_CHECKLIST.md) for the full action list.
