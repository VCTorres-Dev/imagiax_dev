# Auth Specifications — IMAGIAX

_Last updated: 2026-04-04 | Sources: comprehensive-hardening, verification-fixes_

---

## Seamless Panel Authentication (Cookie-Based)

The OHIF AI panel MUST authenticate seamlessly using the existing HTTP-only session cookie instead of a separate localStorage token gate.
- **Verification method**: Log into main app, open OHIF viewer, verify AI panel loads without prompting for login again.
- **Affected Files**: `OHIF Medical Imaging Viewer/Viewers/extensions/imagiax-ai/src/panels/ImagiaxAiPanel.tsx`

### Scenario: Opening the AI Panel
- GIVEN a user who is already authenticated via the main application
- WHEN they open the Imagiax AI panel in OHIF
- THEN the panel MUST automatically call `/auth/me` on mount to verify session
- AND all `apiRequest` calls MUST include `credentials: 'include'`
- AND the legacy `_AUTH_TOKEN_KEY` localStorage gate MUST NOT be present

### Scenario: Cookie Expiry Mid-Session
- GIVEN a user in the OHIF panel whose session cookie has expired
- WHEN any `apiRequest()` returns 401
- THEN the panel MUST redirect to `/login?returnTo={currentPath}`
- AND MUST NOT render an embedded login form

---

## Admin Page RBAC Response (VER-013)

The admin page (`/admin`) MUST return a 403 Forbidden status for authenticated but unauthorized users, instead of redirecting.

### Scenario: Unauthorized access by medico
- GIVEN an authenticated user with the `medico` role (not admin)
- WHEN the user attempts to access `/admin`
- THEN the system MUST return a 403 Forbidden status code with an appropriate error message

### Scenario: Unauthenticated access to admin
- GIVEN an unauthenticated request to `/admin`
- WHEN the server receives the request
- THEN the system MUST redirect to `/login?returnTo=/admin`
