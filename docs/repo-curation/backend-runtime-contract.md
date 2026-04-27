# Backend Runtime Contract (Authoritative Now vs Shadow Target)

## Scope (this phase)
This document materializes the **current backend runtime contract** for `repo-curation-github-sync`.

- Documentation/decision materialization only.
- No runtime switch.
- No deploy rewiring.
- Backend code mutation allowed only for approved Phase-1 shadow contract parity (`src/api/main.py`), without authority switch.

## Authoritative backend runtime (current truth)
The authoritative runtime entrypoint is:

- `servidor_api_orthanc_v1:app`

Evidence in repository:

- `Dockerfile` runs `uvicorn servidor_api_orthanc_v1:app` (CMD).
- `start_api.bat` starts `python servidor_api_orthanc_v1.py`.
- `arrancar_api.py` starts `python -m uvicorn servidor_api_orthanc_v1:app`.
- `OHIF Medical Imaging Viewer/orthanc-imagiax/docker-compose.prod.yml` builds backend from root `Dockerfile`.
- `OHIF Medical Imaging Viewer/orthanc-imagiax/docker-compose.prod.PRODUCTION.yml` builds backend from root `Dockerfile`.

## Runtime contract surfaces relied on by deploy/proxy/ops
Current monolith contract surface (`servidor_api_orthanc_v1.py`) includes:

- `GET /ping` (used by Docker healthcheck in `Dockerfile`).
- `GET /health/db` (explicit DB readiness diagnostic).
- `GET /robots.txt` and `GET /sitemap.xml`.
- `GET /auth/verify` (used by nginx `auth_request`).
- `GET /salud` (authenticated comprehensive health endpoint).

Phase-1 parity closure (`contract-parity-shadow`) updated the factory shadow runtime (`src/api/main.py`) with:

- `GET /ping` (returns `200`, `{ "status": "ok" }`)
- `GET /health/db` (healthy path returns `200`, `{ "ok": true, "database": "up" }`)
- `GET /robots.txt` (returns plain text policy + sitemap reference)
- `GET /sitemap.xml` (returns XML sitemap)

Evidence:

- `src/api/main.py`
- `tests/api/test_runtime_contract_parity.py`
- `docs/repo-curation/backend-route-semantics-parity-matrix.md`

Proxy/ops dependency evidence:

- `OHIF Medical Imaging Viewer/orthanc-imagiax/config/nginx-proxy.conf`
  - `location /api/` -> `fastapi:8010`
  - `location = /api/auth/verify` -> `fastapi:8010/auth/verify` (internal)
  - `location = /robots.txt` -> `fastapi:8010/robots.txt`
  - `location = /sitemap.xml` -> `fastapi:8010/sitemap.xml`
- `Dockerfile` `HEALTHCHECK` expects `GET /ping` to return 200.

## Shadow modular runtime target (NOT authoritative)
`src/api/main.py` is currently a **shadow target only**.

It is not wired as production entrypoint in Docker/compose/start scripts and therefore cannot be treated as runtime-authoritative yet.

### `/salud` semantic delta (explicitly retained in this slice)

- Monolith authority: `/salud` is auth-protected and returns comprehensive diagnostics.
- Factory shadow: `/salud` remains lightweight public `{ "status": "ok" }`, with details split under `/salud/detalle` (admin-only).

In this slice we **do not** force a risky `/salud` behavior collapse just to claim parity. Delta is intentional and remains gate-relevant until explicitly resolved in a future approved slice.

## Why backend publication scope remains blocked (`app/**` vs `src/**`)
There is unresolved backend topology ambiguity:

- `src/api/main.py` imports core modules from `app.*` (`from app.core.config`, `from app.core.database`, `from app.core.middleware`).
- Runtime/deploy surfaces are still pinned to monolith `servidor_api_orthanc_v1.py`.

This means the repo is in a mixed migration state (`app/**` + `src/**`), so backend publication candidate/main work remains blocked until parity gate is satisfied.

## Hard hold (backend)
Until parity gate is green:

- Do **NOT** switch runtime entrypoint away from `servidor_api_orthanc_v1:app`.
- Do **NOT** treat `src/api/main.py` as production-ready.
- Do **NOT** include backend runtime cutover in publication-candidate or backend `main` integration.

See: `docs/repo-curation/runtime-cutover-parity-gate.md`.
