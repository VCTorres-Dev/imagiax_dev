# Backend Parity Verification Evidence (Current-State Execution)

## Scope and method
This record executes `docs/repo-curation/backend-parity-verification-checklist.md` using **repository evidence + targeted parity test modules** (no live environment probes).

Canonical approval-package blocker index (single source of traceability):
- `docs/repo-curation/approval-package/index.md`

- Verification window/date: 2026-04-25 (baseline) + 2026-04-26 (Phase-5 final blocker re-evaluation)
- Operator: SDD apply executor
- Environment: local repo + targeted unittest modules (no staging/prod-like runtime execution)
- Runtime under test: authoritative monolith vs shadow factory comparison
- Checklist source: `docs/repo-curation/backend-parity-verification-checklist.md`

### Targeted verification run (Phase 5 final blocker re-evaluation)

Executed on 2026-04-26:

- `python -m unittest tests.api.test_runtime_contract_parity` → **OK** (`Ran 5 tests in 0.086s`)
- `python -m unittest tests.api.auth.test_auth_verify_parity` → **OK** (`Ran 5 tests in 0.032s`)
- `python -m unittest tests.api.test_startup_resilience_parity` → **OK** (`Ran 3 tests in 7.540s`)

Evidence caveat (recorded, no scope expansion in this slice): startup resilience module emits expected retry/reconnect logs and an asyncio slow-task warning during mocked turbulence, and auth parity emits a Starlette per-request cookies deprecation warning; assertions pass and no harness/runtime refactor was performed in this phase.

## Baseline acknowledged
- Authoritative runtime now: `servidor_api_orthanc_v1:app`
  - Evidence: `Dockerfile:68-73`, `start_api.bat:3`, `arrancar_api.py:8`
- Shadow target: `src/api/main.py`
  - Evidence: `src/api/main.py:1-6`
- Gate baseline prior to this run: `RED / HOLD`
  - Evidence: `docs/repo-curation/runtime-cutover-parity-gate.md:8-27`

### Phase-1 foundation addendum (`backend-cutover-readiness-blockers`)

#### `/salud` semantics baseline (monolith vs factory/shadow)
- Monolith runtime `/salud` is auth-protected and returns comprehensive multi-component diagnostics.
  - Evidence: `servidor_api_orthanc_v1.py:1028-1144` (`Depends(get_current_user)`, component checks for model/Orthanc/PostgreSQL/Gemini, rich payload).
- Factory shadow `/salud` is public lightweight `{"status": "ok"}` and details are split into `/salud/detalle` with admin role.
  - Evidence: `src/api/main.py:309-377` (`/salud` public minimal payload; `/salud/detalle` admin-protected detailed diagnostics).

#### Deploy/start authority baseline and O5 mismatch evidence
- Current active deploy/start authority is monolith runtime entrypoint `servidor_api_orthanc_v1:app`.
  - Evidence: `Dockerfile:66-76`, `OHIF Medical Imaging Viewer/orthanc-imagiax/docker-compose.prod.yml:50-55`, `OHIF Medical Imaging Viewer/orthanc-imagiax/docker-compose.prod.PRODUCTION.yml:72-76`, `start_api.bat:2-5`, `arrancar_api.py:4-10`, `OHIF Medical Imaging Viewer/orthanc-imagiax/start-imagiax.ps1:20-22,234-235`.
- Legacy host-copy path exists (`deploy.sh`) and does not define runtime authority by itself.
  - Evidence: `deploy.sh:4-15` (remote file copy + compose rebuild), contrasted with root-context image authority in compose/Dockerfile above.
- Operational policy in this change: host-copy assumptions are treated as non-authoritative/deprecated for cutover authority decisions; authoritative model is active container/start surfaces.

---

## A) Contract parity checks

| ID | Status | Evidence summary |
|---|---|---|
| C1 (`GET /ping`) | **PASS (Phase 1)** | Monolith exposes `/ping` (`servidor_api_orthanc_v1.py`). Factory now exposes `/ping` with `200` + `{\"status\":\"ok\"}` (`src/api/main.py`; verified in `tests/api/test_runtime_contract_parity.py::test_ping_status_and_shape`). |
| C2 (`GET /health/db`) | **PASS (healthy-path parity in Phase 1)** | Monolith exposes `/health/db` (`servidor_api_orthanc_v1.py`). Factory now exposes `/health/db` with healthy-path `200` + `{\"ok\": true, \"database\": \"up\"}` (`src/api/main.py`; verified in `tests/api/test_runtime_contract_parity.py::test_health_db_status_shape_and_content_type`). |
| C3 (`GET /robots.txt`) | **PASS (Phase 1)** | Factory now exposes `/robots.txt` with plain-text policy and sitemap reference (`src/api/main.py`; verified in `tests/api/test_runtime_contract_parity.py::test_robots_txt_status_content_type_and_body_shape`). |
| C4 (`GET /sitemap.xml`) | **PASS (Phase 1)** | Factory now exposes `/sitemap.xml` with XML body and `application/xml` media type (`src/api/main.py`; verified in `tests/api/test_runtime_contract_parity.py::test_sitemap_xml_status_content_type_and_body_shape`). |
| C5 (`GET /salud`) | **AMBER (DISPOSITIONED intentional delta, approval artifact pending)** | Monolith `/salud` is auth-protected + comprehensive (`servidor_api_orthanc_v1.py:1028-1144`). Factory shadow keeps lightweight public `/salud` and admin-only detail route (`src/api/main.py:311-377`); this is an explicit intentional delta, not parity. Internal-prep package memo: `docs/repo-curation/approval-package/c5-salud-delta-memo.md`. External-pending closure evidence remains mandatory: named approver identity plus signed approval evidence link. C5 cannot be considered closed while either approver identity or evidence link is missing/placeholder. |
| C6 (`GET /api/auth/verify` allow) | **PASS (Phase 2)** | Nginx maps `/api/auth/verify` -> backend `/auth/verify` (`nginx-proxy.conf:128-130`). Monolith allow status is `204` (`servidor_api_orthanc_v1.py:854-860`). Factory allow status now `204` (`src/api/auth/router.py`). Covered by `tests/api/auth/test_auth_verify_parity.py::test_auth_verify_valid_returns_204`. |
| C7 (`GET /api/auth/verify` deny) | **PASS (Phase 2)** | Factory returns `401` on invalid, missing, revoked, and transient revocation-check failure paths (`src/api/auth/router.py`). Covered by targeted tests: `tests/api/auth/test_auth_verify_parity.py::{test_auth_verify_invalid_returns_401,test_auth_verify_missing_returns_401,test_auth_verify_revoked_returns_401,test_auth_verify_transient_revocation_check_failure_returns_401}`. |

**Contract dimension outcome (A): `RED`** (C1-C4 and C6-C7 passing; C5 is dispositioned but not closed because approval artifact is still missing)

---

## B) Deploy / ops wiring checks

Phase 4 (`ops-normalization-under-hold`) performed a targeted O1-O5 recheck by readback of active surfaces, preserving runtime behavior and authority.

| ID | Status | Evidence summary |
|---|---|---|
| O1 (Docker entrypoint) | **PASS (Phase 4, hold-normalized)** | Active image CMD remains monolith (`uvicorn servidor_api_orthanc_v1:app`) and now includes explicit HOLD/shadow annotation (`Dockerfile`). Authority is explicit with no cutover rewiring. |
| O2 (Compose wiring parity) | **PASS (Phase 4, hold-normalized)** | Both production compose variants still build backend from root Dockerfile and now explicitly document HOLD/non-cutover posture (`docker-compose.prod.yml`, `docker-compose.prod.PRODUCTION.yml`). |
| O3 (Operator start scripts) | **PASS (Phase 4, hold-normalized)** | Operator scripts keep monolith runtime commands and now explicitly label factory as shadow/non-authoritative (`start_api.bat`, `arrancar_api.py`, `start-imagiax.ps1`, `deploy_production.sh`). |
| O4 (Nginx upstream/auth/SEO assumptions) | **PASS (Phase 4, targeted evidence scope)** | Nginx assumptions remain consistent with active runtime contract: `/api/*`, `/api/auth/verify`, `/robots.txt`, `/sitemap.xml` mappings unchanged and previously validated by Phase 1-2 parity evidence. This slice adds clarity, not rewiring. |
| O5 (Host-copy vs container-runtime assumptions) | **AMBER (packaged alignment memo; closure still blocked)** | Single authoritative model for current operation is monolith container/start surfaces (`Dockerfile:66-76`, `docker-compose.prod*.yml:50-55/72-76`, `start_api.bat`, `arrancar_api.py`, `start-imagiax.ps1`). `deploy.sh` host-copy path (`deploy.sh:4-15`) is explicitly treated as non-authoritative/deprecated for runtime-authority decisions and packaged in `docs/repo-curation/approval-package/o5-authority-alignment-memo.md`. Remaining gap to close O5: approval-linked retirement/alignment evidence (named approver + signed evidence link); memo packaging alone does not close O5. |

**Deploy/Ops dimension outcome (B): `RED`** (single-authority wording and evidence are explicit; O5 remains blocked pending formal retirement/alignment artifact)

---

## C) Startup / resilience checks

### R3 reproducible evidence runbook (auth-critical turbulence)

This runbook is the minimum reproducible procedure to justify R3 for gate review without changing runtime authority or performing cutover.

#### Preconditions
- HOLD boundaries remain active: authoritative runtime is still `servidor_api_orthanc_v1:app`; no cutover/rollback execution in this change.
- Local test environment can run Python unittest modules from repo root.
- Required files are present and reviewable:
  - `tests/api/test_startup_resilience_parity.py`
  - `tests/api/auth/test_auth_verify_parity.py`
  - `src/api/main.py`
  - `app/core/database.py`
  - `app/core/config.py`

#### Auth-critical turbulence steps
1. Run startup resilience turbulence module:
   - `python -m unittest tests.api.test_startup_resilience_parity`
2. Confirm the three resilience assertions are exercised:
   - startup retry exhaustion/success path (`test_create_pool_with_retries_failures_then_success`)
   - background reconnect + deferred bootstrap recovery (`test_background_reconnect_recovers_and_runs_deferred_bootstrap`)
   - shutdown-time reconnect cancellation (`test_lifespan_cancels_background_reconnect_on_shutdown`)
3. Confirm auth-critical deny/fail-closed parity remains pinned during turbulence context:
   - `python -m unittest tests.api.auth.test_auth_verify_parity`
4. Record command outputs and map them to expected outcomes below.

#### Expected outcomes
- Resilience module finishes `Ran 3 tests ... OK`.
- Logs may include retry/reconnect warnings and mocked DB-down traces; these are acceptable if assertions pass.
- Auth verify module finishes `Ran 5 tests ... OK` and preserves `204/401` contract with fail-closed deny behavior.
- No step changes runtime/deploy authority, compose wiring, nginx behavior, or executes cutover.

#### Evidence capture format
Record each run in this file with the following template:

```markdown
### R3 Evidence Record — <YYYY-MM-DD>
- Operator:
- Scope:
- Preconditions check: PASS/FAIL (notes)
- Commands:
  - `python -m unittest tests.api.test_startup_resilience_parity` → <result>
  - `python -m unittest tests.api.auth.test_auth_verify_parity` → <result or reference to existing same-window evidence>
- Key log excerpts:
  - <excerpt 1>
  - <excerpt 2>
- Expected outcome match: PASS/FAIL (why)
- Residual gap:
- Owner:
- Next evidence needed:
```

### R3 Evidence Record — 2026-04-26
- Operator: SDD apply executor
- Scope: local reproducibility check (docs/evidence posture), no runtime authority changes
- Preconditions check: PASS (HOLD boundaries unchanged; targeted modules and source files available)
- Commands:
  - `python -m unittest tests.api.test_startup_resilience_parity` → `Ran 3 tests in 7.153s` / `OK`
  - `python -m unittest tests.api.auth.test_auth_verify_parity` → `Ran 5 tests` / `OK` (same verification window evidence already captured in this artifact)
- Key log excerpts:
  - `IMAGIAX DB: startup pool unavailable; background reconnect active (first retry in ~2s, then every 8.0s).`
  - `IMAGIAX DB: startup pool init exhausted retries.` followed by mocked `RuntimeError: db down` in turbulence scenario
- Expected outcome match: PASS (all planned turbulence assertions passed and fail-closed auth parity remains pinned)
- Residual gap: still no live runtime turbulence rehearsal against a real DB/network failure window (current proof is deterministic local/mock-targeted evidence)
- Owner: `TBD-ROLLBACK-OWNER` (placeholder until B6 assignment/escalation is completed)
- Next evidence needed: one controlled live-shadow turbulence execution (real Postgres interruption/recovery + `/api/auth/verify` probes) with timestamped logs and command transcript appended here.

| ID | Status | Evidence summary |
|---|---|---|
| R1 (DB startup retry behavior) | **PASS (Phase 3)** | Factory now uses configurable startup retry/backoff via `create_pool_with_retries(...)` in lifespan (`app/core/database.py`, `src/api/main.py`) with env-backed knobs (`app/core/config.py`). Failures-then-success behavior covered by `tests/api/test_startup_resilience_parity.py::test_create_pool_with_retries_failures_then_success`. |
| R2 (DB reconnect after boot race) | **PASS (Phase 3, shadow lifecycle)** | Factory now starts `_background_reconnect_until_ready()` when startup retries exhaust and runs deferred bootstrap once DB is available (`src/api/main.py`). Late-recovery path is covered by `tests/api/test_startup_resilience_parity.py::test_background_reconnect_recovers_and_runs_deferred_bootstrap`. |
| R3 (Auth/critical readiness under DB turbulence) | **AMBER (runbook defined + locally reproducible evidence recorded; live rehearsal pending)** | `/auth/verify` remains fail-closed (`401`) on revocation-check turbulence (`src/api/auth/router.py`, tests in `tests/api/auth/test_auth_verify_parity.py`). Reproducible runbook and 2026-04-26 execution record are now documented in this artifact, including command output/log excerpts from `tests.api.test_startup_resilience_parity`. R3 remains AMBER because live-shadow turbulence rehearsal evidence is still missing (gap/owner/next evidence explicitly recorded above). |

**Startup/Resilience dimension outcome (C): `RED`**

---

## D) Rollback readiness checks

Rollback readiness source of truth: `docs/repo-curation/backend-cutover-rollback-readiness.md`.

**Important gate rule**: rollback-readiness evidence by itself does **NOT** authorize cutover; parity dimensions A/B/C and explicit approval must also be GREEN.

| ID | Status | Evidence summary |
|---|---|---|
| B1 (Known-good refs captured) | **AMBER** | Anchors exist and execution-window known-good ref template is defined, but cutover-window SHA/digest/checksum capture is pending (`backend-cutover-rollback-readiness.md`, section "Known-good refs / rollback anchors"). |
| B2 (Cutover diff bundle) | **RED (template prepared, execution-window instance missing)** | Readiness template/index now exists (`backend-cutover-rollback-readiness.md`, section "B2 diff-bundle readiness template/index"), but no execution-window bundle instance is captured yet (missing frozen diff inventory, env/config deltas, exact operator/rollback transcripts, and timestamped evidence artifacts). |
| B3 (Revert runbook rehearsable) | **AMBER** | Execution-grade revert order is now documented, but no rehearsal is executed in this scope (`backend-cutover-rollback-readiness.md`, section "Revert order"). |
| B4 (Post-rollback utility/SEO smoke) | **AMBER** | Post-rollback checkpoints are formalized with expectations/evidence fields, but not executed in this scope (`backend-cutover-rollback-readiness.md`, section "Post-rollback smoke checkpoints"). |
| B5 (Post-rollback auth semantics 204/401) | **PASS (contract pinned)** | Explicit `/api/auth/verify` -> `/auth/verify` rollback expectation is pinned as `204/401` with fail-closed semantics (`backend-cutover-rollback-readiness.md`, section "Explicit auth rollback expectation"). |
| B6 (Rollback owner/approval) | **RED (escalated, still unassigned)** | Assignment state now includes a formal escalation entry (accountable role + target approver path + checkpoint placeholder), but no real named owner/approver assignment artifact exists yet (`backend-cutover-rollback-readiness.md`, section "Owner / approver assignment state (B6)"). |

**Rollback dimension outcome (D): `RED`**

### B2 completeness gate (objective criteria reference)

For B2 to move from **RED** in a future execution window, all objective completeness criteria listed in
`backend-cutover-rollback-readiness.md` (section "Objective completeness criteria") must exist as real artifacts for the same window. In this slice, only the template/index layer is added.

---

## E) Gate outcome recording (required)

- Contract parity outcome (A): **RED** (C1-C4 and C6-C7 PASS by Phase 2 evidence; C5 is dispositioned intentional delta but approval artifact remains pending)
- Deploy/ops parity outcome (B): **RED**
- Startup/resilience outcome (C): **RED**
- Rollback readiness outcome (D): **RED**
- Final gate recommendation: **KEEP HOLD**
- Decision notes:
  - Checklist hard-stop remains active because unresolved blockers include C5 intentional-delta approval artifact gap and O5 legacy host-copy normalization gap; overall dimensions A/B/C/D are not GREEN and approval is still RED.
  - For this change, E handling is preparation-only governance packaging; approval E is not granted here and still requires a separate explicit approval artifact (see `docs/repo-curation/approval-package/e-explicit-approval-link.md` and `docs/repo-curation/approval-package/final-package-summary.md`).
  - `R3` remains AMBER under targeted/mock evidence only; resilience dimension does not clear gate for cutover.
  - Backend runtime-cutover candidate/main scope remains blocked.
- Approver (if any): none in this batch
- Timestamp: 2026-04-26 (Phase-5 final blocker re-evaluation)

### Final blocker re-score snapshot (Phase 5)

| Blocker | Final status | Disposition reason |
|---|---|---|
| C5 | **DISPOSITIONED / AMBER (not closed)** | Intentional `/salud` delta is documented with impact; missing explicit approval artifact/approver record. |
| O5 | **AMBER (still blocked)** | Single authority model + explicit non-authoritative host-copy wording are now packaged in `approval-package/o5-authority-alignment-memo.md`; O5 still requires approval-linked retirement/alignment evidence (named approver + signed evidence link). |
| R3 | **AMBER (still blocked)** | Reproducible local/mock evidence exists; no live-shadow turbulence rehearsal artifact exists. |
| B2 | **RED (still blocked)** | B2 template/index exists; no execution-window diff-bundle instance/transcripts/evidence set exists. |
| B6 | **RED (still blocked)** | Escalation path exists; no named rollback owner + named approver assignment artifact exists. |

Final readiness statement: **STILL BLOCKED (NOT CUTOVER)**.

## Blocked scope (explicit)
Until the gate is explicitly GREEN with approved execution evidence:

- Do **NOT** switch runtime entrypoint away from `servidor_api_orthanc_v1:app`.
- Do **NOT** rewire deploy/runtime scripts to factory as authoritative.
- Do **NOT** treat backend runtime-cutover scope as candidate/main eligible.

## Final review posture (Phase 5)
- No task in this change switched runtime authority from `servidor_api_orthanc_v1:app`.
- No task rewired nginx authority, Docker CMD/entrypoint authority, compose runtime authority, or operator start-script authority to factory runtime.
- Backend candidate/main runtime-cutover scope remains blocked under **RED / HOLD**.
- A separate future approved change is still required for any real cutover execution.
