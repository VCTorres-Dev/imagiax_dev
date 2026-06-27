# IMAGIAX — Project Handoff

> Executive handoff for a new maintainer team. This document orients you to the architecture, the canonical source of truth, what is included and excluded, and the continuation path. Read this first.

## Quick path

1. **Clone the source of truth:** `git clone https://github.com/VCTorres-Dev/imagiax_dev.git && git checkout c0482551ba1dae1d8d74ffa92138f06f617e06ba`
2. **Read the deployment truth:** [`DEPLOYMENT_STATUS.md`](./DEPLOYMENT_STATUS.md) — production is **not** running this commit; understand the mismatch before touching anything.
3. **Pick up the work:** [`CONTINUATION_CHECKLIST.md`](./CONTINUATION_CHECKLIST.md) — P0/P1/P2 next steps.
4. **Mind the database gap:** [`MIGRATION_GAP.md`](./MIGRATION_GAP.md) — migrations 006–010 are missing from the repo and **must** be reconciled before any automated deploy.

## Source of truth

| Field | Value |
|-------|-------|
| Repository | `https://github.com/VCTorres-Dev/imagiax_dev.git` |
| Canonical commit | `c0482551ba1dae1d8d74ffa92138f06f617e06ba` (tip of `main`) |
| Commit subject | `Merge pull request #16 from VCTorres-Dev/fix/tenant-remediation-schema` |
| Distribution | GitHub Release (not yet published — see [`MANIFEST.md`](./MANIFEST.md)) |

> The handoff docs you are reading are committed **after** `c0482551b`. Treat `c0482551b` as the application code source of truth and the handoff commit as a documentation-only addition layered on top. No runtime code is changed by the handoff commit.

## What is included

- FastAPI backend (`app/` package — the refactored application factory).
- OHIF viewer extension (`ohif-extension-imagiax-ai/`) and Orthanc stack config.
- Migrations 001–005 committed to the repo.
- Pydantic v2 request/response models, JWT auth, AI pipeline (TorchXRayVision + Gemini).
- Test suites under `tests/` (unit + API).
- OpenSpec SDD change history under `openspec/changes/`.

## What is explicitly excluded

The handoff package and any release built from it **MUST NOT** contain:

| Excluded | Rationale |
|----------|-----------|
| `.env`, `.env.production`, `.env.local` | Secrets and credentials |
| `service_account.json`, any `*.pem`/`*.key` | Cloud / payment service keys |
| DICOM files (`*.dcm`), `datos_*`, `RX Dicom Veridicos/` | PHI / patient data |
| Database dumps, backups | PHI + size |
| `__pycache__/`, `.pytest_cache/`, `node_modules/`, `dist/` | Build caches |
| `models/` weights (`*.pth`), untracked `src/` | Large binaries / uncommitted runtime WIP |

See [`MANIFEST.md`](./MANIFEST.md) for the full exclusion manifest and checksum procedure.

## Architecture

```
imagiax.dev (HTTPS) ── nginx (80/443) ──┬── /api/  FastAPI  :8010
                                        ├── /pacs/ Orthanc   :8042
                                        └── /      OHIF      :8080
                                                    │
                                              PostgreSQL 16 (internal Docker net)
```

| Service | Tech | Role |
|---------|------|------|
| fastapi | Python 3.11 + FastAPI + PyTorch | API, AI analysis, payments, reports |
| orthanc | Orthanc + plugins | PACS DICOM storage/retrieval |
| ohif | OHIF v3 + `imagiax-ai` extension | Web DICOM viewer with AI panel |
| postgres | PostgreSQL 16 + asyncpg | Reports, payments, users, clinic/branding config |
| gotenberg | Gotenberg | Clinical PDF generation |
| nginx | nginx:alpine | Reverse proxy + Let's Encrypt SSL |

## Important: two entry points coexist

The codebase contains **two application entry points**, and this is the single most important operational fact:

| Entry point | Location | Where it runs |
|------------|----------|---------------|
| `app.main:create_app` (factory) | `app/main.py` | Dockerfile in this commit; **not confirmed running in production** |
| `api.main:create_app --factory` | `src/api/main.py` (uncommitted) | **Production runtime at imagiax.dev** |

Deploying `c0482551b` directly would run `app.main:create_app`, which does **not** match the production runtime (`api.main:create_app`). Reconciling this is a P0 decision — see [`DEPLOYMENT_STATUS.md`](./DEPLOYMENT_STATUS.md).

## Getting started locally

```bash
git clone https://github.com/VCTorres-Dev/imagiax_dev.git
cd imagiax_dev
git checkout c0482551ba1dae1d8d74ffa92138f06f617e06ba

# Local stack (HTTP, no SSL)
cd "OHIF Medical Imaging Viewer/orthanc-imagiax"
docker compose up -d --build
# API:    http://localhost:8010
# OHIF:   http://localhost:3000
# Orthanc: http://localhost:8042
# Swagger: http://localhost:8010/docs
```

### Test entry points

| Layer | Command | Note |
|-------|---------|------|
| Backend syntax | `python -m py_compile <file>` | No pytest in local env |
| Backend tests | `pytest tests/` | Requires Docker/FastAPI live for integration |
| Frontend unit | `cd "OHIF Medical Imaging Viewer/Viewers" && yarn test:unit` | Jest multi-project |
| Frontend lint/types | `yarn lint && yarn tsc --noEmit` | ESLint + TS |
| E2E | `npx playwright test` | Playwright (baseURL localhost:3335) |

## Continuation path

1. Reconcile the deploy-source decision (`app.main` vs `api.main`).
2. Recover or document migrations 006–010.
3. Close the clinic pilot bootstrap (`IMAGIAX_BOOTSTRAP_MODE`).
4. Package and publish a GitHub Release from `c0482551b`.

Full actionable list: [`CONTINUATION_CHECKLIST.md`](./CONTINUATION_CHECKLIST.md).

## Next step

Read [`DEPLOYMENT_STATUS.md`](./DEPLOYMENT_STATUS.md) to understand exactly why you cannot deploy this commit as-is.