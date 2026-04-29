# Backend PDF authority contract

Define semántica autoritativa del PDF backend para rutas de usuario y admin.

## Endpoints autoritativos

- Usuario autenticado (scoped): `GET /informes/{id}/pdf`
- Admin autenticado: `GET /admin/api/informes/{id}/pdf`

Ambos endpoints deben usar el mismo generador backend (`build_pdf_streaming_response`) y no mutar estado en DB.

## Contrato de respuesta esperado

- `Content-Type: application/pdf`
- `Content-Disposition`:
  - `attachment` por defecto (`mode=download`)
  - `inline` para `mode=inline` o `mode=preview` (alias backward-compatible)
- `X-Content-Type-Options: nosniff`
- sin exposición de secretos en payload/header

## Semántica de seguridad y datos

- Sin mutación de DB ni cambios de estado al descargar/previsualizar PDF.
- Ruta usuario aplica scope por `usuario_id` (no-admin no puede leer PDF de otro usuario).
- Ruta admin requiere rol `admin` o `superadmin`.

## Evidencia local de código (esta change)

- `src/api/ai/router.py`
  - endpoint `@router.get("/informes/{informe_id}/pdf")`
  - parámetro `mode` validado (`inline|download|preview`)
  - llama `build_pdf_streaming_response(informe, mode=mode)`
- `app/admin/router.py`
  - endpoint `@router.get("/admin/api/informes/{informe_id}/pdf")`
  - parámetro `mode` validado (`inline|download|preview`)
  - llama `build_pdf_streaming_response(informe, mode=mode)`
- `src/infrastructure/pdf_service.py`
  - centraliza headers PDF (`application/pdf`, `Content-Disposition`, `nosniff`)
  - `preview` mapeado a `inline`
  - no hay escritura/UPDATE en DB

## Checklist local rápido

1. Buscar endpoints y llamada compartida a `build_pdf_streaming_response`.
2. Confirmar headers en `build_pdf_streaming_response`.
3. Verificar ausencia de SQL mutante en estos code paths.

## Estado

- **Local completo (contrato/documentación/código):** sí.
- **Runtime pendiente (5.2):** falta evidencia en ejecución HTTP real contra canary/host.
