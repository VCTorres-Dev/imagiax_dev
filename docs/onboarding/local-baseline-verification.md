# Local Baseline Verification (Windows) — `local-share-onboarding-package`

**Date (local):** 2026-04-26 12:28:08 -04:00  
**Project:** `imagiax_dev`  
**Change:** `local-share-onboarding-package`  
**Phase:** 1 — Manual baseline verification + evidence capture

---

## 1) Scope and safety boundaries used in this run

- Internal local dev/demo onboarding only.
- No deploy/hosting/prod files modified.
- No AI engine/prompts/pathology/model/inference files modified.
- Goal of this run: verify **what actually works now** on this Windows machine, and log failures honestly.

---

## 2) Inputs inspected before execution

1. `README.md`
2. `INICIAR-IMAGIAX.bat`
3. `OHIF Medical Imaging Viewer/orthanc-imagiax/start-imagiax.ps1`
4. `start_api.bat`
5. `arrancar_api.py`
6. `OHIF Medical Imaging Viewer/orthanc-imagiax/upload-dicoms.ps1`
7. `OHIF Medical Imaging Viewer/orthanc-imagiax/docker-compose.yml`
8. `Dockerfile`

---

## 3) Verified prerequisites (this machine)

| Check | Command | Observed output | Result |
|---|---|---|---|
| Timestamp | `Get-Date -Format "yyyy-MM-dd HH:mm:ss zzz"` | `2026-04-26 12:28:08 -04:00` | ✅ |
| Python | `py --version` | `Python 3.11.9` | ✅ |
| Yarn | `yarn --version` | `1.22.22` | ✅ |
| Docker CLI | `docker version` | Client OK, server initially unavailable (`failed to connect ... dockerDesktopLinuxEngine`) | ⚠️ Docker daemon initially down |

---

## 4) Manual baseline execution log (golden-path attempt)

### EV-001 — Top-level launcher from README-style entry point

**Command:**

```powershell
& "C:\Users\WinterOS\Documents\Workspace\IMAGIAX - copia (2)\INICIAR-IMAGIAX.bat"
```

**Observed output:**

```text
========================================
  IMAGIAX - Inicio rapido
========================================

No se esperaba \OHIF en este momento.
```

**Outcome:** ❌ Failed immediately.

**Evidence-backed blocker:** batch parsing breaks in current path context (contains parentheses in repo path: `IMAGIAX - copia (2)`), so golden path does not start from `INICIAR-IMAGIAX.bat`.

---

### EV-002 — Direct PowerShell launcher execution

**Command:**

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "C:\Users\WinterOS\Documents\Workspace\IMAGIAX - copia (2)\OHIF Medical Imaging Viewer\orthanc-imagiax\start-imagiax.ps1"
```

**Observed key output:**

```text
[1] Verificando Docker Engine
Docker Engine no responde. Intentando iniciar Docker Desktop...
... Docker Engine operativo

[2] Verificando Orthanc PACS
Orthanc no responde. Levantando contenedor...
...
Error response from daemon: ports are not available: exposing port TCP 0.0.0.0:4242 ... bind ... acceso a un socket no permitido ...
ERROR: Orthanc no respondio en 30 segundos.
```

**Outcome:** ❌ Failed in Step 2 (Orthanc startup).

**Evidence-backed blocker:** host port `4242` publish fails, so Orthanc container does not become reachable.

---

### EV-003 — Port-level checkpoints after failures

**Commands used:**

```powershell
$ports = 3000,8010,8042,5432
foreach ($p in $ports) { Test-NetConnection -ComputerName localhost -Port $p }

Invoke-WebRequest http://localhost:8010/salud
Invoke-WebRequest http://localhost:8042/system
```

**Observed:**

- `3000`: `False`
- `8010`: `False`
- `8042`: `False`
- `5432`: `False` (before partial compose recovery)
- `/salud`: connection refused
- `/system`: connection refused

**Outcome:** ❌ No coworker-facing service reachable at this point.

---

### EV-004 — Compose/container state after start attempt

**Commands:**

```powershell
docker compose ps
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
docker ps -a --filter "name=orthanc-imagiax" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
```

**Observed:**

- `orthanc-imagiax`: `Created` (not running)
- `postgres-imagiax`: `Up (healthy)` and host `5432` mapped
- `fastapi-imagiax`: `Up (healthy)` internal `8010/tcp` only
- `nginx-imagiax`: restart loop; later no stable host 80/443 binding
- Orphan warning shown for `fastapi/nginx/gotenberg/ohif` (existing containers from another compose stack)

**Outcome:** ⚠️ Mixed state (partial stack), not a clean local golden path.

---

### EV-005 — Why nginx is unreachable

**Command:**

```powershell
docker logs --tail 80 nginx-imagiax
```

**Observed key error:**

```text
[emerg] host not found in upstream "orthanc" in /etc/nginx/conf.d/default.conf:199
```

**Outcome:** ❌ Nginx loop caused by missing `orthanc` upstream (because Orthanc failed).

---

### EV-006 — FastAPI actual liveness vs `/salud` behavior

**Commands:**

```powershell
docker exec fastapi-imagiax curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8010/ping
docker exec fastapi-imagiax curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8010/salud
```

**Observed:**

- `/ping` => `200`
- `/salud` => `401`

**Code evidence:** `servidor_api_orthanc_v1.py` declares:

- `@app.get("/ping")` (public liveness)
- `@app.get("/salud")` with `Depends(get_current_user)` (auth-protected)

**Outcome:** ⚠️ `/salud` is not a no-auth liveness endpoint. Any onboarding/launcher check treating `/salud` as open health can report false-negative liveness.

---

### EV-007 — PostgreSQL state

**Command:**

```powershell
docker exec postgres-imagiax psql -U imagiax -d imagiax -c "select 1;"
```

**Observed:**

```text
 ?column?
----------
        1
(1 row)
```

**Outcome:** ✅ Postgres is healthy and queryable inside container.

---

### EV-008 — Demo DICOM upload path and hardcoded path behavior

**Command:**

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "...\upload-dicoms.ps1" -DryRun
```

**Observed key output:**

```text
Origen:  C:\Users\WinterOS\Documents\Workspace\IMAGIAX\rx tx_
Origen:  C:\Users\WinterOS\Documents\Workspace\IMAGIAX\OHIF Medical Imaging Viewer\RX Dicom Veridicos
...
[ERROR] No se puede conectar a Orthanc en http://localhost:8042
```

**Additional path evidence:** script defaults are hardcoded to `...\Workspace\IMAGIAX\...` (not this repo root `...\IMAGIAX - copia (2)`).

**Outcome:** ❌ Upload dry-run fails due to Orthanc down; plus path defaults are machine-specific and risky for coworker portability.

---

## 5) Coworker checkpoints (dated evidence)

| Checkpoint | State | Evidence |
|---|---|---|
| OHIF reachable (expected `http://localhost:3000`) | ❌ Not reachable | EV-003 (port 3000 false) |
| FastAPI reachable from host (`http://localhost:8010`) | ❌ Not reachable from host in this flow | EV-003 (port 8010 false) |
| FastAPI process/liveness internal | ⚠️ Partially alive | EV-006 (`/ping` 200, `/salud` 401) |
| Orthanc reachable (`http://localhost:8042`) | ❌ Not reachable | EV-002, EV-003 |
| Postgres state | ✅ Healthy | EV-004, EV-007 |
| Demo DICOM upload path | ❌ Fails currently | EV-008 |
| Hardcoded path failures risk | ⚠️ Confirmed portability risk | EV-001, EV-008 |
| False health-check risk (`/salud` vs liveness) | ⚠️ Confirmed | EV-006 |

---

## 6) Claim-to-evidence trace list (for future README/diagram)

> Rule for next phases: any README/diagram claim without an EV reference below stays out.

| Future claim candidate | Status now | Required evidence reference |
|---|---|---|
| “`INICIAR-IMAGIAX.bat` is the reliable Windows entry point” | ❌ Not verified (currently failing) | EV-001 |
| “`start-imagiax.ps1` brings up full local stack” | ❌ Not verified (blocked at Orthanc) | EV-002, EV-004 |
| “OHIF available at `http://localhost:3000` after startup” | ❌ Not verified | EV-003 |
| “FastAPI health check is `/salud` (no auth)” | ❌ False for current code | EV-006 |
| “FastAPI liveness can be checked via `/ping`” | ✅ Verified internal container check | EV-006 |
| “Orthanc + Postgres start together via local compose” | ⚠️ Partial (Postgres yes, Orthanc no) | EV-002, EV-004, EV-007 |
| “Demo upload script works out-of-box on coworker machine” | ❌ Not verified; currently blocked and path-sensitive | EV-008 |

---

## 7) Explicit blocker list

1. **Launcher parse failure**
   - Step: run `INICIAR-IMAGIAX.bat`
   - Error: `No se esperaba \OHIF en este momento.`
   - Evidence: EV-001

2. **Orthanc cannot bind host port 4242**
   - Step: `start-imagiax.ps1` step 2 / `docker compose up -d`
   - Error: `bind ... 0.0.0.0:4242 ... acceso a un socket no permitido`
   - Evidence: EV-002

3. **Nginx unstable because Orthanc missing in network**
   - Step: inspect `nginx-imagiax` logs
   - Error: `host not found in upstream "orthanc"`
   - Evidence: EV-005

4. **Health-check mismatch risk**
   - Step: evaluate API health endpoint behavior
   - Finding: `/salud` returns 401 (auth-protected) while `/ping` returns 200
   - Evidence: EV-006

5. **Upload script portability risk**
   - Step: inspect/run `upload-dicoms.ps1`
   - Finding: hardcoded source paths tied to `...\Workspace\IMAGIAX\...`
   - Evidence: EV-008

---

## 8) Verified vs failing vs unverified summary

### Verified working
- Docker CLI present; Docker Desktop can be auto-started from script in this environment.
- Postgres container health/query works (`select 1`).
- FastAPI container has internal liveness at `/ping`.

### Verified failing
- `INICIAR-IMAGIAX.bat` fails on this machine path.
- `start-imagiax.ps1` cannot complete due to Orthanc bind failure (`4242`).
- OHIF not reachable on `localhost:3000` in this run.
- Orthanc not reachable on `localhost:8042` in this run.
- DICOM upload script cannot run end-to-end while Orthanc is down.

### Unverified in this slice
- End-to-end coworker walkthrough success from README only.
- Successful local OHIF + Orthanc + FastAPI host-access trifecta in a clean startup without blockers.

---

## 9) Phase 3 continuation — optional safe launcher/path portability adjustments

**Date (local):** 2026-04-26 13:04 -04:00  
**Scope:** local-only launcher/path portability fixes for reproduced blockers (no deploy/prod/AI-core edits)

### EV-009 — Reproduce `INICIAR-IMAGIAX.bat` blocker BEFORE fix

**Command:**

```powershell
& ".\INICIAR-IMAGIAX.bat"
```

**Observed output (before):**

```text
========================================
  IMAGIAX - Inicio rapido
========================================

No se esperaba \OHIF en este momento.
```

**Outcome:** ❌ Reproduced (`EV-001` still valid).

---

### EV-010 — Re-run launcher AFTER fix

**Implemented fix (minimal/local):**

- `INICIAR-IMAGIAX.bat`: enabled delayed expansion and switched `%SCRIPT_DIR%` checks inside `if (...)` block to `!SCRIPT_DIR!` so paths with parentheses are parsed safely.

**Command:**

```powershell
& ".\INICIAR-IMAGIAX.bat"
```

**Observed output (after):**

```text
========================================
  IMAGIAX - Inicio rapido
========================================

[1] Verificando Docker Engine
Docker Engine v'29.2.1' operativo

[2] Verificando Orthanc PACS
Orthanc no responde. Levantando contenedor...
Error response from daemon: ports are not available: exposing port TCP 0.0.0.0:4242 ...
```

**Outcome:** ✅ Batch parser blocker fixed (launcher now reaches PowerShell startup flow).  
**Still failing:** Orthanc bind error on host port `4242` remains (environment/port-permission issue, not path-portability).

---

### EV-011 — Reproduce and fix upload script machine-specific path defaults

**Command (before):**

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "OHIF Medical Imaging Viewer\orthanc-imagiax\upload-dicoms.ps1" -DryRun
```

**Observed path defaults (before):**

```text
Origen:  C:\Users\WinterOS\Documents\Workspace\IMAGIAX\rx tx_
Origen:  C:\Users\WinterOS\Documents\Workspace\IMAGIAX\OHIF Medical Imaging Viewer\RX Dicom Veridicos
```

**Fix applied (minimal/local):**

- `upload-dicoms.ps1`: default `DicomPaths` now resolve relative to repo root discovered from script location (`..\..`), while keeping explicit `-DicomPaths` override support.

**Observed path defaults (after):**

```text
Origen:  C:\Users\WinterOS\Documents\Workspace\IMAGIAX - copia (2)\rx tx_
Origen:  C:\Users\WinterOS\Documents\Workspace\IMAGIAX - copia (2)\OHIF Medical Imaging Viewer\RX Dicom Veridicos
```

**Outcome:** ✅ Hardcoded machine-specific defaults removed for local-share portability.

---

### EV-012 — `/salud` false-liveness risk handling in launcher logic

**Evidence command:**

```powershell
docker exec fastapi-imagiax sh -lc "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8010/ping; printf ' '; curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8010/salud"
```

**Observed:** `200 401`

**Fix applied (minimal/local):**

- `start-imagiax.ps1` health probe switched from `/salud` to `/ping` for startup liveness checks.

**Outcome:** ✅ Launcher no longer assumes `/salud` is public liveness.

---

## 10) Phase 3 status summary

### Improved in this phase
- `INICIAR-IMAGIAX.bat` no longer crashes on repo paths with parentheses; it correctly invokes `start-imagiax.ps1`.
- `upload-dicoms.ps1` default dataset paths are repo-relative instead of hardcoded to one machine path.
- `start-imagiax.ps1` liveness probe now uses `/ping` (public) instead of `/salud` (auth-protected).

### Still failing
- Orthanc startup can still fail with host bind error on `0.0.0.0:4242` in this environment.
- Because Orthanc is down, end-to-end local stack completion and upload execution remain blocked.

### Boundary audit for this phase
- Deploy/prod files edited: **NO**
- AI-core files edited: **NO**

---

## 11) Phase 5 continuation — final shareability verification

**Date (local):** 2026-04-26 13:29:32 -04:00  
**Scope:** fresh coworker-style walkthrough using only `README.md` + listed prerequisites; docs-only recording

### EV-013 — Prerequisites re-check from README list

**Commands:**

```powershell
py --version
yarn --version
docker version
```

**Observed:**

- `Python 3.11.9`
- `1.22.22`
- Docker client/server available (`29.2.1`)

**Outcome:** ✅ Prerequisites listed in README are present on this representative local machine.

---

### EV-014 — Fresh README-driven launcher walkthrough

**Command:**

```powershell
& ".\INICIAR-IMAGIAX.bat"
```

**Observed key output:**

```text
[1] Verificando Docker Engine
Docker Engine v'29.2.1' operativo

[2] Verificando Orthanc PACS
Orthanc no responde. Levantando contenedor...
Error response from daemon: ports are not available: exposing port TCP 0.0.0.0:4242 ...
ERROR: Orthanc no respondio en 30 segundos.
```

**Outcome:** ❌ Fresh run still blocks at Orthanc host bind `4242`.

---

### EV-015 — Service-state validation after failed launcher attempt

**Commands:**

```powershell
docker compose -f "OHIF Medical Imaging Viewer/orthanc-imagiax/docker-compose.yml" ps
docker exec fastapi-imagiax sh -lc "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8010/ping; printf ' '; curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8010/salud"
docker exec postgres-imagiax psql -U imagiax -d imagiax -c "select 1;"
Invoke-WebRequest "http://localhost:8042" -UseBasicParsing
Invoke-WebRequest "http://localhost:3000" -UseBasicParsing
```

**Observed:**

- Compose state shows `fastapi/nginx/gotenberg/ohif` containers present as orphaned/byproduct state; Orthanc still not up in this failed path.
- FastAPI probe: `200 401` (`/ping` alive, `/salud` auth-protected).
- Postgres query returns `1`.
- Host requests to `http://localhost:8042` and `http://localhost:3000` fail (`No es posible conectar con el servidor remoto`).

**Outcome:** ⚠️ Partial local state persists, but documented end-to-end checkpoints remain blocked in this environment.

---

### EV-016 — README optional dry-run upload step

**Command:**

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "OHIF Medical Imaging Viewer\orthanc-imagiax\upload-dicoms.ps1" -DryRun
```

**Observed:**

- Script reports repo-relative default origins under `...\IMAGIAX - copia (2)\...`.
- Fails with `[ERROR] No se puede conectar a Orthanc en http://localhost:8042`.

**Outcome:** ⚠️ Path portability remains improved, but upload still depends on Orthanc reachability.

---

## 12) Final shareability verdict (Phase 5)

### Shareable as-is for internal coworker handoff?

**YES, conditionally** — this onboarding package is shareable for:

- reproducible internal local walkthrough,
- transparent checkpoint validation,
- honest blocker reproduction and troubleshooting.

**NOT shareable as an end-to-end success guarantee** while Orthanc host bind on `4242` remains unresolved on the target machine.

### Remaining HOLD / unresolved items (explicit)

1. **HOLD-L1:** Orthanc host bind on `0.0.0.0:4242` can still fail; this blocks full local stack completion in affected environments (EV-014, EV-015).
2. **HOLD-L2:** Presence of orphaned `nginx/ohif/fastapi` containers can mislead operators; treat orphan state as diagnostic context, not proof that the local Orthanc compose path succeeded (EV-014, EV-015).
3. **HOLD-L3:** Upload flow remains blocked when Orthanc is unreachable even though path defaults are now portable (EV-016).
4. **HOLD-L4:** Repository working tree contains many unrelated edits (including protected deploy/AI-core surfaces) outside this onboarding package; sharing should use only the scoped onboarding file set for this change.

### Boundary audit for this phase

- Deploy/prod files edited: **NO**
- AI-core files edited: **NO**
- Runtime/deploy cutover actions performed: **NO**

Phase 5 result: onboarding docs remain evidence-first, internal-only, and scope-compliant.
