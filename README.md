# IMAGIAX — Local Share Onboarding (Internal Only)

> **INTERNAL / NON-PRODUCTION**: this README is for coworker local dev/demo onboarding only.
> It documents current verified behavior (including blockers) and must not be used as a production/deploy runbook.

---

## Scope of this README

This onboarding package is intentionally limited to local sharing and reproducible dev/demo startup.

- ✅ In scope: local launcher flow, local service checkpoints, known local blockers, troubleshooting.
- ❌ Out of scope: deploy/prod hosting changes and operational procedures.
- ❌ Out of scope: clinical AI core changes (AI engine, prompts, pathology logic, model behavior, inference pipeline).

Guardrails reference: [`docs/onboarding/local-share-guardrails.md`](docs/onboarding/local-share-guardrails.md)

---

## Source of truth (evidence-first)

All claims in this README are constrained to:

1. Verified baseline evidence: [`docs/onboarding/local-baseline-verification.md`](docs/onboarding/local-baseline-verification.md)
2. Current repository launcher/runtime files (`INICIAR-IMAGIAX.bat`, `start-imagiax.ps1`, `upload-dicoms.ps1`, local `docker-compose.yml`)

If a claim cannot be tied to evidence (EV-* references), it is excluded.

---

## Project purpose (for this onboarding slice)

Enable teammates to run the **current local IMAGIAX stack state** for internal dev/demo and understand exactly what is working vs currently blocked.

Current-state runtime map: [`docs/onboarding/local-runtime-flow.md`](docs/onboarding/local-runtime-flow.md)

---

## Verified prerequisites (Windows machine used in baseline)

| Requirement | Evidence | Status |
|---|---|---|
| Python 3.11 available (`py --version`) | Baseline §3 prerequisites table | ✅ Verified (`3.11.9`) |
| Yarn available (`yarn --version`) | Baseline §3 prerequisites table | ✅ Verified (`1.22.22`) |
| Docker CLI installed | Baseline §3 prerequisites table | ✅ Verified |
| Docker daemon may need startup time | EV-002 startup output | ⚠️ Observed (auto-start attempted by launcher) |

Evidence document: [`local-baseline-verification.md`](docs/onboarding/local-baseline-verification.md)

---

## Golden path (ordered local startup)

### 1) Run the top-level launcher from repo root

```powershell
& ".\INICIAR-IMAGIAX.bat"
```

Expected immediate behavior (verified):

- Launcher starts and delegates to `start-imagiax.ps1` without the prior parenthesis-path parse crash (EV-010).

### 2) Let `start-imagiax.ps1` execute its sequence

Script order in current repo:

1. Docker engine check/start
2. Orthanc container startup check
3. PostgreSQL check
4. OHIF startup (unless skipped)
5. FastAPI `/ping` liveness check/start (unless skipped)

### 3) Manual checkpoints after launch attempt

Use these to classify the run:

| Checkpoint | Command / URL | Expected now |
|---|---|---|
| FastAPI liveness | `docker exec fastapi-imagiax ... /ping` | `200` when container is alive (EV-006, EV-012) |
| FastAPI `/salud` | `docker exec fastapi-imagiax ... /salud` | `401` (auth-protected, not public liveness) (EV-006, EV-012) |
| Orthanc UI | `http://localhost:8042` | May fail while `4242` bind issue persists (EV-002) |
| OHIF | `http://localhost:3000` | Not reachable when Orthanc startup is blocked (EV-003) |
| PostgreSQL readiness | `docker exec postgres-imagiax psql ... "select 1;"` | Returns `1` (EV-007) |

### 4) Optional dataset dry-run check

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "OHIF Medical Imaging Viewer\orthanc-imagiax\upload-dicoms.ps1" -DryRun
```

Expected now:

- Default sample paths resolve relative to current repository root (EV-011).
- Upload remains blocked if Orthanc is not reachable (EV-008, EV-002).

---

## Verified current state summary

### Verified working

- `INICIAR-IMAGIAX.bat` parse blocker fixed for paths with parentheses (EV-010).
- PostgreSQL container readiness query works (`select 1`) (EV-007).
- FastAPI internal liveness endpoint `/ping` returns 200 (EV-006, EV-012).
- Upload script defaults are now repo-relative instead of machine-hardcoded (EV-011).

### Verified failing / blocked

- Orthanc host bind may fail on `0.0.0.0:4242` in this environment (EV-002).
- OHIF host availability remains blocked in the failed Orthanc path (EV-003).
- Full end-to-end local startup is not yet verified as successful on this baseline machine (EV-002, EV-003).

### Out of scope for onboarding changes

- Any production/deploy behavior or hosting configuration.
- Any clinical AI output behavior changes.

---

## Troubleshooting (known blockers)

Detailed limitations: [`docs/onboarding/local-current-limitations.md`](docs/onboarding/local-current-limitations.md)

Quick triage list:

1. **Orthanc bind failure on port 4242**
   - Symptom: Docker error exposing `0.0.0.0:4242` (EV-002).
   - Impact: Orthanc down → nginx/stack instability and OHIF unavailable in this run path (EV-005, EV-003).

2. **Health endpoint confusion (`/salud` vs `/ping`)**
   - `/salud` currently returns 401 without auth (EV-006).
   - Use `/ping` for liveness checks (EV-006, EV-012).

3. **Upload script not completing end-to-end**
   - Even with fixed default paths, upload cannot complete while Orthanc is unreachable (EV-008, EV-011).

---

## What this README does NOT claim

- It does **not** claim production readiness.
- It does **not** claim coworker end-to-end success is already achieved on every machine.
- It does **not** claim Orthanc bind blocker is resolved.

---

## Companion onboarding docs

- Baseline evidence ledger: [`docs/onboarding/local-baseline-verification.md`](docs/onboarding/local-baseline-verification.md)
- Scope guardrails / hold list: [`docs/onboarding/local-share-guardrails.md`](docs/onboarding/local-share-guardrails.md)
- Runtime flow diagram (current state): [`docs/onboarding/local-runtime-flow.md`](docs/onboarding/local-runtime-flow.md)
- Current limitations + operator troubleshooting: [`docs/onboarding/local-current-limitations.md`](docs/onboarding/local-current-limitations.md)
