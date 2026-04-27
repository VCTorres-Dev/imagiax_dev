# OHIF Phase 2 Pre-Merge Verification (Two-File Targeted Merge)

Change: `repo-curation-github-sync`
Phase: `2` (pre-merge verification only)
Date: `2026-04-25`

## Scope Guard

- This artifact verifies readiness for a **targeted two-file merge decision** only.
- No application/runtime source files were edited in this phase.
- No build/test pipelines were run.
- Out-of-scope and untouched: `.gitignore`, `OHIF Medical Imaging Viewer/Viewers/.git`, deploy/local script mutations, branch/main migration.

---

## Evidence Commands (Read-Only)

1. `powershell -ExecutionPolicy Bypass -File scripts/repo-curation/verify-ohif-runtime-paths.ps1`
2. `powershell -ExecutionPolicy Bypass -File scripts/repo-curation/measure-ohif-drift.ps1`
3. `git diff --no-index --name-status -- "ohif-extension-imagiax-ai/src" "OHIF Medical Imaging Viewer/Viewers/extensions/imagiax-ai/src"`
4. `Get-FileHash -Algorithm SHA256` on the four target files.
5. Source contract reads/greps:
   - canonical/mirror: `ImagiaxAiPanel.tsx`, `services/imagiaxApi.ts`
   - backend/security: `src/api/auth/router.py`, `src/api/auth/dependencies.py`, `src/api/ai/router.py`, `app/informes/router.py`, `src/infrastructure/pdf_service.py`
   - gateway: `OHIF Medical Imaging Viewer/orthanc-imagiax/config/nginx-proxy.conf`

---

## Goal 1 — Runtime/deploy path coupling still points to nested `Viewers/...`

**Result:** PASS

- `docs/repo-curation/ohif-runtime-path-verification.json` status: `pass`
- Checks: `4`, Missing files: `0`, Pattern mismatches: `0`
- Verified references still point to nested runtime path:
  - `deploy_production.sh` → `OHIF Medical Imaging Viewer/Viewers/extensions/imagiax-ai/src`
  - `start_ohif.bat` → `...\OHIF Medical Imaging Viewer\Viewers`
  - `arrancar_ohif.py` → `...\OHIF Medical Imaging Viewer\Viewers\platform\app`
  - `OHIF Medical Imaging Viewer/orthanc-imagiax/start-imagiax.ps1` → `...\OHIF Medical Imaging Viewer\Viewers\platform\app`

---

## Goal 2 — Two deploy-critical files remain the only meaningful drift for this batch scope

**Result:** PASS (with explicit caveat)

From `docs/repo-curation/ohif-drift-report.json` and `git diff --no-index --name-status`:

- Shared files: `8`
- Hash mismatches: `2`
  - `ImagiaxAiPanel.tsx`
  - `services/imagiaxApi.ts`
- Canonical-only: `1`
  - `pipelineConfig.ts`

Interpretation for this phase:

- The two mismatches are exactly the deploy-critical targeted merge files.
- `pipelineConfig.ts` exists only in canonical, but current verification found **no in-tree imports/usages** in either source tree for this batch path set, so it is treated as **non-blocking / non-meaningful for Phase 2 targeted merge scope**.

---

## Goal 3 — Auth/session safety assumptions still hold

**Result:** PASS

Mirror/runtime side evidence:

- `OHIF .../services/imagiaxApi.ts` uses cookie-session model:
  - `GET /auth/me`
  - `POST /auth/logout`
  - `credentials: 'include'` in auth and API requests

Canonical side still diverges (token/localStorage + `Authorization: Bearer ...`) and therefore remains unsafe to keep as-is for runtime coupling.

Backend/gateway evidence still aligns with mirror assumptions:

- `src/api/auth/router.py` sets/deletes `imagiax_session` cookie and supports `/auth/me` + `/auth/logout`.
- `src/api/auth/dependencies.py` supports cookie-based auth extraction/renewal.
- `OHIF Medical Imaging Viewer/orthanc-imagiax/config/nginx-proxy.conf` retains `auth_request` gate to auth verify endpoint.

Conclusion: keeping mirror auth/session contract remains the safe baseline.

---

## Goal 4 — History/listado and PDF safety assumptions still hold

**Result:** PASS

History/listado:

- Mirror `services/imagiaxApi.ts` calls `/informes/listado` with paginated contract (`limit`, `cursor_id`, `next_cursor`).
- Backend endpoints still expose paginated listing in:
  - `src/api/ai/router.py` (`/listado`, `limit`, `cursor_id`)
  - `app/informes/router.py` (`/informes/listado` pagination contract)

PDF:

- Mirror panel uses backend-authored PDF flow via `downloadInformePdf(informeId)`.
- Backend endpoint still serves report PDF (`/informes/{informe_id}/pdf`) and PDF generation remains server-owned in `src/infrastructure/pdf_service.py`.
- Canonical panel still contains client-side jsPDF generation path, which remains a drift risk if merged blindly.

Conclusion: mirror/backend PDF and history contracts remain aligned and should be preserved.

---

## Goal 5 — Clinical visibility deltas isolated enough for targeted `ImagiaxAiPanel.tsx` merge

**Result:** CONDITIONAL PASS

Observed isolation:

- Clinical visibility/triage prominence deltas are concentrated inside `ImagiaxAiPanel.tsx` presentation blocks.
- API transport/auth/history/PDF contract deltas are concentrated inside `services/imagiaxApi.ts`.

Why this is conditional:

- Mirror currently lacks the high-visibility triage banner/hallazgos prominence seen in canonical.
- Any merge that accidentally reintroduces canonical token/localStorage auth or client-side PDF path is unsafe.

Decision for next phase: a **proof-gated targeted merge across exactly these two files is plausible**, provided abort conditions (below) are enforced.

---

## Goal 6 — Explicit abort conditions for next implementation phase

If any condition below occurs, status MUST be `ABORT/HOLD` and rollback plan applied:

1. Runtime/deploy path verification fails (any mismatch away from nested `Viewers/...`).
2. Drift scope expands beyond the two target files in a way that is required for correctness (third-file dependency becomes mandatory).
3. Merge candidate reintroduces token/localStorage auth model for runtime path (instead of cookie-session `credentials: 'include'` + `/auth/me` + `/auth/logout`).
4. Merge candidate breaks backend-paginated history contract (`limit/cursor_id/next_cursor`) or replaces backend PDF with client-generated PDF as source of truth.
5. Any change is required in blocked boundaries for this phase: `.gitignore`, nested `Viewers/.git`, deploy/local scripts, branch/main migration.
6. Unresolved API/prop-shape drift appears that cannot be resolved inside only `ImagiaxAiPanel.tsx` + `services/imagiaxApi.ts`.

---

## Two-File Rollback Evidence (Pre-Merge Snapshot)

Reference context:

- Branch: `backup/pre-audit-2026-04-13`
- HEAD short: `75423a1`

Pre-merge SHA256:

- Canonical
  - `ohif-extension-imagiax-ai/src/ImagiaxAiPanel.tsx`
    `940730DF53E95DAC5FC7D645F018B9AFD2761A800FEC8D8D06786BF15ABC899D`
  - `ohif-extension-imagiax-ai/src/services/imagiaxApi.ts`
    `041B3B97538BBF3B1CD0C2729C0CCD2653F7C11F751ECC8B97606110C1B037AE`
- Mirror
  - `OHIF Medical Imaging Viewer/Viewers/extensions/imagiax-ai/src/ImagiaxAiPanel.tsx`
    `A438D53E1DB005099C02653B3301493B8EFDB7C2EE50071F39AB4E2EF8664815`
  - `OHIF Medical Imaging Viewer/Viewers/extensions/imagiax-ai/src/services/imagiaxApi.ts`
    `DC56901738F0B8477F17CBC290C1ADDA5E3C37A8AA1C54A6AD185C6974B8A9D0`

Restore plan (if merge phase aborts):

- Use git checkout/restore for only these two canonical files from pre-merge reference.
- Re-run:
  - `scripts/repo-curation/measure-ohif-drift.ps1`
  - `scripts/repo-curation/verify-ohif-runtime-paths.ps1`
- Confirm hashes match this snapshot before leaving HOLD.

---

## Phase Decision

**Decision:** `HOLD-READY` (not auto-merge)

- Verification supports moving to the **next guarded implementation phase** (two-file merge only).
- Execution remains blocked unless all abort conditions are actively enforced during merge.
