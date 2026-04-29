# Canary runtime wiring template (safe, local-only)

Este documento acompaña `OHIF Medical Imaging Viewer/orthanc-imagiax/docker-compose.canary.template.yml`.

## Objetivo

Proveer wiring **canary-only** con paridad de `APP_CONFIG`, sin cutover productivo, sin nginx reload y sin secretos hardcodeados.

## Garantías de seguridad del template

- bind exclusivo: `127.0.0.1:18080:80`
- imagen con tag variable: `${IMAGIAX_CANARY_TAG}`
- contrato APP_CONFIG preservado:
  - `window.__IMAGIAX_API_URL__ = '/api'`
  - `window.__IMAGIAX_V2_API_URL__ = '/api'`
  - `window.__IMAGIAX_CONFIG__ = { backend_url: '/api' }`
  - DICOMweb por `/pacs/wado` y `/pacs/dicom-web`
- no define secretos ni credenciales

## Estado de trazabilidad

- **Local completo**: template creado y documentado.
- **Runtime pendiente**: falta evidencia en host/canary real (contenedor levantado, inspección de runtime config y smoke funcional).

## Evidencia sugerida (cuando se ejecute canary real)

1. Ejecutar gate de paridad APP_CONFIG:

```powershell
powershell -ExecutionPolicy Bypass -File ".\scripts\verify-ohif-canary-app-config-parity.ps1" \
  -ComposePath ".\OHIF Medical Imaging Viewer\orthanc-imagiax\docker-compose.canary.template.yml"
```

2. Confirmar fuente canónica OHIF:

```powershell
powershell -ExecutionPolicy Bypass -File ".\scripts\verify-ohif-canonical-source.ps1"
```
