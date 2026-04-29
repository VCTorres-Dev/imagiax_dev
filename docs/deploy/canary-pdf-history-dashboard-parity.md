# Canary parity gate — PDF / History / Dashboard

Este gate define cuándo un canary es **entorno válido de prueba** para esta change.

## Hard gate (bloqueante)

No se acepta ninguna validación manual/funcional si falla alguno:

1. **Paridad APP_CONFIG OHIF**
   - `APP_CONFIG` debe estar declarado en compose/manifiesto canary.
   - Debe incluir:
     - `window.__IMAGIAX_API_URL__ = '/api'`
     - `window.__IMAGIAX_V2_API_URL__ = '/api'`
     - `window.__IMAGIAX_CONFIG__ = { backend_url: '/api' }`
2. **Sin fallback inválido de runtime**
   - Si logs o snapshot contienen `Not using custom APP_CONFIG`, el entorno es inválido.
3. **Paridad de artefacto OHIF canónico**
   - `scripts/verify-ohif-canonical-source.ps1` debe pasar con hash esperado.
4. **Sin exposición de secretos**
   - No usar `docker compose config` para este gate.
   - No imprimir valores de secretos/env en evidencias.

## Script local de gate (read-only)

```powershell
powershell -ExecutionPolicy Bypass -File ".\scripts\verify-ohif-canary-app-config-parity.ps1" \
  -ComposePath ".\OHIF Medical Imaging Viewer\orthanc-imagiax\docker-compose.prod.yml" \
  -CanaryLogPath ".\canary-artifacts\logs\ohif-canary.log" \
  -AppConfigSnapshotPath ".\canary-artifacts\runtime\app-config.safe.js"
```

Notas:
- `-CanaryLogPath` y `-AppConfigSnapshotPath` son opcionales, pero recomendados para capturar evidencia de fallback.
- El script no imprime secretos; sólo valida tokens y patrones seguros.

## Reglas de invalidez explícitas

El canary queda marcado como **invalid test environment** si ocurre cualquiera:

- `Not using custom APP_CONFIG` aparece en logs/config.
- El gate de hash canónico OHIF falla.
- Faltan tokens de APP_CONFIG esperados.
- Se detectan literales sensibles en el bloque APP_CONFIG.

## Evidencia mínima a registrar

- salida de `verify-ohif-canonical-source.ps1` (PASS),
- salida de `verify-ohif-canary-app-config-parity.ps1` (PASS),
- fecha/hora de captura y commit probado.
