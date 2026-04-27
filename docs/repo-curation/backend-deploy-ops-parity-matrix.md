# Backend Deploy/Ops Wiring Parity Matrix (Monolith vs Factory)

## Scope
Evidence artifact for deploy/ops normalization **under HOLD**.

- No deploy/runtime rewiring to factory authority is executed here.
- This phase only makes active surfaces explicit about runtime authority.

## Baseline runtime authority (explicit)

Current authoritative runtime remains `servidor_api_orthanc_v1:app`.
`src/api/main.py` remains shadow/non-authoritative in active deploy/start surfaces.

## Active surface inventory (normalized)

| Surface | Current wiring | What it implies |
|---|---|---|
| `Dockerfile` | `CMD ["uvicorn", "servidor_api_orthanc_v1:app", ...]` + HOLD annotation | Container runtime authoritative entrypoint is monolith |
| `Dockerfile` | `HEALTHCHECK ... /ping` | Runtime must expose `GET /ping` |
| `start_api.bat` | `python servidor_api_orthanc_v1.py` + HOLD annotation | Local operator start path pinned to monolith |
| `arrancar_api.py` | `python -m uvicorn servidor_api_orthanc_v1:app` + HOLD annotation | Local operator start path pinned to monolith |
| `docker-compose.prod.yml` | `fastapi` built from root `Dockerfile` + HOLD annotation | Production compose inherits monolith command |
| `docker-compose.prod.PRODUCTION.yml` | `fastapi` built from root `Dockerfile` + HOLD annotation | Production-alt compose inherits monolith command |
| `deploy_production.sh` | Hot-copy + remote restart flow (`docker compose ... restart fastapi`) and post-check against `/salud` | Operational deploy path still orchestrates monolith-wired container runtime; does not establish factory entrypoint authority |
| `start-imagiax.ps1` | starts `py -m uvicorn servidor_api_orthanc_v1:app` + HOLD annotation | Local all-in-one start path pinned to monolith |
| `nginx-proxy.conf` | `/api/ -> fastapi:8010`, `/api/auth/verify -> /auth/verify`, `/robots.txt`, `/sitemap.xml` proxied to backend | Proxy contract expects those backend endpoints and semantics |

## Deploy/Ops parity matrix

| Dimension | Monolith alignment | Factory alignment today | Parity |
|---|---|---|---|
| Container entrypoint authority clarity | Explicitly monolith + HOLD-labeled | Not wired by design in this phase | ✅ (under HOLD) |
| Docker liveness (`/ping`) | Satisfied on authoritative runtime | Available in factory but non-authoritative | ✅ (under HOLD) |
| Nginx SEO passthrough (`/robots.txt`, `/sitemap.xml`) | Satisfied on authoritative runtime | Available in factory but non-authoritative | ✅ (under HOLD) |
| Nginx auth subrequest semantics (`/auth/verify`) | `204/401` aligned with auth_request safety | Shadow runtime aligned; still non-authoritative | ✅ (under HOLD) |
| Local startup scripts authority clarity | Explicitly monolith + HOLD-labeled | Not factory-based by design | ✅ (under HOLD) |
| Compose runtime consistency | Explicitly inherits monolith Dockerfile CMD | No factory-authoritative wiring (expected in HOLD) | ✅ (under HOLD) |
| Production deploy wiring (`deploy_production.sh`) | Explicitly keeps monolith authority and current restart flow | No factory-authoritative cutover proof | ⚠️ (cutover still blocked) |

## Additional ops inconsistency evidence (must be normalized before cutover)

### Host-copy vs image-runtime mismatch
`deploy.sh` copies selected files (`imagiax_auth.py`, `imagiax_admin.py`, `imagiax_db.py`) to remote `.../orthanc-imagiax/app/`, but runtime containers are built from root context using `Dockerfile` that copies monolith files into `/app` and starts `servidor_api_orthanc_v1:app`.

This mixed deploy model is a blocker because runtime authority cannot be switched safely while deployment assumptions are split between remote file copy and image build behavior.

## Checklist mapping (Phase 4 targeted verification)

| Checklist ID | Targeted result | Evidence source |
|---|---|---|
| O1 | PASS (authority explicit on Docker runtime entrypoint) | `Dockerfile` HOLD annotation + monolith CMD |
| O2 | PASS (compose wiring explicit and authority-preserving) | `docker-compose.prod.yml`, `docker-compose.prod.PRODUCTION.yml` HOLD annotations + root Dockerfile build |
| O3 | PASS (operator scripts explicitly monolith-authoritative) | `start_api.bat`, `arrancar_api.py`, `start-imagiax.ps1` HOLD annotations + monolith launch commands |
| O4 | PASS (nginx assumptions consistent with active runtime contract) | `nginx-proxy.conf` routes + monolith/factory parity evidence from Phases 1-2 |
| O5 | AMBER (single-authority wording established; legacy mismatch remains) | `deploy.sh` host-copy model is treated as non-authoritative/deprecated for runtime-authority decisions; active authority remains compose+Dockerfile+operator start surfaces |

## Cutover decision for this phase
Deploy/ops cutover readiness is **NOT proven** and remains blocked.

## Gate impact
`docs/repo-curation/runtime-cutover-parity-gate.md` remains **RED / HOLD**.
