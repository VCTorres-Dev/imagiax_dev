# Exploration: IMAGIAX Monolith Refactor

## Current State

El backend de IMAGIAX es un monolito Python/FastAPI compuesto por 9 archivos planos en `/app/`:

| Archivo | Líneas | Responsabilidades mezcladas |
|---|---|---|
| `servidor_api_orthanc_v1.py` | 2,425 | App factory, DICOM processing, Orthanc client, LLM generation, rate limiter, middleware, DICOMweb proxy, payment orchestration |
| `imagiax_admin.py` | 3,518 | Admin REST API, **auth endpoints** (/auth/login /auth/me /auth/logout), PDF generation, login HTML (~400 líneas), admin SPA HTML (~2,600 líneas inline como string Python) |
| `pagos_mp.py` | 3,237 | MercadoPago API client, business logic, webhooks, **HTML de /pagar y /historial (~1,500 líneas inline)** |
| `imagiax_pagos_db.py` | 1,216 | Payment DB layer (bien estructurado) |
| `imagiax_auth.py` | 859 | JWT auth, RBAC, brute-force protection — **no tiene sus propios endpoints** |
| `imagiax_db.py` | 668 | Informe DB CRUD layer |
| `prompt_orchestrator.py` | 830 | Exam classification + prompt assembly + YAML templates |
| `pagos_tbk.py` | 543 | Transbank Oneclick (sync SDK en asyncio executor) |
| `servidor_api_v6.py` | ? | PyTorch model loader (DEVICE, NIH_PATOLOGIAS, UMBRALES, get_model) |

### Grafo de dependencias actual

```
servidor_api_orthanc_v1.py  ← Entry point (app factory)
  ├── imagiax_db
  ├── imagiax_pagos_db  ← importa imagiax_db.get_pool() de forma diferida (anti-circular)
  ├── imagiax_auth  ← importa imagiax_db
  ├── pagos_mp  ← importa imagiax_pagos_db, imagiax_auth
  ├── pagos_tbk  ← importa imagiax_pagos_db, imagiax_auth
  ├── imagiax_admin  ← importa imagiax_auth, imagiax_db, imagiax_pagos_db
  ├── servidor_api_v6  ← PyTorch
  └── prompt_orchestrator
```

### Problemas concretos identificados

1. **Auth endpoints en Admin**: `/auth/login`, `/auth/me`, `/auth/logout` viven en `imagiax_admin.router`. Esto crea acoplamiento falso: el módulo admin tiene que conocer los contratos de auth para servir endpoints que no son administrativos.

2. **HTML embebido como strings Python**: 
   - `imagiax_admin.py` tiene `_ADMIN_HTML_SOURCE` (~2,600 líneas) que se parte en módulo-load via string manipulation (`partition()`, `_slice_or_fail()`). Resultado: `_LOGIN_HTML` y `_ADMIN_HTML`.
   - `pagos_mp.py` tiene `_page_exito`, `_page_pendiente`, `_page_error`, `_plan_card_html` (~1,500 líneas) como funciones que retornan strings HTML.
   - Resultado: editar HTML requiere entender Python y viceversa. Tests son imposibles. IDE highlighting inexistente.

3. **`servidor_api_orthanc_v1.py` es un God Object**: Mezcla app factory + DICOM processing (torch, pydicom, cv2) + Orthanc HTTP client + LLM generation (250+ líneas de `generar_informe_llm`) + rate limiter + middleware + DICOMweb proxy + payment orchestration.

4. **PDF generation inline**: `admin_informe_pdf` en `imagiax_admin.py` (líneas 326–441) genera el PDF con fpdf2 directamente en el handler. Imposible de testear o reutilizar.

5. **Globals/singletons dispersos**: `_orthanc_client`, `_pacs_client` (httpx), `_gemini_client`, `_analysis_limiter`, `_pool` (asyncpg) están distribuidos en múltiples archivos sin una capa de DI.

---

## Affected Areas

- `servidor_api_orthanc_v1.py` — God Object a dividir en al menos 4 módulos
- `imagiax_admin.py` — Extraer HTML, mover auth routes, separar PDF service
- `pagos_mp.py` — Extraer HTML, separar MP client de business logic de routes
- `imagiax_auth.py` — Recibir sus endpoints (login/me/logout)
- `Dockerfile` — COPY list requiere actualización post-refactor

---

## Approaches

### Approach 1: Refactor plano (mismo nivel, más archivos)

Mantener todos los `.py` en `/app/` pero dividir los archivos grandes en archivos nuevos con convención de nombre:

```
app/
├── servidor_api_orthanc_v1.py   ← solo app factory + lifespan
├── imagiax_ai.py                ← DICOM + LLM + Orthanc client
├── imagiax_admin_routes.py      ← solo API routes (/admin/api/*)
├── imagiax_admin_pdf.py         ← PDF service
├── imagiax_auth_routes.py       ← /auth/* endpoints
├── pagos_mp_client.py           ← httpx client + MercadoPago API
├── pagos_mp_routes.py           ← FastAPI routes
...
```

- **Pros**: Sin cambios en Dockerfile más allá de agregar líneas al COPY. Sin cambios en imports de módulos externos. Cero riesgo de circular imports nuevos.
- **Cons**: Escala mal. El directorio raíz se llena de archivos. Sin estructura de paquetes, los imports se vuelven implícitos. No soluciona el problema de HTML embebido.
- **Effort**: Low

### Approach 2: Paquetes Python (`app/` como package root) — RECOMENDADO

Crear packages con `__init__.py` en subdirectorios, extraer HTML a archivos `.html` separados:

```
/app/
├── main.py                     ← Entry point (uvicorn target)
├── core/
│   ├── __init__.py
│   ├── config.py               ← Settings, env vars
│   ├── database.py             ← asyncpg pool (imagiax_db.py)
│   ├── logging.py              ← logging_config.py
│   └── middleware.py           ← rate_limit + request_logging
├── auth/
│   ├── __init__.py
│   ├── service.py              ← imagiax_auth.py (JWT, RBAC, brute-force)
│   └── router.py               ← /auth/login, /auth/me, /auth/logout, /auth/verify
├── admin/
│   ├── __init__.py
│   ├── router.py               ← /admin/api/* routes
│   ├── pdf_service.py          ← fpdf2 PDF generation
│   └── templates/
│       ├── login.html          ← _LOGIN_HTML extraído
│       └── dashboard.html      ← _ADMIN_HTML extraído
├── pagos/
│   ├── __init__.py
│   ├── db.py                   ← imagiax_pagos_db.py
│   ├── mp/
│   │   ├── __init__.py
│   │   ├── client.py           ← _mp_get, _mp_post, _mp_put, _mp_headers
│   │   ├── service.py          ← cobrar_scan_automatico, webhook processing
│   │   ├── router.py           ← FastAPI routes (estado_creditos, etc.)
│   │   └── templates/
│   │       ├── exito.html
│   │       ├── pendiente.html
│   │       ├── error.html
│   │       └── pago.html       ← /pagar page
│   └── tbk/
│       ├── __init__.py
│       └── router.py           ← pagos_tbk.py
├── ai/
│   ├── __init__.py
│   ├── dicom.py                ← dicom_to_tensor, infer_multilabel, analyze_single_instance
│   ├── orthanc.py              ← orthanc_get/post/put, resolve_orthanc_instance_id, etc.
│   ├── llm.py                  ← generar_informe_llm, _generar_informe_fallback
│   └── prompt_orchestrator.py  ← prompt_orchestrator.py (mover tal cual)
├── informes/
│   ├── __init__.py
│   ├── db.py                   ← imagiax_db.py
│   ├── router.py               ← /generar/informe, /informes/*, /resultados/*
│   └── templates/
│       └── ver_informe.html    ← HTML inline de ver_informe (~80 líneas)
└── models/
    ├── __init__.py
    └── servidor_api_v6.py      ← PyTorch model loader (mover tal cual)
```

- **Pros**: Estructura de paquetes estándar Python. Cada carpeta tiene responsabilidad única. HTML editable como HTML real. Imports explícitos (from `app.auth.service import ...`). Testeable por unidad. Escala indefinidamente.
- **Cons**: Requiere actualizar todos los imports en todos los archivos. Requiere actualizar `Dockerfile COPY` para incluir las carpetas. Riesgo de circular imports si no se es cuidadoso con las capas.
- **Effort**: High

### Approach 3: Híbrido — paquetes solo para los módulos grandes

Mantener los archivos chicos como están, crear paquetes solo para los 3 archivos problemáticos:

```
/app/
├── servidor_api_orthanc_v1.py  ← reducido a ~600 líneas (solo app factory)
├── imagiax_auth.py             ← + recibe sus endpoints
├── imagiax_db.py               ← sin cambios
├── prompt_orchestrator.py      ← sin cambios
├── servidor_api_v6.py          ← sin cambios
├── admin/
│   ├── __init__.py
│   ├── router.py
│   ├── pdf_service.py
│   └── templates/
├── pagos/
│   ├── __init__.py
│   ├── db.py
│   ├── mp_client.py
│   ├── mp_routes.py
│   ├── tbk_router.py
│   └── templates/
└── ai/
    ├── __init__.py
    ├── dicom.py
    ├── orthanc.py
    └── llm.py
```

- **Pros**: Cambios quirúrgicos — solo los 3 archivos > 2000 líneas se restructuran. Menos archivos tocados. Riesgo más bajo.
- **Cons**: Inconsistente — algunos módulos son paquetes, otros son archivos sueltos. Los módulos "small" como `imagiax_auth.py` siguen sin estructura de package.
- **Effort**: Medium

---

## Recommendation

**Approach 2 (paquetes Python completo)** es la solución correcta, pero la ejecutamos en 5 fases independientes y desplegables para minimizar riesgo:

### Fases de migración (orden óptimo por riesgo ↑)

```
Fase 1 — Extraer HTML (RIESGO MÍNIMO — solo mover strings, sin lógica)
  ├── imagiax_admin.py: _ADMIN_HTML_SOURCE → admin/templates/login.html + admin/templates/dashboard.html
  ├── pagos_mp.py: _page_exito/_page_pendiente/_page_error → pagos/mp/templates/*.html
  └── servidor_api_orthanc_v1.py: ver_informe HTML inline → informes/templates/ver_informe.html
  Método: usar Jinja2 (ya en el stack via fastapi) o FileResponse con string.format()

Fase 2 — Mover auth endpoints (RIESGO BAJO — cambio de router, sin lógica nueva)
  ├── Crear auth/router.py con /auth/login, /auth/me, /auth/logout
  ├── Mover /auth/verify de servidor_api_orthanc_v1.py → auth/router.py
  └── imagiax_admin.py pierde ~150 líneas

Fase 3 — Separar Admin (RIESGO BAJO — sin dependencias externas nuevas)
  ├── admin/router.py ← /admin/api/* routes
  ├── admin/pdf_service.py ← fpdf2 logic (~120 líneas)
  └── imagiax_admin.py queda como __init__.py re-exportando

Fase 4 — Separar Pagos (RIESGO MEDIO — cobrar_scan_automatico es llamado desde servidor_api)
  ├── pagos/mp/client.py ← _mp_get/_mp_post/_mp_put
  ├── pagos/mp/service.py ← cobrar_scan_automatico, webhook handlers
  ├── pagos/mp/router.py ← FastAPI routes
  ├── pagos/tbk/router.py ← pagos_tbk.py movido
  └── pagos/db.py ← imagiax_pagos_db.py movido (con import diferido resuelto)

Fase 5 — Dividir servidor_api_orthanc_v1.py (RIESGO ALTO — God Object central)
  ├── ai/dicom.py ← dicom_to_tensor, infer_multilabel, analyze_single_instance
  ├── ai/orthanc.py ← orthanc_get/post/put, resolve_*, find_*, etc.
  ├── ai/llm.py ← generar_informe_llm, _generar_informe_fallback
  ├── informes/router.py ← /generar/informe, /informes/*, /resultados/*
  ├── core/middleware.py ← rate_limit_middleware, request_logging_middleware
  └── main.py ← solo app factory + lifespan (objetivo: < 150 líneas)
```

### Decisiones de diseño clave

1. **Jinja2 para templates** — FastAPI ya incluye Jinja2 como dependencia opcional. Los templates se cargan con `Jinja2Templates(directory="...")`. La variable de entorno `VIEWER_URL` pasa al template como `context`. El hack de `_slice_or_fail` desaparece.

2. **Circular imports — solución arquitectural**: La capa de dependencias es:
   ```
   core/database.py ← auth/service.py ← pagos/* ← admin/* ← informes/* ← ai/* ← main.py
   ```
   Cada capa solo importa de capas inferiores. `core/database.py` no importa nada del proyecto.

3. **Dockerfile**: Cambiar las líneas `COPY *.py ./` por:
   ```dockerfile
   COPY main.py logging_config.py imagiax_model.py ./
   COPY core/ ./core/
   COPY auth/ ./auth/
   COPY admin/ ./admin/
   COPY pagos/ ./pagos/
   COPY ai/ ./ai/
   COPY informes/ ./informes/
   COPY models/ ./models/
   COPY prompts/ ./prompts/
   ```

4. **Backward compatibility**: Durante la migración, cada archivo viejo se convierte en un shim que re-exporta desde el nuevo paquete. Ejemplo:
   ```python
   # imagiax_auth.py — shim durante transición
   from auth.service import *  # noqa: F401,F403
   from auth.service import get_current_user, UserPayload, require_role
   ```
   Esto permite migrar fase por fase sin romper el servidor.

---

## Risks

- **Import circular en ai/**: `ai/llm.py` llama `cobrar_scan_automatico` de `pagos/mp/service.py` y `cobrar_scan_oneclick` de `pagos/tbk`. Solución: pasar las funciones de cobro como parámetros (injection) en `generar_informe` en vez de importarlas directamente.
- **_ADMIN_HTML_SOURCE split logic**: El hack de `partition()` + `_slice_or_fail()` desaparece solo si los templates se convierten a archivos `.html` reales. Si se usan como strings para Jinja2, hay que reescribir la lógica de construcción. Solución recomendada: dos archivos HTML separados (`login.html`, `dashboard.html`) con variables Jinja2 `{{ viewer_url }}`, `{{ return_to }}`.
- **`_pool` singleton**: `imagiax_db.py` expone `get_pool()` que es llamado vía import diferido desde `imagiax_pagos_db.py`. Con la estructura de paquetes, esto se resuelve en `core/database.py` con `get_pool()` como función pública.
- **Deploy en producción**: Cada fase debe testearse en staging (copia local) antes de SCP a imagiax.dev. El servidor usa Docker con `COPY` — se necesita rebuild de la imagen en cada fase.
- **Jinja2 templates con f-strings**: El HTML de pagos usa f-strings pesados con HTML embebido. Convertirlos a Jinja2 `{{ variable }}` requiere revisar cada variable de contexto. Riesgo de omitir una variable y tener un template con `{{ }}` visible en producción.

---

## Ready for Proposal

**Sí** — hay suficiente información para crear la propuesta formal (sdd-propose).

El orchestrator debe informar al usuario:
1. La exploración está completa y persistida en Engram
2. El plan recomendado es Approach 2 (paquetes completos) en 5 fases
3. Antes de arrancar necesitamos confirmar: **¿arrancamos con Fase 1 (solo HTML extraction, cero riesgo lógico)?** o **¿queremos el plan completo para las 5 fases de una?**
4. Confirmar si se quiere Jinja2 o una solución más simple (FileResponse con `.read_text()` + `.format()`)
