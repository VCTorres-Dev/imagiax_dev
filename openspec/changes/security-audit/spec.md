# Security Audit Remediation Specification

## Cross-Cutting Requirements

### Requirement: REQ-SEC-AUDIT-001 (Audit Logs)
- **Priority:** P0 (Critical)
- **Requirement:** All security fixes MUST include audit log entries for relevant actions.
- **Acceptance Criteria:**
  - Given a security-critical action (e.g., payment, auth failure, proxy access), When the action occurs, Then an audit log entry is written.
- **Affected Files:** `servidor_api_orthanc_v1.py`, `pagos_mp.py`, `pagos_tbk.py`
- **Constraints:** Audit logging must not block the main execution thread or fail the primary request.

### Requirement: REQ-SEC-TEST-001 (Regression Testing)
- **Priority:** P1 (High)
- **Requirement:** Each fix MUST have a documented regression test plan.
- **Acceptance Criteria:**
  - Given a fix, When deployed, Then it can be manually or automatically verified using the test plan.
- **Affected Files:** N/A (Documentation)
- **Constraints:** Must be executable without pytest.

### Requirement: REQ-SEC-ROLLBACK-001 (Independent Phases)
- **Priority:** P1 (High)
- **Requirement:** Each phase MUST be independently deployable and rollbackable.
- **Acceptance Criteria:**
  - Given Phase 1 is deployed, When Phase 2 is rolled back, Then Phase 1 remains functional.
- **Affected Files:** All modified files.
- **Constraints:** No hard dependencies between phases.

### Requirement: REQ-SEC-COMPAT-001 (API Compatibility)
- **Priority:** P0 (Critical)
- **Requirement:** Security fixes MUST NOT introduce breaking changes to existing API contracts consumed by the frontend.
- **Acceptance Criteria:**
  - Given a security fix is applied, When the frontend calls the API, Then the request succeeds without modification to the frontend.
- **Affected Files:** All API endpoints.
- **Constraints:** Response schemas must remain backward compatible.

---

## Phase 1 — CRITICAL (P0)

### Requirement: REQ-SEC-001 (Path Traversal in DICOMweb proxy)
- **Maps to:** SEC-BE-003
- **Priority:** P0
- **Requirement:** The proxy MUST reject paths containing `..` or attempting to access outside `/dicom-web/`.
- **Acceptance Criteria:**
  - Given a proxy request with `../`, When processed, Then return 400 Bad Request.
- **Affected Files:** `servidor_api_orthanc_v1.py`
- **Constraints:** Valid DICOMweb paths must still proxy correctly.

### Requirement: REQ-SEC-002 (IDOR in /tbk/cobrar)
- **Maps to:** SEC-PAY-003
- **Priority:** P0
- **Requirement:** The endpoint MUST use the authenticated user's ID from the JWT, not the `username` in the request body.
- **Acceptance Criteria:**
  - Given a `POST /tbk/cobrar` request, When processed, Then the charge is applied only to the authenticated user's card.
- **Affected Files:** `pagos_tbk.py`
- **Constraints:** API signature must be backward compatible if possible, or gracefully ignore the `username` field.

### Requirement: REQ-SEC-003 (Client-controlled charge amount)
- **Maps to:** SEC-PAY-004
- **Priority:** P0
- **Requirement:** The charge amount MUST be determined server-side (e.g., `PRECIO_SCAN_CLP`), ignoring the `amount` field in the request.
- **Acceptance Criteria:**
  - Given a request with `amount: 1`, When processed, Then the user is charged the official server-side price.
- **Affected Files:** `pagos_tbk.py`
- **Constraints:** Legitimate charges must process for the correct amount.

### Requirement: REQ-SEC-004 (/pagos/exito without auth)
- **Maps to:** SEC-PAY-002
- **Priority:** P0
- **Requirement:** The callback MUST require a valid user session.
- **Acceptance Criteria:**
  - Given an unauthenticated GET to `/pagos/exito`, When processed, Then return 401 Unauthorized.
- **Affected Files:** `pagos_mp.py`
- **Constraints:** Must successfully redirect authenticated users.

### Requirement: REQ-SEC-005 (MP Webhook bypass)
- **Maps to:** SEC-PAY-001
- **Priority:** P0
- **Requirement:** MP webhooks MUST validate the HMAC signature using `MP_WEBHOOK_SECRET` in all environments.
- **Acceptance Criteria:**
  - Given a webhook payload with an invalid signature in development, When processed, Then return 401/403.
- **Affected Files:** `pagos_mp.py`
- **Constraints:** Must not reject valid production webhooks.

### Requirement: REQ-SEC-006 (Proxy revoked token bypass)
- **Maps to:** SEC-BE-002
- **Priority:** P0
- **Requirement:** The proxy MUST fully validate the JWT, including checking against the revocation list.
- **Acceptance Criteria:**
  - Given a revoked token, When accessing the proxy, Then return 401 Unauthorized.
- **Affected Files:** `servidor_api_orthanc_v1.py`
- **Constraints:** Valid tokens must continue to proxy correctly.

### Requirement: REQ-SEC-007 (/health/db info leak)
- **Maps to:** SEC-BE-001
- **Priority:** P0
- **Requirement:** The health endpoint MUST NOT leak internal database details or topology in error responses.
- **Acceptance Criteria:**
  - Given a database connection failure, When calling `/health/db`, Then the response only indicates failure without exposing the exception string.
- **Affected Files:** `servidor_api_orthanc_v1.py`
- **Constraints:** Must still return 200 OK when healthy.

---

## Phase 2 — HIGH (P1)

### Requirement: REQ-SEC-008 (TOCTOU race condition)
- **Maps to:** SEC-PAY-005
- **Priority:** P1
- **Requirement:** Payment crediting MUST use an atomic database transaction.
- **Acceptance Criteria:**
  - Given two concurrent webhook requests for the same payment, When processed, Then the user is credited only once.
- **Affected Files:** `imagiax_pagos_db.py`
- **Constraints:** Must not block non-payment database queries.

### Requirement: REQ-SEC-009 (_IS_TEST flag from token prefix)
- **Maps to:** SEC-PAY-006
- **Priority:** P1
- **Requirement:** The system MUST use an explicit environment variable (`IMAGIAX_PAYMENT_MODE`) to determine test mode.
- **Acceptance Criteria:**
  - Given `IMAGIAX_PAYMENT_MODE=production`, When a test token is used, Then the system treats it as a production error, not a test.
- **Affected Files:** `pagos_mp.py`, `pagos_tbk.py`
- **Constraints:** Must fail fast on startup if configuration is contradictory.

### Requirement: REQ-SEC-010 (Test mode auto-approval)
- **Maps to:** SEC-PAY-007
- **Priority:** P1
- **Requirement:** Production environments MUST NOT allow auto-approval of charges, regardless of TBK settings.
- **Acceptance Criteria:**
  - Given `APP_ENV=production` and `TBK_ENVIRONMENT=integration`, When the app starts, Then it raises a RuntimeError.
- **Affected Files:** `pagos_tbk.py`
- **Constraints:** Must allow auto-approval in true development environments.

### Requirement: REQ-SEC-011 (Plaintext production credentials)
- **Maps to:** SEC-PAY-008
- **Priority:** P1
- **Requirement:** Production credentials MUST be managed securely and not stored in plaintext files in the repository.
- **Acceptance Criteria:**
  - Given the source code, When inspected, Then no production credentials or predictable passwords are present.
- **Affected Files:** `.env.production`
- **Constraints:** Must ensure deployment still injects variables correctly.

### Requirement: REQ-SEC-012 (DoS via massive inference)
- **Maps to:** SEC-AI-001
- **Priority:** P1
- **Requirement:** The `max_instances` limit MUST be reduced (e.g., to 50) and rate-limited.
- **Acceptance Criteria:**
  - Given a request for 500 instances, When processed, Then return 400 Bad Request.
- **Affected Files:** `servidor_api_orthanc_v1.py`
- **Constraints:** Normal studies (<=50 instances) must process successfully.

### Requirement: REQ-SEC-013 (Synchronous TBK SDK)
- **Maps to:** SEC-AI-002
- **Priority:** P1
- **Requirement:** Transbank SDK calls MUST be executed in a thread pool executor to avoid blocking the async event loop.
- **Acceptance Criteria:**
  - Given a pending TBK SDK call, When executing, Then other async requests to the server succeed concurrently.
- **Affected Files:** `pagos_tbk.py`
- **Constraints:** SDK behavior must remain identical.

### Requirement: REQ-SEC-014 (Shared Gemini key)
- **Maps to:** SEC-INFRA-001
- **Priority:** P1
- **Requirement:** Dev and Prod MUST use different Gemini API keys.
- **Acceptance Criteria:**
  - Given a dev environment, When querying Gemini, Then it uses a key distinct from the prod key.
- **Affected Files:** `.env`, `.env.production`
- **Constraints:** Dev keys must be provisioned.

### Requirement: REQ-SEC-015 (Predictable admin password)
- **Maps to:** SEC-INFRA-002
- **Priority:** P1
- **Requirement:** The default admin password MUST be a cryptographically secure random string.
- **Acceptance Criteria:**
  - Given the default configuration, When checking the admin password, Then it is at least 32 characters of high entropy.
- **Affected Files:** `.env.production`
- **Constraints:** Admins must be able to securely retrieve the new password during setup.

---

## Phase 3 — MEDIUM (P2)

### Requirement: REQ-SEC-016 (PHI exposure without role isolation)
- **Maps to:** SEC-BE-006
- **Priority:** P2
- **Requirement:** `ver_informe()` MUST verify the user has the `medico` or `admin` role, or owns the report.
- **Acceptance Criteria:**
  - Given a `recepcion` user, When attempting to view a report, Then return 403 Forbidden.
- **Affected Files:** `servidor_api_orthanc_v1.py`

### Requirement: REQ-SEC-017 (No CSP)
- **Maps to:** SEC-BE-007
- **Priority:** P2
- **Requirement:** The application MUST return a baseline Content-Security-Policy header.
- **Acceptance Criteria:**
  - Given any HTTP response, When inspected, Then it includes a `Content-Security-Policy` header.
- **Affected Files:** `servidor_api_orthanc_v1.py`

### Requirement: REQ-SEC-018 (Card data in raw webhooks)
- **Maps to:** SEC-PAY-009
- **Priority:** P2
- **Requirement:** The system MUST NOT persist raw webhook responses containing PCI-DSS sensitive data.
- **Acceptance Criteria:**
  - Given a webhook containing card data, When saved to DB, Then the card data is masked or omitted.
- **Affected Files:** `imagiax_pagos_db.py`

### Requirement: REQ-SEC-019 (Exception in TBK redirect URL)
- **Maps to:** SEC-AI-003
- **Priority:** P2
- **Requirement:** TBK errors MUST be masked in the redirect URL using an opaque error code.
- **Acceptance Criteria:**
  - Given a TBK exception, When redirecting, Then the URL contains `msg=tbk_auth_failed` instead of the exception string.
- **Affected Files:** `pagos_tbk.py`

### Requirement: REQ-SEC-020 (CORS wildcard validation)
- **Maps to:** SEC-BE-008
- **Priority:** P2
- **Requirement:** The server MUST raise an error on startup if `CORS_ORIGINS` contains `*` while `allow_credentials=True`.
- **Acceptance Criteria:**
  - Given `CORS_ORIGINS=["*"]`, When starting the server, Then it crashes with a clear validation error.
- **Affected Files:** `servidor_api_orthanc_v1.py`

### Requirement: REQ-SEC-021 (Mass PHI extraction via listado)
- **Maps to:** SEC-BE-009
- **Priority:** P2
- **Requirement:** `/informes/listado` MUST enforce a maximum limit of 50 records and implement rate limiting.
- **Acceptance Criteria:**
  - Given a request for `limit=500`, When processed, Then it returns a 400 Bad Request or caps at 50.
- **Affected Files:** `servidor_api_orthanc_v1.py`

### Requirement: REQ-SEC-022 (Malformed external_reference default)
- **Maps to:** SEC-PAY-010
- **Priority:** P2
- **Requirement:** Webhooks MUST explicitly validate `external_reference` and fail safely if malformed.
- **Acceptance Criteria:**
  - Given a webhook with missing `external_reference`, When processed, Then it logs an error and does not credit a default package.
- **Affected Files:** `pagos_mp.py`

### Requirement: REQ-SEC-023 (X-Forwarded-For spoofing)
- **Maps to:** SEC-INFRA-003
- **Priority:** P2
- **Requirement:** The application MUST validate the presence of a trusted proxy in production before trusting `X-Forwarded-For`.
- **Acceptance Criteria:**
  - Given a request directly to Uvicorn with a spoofed header in production, When rate limiting applies, Then it ignores the spoofed IP.
- **Affected Files:** `servidor_api_orthanc_v1.py`, `Dockerfile`

---

## Phase 4 — LOW (P3)

### Requirement: REQ-SEC-024 (Low Severity Findings)
- **Priority:** P3
- **Requirement:** Apply defense-in-depth improvements for SEC-AI-004, SEC-PAY-011, SEC-AI-005, SEC-BE-010, SEC-BE-011, SEC-BE-012, SEC-INFRA-004.
- **Acceptance Criteria:**
  - Given the codebase, When audited, Then input size limits, secure defaults, and infrastructure definitions (docker-compose) are present.
- **Affected Files:** Various.
