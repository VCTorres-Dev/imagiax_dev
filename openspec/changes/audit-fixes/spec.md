# Specification: audit-fixes

## Overview
This document outlines the acceptance criteria and testable requirements for fixing 14 security and stability issues discovered during the IMAGIAX platform audit.

## Constraint: CRLF & Python Files
**CRITICAL:** Most Python files in this project use CRLF (`\r\n`) line endings. Standard file edit tools often fail to correctly match or insert content, breaking the codebase. **All Python file modifications during implementation must be done using a Python script reading/writing raw bytes to ensure line endings are preserved.**

---

## Requirements and Acceptance Criteria

### Critical Priority

#### C-001: Secure Analysis and Results Endpoints
*   **Description**: The `/analizar/*` and `/resultados/*` endpoints lack authentication, allowing unauthorized access to Protected Health Information (PHI).
*   **Acceptance Criteria**:
    *   The `get_current_user` dependency is added to all routes under `/analizar/` and `/resultados/`.
    *   Requests without a valid token return a `401 Unauthorized`.
*   **Files Affected**: `main.py` or relevant router files.

#### C-002: Fix SQL Injection Vulnerability in `guardar_informe`
*   **Description**: `guardar_informe()` in the database module uses string interpolation or broken placeholders, creating an SQL injection risk.
*   **Acceptance Criteria**:
    *   The SQL execution in `guardar_informe()` correctly uses the driver's parameterized query syntax (e.g., `$1, $2` or `%s`).
    *   User inputs are never directly concatenated into the SQL string.
*   **Files Affected**: `imagiax_db.py`

#### C-003: Persistent Login Brute-Force Tracking
*   **Description**: Login attempts are currently tracked in an in-memory dictionary, which is lost on restart and does not scale across workers.
*   **Acceptance Criteria**:
    *   Failed login attempts are recorded in PostgreSQL instead of memory.
    *   Rate limiting applies based on DB records (e.g., max 5 failures per 15 mins).
*   **Files Affected**: `auth.py`, `imagiax_db.py`

#### C-004: Prevent Cross-User PHI Access (Listings)
*   **Description**: The `/informes/listado` and `/informes/{id}/ver` endpoints do not filter by the authenticated user's ID, exposing all patient data.
*   **Acceptance Criteria**:
    *   `/informes/listado` returns only the reports belonging to `current_user.id`.
*   **Files Affected**: `main.py`, `imagiax_db.py`

---

### High Priority

#### H-001: Non-blocking AI Inference
*   **Description**: PyTorch/Gemini AI inference runs synchronously, blocking the FastAPI event loop.
*   **Acceptance Criteria**:
    *   AI inference function calls are wrapped in `asyncio.get_event_loop().run_in_executor(None, func, *args)`.
*   **Files Affected**: `ai_service.py` / `main.py`

#### H-002: Non-blocking Transbank Deletion
*   **Description**: Transbank `inscription.delete()` is a synchronous network call blocking the event loop.
*   **Acceptance Criteria**:
    *   The deletion call is wrapped in `asyncio.get_event_loop().run_in_executor`.
*   **Files Affected**: `transbank_service.py` / `main.py`

#### H-003: Docker Container Memory Limits
*   **Description**: Containers lack memory constraints, risking OOM kills on the 4GB RAM DigitalOcean droplet.
*   **Acceptance Criteria**:
    *   `docker-compose.prod.yml` defines `deploy.resources.limits.memory` for backend, db, and frontend services.
*   **Files Affected**: `docker-compose.prod.yml`

#### H-004: Fallback Flag and Sanitization in AI Reports
*   **Description**: Fallback reports (when AI fails) lack explicit flagging and may contain raw AI system language.
*   **Acceptance Criteria**:
    *   The API response for reports includes an `is_fallback: boolean` field.
    *   Fallback text is passed through the `stripAILanguage()` function before saving/returning.
*   **Files Affected**: `ai_service.py`, `main.py`

#### H-005: IDOR Fix on Report Viewing
*   **Description**: Viewing a specific report via `/informes/{id}/ver` does not check if the report belongs to the requester.
*   **Acceptance Criteria**:
    *   The endpoint queries the database verifying `report.user_id == current_user.id`.
    *   Returns `403 Forbidden` or `404 Not Found` if the ownership check fails.
*   **Files Affected**: `main.py`, `imagiax_db.py`

#### H-006: Fix CORS/Cookies in OHIF Panel Config Fetch
*   **Description**: The `loadBackendConfig()` fetch call in the OHIF panel lacks credentials, preventing cookie-based session verification.
*   **Acceptance Criteria**:
    *   `fetch` options for `loadBackendConfig()` include `credentials: 'include'`.
*   **Files Affected**: `ImagiaxAiPanel.tsx`

#### H-007: Remove Username Fallback in Transbank Flow
*   **Description**: The Transbank inscription flow falls back to a default/hardcoded username if none is found, which is a security risk.
*   **Acceptance Criteria**:
    *   The username fallback is completely removed; the flow fails gracefully if the user is unidentified.
*   **Files Affected**: `transbank_service.py` / `main.py`

---

### Medium Priority

#### M-002: Clean up `_general_limiter`
*   **Description**: Dead rate-limiting code exists.
*   **Acceptance Criteria**:
    *   The unused `_general_limiter` is completely removed from the codebase.
*   **Files Affected**: `main.py` / `auth.py`

#### M-008: Secure PHI History Storage (Frontend)
*   **Description**: Patient history is stored in `localStorage`, persisting indefinitely.
*   **Acceptance Criteria**:
    *   All references to saving/loading PHI history are migrated from `localStorage` to `sessionStorage`.
*   **Files Affected**: Relevant React/TS files (e.g., `history.ts` or components).

#### M-009: Add Credentials to API V2 Requests
*   **Description**: Generic API V2 requests lack credentials configuration.
*   **Acceptance Criteria**:
    *   `apiV2Request` helper function is updated to append `credentials: 'include'` by default.
*   **Files Affected**: `imagiaxApi.ts`
