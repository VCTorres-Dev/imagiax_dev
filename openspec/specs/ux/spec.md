# UX Specifications — IMAGIAX

_Last updated: 2026-04-04 | Sources: comprehensive-hardening_

---

## Clean Study URLs

The API MUST provide a clean short-URL endpoint that redirects to the OHIF viewer URL with full StudyInstanceUID.
- **Verification method**: `curl -I https://imagiax.dev/estudio/abc1234` returns a 302 redirect to the OHIF viewer path.
- **Affected Files**: `servidor_api_orthanc_v1.py`, `imagiax_db.py`

### Scenario: Accessing a clean short URL
- GIVEN a request to `/estudio/{short_id}`
- WHEN the backend processes the request
- THEN it MUST return a 302 Redirect to the full OHIF viewer URL preserving necessary query parameters

### Scenario: Short ID not found
- GIVEN a request to `/estudio/{short_id}` where the short_id does not exist
- WHEN the backend processes the request
- THEN it MUST return a 404 Not Found

---

## Backend History Hydration on Mount

The AI panel MUST fetch the latest report history from the backend on mount instead of relying solely on localStorage.
- **Verification method**: Open AI panel, network tab shows GET request to fetch history.
- **Affected Files**: `OHIF Medical Imaging Viewer/Viewers/extensions/imagiax-ai/src/panels/ImagiaxAiPanel.tsx`

### Scenario: Viewing report history
- GIVEN a user opens the AI panel
- WHEN the component mounts
- THEN it MUST call the backend API (`listadoInformes`) to hydrate the state
- AND the UI MUST display the freshest backend data instead of stale localStorage data

---

## Deduplicated OHIF Extension Directory

There MUST be exactly one source of truth for the `imagiax-ai` OHIF extension.
- **Affected Files**: `OHIF Medical Imaging Viewer/Viewers/extensions/imagiax-ai/` (canonical)

### Scenario: Building the OHIF Viewer
- GIVEN the build process runs
- WHEN compiling extensions
- THEN it MUST use exactly one source of truth for the `imagiax-ai` extension without duplicate module errors

---

## Dev Scripts Organization

Utility and development scripts MUST be organized under `scripts/dev/` and excluded from production Docker images.
- **Affected Files**: `scripts/dev/`, `.dockerignore`

### Scenario: Building the Production Container
- GIVEN a `docker build` command
- WHEN the context is sent to the Docker daemon
- THEN utility scripts MUST NOT be included in the final image context due to `.dockerignore` filters
