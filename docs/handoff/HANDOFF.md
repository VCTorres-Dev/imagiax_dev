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
| Application baseline commit | `c0482551ba1dae1d8d74ffa92138f06f617e06ba` |
| Application baseline subject | `Merge pull request #16 from VCTorres-Dev/fix/tenant-remediation-schema` |
| Handoff docs commit | `7f960427a4262d78136ea7d4e165d963d4a81b35` (current tip of `main`) |
| Handoff docs subject | `Merge pull request #18 from VCTorres-Dev/docs/handoff-export` |
| Release | [`handoff-2026-06-27`](https://github.com/VCTorres-Dev/imagiax_dev/releases/tag/handoff-2026-06-27) — published |

> The GitHub Release `handoff-2026-06-27` is **published** and points to the handoff docs commit `7f960427a`. Treat `c0482551b` as the application-code source of truth and `7f960427a` as a documentation-only addition layered on top of it. No runtime code is changed by either commit. See [`MANIFEST.md`](./MANIFEST.md) for the release's asset list and checksum procedure.

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

The local compose file in this repository starts **infrastructure only** (`orthanc` and `postgres`). It does not start the full API and OHIF application by itself. Treat local startup as three separate layers until the deploy-source decision is resolved.

### 1. Clone the handoff source

```bash
git clone https://github.com/VCTorres-Dev/imagiax_dev.git
cd imagiax_dev
git checkout handoff-2026-06-27
```

### 2. Start local infrastructure

```bash
cd "OHIF Medical Imaging Viewer/orthanc-imagiax"
docker compose up -d
# Orthanc:  http://localhost:8042
# Postgres: localhost:5432
```

### 3. Start backend and OHIF explicitly

```bash
# Backend, from repo root, after setting a local .env based on .env.example:
uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8010

# OHIF: verify the current OHIF workspace before running.
# The production viewer/runtime still needs source reconciliation.
```

If this fails, do not patch blindly. First resolve the P0 source/runtime mismatch in [`DEPLOYMENT_STATUS.md`](./DEPLOYMENT_STATUS.md).

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
4. Maintain the published GitHub Release `handoff-2026-06-27` and its standalone assets/checksums.

Full actionable list: [`CONTINUATION_CHECKLIST.md`](./CONTINUATION_CHECKLIST.md).

## Next step

Read [`DEPLOYMENT_STATUS.md`](./DEPLOYMENT_STATUS.md) to understand exactly why you cannot deploy this commit as-is.
