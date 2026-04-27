# Runbook de canary seguro (sin tocar tráfico de producción)

## Objetivo

Validar una imagen candidata de backend en el mismo host y red de producción, **sin tocar el tráfico público ni la ruta activa de producción**.

## No objetivos (prohibido en esta fase)

- No editar ni recargar nginx.
- No reemplazar el contenedor `fastapi-imagiax`.
- No reiniciar producción.
- No hacer switch de tráfico público al canary.

## Hechos de autoridad (topología observada)

- Red de app: `orthanc-imagiax_default`.
- Backend productivo: contenedor `fastapi-imagiax`, puerto interno `8010`, sin publish directo en host.
- Nginx publica `80/443` y enruta `/api/` a `fastapi:8010`.
- Contenedores relevantes: `fastapi-imagiax`, `orthanc-imagiax`, `postgres-imagiax`, `nginx-imagiax`, `gotenberg-imagiax`, `ohif-imagiax`.
- Canary factible: `fastapi-imagiax-canary` en la misma red, publicado solo en loopback `127.0.0.1:18010:8010`.
- Shape de runtime (env/mount):
  - mismo env file del host que producción;
  - bind RO: `/root/imagiax/service_account.json:/app/service_account.json:ro`;
  - volumen: `orthanc-imagiax_torchxrayvision-cache:/home/imagiax/.torchxrayvision`.
- Producción saludable actual:
  - comando: `uvicorn api.main:create_app --factory`;
  - image id: `sha256:a271ce9348ea5a338500b395441fb2a87950e8a9f554b518b7e5a7549130ee64`.

## Prerrequisitos

1. Rama esperada para baseline: `prod/current-runtime-baseline`.
2. Commit baseline de referencia: `8649f799bb35c6a241f9b42a5967e1915ad33d71`.
3. Árbol local limpio (sin cambios pendientes).
4. Puerto host `18010` libre en servidor.
5. No exponer secretos en logs ni outputs.
6. Mantener el cambio local existente de Dockerfile (instalación de `curl` para HEALTHCHECK).

## Chequeos previos (solo lectura)

Ejecutar antes de cualquier comando mutable:

```powershell
./scripts/verify-production-runtime.ps1
./scripts/verify-baseline-hashes.ps1
./scripts/verify-canary-prereqs.ps1
```

Si cualquiera falla, **no avanzar**.

## Secuencia de canary (comandos mutables)

> ## NO EJECUTAR SIN AUTORIZACION EXPLICITA
>
> Los siguientes comandos son de referencia operativa. No correrlos automáticamente.

### 1) Build de imagen candidata

```powershell
docker build -t imagiax-backend:canary-<git_sha> .
```

### 2) Publicar imagen en host remoto (si aplica)

```powershell
docker save imagiax-backend:canary-<git_sha> -o imagiax-backend-canary.tar
scp -i "$env:USERPROFILE\.ssh\do_imagiax" .\imagiax-backend-canary.tar root@imagiax.dev:/root/imagiax/
ssh -i "$env:USERPROFILE\.ssh\do_imagiax" root@imagiax.dev "docker load -i /root/imagiax/imagiax-backend-canary.tar"
```

### 3) Crear canary aislado (sin cutover)

```bash
docker rm -f fastapi-imagiax-canary 2>/dev/null || true
docker run -d \
  --name fastapi-imagiax-canary \
  --network orthanc-imagiax_default \
  --env-file /root/imagiax/.env.production \
  -e IMAGIAX_CANARY_READONLY=1 \
  -p 127.0.0.1:18010:8010 \
  -v /root/imagiax/service_account.json:/app/service_account.json:ro \
  -v orthanc-imagiax_torchxrayvision-cache:/home/imagiax/.torchxrayvision \
  imagiax-backend:canary-<git_sha>
```

## Smoke tests del canary

### Automáticos mínimos

```powershell
./scripts/smoke-canary.ps1 -BaseUrl "http://127.0.0.1:18010"
```

Validaciones:
- `GET /salud` debe devolver `200`.
- `GET /config/frontend` sin sesión debe devolver `401`, igual que producción actual.

Si `/config/frontend` devuelve algo distinto de `401` sin autenticación, tratar como señal de desalineación auth/backend del canary y **bloquear avance** hasta diagnóstico.

### Manuales controlados

- Login/auth y validación de flujo clínico: ejecución manual y controlada.
- Usar solo datos de prueba no sensibles (sin PHI real).
- No usar secretos ni credenciales reales en outputs compartidos.

## Criterios de éxito

Se considera canary válido cuando:

1. `verify-canary-prereqs.ps1` pasa completo.
2. `smoke-canary.ps1` pasa completo (`/salud=200` y `/config/frontend=401` sin sesión).
3. Prueba manual de auth y flujo clínico controlado no reporta errores críticos.
4. No se tocó nginx ni tráfico público durante toda la validación.

## Criterios de falla

Cualquier condición siguiente implica rechazo de canary:

- Puerto `18010` ocupado antes de levantar.
- Runtime observado del contenedor productivo no contiene `api.main:create_app`.
- Falla de `/salud`.
- `/config/frontend` sin sesión no devuelve `401`.
- Evidencia de desvío operativo (intento de reload nginx, restart de prod o reemplazo de `fastapi-imagiax`).

## Rollback / remoción segura

Si falla el canary, remover **solo** artefactos de canary:

> ## NO EJECUTAR SIN AUTORIZACION EXPLICITA

```bash
docker rm -f fastapi-imagiax-canary
docker image rm imagiax-backend:canary-<git_sha>
```

Reglas:

- Nunca borrar/recrear `fastapi-imagiax` en esta fase.
- Nunca tocar nginx en esta fase.
- Conservar referencia explícita de imagen productiva vigente para rollback operacional:
  - `sha256:a271ce9348ea5a338500b395441fb2a87950e8a9f554b518b7e5a7549130ee64`.
