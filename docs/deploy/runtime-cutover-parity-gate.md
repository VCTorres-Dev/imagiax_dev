# Runtime cutover parity gate (canary evidence)

Este documento registra comandos de evidencia para validar paridad antes de aceptar pruebas manuales.

## 1) Fuente canónica OHIF (local)

```powershell
powershell -ExecutionPolicy Bypass -File ".\scripts\verify-ohif-canonical-source.ps1"
```

Debe terminar en PASS de tokens + hashes.

## 2) Paridad APP_CONFIG (local, sin secretos)

```powershell
powershell -ExecutionPolicy Bypass -File ".\scripts\verify-ohif-canary-app-config-parity.ps1" \
  -ComposePath ".\OHIF Medical Imaging Viewer\orthanc-imagiax\docker-compose.prod.yml"
```

Opcional con evidencia de logs/snapshot:

```powershell
powershell -ExecutionPolicy Bypass -File ".\scripts\verify-ohif-canary-app-config-parity.ps1" \
  -ComposePath ".\OHIF Medical Imaging Viewer\orthanc-imagiax\docker-compose.prod.yml" \
  -CanaryLogPath ".\canary-artifacts\logs\ohif-canary.log" \
  -AppConfigSnapshotPath ".\canary-artifacts\runtime\app-config.safe.js"
```

## 3) Reglas de bloqueo

- Si aparece `Not using custom APP_CONFIG` en logs/config, bloquear aceptación.
- Si falla hash canónico OHIF, bloquear aceptación.
- Si faltan tokens de APP_CONFIG, bloquear aceptación.

## 4) Seguridad operacional

- No usar `docker compose config` (puede exponer secretos).
- No incluir valores de env/secrets en salidas compartidas.
- Este gate no autoriza deploy/cutover por sí mismo; sólo valida entorno de prueba.
