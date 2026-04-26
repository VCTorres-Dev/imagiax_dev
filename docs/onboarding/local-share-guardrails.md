# Local Share Guardrails — `local-share-onboarding-package`

**Project:** `imagiax_dev`  
**Change:** `local-share-onboarding-package`  
**Scope slice:** Phase 2 — Protected-boundary documentation  
**Classification:** **NON-PRODUCTION / INTERNAL ONLY**

---

## 1) Why this document exists

This guardrail defines the hard boundaries for onboarding work so future slices cannot drift into deploy/prod or clinical AI-core surfaces.

Reference evidence baseline: [`docs/onboarding/local-baseline-verification.md`](./local-baseline-verification.md)

---

## 2) Explicit allowlist for this onboarding effort

## 2.1 Change-level allowlist (all phases, still constrained)

- `README.md` (onboarding instructions only)
- `docs/onboarding/*` (verification, guardrails, runtime flow, troubleshooting)
- Optional local launcher scripts **only if** a Phase 1/Phase 5 blocker is reproduced and documented:
  - `INICIAR-IMAGIAX.bat`
  - `OHIF Medical Imaging Viewer/orthanc-imagiax/start-imagiax.ps1`
  - `OHIF Medical Imaging Viewer/orthanc-imagiax/upload-dicoms.ps1`

## 2.2 Phase 2 (this slice) effective allowlist

- `docs/onboarding/local-share-guardrails.md` (this file)
- Optional minimal cross-link in `docs/onboarding/local-baseline-verification.md` only if needed for navigation consistency

---

## 3) HOLD / protected surfaces — deploy, hosting, production

Any edit in these surfaces is **FORBIDDEN** for this change unless scope is explicitly re-opened in a new approved change.

### Protected files and categories

- Deploy scripts and ops entrypoints
  - `deploy.sh`
  - `deploy_production.sh`
- Nginx/proxy/runtime hosting config
  - `OHIF Medical Imaging Viewer/orthanc-imagiax/config/nginx-proxy.conf`
  - `OHIF Medical Imaging Viewer/orthanc-imagiax/config/nginx-rate-zones.conf`
  - any nginx prod proxy config under hosting paths
- Runtime container/deploy definitions
  - `Dockerfile`
  - `OHIF Medical Imaging Viewer/orthanc-imagiax/docker-compose.prod.yml`
  - `OHIF Medical Imaging Viewer/orthanc-imagiax/docker-compose.prod.PRODUCTION.yml`
  - Any prod hosting compose/orchestration file
- Production start/runbook scripts
  - any production startup/shutdown/maintenance scripts or runbooks

### Forbidden edit classes in deploy/prod surfaces

- Changing ports, domains, reverse-proxy upstreams, TLS, routing, or rate-limits
- Changing production compose services, image tags, volume mappings, env loading, or startup order
- Changing deploy automation commands, SSH targets, rollout/rollback semantics
- Any change that could alter current hosting behavior

---

## 4) HOLD / protected surfaces — clinical AI core

The clinical AI core is **ABSOLUTELY PROTECTED** and out of scope.

### Protected categories

- AI engine code
- Prompt definitions/orchestration
- Pathology recognition logic
- Model behavior
- Inference pipeline

### Known protected file examples

- `app/ai/prompt_orchestrator.py`
- Any file under AI/prompt/pathology/inference areas

### Forbidden edit classes in AI-core surfaces

- Prompt text or orchestration policy changes
- Model selection/dispatch/threshold behavior changes
- Diagnostic/pathology logic edits
- Inference request/response flow changes
- Any code change that can affect clinical output behavior

---

## 5) Allowed vs forbidden edit examples

## Allowed examples

- Add onboarding documentation with verified evidence links
- Add explicit HOLD notes and internal-only warnings in onboarding docs
- Add a current-state runtime flow diagram derived from validated baseline evidence
- (Later phases only) minimal local launcher path portability fix after blocker reproduction evidence

## Forbidden examples

- “Quick fix” in nginx config to make local flow easier
- Editing prod compose or Dockerfile for onboarding convenience
- Updating deploy scripts to support coworker local installs
- Tweaking prompt/inference logic to simplify demo behavior

---

## 6) Review checklist (audit-ready)

Use this checklist before merging any onboarding slice in this change.

| Check | Protected mapping | Forbidden class to detect | Pass criteria |
|---|---|---|---|
| NON-PRODUCTION label present | All onboarding docs | Missing internal-only warning | Every onboarding doc explicitly states internal-only/non-production |
| Deploy scripts untouched | `deploy.sh`, `deploy_production.sh` | Any line-level edit | No diff in deploy scripts |
| Hosting/runtime config untouched | nginx config, `Dockerfile`, prod compose files | Routing/ports/env/service edits | No diff in protected hosting/runtime config |
| AI-core untouched | AI engine/prompt/pathology/inference surfaces | Any code/content edit | No diff in AI-core surfaces |
| Candidate file list in-scope | Proposed change files | Out-of-allowlist file appears | Every candidate file is allowlisted or explicitly HOLD/rejected |
| Evidence-first discipline | README/diagram claims | Claims without EV trace | Claims map to baseline evidence IDs |

---

## 7) Candidate file list validation for this onboarding change

Validation basis:
- Proposal affected areas
- Tasks phase definitions
- Phase 1 baseline artifact

| Candidate file/surface | Classification by guardrails | Phase 2 decision |
|---|---|---|
| `docs/onboarding/local-share-guardrails.md` | ✅ Allowlist (Phase 2) | **ALLOWED NOW** |
| `docs/onboarding/local-baseline-verification.md` | ✅ Allowlist (docs) | Allowed, but no change required in this slice |
| `README.md` | ✅ Allowlist (future phase) | HOLD for Phase 4 |
| `docs/onboarding/local-runtime-flow.md` (future) | ✅ Allowlist (future phase) | HOLD for Phase 4 |
| `INICIAR-IMAGIAX.bat` | ⚠️ Conditional allowlist | HOLD unless blocker fix is explicitly approved in Phase 3 |
| `OHIF Medical Imaging Viewer/orthanc-imagiax/start-imagiax.ps1` | ⚠️ Conditional allowlist | HOLD unless blocker fix is explicitly approved in Phase 3 |
| `OHIF Medical Imaging Viewer/orthanc-imagiax/upload-dicoms.ps1` | ⚠️ Conditional allowlist | HOLD unless blocker fix is explicitly approved in Phase 3 |
| Deploy/hosting/prod surfaces (Section 3) | ⛔ Protected HOLD | FORBIDDEN |
| AI-core surfaces (Section 4) | ⛔ Protected HOLD | FORBIDDEN |

## Currently allowed files for this specific Phase 2 execution

- `docs/onboarding/local-share-guardrails.md`

Result: **PASS** — candidate list is scoped, protected categories are explicit, and this slice remains docs-only.

---

## 8) Phase 2 boundary audit result (this actual slice)

- Changed files in this slice:
  - `docs/onboarding/local-share-guardrails.md` (created)
- Protected deploy/prod files edited: **NO**
- Protected AI-core files edited: **NO**

Final statement: This slice complies with internal-share-only boundaries and introduces no runtime/deploy/AI-core behavior changes.
