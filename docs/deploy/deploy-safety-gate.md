# Gate de deploy seguro

Este gate existe para evitar deploys desde un árbol local mezclado o desde una rama que no representa producción.

## Principio

Un deploy seguro debe partir de una fuente reproducible:

```text
commit limpio -> imagen construida desde ese commit -> health check -> activación -> rollback posible
```

No es aceptable tratar un `scp` de archivos sueltos más `docker restart` como deploy backend, salvo que esos archivos estén montados como volumen y el runtime los use de forma demostrable.

## Bloqueos obligatorios

El deploy debe abortar si ocurre cualquiera de estas condiciones:

1. `git status --porcelain` no está limpio.
2. La rama no pertenece a una allowlist explícita (`main`, `release/*`, `hotfix/*`, `deploy/*`, `prod/*`).
3. Hay archivos no trackeados sensibles:
   - `.env*`
   - `service_account*.json`
   - `*.sql`
   - `*.dcm`, `*.dicom`
   - `_quarantine/**`
   - `.sdd-temp/**`
   - `_backup/**`
   - `nul`
4. El runtime vivo no coincide con el runtime esperado.
5. El `Dockerfile` local no contiene el runtime esperado.
6. Hay cambios de Dockerfile, compose o nginx mezclados con cambios de negocio.
7. No hay rollback identificado antes de tocar producción.

## Runtime esperado hoy

Según inspección solo lectura del contenedor vivo, el runtime esperado actualmente es:

```text
api.main:create_app --factory
```

Si un script local o Dockerfile apunta a otro entrypoint, el deploy debe detenerse hasta resolver la contradicción.

Importante: con el estado observado hoy, una rama cuyo `Dockerfile` apunte a `servidor_api_orthanc_v1:app` debe fallar el gate aunque producción esté saludable. Saludable no significa reproducible.

## Flujo recomendado

1. Ejecutar verificación de producción viva:

   ```powershell
   ./scripts/verify-production-runtime.ps1
   ```

2. Ejecutar guard local:

   ```powershell
   ./scripts/predeploy-guard.ps1 -ExpectedRuntime "api.main:create_app"
   ```

3. Verificar que los archivos críticos de la baseline coinciden con la captura de producción:

   ```powershell
   ./scripts/verify-baseline-hashes.ps1
   ```

4. Si las verificaciones pasan, construir una imagen etiquetada por commit.

5. Validar la imagen nueva fuera de tráfico o con canary.
   - Runbook de referencia: `docs/deploy/canary-runbook.md`
   - Prerrequisitos de canary (solo lectura):

     ```powershell
     ./scripts/verify-canary-prereqs.ps1
     ```

   - Smoke mínimo de canary (solo lectura, ejecutado en host remoto vía SSH):

     ```powershell
     ./scripts/smoke-canary.ps1
     ```

6. Activar solo si health checks y smoke tests pasan.

7. Conservar image id anterior para rollback inmediato.

## Gate específico OHIF

OHIF es una superficie distinta del backend FastAPI. Un deploy backend **no** actualiza el bundle OHIF servido por `ohif-imagiax`.

Para cambios en `ohif-extension-imagiax-ai/**`:

1. No desplegar como parte de un backend-only cutover esperando cambios de UI.
2. Reconciliar la extensión canónica del commit candidato dentro del monorepo `Viewers` antes de construir.
3. Construir una imagen OHIF etiquetada por commit; no usar `latest` como candidato.
4. Ejecutar el canary y aceptación manual de `docs/deploy/ohif-canary-runbook.md`.
5. Mantener rollback tag de la imagen OHIF activa antes de reemplazar `ohif-imagiax`.

## Smoke checks mínimos

Antes de considerar sano un deploy:

```text
GET /salud
GET /config/frontend
auth/login + auth/me
flujo OHIF carga visor
generación de informe con caso controlado/no sensible
guardar decisión médica
```

## Estado actual

Este branch convierte la producción viva observada en una baseline reproducible inicial. El gate debe seguir considerándose **cerrado para deploy backend** hasta que:

1. el `Dockerfile` local apunte a `api.main:create_app --factory`;
2. el runtime local incluya `src/`, `app/`, `prompts/` y `requirements.txt`;
3. `predeploy-guard.ps1` pase en un árbol limpio;
4. exista estrategia de rollback antes de reemplazar contenedores.
