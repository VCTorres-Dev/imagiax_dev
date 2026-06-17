# Infrastructure Specifications — IMAGIAX

_Last updated: 2026-04-04 | Sources: verification-fixes_

---

## Reports Listing Access Control (VER-003)

The `/api/informes/listado` endpoint MUST require authentication and enforce Role-Based Access Control (RBAC).

### Scenario: Unauthenticated access attempt
- GIVEN an unauthenticated user
- WHEN the user sends a `GET` request to `/api/informes/listado`
- THEN the system MUST return a 401 Unauthorized status

### Scenario: Authenticated medico access
- GIVEN an authenticated user with the `medico` role
- WHEN the user sends a `GET` request to `/api/informes/listado`
- THEN the system MUST return only the reports belonging to that user

### Scenario: Authenticated admin access
- GIVEN an authenticated user with the `admin` role
- WHEN the user sends a `GET` request to `/api/informes/listado`
- THEN the system MUST return all reports

---

## Report Generation Auth Guards (VER-009)

The `/generar/informe` and `/informes/guardar` endpoints MUST require authentication and RBAC via `Depends(get_current_user)`.

### Scenario: Unauthenticated access to report endpoints
- GIVEN an unauthenticated user
- WHEN the user sends a `POST` request to `/generar/informe` or `/informes/guardar`
- THEN the system MUST return a 401 Unauthorized status

### Scenario: Authenticated medico saving a report
- GIVEN an authenticated user with the `medico` role
- WHEN the user sends a valid `POST` request to `/generar/informe` or `/informes/guardar`
- THEN the system MUST process the request successfully

---

## Docker Port Documentation (VER-010)

The `docker-compose.prod.yml` file MUST include a comment explaining that the host-level `curl localhost:8010/salud` command fails because the port is only exposed internally to the Docker network.

### Scenario: Documentation presence
- GIVEN the `docker-compose.prod.yml` file
- WHEN reviewing the `backend` service definition
- THEN there MUST be a comment explaining the network topology and why port 8010 is not accessible from the host

---

## Dockerfile Healthcheck Documentation (VER-014)

The comment explaining the HEALTHCHECK in the Dockerfile MUST accurately reflect the actual endpoint being used (`/salud`).

### Scenario: Accurate healthcheck documentation
- GIVEN the Dockerfile
- WHEN reviewing the HEALTHCHECK instruction comment
- THEN it MUST state `/salud` instead of `/ping`
