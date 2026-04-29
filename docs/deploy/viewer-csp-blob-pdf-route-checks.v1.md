# Viewer CSP + backend PDF route checks (v1)

Artifacto local versionado para aplicar **mínimo cambio** de CSP cuando el visor PDF embebido usa `blob:`.

> Alcance: documentación y validación local/runbook. **No aplica runtime** ni toca producción.

## Minimal nginx CSP delta (viewer autenticado)

En el `location` autenticado del viewer (mismo bloque donde ya existe `Content-Security-Policy`), agregar solo:

```nginx
frame-src 'self' blob: data:;
object-src 'self' blob:;
```

### Notas de minimización

- No abrir `default-src`.
- No agregar orígenes externos innecesarios.
- Mantener alcance únicamente al route-block autenticado del viewer.

## Route contract checks (backend-final PDF)

El frontend OHIF debe resolver siempre contra rutas backend autoritativas:

- Historial: `GET /informes/{id}/pdf`
- Fallback open/new tab: `GET /informes/{id}/pdf`
- Download: mismo endpoint con `Content-Disposition` backend

## Local verification checklist (safe)

1. Confirmar helper de preview backend:
   - `ohif-extension-imagiax-ai/src/services/imagiaxApi.ts`
   - `getInformePdfPreview()` + `downloadInformePdf()`
2. Confirmar uso de ID histórico seleccionado (no latest implícito):
   - `ohif-extension-imagiax-ai/src/ImagiaxAiPanel.tsx`
   - `ReportViewerModal` usa `getHistoryInformeId(entry)`
3. Confirmar fallback no-blank por timeout visual:
   - overlay informativo + acciones `Abrir PDF` / `Descargar PDF`
   - error real solo en fetch/render failure
4. Confirmar refresh acumulativo (25/50/75/100):
   - `fetchHistorialFromBackend(false)` prepende página 1, preserva acumulado y deduplica por `informeId`

## Runtime validation commands (documentados, NO ejecutar en este apply local)

```bash
nginx -t
curl -I "https://<host-viewer>/api/informes/<uuid>/pdf"
```

Esperado:
- `nginx -t` OK
- `Content-Type: application/pdf`
- sin violaciones CSP de `frame-src`/`object-src` para `blob:` en consola del navegador
