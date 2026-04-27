# Runtime Cutover Parity Gate (Future Execution Gate)

## Purpose
Define the mandatory gate that must be satisfied before any backend runtime cutover from `servidor_api_orthanc_v1:app` to the modular target.

This is a **future execution gate**. In current phase, we only materialize the gate.

## Gate status (current)
**RED / HOLD** — not satisfied.

## HOLD boundary restatement (approval-package final state)
- Runtime authority remains **only** `servidor_api_orthanc_v1:app`.
  - Evidence: `Dockerfile:66-76`, `OHIF Medical Imaging Viewer/orthanc-imagiax/docker-compose.prod.yml:50-55`, `OHIF Medical Imaging Viewer/orthanc-imagiax/docker-compose.prod.PRODUCTION.yml:72-76`, `start_api.bat:2-5`, `arrancar_api.py:4-10`, `OHIF Medical Imaging Viewer/orthanc-imagiax/start-imagiax.ps1:20-22,234-235`.
- Approval gate **E remains ungranted** in this change.
- This change performs **no cutover execution** and **no rollback execution**.
- Any host-copy assumption (for example `deploy.sh`) is **non-authoritative/deprecated for runtime-authority decisions**; authority decisions are made from active container/start surfaces listed above.

## Evidence artifacts (this repository)
- Canonical approval-package blocker index: `docs/repo-curation/approval-package/index.md`
- Final approval-package summary (consumed for final review): `docs/repo-curation/approval-package/final-package-summary.md`
- E explicit approval linkage contract: `docs/repo-curation/approval-package/e-explicit-approval-link.md`
- O5 authority alignment memo (packaging-only): `docs/repo-curation/approval-package/o5-authority-alignment-memo.md`
- R3 live-shadow requirements (packaging-only): `docs/repo-curation/approval-package/r3-live-shadow-requirements.md`
- B2 execution-window bundle template (packaging-only): `docs/repo-curation/approval-package/b2-window-bundle-template.md`
- `docs/repo-curation/backend-route-semantics-parity-matrix.md`
- `docs/repo-curation/backend-deploy-ops-parity-matrix.md`
- `docs/repo-curation/backend-startup-resilience-parity-evidence.md`
- `docs/repo-curation/backend-cutover-rollback-readiness.md`
- `docs/repo-curation/backend-parity-verification-checklist.md`
- `docs/repo-curation/backend-parity-verification-evidence.md`
- `docs/repo-curation/backend-runtime-contract.md`
- `docs/repo-curation/runtime-cutover-blockers.md`

Current evidence summary:
- Contract parity: **RED** (`backend-parity-verification-evidence.md`)
- Deploy/ops parity: **RED** (`backend-parity-verification-evidence.md`)
- Resilience parity: **RED** (`backend-parity-verification-evidence.md`)
- Rollback readiness: **RED** (`backend-parity-verification-evidence.md`; rollback runbook quality improved but B2/B6 remain unresolved)
- Approval: **RED**

Canonical approval-package status wording for final review MUST be read from:
- `docs/repo-curation/approval-package/final-package-summary.md`

Current per-dimension status detail (approval-package final review baseline):
- **A Contract = RED**: C1/C2/C3/C4/C6/C7 pass; C5 is now **dispositioned** as intentional `/salud` delta (operational/security impact documented), but still blocked from closure until an explicit approval artifact/approver record exists.
- **B Deploy/Ops = RED**: single deploy/start authority is explicitly monolith container/start surfaces; host-copy model is explicitly non-authoritative/deprecated for cutover decisions, but O5 remains blocked until approval-linked retirement/alignment evidence exists (the memo package alone does not close O5).
- **C Resilience = RED**: R1/R2 pass; R3 remains blocked (AMBER) because only requirements packaging exists and no live-shadow turbulence rehearsal artifact instance exists.
- **D Rollback = RED**: B5 is pinned PASS (`204/401` auth semantics), B2 template packaging exists, but B2 remains blocked without a real execution-window bundle instance and B6 remains blocked without named owner/approver assignment artifact (B1/B3/B4 still AMBER).
- **E Approval = RED (preparation-only in this change)**: governance packaging can be prepared, but approval E remains HOLD until a separate explicit approval artifact exists.

Rollback artifacts reference:
- `docs/repo-curation/backend-cutover-rollback-readiness.md` (execution-grade rollback runbook format)
- `docs/repo-curation/backend-parity-verification-evidence.md` (B1-B6 scored status)

### B2 readiness linkage (Phase 3, documentation-only)
- Prepared artifact: `backend-cutover-rollback-readiness.md` → section **"B2 diff-bundle readiness template/index (pre-execution only)"**.
- Current status remains **RED** because only template/index scaffolding exists; no cutover-window bundle instance is captured in this change.
- Objective criteria to move B2 off RED (future window):
  1. Frozen file diff inventory with forward and reverse-diff artifacts.
  2. Frozen env/config delta inventory with explicit rollback restore values.
  3. Exact operator command transcripts (not placeholders).
  4. Exact rollback command transcripts (reverse-diff + restore).
  5. Timestamped evidence artifacts cross-linked from one B2 manifest.

Explicit rule: rollback readiness alone does **NOT** authorize cutover. Gate can only move off HOLD when dimensions A/B/C/D are satisfied and explicit approval is documented.

Therefore the gate remains **RED / HOLD** and backend publication-candidate/main scope remains blocked.

## Final readiness summary (approval-package final review)
**STILL BLOCKED (NOT CUTOVER)**

Final blocker re-score for this change:
- **C5**: **DISPOSITIONED, NOT CLOSED** — intentional `/salud` delta is documented, but approval artifact/approver identity is still missing.
- **O5**: **STILL BLOCKED** — authority wording is packaged in `o5-authority-alignment-memo.md` with explicit non-authoritative host-copy language, but O5 remains open until approval-linked retirement/alignment evidence (named approver + signed evidence link) exists.
- **R3**: **STILL BLOCKED (AMBER)** — reproducible local runbook exists, but live-shadow turbulence rehearsal evidence is still missing.
- **B2**: **STILL BLOCKED (RED)** — template/index exists, but no execution-window diff-bundle instance/transcript set exists.
- **B6**: **STILL BLOCKED (RED)** — assignment record is now packaged (`docs/repo-curation/approval-package/b6-assignment-record.md`), but required fields remain placeholders and no resolvable named-assignment evidence artifact is present.

Interpretation: this repository is **not ready for cutover execution**. At best, this is a hold-preserving documentation posture awaiting missing governance/operational artifacts.

## Final review posture (approval-package complete, still not cutover)
- No task in `backend-cutover-readiness-blockers` switched runtime authority.
- No task rewired deploy/runtime surfaces to factory authority (nginx, Docker CMD/entrypoint, compose, operator start scripts remain monolith-authoritative).
- This slice only re-evaluates and refreshes parity evidence; it does not perform cutover.
- Any real cutover still requires a **separate future approved change** after this gate is explicitly GREEN.

Historical traceability note (legacy phase references from earlier packaging rounds; current authority is the approval-package final summary):
- For `backend-cutover-readiness-blockers` Phase 1 specifically:
  - Scope is boundary + blocker-foundation evidence only (`C5`/`O5` baseline and disposition wording).
  - HOLD remains global and unchanged.

- For `backend-cutover-readiness-blockers` Phase 2 specifically:
  - R3 now has a reproducible runbook and documented local execution record in `backend-parity-verification-evidence.md` (auth-critical turbulence procedure + evidence capture format).
  - R3 remains **AMBER/open** pending one controlled live-shadow turbulence rehearsal artifact (owner currently `TBD-ROLLBACK-OWNER`; replacement required via B6 assignment/escalation path).
  - HOLD remains global and unchanged.

- For `backend-cutover-readiness-blockers` Phase 4 specifically:
  - Approval E handling is **preparation-only** (governance packaging + escalation path), not approval grant.
  - HOLD remains mandatory until a separate approval artifact exists under release authority.

- For `backend-cutover-approval-package` final summary specifically:
  - Final package summary source is `docs/repo-curation/approval-package/final-package-summary.md`.
  - Package completion is documentation-preparation only; it does not grant approval and does not authorize cutover.
  - E remains `RED/UNGRANTED` until a separate explicit approval artifact exists.

## Required gate dimensions

### A) Contract parity (API/runtime behavior)
Must prove parity (or approved intentional delta) for:

- `/ping`
- `/health/db`
- `/robots.txt`
- `/sitemap.xml`
- `/auth/verify` behavior under valid/invalid/revoked/failure scenarios
- `/salud` family semantics used by current operations

Acceptance condition:
- Route presence + response-shape/status parity documented with evidence.

### B) Deploy/ops parity
Must prove all runtime surfaces are explicitly and consistently rewired:

- `Dockerfile` entrypoint
- compose runtime behavior under `orthanc-imagiax`
- local start scripts used by operators
- nginx proxy assumptions (`/api/*`, auth_request path, health/seo passthrough)

Acceptance condition:
- No remaining monolith-only operational assumption in active deploy/start paths.

### C) Resilience parity
Must prove startup behavior is at least equivalent to monolith resilience:

- DB retry strategy at startup
- DB reconnect/failure handling during boot races
- auth/critical service readiness behavior under transient DB failure

Acceptance condition:
- Equivalent or stronger resilience guarantees are documented and evidenced.

### D) Rollback evidence
Must provide reversible rollback plan with concrete artifacts before cutover execution.

Acceptance condition:
- Rollback reference exists and is validated for runtime/deploy restoration to `servidor_api_orthanc_v1:app`.

### E) Approval gate
Must have explicit release approval for cutover execution scope.

Acceptance condition:
- Documented approval to lift hold for backend cutover scope.

## Forbidden before gate is GREEN
- Switching container/backend entrypoint to factory runtime.
- Rewiring deploy/runtime scripts to factory as authoritative.
- Deleting or demoting monolith runtime paths required for rollback.
- Treating backend cutover scope as eligible for publication-candidate/backend `main` integration.

## Phase-safe next step
After this re-evaluation batch, the next safe phase is:

1. Close remaining blockers (`C5`, `O5`, `R3`, and rollback assignment/readiness gaps) with explicit evidence.
2. Obtain explicit approval evidence once A/B/C/D are truly GREEN.
3. Keep backend hold active until gate transitions from RED to GREEN.

## Blocking statement (must remain in force)
Until this gate is explicitly GREEN with evidence and approval:
- backend candidate branch work for runtime cutover scope is blocked;
- backend `main` integration for runtime cutover scope is blocked.
