# Security Specifications — IMAGIAX

_Last updated: 2026-04-04 | Sources: comprehensive-hardening, verification-fixes_

---

## CORS Hardening

The backend API MUST strictly restrict CORS methods and headers to prevent unauthorized cross-origin access.
- **Verification method**: `curl -X OPTIONS -H "Origin: https://imagiax.dev" -I https://imagiax.dev/api/...`
- **Affected Files**: `servidor_api_orthanc_v1.py`

### Scenario: Preflight options request
- GIVEN an OPTIONS request from an allowed origin
- WHEN the request asks for allowed methods
- THEN the response MUST ONLY allow GET, POST, PUT, DELETE, PATCH, and OPTIONS
- AND the allowed headers MUST ONLY be Content-Type, Authorization, and X-Requested-With.

---

## HSTS Preload

The Nginx configuration MUST include the `preload` directive in its Strict-Transport-Security header.
- **Verification method**: `curl -I https://imagiax.dev` and check for `Strict-Transport-Security: max-age=...; includeSubDomains; preload`
- **Affected Files**: `nginx-proxy.conf`

### Scenario: Connecting via HTTPS
- GIVEN an HTTPS connection to the platform
- WHEN the server responds
- THEN the `Strict-Transport-Security` header MUST contain `; preload`.

---

## Production-safe Error Details

The OHIF frontend MUST NOT leak error details to the user in production.
- **Verification method**: Trigger an error in OHIF and verify UI shows generic error.
- **Affected Files**: `docker-compose.prod.yml` (APP_CONFIG), `OHIF Medical Imaging Viewer/Viewers/platform/app/public/config/default.js`

### Scenario: Frontend Error Occurs
- GIVEN an exception in the OHIF viewer
- WHEN the error notification is displayed
- THEN `showErrorDetails` MUST be set to `'user'` rather than `'always'`.

---

## Secure Frontend Config Endpoint

The `/config/frontend` API endpoint MUST require authentication.
- **Verification method**: `curl -I https://imagiax.dev/api/config/frontend` (without auth) returns 401.
- **Affected Files**: `servidor_api_orthanc_v1.py`

### Scenario: Requesting frontend configuration unauthenticated
- GIVEN an unauthenticated request to `/config/frontend`
- WHEN the backend receives the request
- THEN it MUST return a 401 Unauthorized.

### Scenario: Authenticated request
- GIVEN an authenticated request to `/config/frontend`
- WHEN the backend receives the request
- THEN it MUST return a 200 OK with the configuration.

---

## JWT Revocation and Deactivation Check (VER-001/VER-011)

The `/auth/verify` endpoint MUST validate that the JWT is not revoked (`is_jti_revoked()`) AND that the associated user is not deactivated (`usuarios.activo = FALSE`).

### Scenario: Accessing OHIF/PACS after logout
- GIVEN a user who was logged in and has a valid session cookie
- WHEN the user logs out via `POST /auth/logout`
- AND subsequently requests `GET /auth/verify` using the same old cookie
- THEN the system MUST return a 401 Unauthorized status
- AND the user MUST NOT be granted access to OHIF or PACS

### Scenario: Accessing OHIF/PACS with a deactivated user account
- GIVEN a user whose account is deactivated (`usuarios.activo = FALSE`)
- WHEN the user attempts to access `GET /auth/verify` with their session cookie
- THEN the system MUST return a 401 Unauthorized status

---

## Nginx Path Traversal Protection (VER-002)

The Nginx configuration MUST reject any request containing `..` in the URL path BEFORE forwarding the request to the backend.

### Scenario: Path traversal attempt to Orthanc system info
- GIVEN a malicious request with a path containing `..`, such as `https://imagiax.dev/pacs/dicom-web/../system`
- WHEN the request is received by Nginx
- THEN Nginx MUST reject the request with a 400 Bad Request or 403 Forbidden status
- AND the request MUST NOT reach the backend or Orthanc system endpoints

---

## SQL Parameterization in Payment Accreditation (VER-004)

The `acreditar_pago()` function MUST use proper parameterized SQL placeholders (`$1, $2, ...`) for all database inserts to prevent SQL injection.

### Scenario: Successful payment accreditation
- GIVEN valid payment data from a webhook
- WHEN the webhook triggers `acreditar_pago()`
- THEN the system MUST successfully execute the `INSERT` query using the correct parameterized values

### Scenario: SQL injection attempt via payment data
- GIVEN malicious payment data containing SQL injection payloads
- WHEN the webhook triggers `acreditar_pago()`
- THEN the system MUST safely parameterize the input
- AND no SQL injection MUST be possible

---

## Environment File Git Tracking (VER-005)

The production environment file (`.env.production`) MUST NOT be tracked in the git repository.

### Scenario: Git status check
- GIVEN the `.env.production` file exists in the project root
- WHEN `git status` is executed
- THEN `.env.production` MUST NOT appear as a tracked file

### Scenario: Gitignore enforcement
- GIVEN the `.gitignore` file
- WHEN reviewing its contents
- THEN it MUST include `.env.production`

---

## JWT Secret Length Validation (VER-007)

The system MUST validate the length of `JWT_SECRET` at startup, ensuring it is at least 32 bytes long.

### Scenario: Startup with weak secret
- GIVEN a `.env` configuration where `JWT_SECRET` is less than 32 bytes
- WHEN the application attempts to start
- THEN it MUST raise a `RuntimeError` with a clear explanation message

### Scenario: Startup with secure secret
- GIVEN a `.env` configuration where `JWT_SECRET` is 64 bytes
- WHEN the application attempts to start
- THEN the application MUST start normally

---

## Secure JavaScript Serialization (VER-016)

The system MUST use `json.dumps()` for safely embedding Python variables into JavaScript strings, replacing any custom serialization helpers to prevent Cross-Site Scripting (XSS).

### Scenario: Safe JS variable embedding
- GIVEN a template or response rendering JavaScript
- WHEN embedding Python strings or variables
- THEN the system MUST serialize them using `json.dumps()`

---

## Escape Sequence Syntax (VER-015)

Python source files MUST NOT produce `SyntaxWarning` messages for invalid escape sequences.

### Scenario: Python syntax warning check
- GIVEN any Python module in the project
- WHEN executing `python -W error -m py_compile <file>`
- THEN no `SyntaxWarning` MUST be raised
