# Baseline actual de producción

Fecha de captura inicial: 2026-04-27.

Este documento registra lo observado en producción mediante inspección **solo lectura**. No autoriza deploy, restart, rebuild ni cutover.

## Resultado principal

La producción viva no debe asumirse a partir del `Dockerfile` local ni de documentación previa. Debe verificarse contra el contenedor real.

La inspección del contenedor `fastapi-imagiax` mostró:

```text
CMD: uvicorn api.main:create_app --factory --host 0.0.0.0 --port 8010 --workers 1 --proxy-headers --forwarded-allow-ips 172.16.0.0/12,192.168.0.0/16,10.0.0.0/8
Módulo cargado: /app/src/api/main.py
Imagen: sha256:a271ce9348ea5a338500b395441fb2a87950e8a9f554b518b7e5a7549130ee64
Estado: healthy
PYTHONPATH: /app/src
Healthcheck interno: GET /salud
```

Conclusión: la autoridad viva observada es el runtime factory/modular:

```text
api.main:create_app --factory
```

No debe tratarse `servidor_api_orthanc_v1:app` como autoridad activa sin volver a verificar producción.

## Contradicción detectada

Hay deriva entre tres superficies:

| Superficie | Estado observado |
|---|---|
| Producción viva | `api.main:create_app --factory`, cargando `/app/src/api/main.py` |
| `origin/main` al iniciar esta rama | contenía `Dockerfile` orientado a `servidor_api_orthanc_v1:app` |
| `prod/current-runtime-baseline` | debe reconstruir el runtime vivo factory/modular observado |
| Workspace principal local | contiene mezcla de monolito, factory, cambios no committeados y archivos sensibles/no trackeados |

Esta deriva hace que cualquier deploy directo desde el workspace principal sea riesgoso. Esta rama existe para cerrar esa brecha de forma controlada.

## Layout runtime observado

El contenedor vivo contiene:

```text
/app/src
/app/app
/app/prompts
/app/requirements.txt
/app/service_account.json   # montado como bind read-only, no versionar
```

El repositorio debe poder reconstruir `/app/src`, `/app/app`, `/app/prompts` y `requirements.txt`. Los secretos y credenciales siguen fuera de Git.

## Hashes de archivos críticos observados

```text
8dec78f0180cac8a9d6051e425c1691ce8ccccb4c41d2628e3680cb32ea71e49  /app/src/api/main.py
48ee2bb16f467ccbce5357de8feef8227a174e1dcbad4b04a272a1ad2a4ba465  /app/src/api/ai/router.py
8a8124d678c8ef406457ae7bf28852af0f126bded4afb6c801278e7fca08a501  /app/src/infrastructure/llm_service.py
46054216f88bb3a5f21425be31eeae628fbf5b08cbbd4a3b16b734393bd383d8  /app/src/infrastructure/dicom_service.py
cd9357ac95a5475189ba689f626cc7d64a7ed93992b9c031c67d44e434f299b2  /app/src/infrastructure/pdf_service.py
b42507c7fc55764ad1cd40cf204a0bd7fdbdd863a98abd15897c344e71c77c6e  /app/src/core/medical_logic.py
f617d379e8f69fb2c03982c4acbb5f26dd0c9c1c3d8d1460f0fa1c361a56022d  /app/app/ai/prompt_orchestrator.py
d2ada8a63459ed87c4aad2a4d3420b19296bf092658da45d09b5df4c5066b3cc  /app/app/ai/quality_gate.py
4dfc82b45240284afe71e5c9f5c55223fcc5e96c6b25c0c070d23a69d1f5d822  /app/app/ai/auditor.py
1a3a7922f64bc31d81a4d24d0871cf1b568f03310d8e46a95cb3497b0990275f  /app/app/ai/clinical_audit_engine.py
88430d4c8814f533aa51eeaa6c3bacdbac831047eeefaae938106b426c39aca1  /app/app/ai/attention_booster.py
```

## Regla hasta normalizar

Hasta que exista una rama de release que reproduzca producción viva:

1. no deployar desde `C:\Users\WinterOS\Documents\Workspace\IMAGIAX - copia (2)`;
2. no usar scripts que copien archivos sueltos y luego hagan solo `restart`;
3. no cambiar Dockerfile, compose o nginx junto con features clínicas o frontend;
4. no asumir que un archivo copiado al host entra al contenedor si no hay bind mount;
5. verificar el runtime vivo antes de cada intento de deploy.

## Próximo objetivo técnico

Crear una rama de release segura que haga reproducible lo que corre hoy:

```text
prod/current-runtime-baseline
```

Esta rama debe decidir una sola autoridad:

- elegido por evidencia actual: `api.main:create_app --factory`;
- alternativa: volver explícitamente al monolito, pero solo con rollback y validación.

Mientras esa decisión no esté cerrada, el deploy backend debe quedar bloqueado.

## Baseline post-cutover OHIF (2026-04-29)

Fecha de captura: 2026-04-29 (post-cutover validado).

### Baseline backend (sin cambios)

- Contenedor: `fastapi-imagiax`
- Imagen runtime (tag): `imagiax-backend:rollback-precutover-3da43fd`
- Image ID: `sha256:f84e80020e81cd80c2deb56c2bf76012df38e78a02ab7f091e5c677575e50df1`

### Baseline frontend OHIF (normalizado)

- Contenedor: `ohif-imagiax`
- Imagen runtime (tag): `orthanc-imagiax-ohif:prod-18d45af`
- Image ID: `sha256:6d22a20e0b275f9bf66be723b92a0b5160082000b6a53a22b1b150361df2cae4`

Producción **ya no** está corriendo una imagen con naming de canary. El baseline vigente queda normalizado a:

```text
orthanc-imagiax-ohif:prod-18d45af
```

### Rollback OHIF disponible

- `orthanc-imagiax-ohif:rollback-precanonical-canary-e186bec`
- `orthanc-imagiax-ohif:rollback-precutover-3da43fd`
- Ambas tags apuntan al mismo image ID: `sha256:534205c182f9787e9f5f686379b1e5c43cfd5aff165f454e5240c48251a12ba0`

### Compose override remoto activo

Ruta:

```text
/root/imagiax/OHIF Medical Imaging Viewer/orthanc-imagiax/docker-compose.ohif-canary-cutover.override.yml
```

Estado esperado/validado en esa ruta:

```text
orthanc-imagiax-ohif:prod-18d45af
```

### Checks públicos post-cutover

- `GET /api/salud` → `200`
- `GET /` → `302`
- `GET /api/config/frontend` sin auth → `401`

### Issues menores conocidos (no bloqueantes)

- Historial: label `Ver`
- Dashboard: detalles menores pendientes
