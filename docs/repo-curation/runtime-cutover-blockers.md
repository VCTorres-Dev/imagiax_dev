# Runtime Cutover Blockers (Monolith vs Factory)

## Scope
This artifact documents blockers only. It does **not** execute cutover.

## Current status summary
- **Authoritative runtime now**: `servidor_api_orthanc_v1:app`.
- **Shadow target**: `src/api/main.py` (not wired as entrypoint).
- **Decision**: hold backend cutover and backend `main` integration until parity gate exists and is satisfied.

## Active ops surface normalization status (Phase 4)

Authority-preserving HOLD annotations are now present in active operational surfaces to remove ambiguity without cutover:

- `Dockerfile`
- `OHIF Medical Imaging Viewer/orthanc-imagiax/docker-compose.prod.yml`
- `OHIF Medical Imaging Viewer/orthanc-imagiax/docker-compose.prod.PRODUCTION.yml`
- `start_api.bat`
- `arrancar_api.py`
- `OHIF Medical Imaging Viewer/orthanc-imagiax/start-imagiax.ps1`
- `deploy_production.sh`

These annotations are documentation-only clarifications; runtime behavior and authority remain unchanged.

## Blocker matrix

> Route inventory note: the route lists below are a **focused blocker inventory** for monolith-vs-factory cutover risk analysis. They are intentionally non-exhaustive and only include endpoints/material differences that currently gate parity.

### 1) Utility route parity gap
Monolith (`servidor_api_orthanc_v1.py`) exposes:

- `/ping`
- `/health/db`
- `/robots.txt`
- `/sitemap.xml`

Factory (`src/api/main.py`) currently exposes:

- `/ping`
- `/health/db`
- `/robots.txt`
- `/sitemap.xml`
- `/salud`
- `/salud/detalle`
- `/config/frontend`

Utility/SEO routes were added for parity in prior phases, but factory remains shadow/non-authoritative in active ops surfaces.

Impact:
- Docker `HEALTHCHECK` in `Dockerfile` depends on `/ping`.
- Nginx routes `robots.txt` and `sitemap.xml` to backend.

### 2) Auth verify semantic drift risk
Both stacks provide `/auth/verify`, but semantics differ:

- Monolith verify (`servidor_api_orthanc_v1.py`) is cookie-signature check only and returns `204/401`.
- Factory verify (`src/api/auth/router.py`) uses two-phase logic (signature + DB revocation) and now aligns to `204/401` with fail-closed deny on revocation-check turbulence.

Impact:
- Nginx `auth_request` parity is improved, but production authority remains monolith and full cutover evidence is still incomplete.

### 3) Startup resilience parity gap
Monolith has explicit DB startup resilience:

- Retry loop for pool init (`_init_postgres_pool_with_retries`).
- Background reconnect task (`_postgres_background_reconnect`) when DB not ready at boot.

Factory startup now includes retry/backoff pool creation and background reconnect/deferred bootstrap flow in `src/api/main.py` + `app/core/database.py`.

Impact:
- Residual risk is reduced at code/test level, but remains AMBER until full live-runbook evidence is completed.

### 4) Ops-surface wiring mismatch
Operational wiring is still monolith-oriented:

- `Dockerfile` CMD points to `servidor_api_orthanc_v1:app` (now explicitly HOLD-annotated).
- Compose stacks build from same Dockerfile (`docker-compose.prod.yml`, `docker-compose.prod.PRODUCTION.yml`) and are HOLD-annotated.
- Local start scripts (`start_api.bat`, `arrancar_api.py`, `start-imagiax.ps1`) target monolith and are HOLD-annotated.

Impact:
- `src/api/main.py` cannot become authoritative by documentation intent alone; wiring and behavior parity are still missing.
- Ambiguity risk on active surfaces is reduced, but authority switch is still blocked.

### 5) Host-copy vs container-runtime mismatch
`deploy.sh` still copies selected python files into remote `OHIF Medical Imaging Viewer/orthanc-imagiax/app/`, while runtime container image is built from root `Dockerfile` and launches monolith entrypoint from copied root files in image context.

Impact:
- Deployment path assumptions are mixed and must be normalized before safe runtime-authority change.

### 6) `app/**` and `src/**` migration boundary unresolved
`src/api/main.py` still depends on `app.*` modules, while monolith remains active runtime.

Impact:
- Backend topology is mixed and non-singular; publication candidate/backend `main` work must remain blocked.

## Explicit HOLD
Until parity gate is green:

- No backend runtime cutover.
- No deploy rewiring to factory runtime.
- No backend publication-candidate integration that assumes `src/api/main.py` is ready.
- No backend `main` integration for cutover scope.

Reference gate: `docs/repo-curation/runtime-cutover-parity-gate.md`.
