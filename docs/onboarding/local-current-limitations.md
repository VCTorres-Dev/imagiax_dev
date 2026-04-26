# Local Current Limitations & Troubleshooting

**Classification:** INTERNAL / NON-PRODUCTION  
**Purpose:** keep README concise while preserving actionable blocker handling for coworkers.

Primary evidence source: [`local-baseline-verification.md`](./local-baseline-verification.md)

---

## 1) Known current limitations (verified)

### L-01 — Orthanc bind blocker on port 4242

- **Symptom:** startup reports Docker bind error exposing `0.0.0.0:4242`.
- **Evidence:** EV-002.
- **Impact:** Orthanc unavailable; OHIF path can stay down; nginx container becomes unstable due to missing Orthanc upstream (EV-003, EV-005).
- **Status:** unresolved in this onboarding phase.

### L-02 — `/salud` is not a public health check

- **Symptom:** `/salud` returns `401` without auth.
- **Evidence:** EV-006, EV-012.
- **Impact:** false negatives if used as startup liveness endpoint.
- **Current handling:** launcher now checks `/ping` for liveness in local startup flow.

### L-03 — DICOM upload end-to-end depends on Orthanc availability

- **Symptom:** `upload-dicoms.ps1` cannot complete when Orthanc is down.
- **Evidence:** EV-008.
- **Impact:** demo dataset preload is blocked until Orthanc is reachable.
- **Current handling:** path portability improved (repo-relative defaults, EV-011), but Orthanc availability remains prerequisite.

---

## 2) Quick troubleshooting checklist

1. Run launcher from repo root:

   ```powershell
   & ".\INICIAR-IMAGIAX.bat"
   ```

2. If startup stops at Orthanc stage, capture the exact Docker bind error text and compare with EV-002 pattern.

3. Verify partial service state:

   ```powershell
   docker compose -f "OHIF Medical Imaging Viewer/orthanc-imagiax/docker-compose.yml" ps
   docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
   ```

4. Verify FastAPI liveness correctly:

   ```powershell
   docker exec fastapi-imagiax sh -lc "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8010/ping"
   ```

5. Do **not** use `/salud` as a no-auth liveness check in onboarding troubleshooting.

---

## 3) Escalation notes

- If `4242` bind issue persists, treat the run as known-blocked baseline, not as operator error.
- Keep evidence-first reporting (timestamp + command + exact output) to avoid drift in onboarding docs.
- Do not modify deploy/prod or AI-core surfaces as a workaround; that violates this change scope.
