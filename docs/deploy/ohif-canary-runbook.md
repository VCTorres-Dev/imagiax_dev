# Runbook de canary seguro para OHIF

## Objetivo

Validar una imagen candidata de OHIF con la extensión `imagiax-ai` integrada, sin tocar el tráfico público ni reemplazar `ohif-imagiax` hasta tener evidencia.

## Principios no negociables

- No usar `docker compose config` sin sanitizar: puede imprimir secretos.
- No reconstruir OHIF desde un árbol no versionado o no reconciliado.
- No reemplazar `ohif-imagiax` sin rollback explícito de la imagen actual.
- No tocar ni recargar nginx durante el canary.
- No publicar PHI, DICOM ni credenciales en logs, capturas o reportes.

## Estado observado

- Servicio productivo: `ohif-imagiax`.
- Imagen productiva observada: `orthanc-imagiax-ohif`.
- Runtime: nginx sirviendo `/usr/share/nginx/html`.
- La imagen OHIF embebe `platform/app/dist`; los cambios en `ohif-extension-imagiax-ai/src/**` requieren rebuild de OHIF.
- En el servidor existe `/root/imagiax/OHIF Medical Imaging Viewer/Viewers`, pero no debe tratarse como fuente reproducible hasta reconciliarlo contra el commit candidato.

## Gate previo obligatorio

Antes de cualquier build o contenedor nuevo:

1. El worktree local debe estar limpio y en una rama deployable.
2. Registrar el commit exacto candidato.
3. Confirmar que el cambio OHIF vive en `ohif-extension-imagiax-ai/src/**`.
4. Reconciliar la extensión en el build context de OHIF:
   - fuente canónica: `ohif-extension-imagiax-ai/` del commit candidato;
   - destino de build: `OHIF Medical Imaging Viewer/Viewers/extensions/imagiax-ai/` o el path equivalente del monorepo;
   - comparar hashes/diff antes de build.
5. Guardar rollback de OHIF actual:
   - image id activo;
   - tag rollback (`orthanc-imagiax-ohif:rollback-<fecha>-<sha>`);
   - opcional: `docker save` a `canary-artifacts/` si no hay registry confiable.

Chequeo automatizado de prerequisitos (solo lectura):

```powershell
./scripts/verify-ohif-prereqs.ps1
```

Si el worktree todavía tiene cambios locales durante una revisión previa, se puede usar `-AllowDirtyTree`, pero **nunca** para autorizar build/deploy.

## Build candidato

> No ejecutar sin autorización explícita.

Desde el build context reconciliado de OHIF:

```bash
docker build -t orthanc-imagiax-ohif:canary-<git_sha> .
```

No usar `latest` para la imagen candidata.

## Canary sin tráfico público

> No ejecutar sin autorización explícita.

Ejemplo de contenedor canary aislado en loopback:

```bash
docker rm -f ohif-imagiax-canary 2>/dev/null || true
docker run -d \
  --name ohif-imagiax-canary \
  --network orthanc-imagiax_default \
  -p 127.0.0.1:18080:80 \
  orthanc-imagiax-ohif:canary-<git_sha>
```

El ejemplo anterior muestra solo la forma del aislamiento. Para una validación funcional real, el canary debe recibir el mismo `APP_CONFIG` auditado que producción, sin imprimir secretos ni usar `docker compose config`. No pegarlo a mano en una línea larga; usar un override/env-file temporal revisado y sin secretos, o generar un archivo `app-config.safe.js` a partir de la sección OHIF ya auditada. El config esperado conserva:

- `window.__IMAGIAX_API_URL__ = '/api'`
- `window.__IMAGIAX_V2_API_URL__ = '/api'`
- `window.__IMAGIAX_CONFIG__ = { backend_url: '/api' }`
- data source Orthanc con rutas `/pacs/...`

## Smoke técnico del canary

Ejecutar contra `http://127.0.0.1:18080` desde el host remoto:

```bash
curl -fsS http://127.0.0.1:18080/ >/dev/null
docker exec ohif-imagiax-canary sh -lc 'gzip -cd /usr/share/nginx/html/*.js.gz 2>/dev/null | grep -a "fetchInformePdfBlob" >/dev/null'
docker exec ohif-imagiax-canary sh -lc 'gzip -cd /usr/share/nginx/html/*.js.gz 2>/dev/null | grep -a "Cargando PDF autoritativo" >/dev/null'
```

Estos checks solo validan que el bundle contiene el fix esperado. No reemplazan la aceptación manual.

Smoke automatizado equivalente:

```powershell
./scripts/smoke-ohif-canary.ps1 -BaseUrl "http://127.0.0.1:18080"
```

## Aceptación manual mínima

Con datos controlados/no sensibles:

1. Abrir viewer OHIF canary.
2. Confirmar que carga el panel IMAGIAX.
3. Login/sesión válida.
4. Abrir historial con un informe `backendSynced=true`.
5. `Ver PDF` debe mostrar estado de carga y luego iframe con PDF oficial del backend.
6. `Descargar PDF` debe descargar el mismo artefacto.
7. Si se cierra sesión, el panel debe mostrar error explícito, no pantalla blanca silenciosa.
8. Una entrada legacy sin `backendSynced=true` puede usar fallback local.

## Cutover

Solo si canary técnico y aceptación manual pasan:

1. Crear/confirmar rollback tag de la imagen OHIF actual.
2. Cambiar el servicio `ohif` a la imagen candidata etiquetada.
3. Recrear solo `ohif`, sin nginx reload.
4. Verificar público:
   - `/` carga OHIF;
   - `/api/salud` sigue `200`;
   - `/pacs/...` sigue accesible desde el viewer;
   - historial `Ver PDF` y `Descargar PDF` funcionan.

## Rollback

Si falla el cutover:

```bash
docker tag orthanc-imagiax-ohif:rollback-<fecha>-<sha> orthanc-imagiax-ohif:latest
docker compose up -d --no-build --no-deps ohif
```

Conservar logs del fallo y no borrar la imagen candidata hasta terminar el diagnóstico.
