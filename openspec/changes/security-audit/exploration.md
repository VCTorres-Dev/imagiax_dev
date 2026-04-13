# Exploration: IMAGIAX — Security Audit Completo

**Fecha**: 2026-04-03  
**Auditor**: SDD Explore Agent  
**Alcance**: Plataforma IMAGIAX completa — pagos, IA, backend, auth, infraestructura

---

## Resumen Ejecutivo

Se identificaron **27 hallazgos de seguridad** distribuidos en 4 dominios:

| Severidad | Pagos/AI | Backend/Auth | Infra | Total |
|-----------|----------|--------------|-------|-------|
| 🔴 CRITICAL | 4 | 3 | 0 | **7** |
| 🟠 HIGH | 4 | 2 | 2 | **8** |
| 🟡 MEDIUM | 4 | 3 | 1 | **8** |
| 🔵 LOW | 3 | 3 | 1 | **7** |
| ✅ INFO | 9+ | 10+ | — | 19+ |

**Riesgo más alto**: Path Traversal en proxy DICOMweb (SEC-BE-003) permite acceso no autorizado a TODA la API de Orthanc. IDOR en `/tbk/cobrar` (SEC-PAY-003) permite que cualquier usuario autenticado cargue dinero en la tarjeta de otro usuario.

---

## Stack Auditado

- **Backend**: FastAPI 0.129 + Uvicorn, Python 3.11, puerto 8010
- **Base de datos**: PostgreSQL via asyncpg (raw queries, sin ORM)
- **PACS**: Orthanc en localhost:8042 (DICOMweb + REST API)
- **IA/ML**: PyTorch ResNet50-512-all (TorchXRayVision) + Google Gemini 2.5 Flash Vision
- **Pagos**: MercadoPago (webhook + tarjeta guardada) + Transbank Webpay Oneclick
- **Auth**: JWT (PyJWT) + bcrypt 12 rounds, RBAC (superadmin/admin/medico/recepcion)
- **Frontend**: OHIF Viewer (React) puerto 3000 + panel admin SSR (FastAPI)
- **Infra**: Dockerfile con usuario no-root; sin docker-compose encontrado

---

## Archivos Auditados

```
├── servidor_api_orthanc_v1.py   — API principal (2300+ líneas) — AUDITADO
├── imagiax_auth.py              — Auth JWT, RBAC, bcrypt — AUDITADO
├── imagiax_db.py                — DB layer informes médicos — AUDITADO
├── imagiax_pagos_db.py          — DB layer pagos y créditos — AUDITADO
├── pagos_mp.py                  — MercadoPago router — AUDITADO
├── pagos_tbk.py                 — Transbank Webpay OneClick router — AUDITADO
├── imagiax_model.py             — Módulo inferencia PyTorch — AUDITADO
├── Dockerfile                   — Imagen de producción — AUDITADO
├── .env                         — Credenciales dev — AUDITADO
├── .env.production              — Credenciales prod — AUDITADO (via compacted context)
└── .gitignore                   — Política de secretos — AUDITADO
```

**No auditados** (fuera de alcance o no encontrados):
- `imagiax_admin.py` — panel SSR (HTML/JS/CSS en Python)
- `docker-compose*.yml` — no encontrado en el repositorio
- `nginx*.conf` — no encontrado
- Extensión OHIF `ohif-extension-imagiax-ai/`

---

## Hallazgos — CRITICAL 🔴

### SEC-PAY-001 — MP Webhook bypassed en development
- **Archivo**: `pagos_mp.py`
- **Descripción**: `MP_WEBHOOK_SECRET` no está seteada en `.env` (solo en `.env.production`). El flag `_IS_TEST=True` (derivado de `MP_ACCESS_TOKEN.startswith("TEST-")`) omite la validación HMAC completamente. Cualquier POST a `/pagos/webhook` es aceptado en dev.
- **Prueba**: `.env` → `# MP_WEBHOOK_SECRET=` (comentada). Código: `if _IS_TEST: pass # skip signature check`
- **Impacto**: En development (y cualquier entorno con token TEST-*), atacante puede enviar eventos falsos de pago aprobado para acreditar créditos sin pagar.
- **Recomendación**: Requerir `MP_WEBHOOK_SECRET` siempre, incluso en testing. Usar webhooks de sandbox con secret real.
- **CWE**: CWE-345 (Insufficient Verification of Data Authenticity)

### SEC-PAY-002 — /pagos/exito sin autenticación
- **Archivo**: `pagos_mp.py`
- **Descripción**: El callback GET `/pagos/exito` que acredita créditos no tiene `Depends(get_current_user)`. Cualquier usuario no autenticado puede invocar el endpoint con un `payment_id` real.
- **Prueba**: `@app.get("/pagos/exito")` sin dependencia de autenticación. Parámetro `payment_id` viene del query string.
- **Impacto**: Cualquier persona (sin cuenta) puede disparar la acreditación de créditos en una cuenta si conoce un `payment_id` válido.
- **Recomendación**: Agregar `user: UserPayload = Depends(get_current_user)` al endpoint. Validar que el `payment_id` pertenece al usuario autenticado.
- **CWE**: CWE-306 (Missing Authentication for Critical Function)

### SEC-PAY-003 — IDOR en /tbk/cobrar (cobro a tarjeta de otro usuario)
- **Archivo**: `pagos_tbk.py`
- **Descripción**: El endpoint `POST /tbk/cobrar` requiere solo `get_current_user` (cualquier rol). El body incluye `username` del objetivo del cobro. Cualquier médico autenticado puede pasar el `username` de otro usuario y cargar dinero en su tarjeta Transbank registrada.
- **Prueba**: `CobrarRequest.username` es campo libre. No hay verificación de que `request.username == current_user.username`.
- **Impacto**: IDOR crítico. Médico A puede cobrar en la tarjeta de Médico B o de cualquier paciente. Combinado con SEC-PAY-004 (monto libre), el atacante controla el monto.
- **Recomendación**: Cambiar a `require_role("admin")` O eliminar el campo `username` y usar siempre `user.user_id` del token JWT.
- **CWE**: CWE-639 (Authorization Bypass Through User-Controlled Key)

### SEC-PAY-004 — Monto de cobro controlado por el cliente
- **Archivo**: `pagos_tbk.py`
- **Descripción**: `CobrarRequest.amount` viene del body del request con única restricción `ge=50`. Cualquier usuario autenticado puede enviar un monto arbitrario.
- **Prueba**: `class CobrarRequest: amount: int = Field(..., ge=50)`
- **Impacto**: Combinado con SEC-PAY-003, atacante puede cobrar $1.000.000 CLP en la tarjeta de cualquier usuario registrado.
- **Recomendación**: Eliminar `amount` del request body. El monto debe ser siempre `PRECIO_SCAN_CLP` (constante del servidor) o derivarse de un `plan_id` validado server-side.
- **CWE**: CWE-20 (Improper Input Validation)

### SEC-BE-003 — Path Traversal en proxy DICOMweb
- **Archivo**: `servidor_api_orthanc_v1.py`, función `pacs_dicom_proxy()`
- **Descripción**: El parámetro `path: str` de la ruta `/pacs/dicom-web/{path}` se concatena directamente en `upstream_url = f"{ORTHANC_BASE_URL}/dicom-web/{path}"` sin sanitización. Permite secuencias `../ ` para salir del prefijo `/dicom-web/` y acceder a cualquier endpoint de la API REST de Orthanc.
- **Prueba**: `GET /pacs/dicom-web/../instances` → Orthanc responde con lista de todas las instancias. `GET /pacs/dicom-web/../tools/execute-script` → acceso al scripting Lua de Orthanc si está habilitado.
- **Impacto**: Acceso completo autenticado a toda la API de Orthanc (listar/descargar/eliminar estudios, modificar metadatos DICOM, acceder a herramientas admin de Orthanc). La autenticación Basic Auth la inyecta el proxy automáticamente con las credenciales de IMAGIAX.
- **Recomendación**: Agregar validación `if ".." in path or path.startswith("/"):` → 400. Usar `pathlib.PurePosixPath` para normalizar y verificar que la ruta permanece bajo `/dicom-web/`.
- **CWE**: CWE-22 (Path Traversal)

### SEC-BE-002 — Proxy DICOMweb no verifica tokens revocados
- **Archivo**: `servidor_api_orthanc_v1.py`, función `pacs_dicom_proxy()`
- **Descripción**: El proxy llama a `verify_session_cookie_token()` que verifica solo la firma JWT (sin consulta a DB). Un usuario eliminado o con logout global sigue accediendo al proxy hasta que expira el JWT.
- **Prueba**: `imagiax_auth.verify_session_cookie_token(token)` → solo `_jwt.decode(...)`, sin `is_jti_revoked()` ni `is_user_revoked()`.
- **Impacto**: Usuario revocado/eliminado puede acceder a imágenes DICOM de todos los pacientes durante hasta 168 horas (JWT_REMEMBER_HOURS).
- **Recomendación**: Reemplazar `verify_session_cookie_token()` con `decode_token()` + `is_user_revoked()` en el proxy. O usar tokens de corta duración para el proxy con refresh automático.
- **CWE**: CWE-613 (Insufficient Session Expiration)

### SEC-BE-001 — /health/db sin autenticación expone información interna
- **Archivo**: `servidor_api_orthanc_v1.py`
- **Descripción**: `GET /health/db` no requiere JWT. Si Postgres no responde, retorna mensajes de error detallados: `f"PostgreSQL no responde: {exc!s}"`. El error puede contener hostname, puerto y nombre de base de datos.
- **Impacto**: Expone topología interna de red y configuración de base de datos a internet.
- **Recomendación**: Proteger con JWT o restringir a red interna via nginx. O solo retornar `{"ok": false}` sin detalles del error en producción.
- **CWE**: CWE-200 (Information Exposure)

---

## Hallazgos — HIGH 🟠

### SEC-PAY-005 — TOCTOU en acreditar_pago() (doble gasto)
- **Archivo**: `imagiax_pagos_db.py`
- **Descripción**: La función hace SELECT para verificar si el pago ya fue procesado, y luego INSERT — sin transacción atómica. Bajo carga concurrente, dos requests simultáneos pueden pasar el SELECT y ambos ejecutar el INSERT.
- **Impacto**: Un usuario podría acreditar el mismo pago dos veces (double-spend).
- **Recomendación**: Envolver SELECT + INSERT en `async with conn.transaction()`. Aprovechar el `ON CONFLICT (mp_payment_id) DO NOTHING` ya existente como única fuente de idempotencia.
- **CWE**: CWE-362 (Concurrent Execution using Shared Resource)

### SEC-PAY-006 — Flag _IS_TEST derivado del prefijo del token
- **Archivo**: `pagos_tbk.py`, `pagos_mp.py`
- **Descripción**: `_IS_TEST = MP_ACCESS_TOKEN.startswith("TEST-")`. Si un token de producción accidentalmente comienza con "TEST-", todos los cobros son simulados.
- **Impacto**: Escenario poco probable pero de consecuencias catastróficas (todos los scans serían gratuitos).
- **Recomendación**: Usar una variable explícita `IMAGIAX_PAYMENT_MODE=production|testing` independiente del token. Validar al inicio que en `APP_ENV=production`, `IMAGIAX_PAYMENT_MODE` también sea `production`.
- **CWE**: CWE-807 (Reliance on Untrusted Inputs in a Security Decision)

### SEC-PAY-007 — cobrar_scan_automatico() siempre aprueba en TEST mode
- **Archivo**: `pagos_tbk.py`
- **Descripción**: En modo test, `cobrar_scan_oneclick()` retorna `(True, f"simulated_{buy_order}", None)` sin ningún cobro real.
- **Impacto**: Si `.env` con `TBK_ENVIRONMENT=integration` llega accidentalmente a producción, todos los scans son gratis.
- **Recomendación**: Ver SEC-PAY-006. Agregar startup check: si `APP_ENV=production` y `TBK_ENVIRONMENT=integration`, lanzar RuntimeError.
- **CWE**: CWE-807

### SEC-PAY-008 — Credenciales de producción en .env.production en texto plano
- **Archivo**: `.env.production`
- **Descripción**: El archivo contiene `GEMINI_API_KEY` real, `JWT_SECRET` real, `IMAGIAX_ADMIN_PASSWORD=ImagiaxAdmin2024!`, y `POSTGRES_URL` con contraseña.
- **Prueba**: `.env.production` existe en el directorio del proyecto (aunque en .gitignore).
- **Impacto**: Si el servidor o el laptop del developer es comprometido, todas las credenciales de producción quedan expuestas de una vez.
- **Recomendación**: Usar un secrets manager (HashiCorp Vault, AWS Secrets Manager, GCP Secret Manager). La misma key de Gemini NO debe usarse en dev y prod.
- **CWE**: CWE-312 (Cleartext Storage of Sensitive Information)

### SEC-AI-001 — max_instances=500 permite DoS vía inferencia masiva
- **Archivo**: `servidor_api_orthanc_v1.py`, `GenerarInformeRequest`
- **Descripción**: `max_instances: int = Field(default=20, ge=1, le=500)`. Un usuario con acceso puede solicitar análisis de 500 instancias + 2 llamadas Gemini Vision.
- **Impacto**: DoS costoso: bloqueo de GPU por ~5-10 minutos, agotamiento de quota Gemini, factura de Gemini inflada.
- **Recomendación**: Reducir `le=500` a `le=50` para `GenerarInformeRequest`. Agregar límite global de tokens Gemini por usuario/día.
- **CWE**: CWE-400 (Uncontrolled Resource Consumption)

### SEC-AI-002 — Transbank SDK calls son síncronas dentro de handlers async
- **Archivo**: `pagos_tbk.py`
- **Descripción**: `inscription.start()`, `inscription.finish()`, `transaction.authorize()` son métodos síncronos del SDK Transbank que bloquean el event loop de asyncio.
- **Impacto**: Un cobro TBK (que puede tardar 2-10s) bloquea TODAS las requests concurrentes del servidor durante ese tiempo.
- **Recomendación**: Envolver en `await asyncio.get_event_loop().run_in_executor(None, sdk_call)` para mover la llamada a un thread pool.
- **CWE**: CWE-400 (Uncontrolled Resource Consumption)

### SEC-INFRA-001 — Misma key Gemini en dev y producción
- **Archivo**: `.env`, `.env.production`
- **Descripción**: `GEMINI_API_KEY=AIzaSyCHZ3JDxddgVQmTTirW6GGtilSG-BHfHSQ` — idéntica en ambos archivos.
- **Impacto**: Compromiso del entorno de desarrollo (laptop) expone la key de producción, con potencial de factura elevada o data leakage.
- **Recomendación**: Usar keys separadas por entorno. Rotar la key de producción inmediatamente.
- **CWE**: CWE-321 (Use of Hard-coded Cryptographic Key)

### SEC-INFRA-002 — .env.production con credenciales admin en plaintext
- **Archivo**: `.env.production`
- **Descripción**: `IMAGIAX_ADMIN_PASSWORD=ImagiaxAdmin2024!` — contraseña predecible con patrón `NombreProducto+Año+Símbolo`.
- **Impacto**: Si se filtra el archivo (backup, snapshot), acceso inmediato a la cuenta superadmin de producción.
- **Recomendación**: Usar contraseñas verdaderamente aleatorias (ej: `openssl rand -base64 32`). Nunca usar nombres propios + año como contraseña admin.
- **CWE**: CWE-521 (Weak Password Requirements)

---

## Hallazgos — MEDIUM 🟡

### SEC-BE-006 — /informes/{id}/ver expone PHI sin aislamiento de roles
- **Archivo**: `servidor_api_orthanc_v1.py`, `ver_informe()`
- **Descripción**: El endpoint requiere `get_current_user` (cualquier rol). Un usuario `recepcion` puede ver el contenido de cualquier informe médico (patient_name, modalidad, texto del informe) conociendo o adivinando el UUID.
- **Recomendación**: Agregar verificación de rol (`medico` o `admin`) o verificar que el informe pertenece al usuario. Los UUIDs de informe no son secretos (se generan aleatoriamente pero podrían adivinarse si hay un listado).
- **CWE**: CWE-639

### SEC-BE-007 — Sin Content Security Policy (CSP)
- **Archivo**: `servidor_api_orthanc_v1.py`, `security_headers_middleware()`
- **Descripción**: Faltan headers CSP. El panel `/admin` ejecuta JavaScript inline y carga CSS inline — una CSP mal configurada podría romperlo, pero la ausencia de CSP deja la app sin protección contra XSS de terceros.
- **Recomendación**: Agregar `Content-Security-Policy: default-src 'self'; script-src 'self' 'unsafe-inline'; ...` como mínimo.
- **CWE**: CWE-1021

### SEC-PAY-009 — raw_webhook/raw_response almacenan datos de tarjeta (PCI-DSS)
- **Archivo**: `imagiax_pagos_db.py`
- **Descripción**: Las respuestas crudas de MP y TBK se guardan como JSONB. Pueden incluir `card_holder_name`, primeros 6 y últimos 4 dígitos de tarjeta, banco emisor.
- **Recomendación**: Filtrar/mascarar campos sensibles antes de persistir. O no guardar el raw response.
- **CWE**: CWE-312

### SEC-AI-003 — Excepción TBK en URL de redirect (information leakage)
- **Archivo**: `pagos_tbk.py`
- **Descripción**: `f"{IMAGIAX_BASE_URL}/pagar?tbk_status=error&msg={exc}"` — el mensaje de excepción del SDK TBK queda en la URL del navegador, visible en logs del servidor, referrer headers y analytics.
- **Recomendación**: Usar un código de error opaco en la URL (`msg=tbk_auth_failed`) y loggear el detalle server-side.
- **CWE**: CWE-209 (Generation of Error Message Containing Sensitive Information)

### SEC-BE-008 — CORS no valida que origins no sean wildcards
- **Archivo**: `servidor_api_orthanc_v1.py`
- **Descripción**: `allow_credentials=True` es correcto con origins explícitos, pero no hay validación en startup de que `CORS_ORIGINS` no contiene `*`.
- **Recomendación**: Agregar check en lifespan: `if "*" in _cors_origins: raise RuntimeError("CORS_ORIGINS no puede contener wildcard cuando allow_credentials=True")`.
- **CWE**: CWE-942 (Overly Permissive Cross-domain Whitelist)

### SEC-BE-009 — /informes/listado permite extraer 500 registros con PHI por request
- **Archivo**: `servidor_api_orthanc_v1.py`, `listado_informes()`
- **Descripción**: Límite `le=500` sin rate limiting. Una cuenta médica comprometida puede extraer toda la base de datos de informes.
- **Recomendación**: Reducir límite máximo a 50. Agregar rate limiting en este endpoint.
- **CWE**: CWE-359 (Exposure of Private Personal Information to an Unauthorized Actor)

### SEC-PAY-010 — external_reference malformado genera crédito incorrecto
- **Archivo**: `pagos_mp.py`
- **Descripción**: `ext_ref.get("plan_id", "pack_individual")` — si `external_reference` falta o está malformado, se acredita `pack_individual` (1 crédito) silenciosamente.
- **Recomendación**: Validar explícitamente la presencia y formato de `external_reference`. Fallar con log de error en lugar de usar default.
- **CWE**: CWE-20

### SEC-INFRA-003 — X-Forwarded-For puede ser spoofed en entorno sin nginx
- **Archivo**: `Dockerfile`, `servidor_api_orthanc_v1.py`
- **Descripción**: `--forwarded-allow-ips 172.16.0.0/12,...` confía en headers X-Forwarded-For de redes Docker privadas. Si uvicorn es expuesto directamente, cualquier cliente puede enviar X-Forwarded-For para bypassear rate limiting basado en IP.
- **Recomendación**: Validar en startup que si `APP_ENV=production`, existe un proxy verificado en frente. O usar `--forwarded-allow-ips` solo con la IP del nginx específico.
- **CWE**: CWE-348 (Use of Less Trusted Source)

---

## Hallazgos — LOW 🔵

### SEC-AI-004 — Sin límite de tamaño explícito en preprocesar() para llamadas directas
- **Archivo**: `imagiax_model.py`
- **Descripción**: `preprocesar()` no tiene validación de tamaño. `cv2.imdecode` con input inválido retorna None y se lanza `ValueError` (manejado). El guard de 200MB está en `dicom_to_tensor()`, pero no en todas las rutas de llamada.
- **CWE**: CWE-400

### SEC-PAY-011 — MP_PUBLIC_KEY expuesto en HTML de /pagar
- **Archivo**: `imagiax_admin.py` (page `/pagar`)
- **Descripción**: `MP_PUBLIC_KEY` visible en el HTML renderizado. By design del SDK de MP, pero notable.
- **Impacto**: Mínimo (es una public key, no el access token). Sin riesgo directo.

### SEC-AI-005 — Rate limiter en memoria, IP-only, no compartido entre workers
- **Archivo**: `servidor_api_orthanc_v1.py`, `_RateLimiter`
- **Descripción**: Actualmente OK (workers=1). Si se aumentan workers o se usa Redis, el rate limiter se vuelve inefectivo.
- **Recomendación**: Documentar el límite de workers=1 como requerimiento. Usar Redis si se escala.
- **CWE**: CWE-307

### SEC-BE-010 — Sin HSTS (comentario documenta que lo maneja nginx)
- **Archivo**: `servidor_api_orthanc_v1.py`
- **Descripción**: Correcto si nginx siempre está en frente. Riesgo residual si se expone directamente.
- **CWE**: CWE-311

### SEC-BE-011 — /robots.txt expone paths internos
- **Archivo**: `servidor_api_orthanc_v1.py`
- **Descripción**: robots.txt lista `/admin`, `/auth/`, `/pacs/`, `/docs`. Útil para recon.
- **Impacto**: Mínimo (estos paths son descubribles por otros medios).

### SEC-BE-012 — String interpolation en SQL INTERVAL
- **Archivo**: `servidor_api_orthanc_v1.py`, `_periodic_cleanup()`
- **Descripción**: `"($1 || ' days')::INTERVAL"` con `str(int(os.getenv(...)))`. Seguro porque es int, pero patrón frágil.
- **CWE**: CWE-89 (precaución, no vulnerable actualmente)

### SEC-INFRA-004 — Sin docker-compose en repositorio
- **Descripción**: No se encontró `docker-compose*.yml` en el proyecto. Despliegue de producción no documentado en código.

---

## Positivos Destacados ✅

- **No SQL injection**: Todas las queries usan parámetros `$1`, `$2` de asyncpg. No hay f-strings en SQL.
- **bcrypt 12 rounds**: Correcto para datos médicos PHI.
- **JTI revocation**: Sistema completo de blacklist de tokens implementado.
- **Superadmin indestructible**: Protección de código contra eliminación/desactivación del superadmin.
- **Deduplicación de informes**: `buscar_informe_reciente()` con ventana de 120s evita doble análisis.
- **DICOM guards**: Validación de tamaño (200MB), modalidad whitelist, pixel array oversized check.
- **Secret en startup**: `imagiax_auth.py` lanza `RuntimeError` si `JWT_SECRET` está vacío al arrancar.
- **No PII en Gemini**: Solo `patient_age` llega al prompt — nunca `patient_name` o `patient_id`.
- **html.escape()**: Usado en `/informes/{id}/ver` antes de renderizar.
- **Audit log completo**: Login, registro, generación de informe, decisión médica — todo auditado.
- **Cookie SameSite=Strict + httpOnly**: Protección sólida contra CSRF y XSS.
- **docs_url=None en producción**: Swagger/ReDoc deshabilitados en producción.
- **.env gitignored**: Confirmado que ni `.env` ni `.env.production` están en el historial de git.
- **Dockerfile non-root user**: uid=1001 dedicado.

---

## Affected Areas

| Archivo | Hallazgos |
|---------|-----------|
| `pagos_tbk.py` | SEC-PAY-003, SEC-PAY-004, SEC-PAY-006, SEC-PAY-007, SEC-AI-002, SEC-AI-003 |
| `pagos_mp.py` | SEC-PAY-001, SEC-PAY-002, SEC-PAY-010, SEC-PAY-011 |
| `imagiax_pagos_db.py` | SEC-PAY-005, SEC-PAY-009 |
| `servidor_api_orthanc_v1.py` | SEC-BE-001, SEC-BE-002, SEC-BE-003, SEC-BE-006 a SEC-BE-012, SEC-AI-001, SEC-AI-005 |
| `imagiax_auth.py` | SEC-BE-004, SEC-BE-005 |
| `.env` / `.env.production` | SEC-PAY-008, SEC-INFRA-001, SEC-INFRA-002 |
| `Dockerfile` | SEC-INFRA-003 |
| (ausente) | SEC-INFRA-004 |

---

## Recomendaciones Prioritarias

### Crítico — Acción inmediata

1. **SEC-BE-003** (Path Traversal proxy): Agregar `if ".." in path or not path.replace("/", "").replace("-", "").replace(".", "").isalnum():` antes de construir `upstream_url`. Bloquear rutas que salgan del prefijo DICOMweb.

2. **SEC-PAY-003 + SEC-PAY-004** (IDOR + monto libre en TBK): Eliminar `username` y `amount` de `CobrarRequest`. Usar siempre `current_user.user_id` y `PRECIO_SCAN_CLP` del servidor.

3. **SEC-PAY-002** (Webhook MP sin auth): Agregar `user: UserPayload = Depends(get_current_user)` a `/pagos/exito`.

4. **SEC-BE-002** (Proxy DICOMweb bypass revocación): Reemplazar `verify_session_cookie_token()` con llamada completa a `get_current_user()` en el proxy.

### Alta prioridad

5. **SEC-PAY-008 + SEC-INFRA-001** (Credenciales prod): Rotar key Gemini de producción. Separar keys por entorno. Usar secrets manager.

6. **SEC-AI-002** (TBK SDK síncrono): `await asyncio.get_event_loop().run_in_executor(None, tbk_call)`.

7. **SEC-INFRA-002** (Contraseña admin predecible): Cambiar `ImagiaxAdmin2024!` por contraseña aleatoria de 32+ caracteres.

---

## Approaches para Remediación

### Opción A — Remediación incremental (recomendada)
Priorizar los 4 críticos esta semana, los 8 high en el siguiente sprint.
- **Pros**: Menor riesgo de regresión, cambios aislados, fácil de revisar
- **Cons**: Exposición temporal a vulnerabilidades high
- **Esfuerzo**: Bajo-Medio

### Opción B — Refactoring de seguridad unificado
Abordar todos los hallazgos en un solo PR de seguridad.
- **Pros**: Una sola revisión, coherencia
- **Cons**: PR gigante, mayor riesgo de introducir bugs, más difícil de revisar
- **Esfuerzo**: Alto

### Recommendation
**Opción A**: Crear 2 cambios en paralelo:
1. `fix/security-critical` — SEC-BE-003, SEC-PAY-002, SEC-PAY-003, SEC-PAY-004, SEC-BE-002
2. `fix/security-high` — SEC-PAY-005, SEC-PAY-008, SEC-AI-001, SEC-AI-002, SEC-INFRA-001+002

---

## Ready for Proposal

**Sí** — con suficiente información para crear una propuesta de cambio (`security-audit-fix`) que incluya los 7 hallazgos críticos como alcance mínimo. Los hallazgos high y medium pueden ser un segundo cambio separado.

---

---

# Auditoría Fase 2 — Frontend, Admin Panel e Infraestructura

**Fecha**: 2026-04-03  
**Auditor**: SDD Explore Agent (continuación)  
**Alcance**: `imagiax_admin.py` (panel SSR), `ohif-extension-imagiax-ai/`, `OHIF Medical Imaging Viewer/` (configs + nginx + orthanc)

---

## Archivos Auditados (Fase 2)

```
├── imagiax_admin.py                                        — Panel admin SSR (~2900 líneas Python + HTML/CSS/JS embebido)
├── ohif-extension-imagiax-ai/src/ImagiaxAiPanel.tsx       — Panel OHIF React principal (~1200+ líneas)
├── ohif-extension-imagiax-ai/src/services/imagiaxApi.ts   — Cliente API + gestión de token auth
├── ohif-extension-imagiax-ai/src/index.tsx                — Entry point de la extensión
├── ohif-extension-imagiax-ai/src/getPanelModule.tsx        — Registro del panel en OHIF
├── ohif-extension-imagiax-ai/src/getCommandsModule.ts     — Comandos OHIF
├── OHIF Medical Imaging Viewer/.../config/default.js       — Config OHIF por defecto
├── OHIF Medical Imaging Viewer/.../config/docker-nginx-orthanc.js
├── OHIF Medical Imaging Viewer/.../config/local_orthanc.js
├── OHIF Medical Imaging Viewer/.../config/docker-nginx-orthanc-keycloak.js
├── OHIF Medical Imaging Viewer/.../config/google.js        — Config OAuth Google (contiene client_id)
├── OHIF Medical Imaging Viewer/.../public/serve.json       — Solo rewrites, sin headers de seguridad
├── OHIF Medical Imaging Viewer/.../public/_headers         — Vacío
├── OHIF Medical Imaging Viewer/orthanc-imagiax/config/nginx-proxy.conf  — Nginx reverso principal
└── OHIF Medical Imaging Viewer/orthanc-imagiax/config/orthanc.json      — Config Orthanc PACS
```

---

## Resumen Fase 2

Se identificaron **24 hallazgos adicionales** distribuidos en 2 dominios nuevos:

| Severidad | Admin Panel | Frontend/OHIF | Infraestructura | Total Fase 2 |
|-----------|-------------|---------------|-----------------|--------------|
| 🔴 CRITICAL | 0 | 0 | 1 | **1** |
| 🟠 HIGH | 3 | 3 | 1 | **7** |
| 🟡 MEDIUM | 4 | 3 | 2 | **9** |
| 🔵 LOW | 2 | 3 | 0 | **5** |
| ✅ INFO | 2 | 1 | 1 | **4** |

**Nota sobre SEC-ADMIN-001**: La ausencia de `X-Frame-Options` y CSP en el handler Python es **mitigada en producción por nginx** (que agrega estos headers para `/admin`). Sin embargo, en desarrollo local (sin nginx), el panel está desprotegido.

**Hallazgo más grave (Fase 2)**: `orthanc.json` contiene la credencial del PACS (`ImagiaxPACS_2026!`) hardcodeada en el repositorio (SEC-INFRA-NEW-001 — CRITICAL). Cualquier persona con acceso al repo tiene acceso completo al PACS de producción.

---

## Hallazgos — Admin Panel (`imagiax_admin.py`)

### SEC-ADMIN-001 — Sin headers de seguridad en respuesta directa del handler FastAPI
- **Archivo**: `imagiax_admin.py`, función `admin_panel()` (~línea 500)
- **Descripción**: `GET /admin` retorna `HTMLResponse` con solo `Cache-Control` y `Pragma`. No incluye `X-Frame-Options`, `Content-Security-Policy`, `X-Content-Type-Options`. En producción, nginx los agrega — pero en desarrollo local (puerto 8010 directo, sin nginx) el panel está completamente desprotegido.
- **Prueba**: El handler solo añade dos headers. El middleware `security_headers_middleware()` en `servidor_api_orthanc_v1.py` no se aplica a `imagiax_admin.py` ya que es un router separado.
- **Impacto**: Clickjacking posible en desarrollo. En producción mitigado por nginx. Si nginx falla o se bypasea, el panel queda expuesto.
- **Recomendación**: Agregar `X-Frame-Options: DENY`, `Content-Security-Policy`, `X-Content-Type-Options: nosniff` directamente en el handler de `imagiax_admin.py`. No depender exclusivamente de nginx para headers de seguridad del panel admin.
- **Severidad**: MEDIUM (mitigado en prod por nginx, crítico en dev)
- **CWE**: CWE-1021 (Improper Restriction of Rendered UI Layers)

### SEC-ADMIN-002 — Handler `window.onerror` expone stack traces vía `alert()` en producción
- **Archivo**: `imagiax_admin.py`, bloque `_ADMIN_HTML_SOURCE`, sección `<script>` inicial
- **Descripción**: El JS embebido tiene `window.onerror = function(message, source, lineno, colno, error) { alert(message + ...); const el = document.createElement(...); el.innerHTML = ...; document.body.appendChild(el); }`. En producción, cualquier error JS muestra un `alert()` al usuario con el mensaje de error y un div en el DOM con la traza, exponiendo información interna.
- **Impacto**: Un atacante puede provocar un error JS intencionalmente para obtener información del stack. Degradación de UX en producción. Potencial injection vía `el.innerHTML` si `message` contiene HTML (depende del origen del error).
- **Recomendación**: En producción, solo loggear errores a consola o a un servicio de error tracking (Sentry). Nunca usar `alert()` en producción.
- **Severidad**: MEDIUM
- **CWE**: CWE-209 (Generation of Error Message Containing Sensitive Information)

### SEC-ADMIN-003 — XSS en `loadInformes()`: campos de paciente no escapados en `innerHTML`
- **Archivo**: `imagiax_admin.py`, función JS `loadInformes()`
- **Descripción**: La función `renderUsers()` usa correctamente `escHtml()` para `nombre` y `email`. Sin embargo, `loadInformes()` interpola directamente en template literals de `innerHTML`: `${i.patient_id||''}`, `${i.modalidad || '—'}`, `${i.cobro_estado}` sin llamar a `escHtml()`. Los datos de paciente provienen de metadatos DICOM vía Orthanc — un atacante que pueda subir un archivo DICOM con tags maliciosos controla estos valores.
- **Prueba**: `DICOM tag PatientID = "<img src=x onerror=alert(1)>"` → renderizado directo en `innerHTML` sin escape → XSS persistente.
- **Impacto**: XSS persistente en el panel admin. Si se combina con SEC-ADMIN-006 (token en sessionStorage), el atacante puede exfiltrar el JWT del admin.
- **Recomendación**: Aplicar `escHtml()` a TODOS los campos interpolados en `innerHTML`: `${escHtml(i.patient_id||'')}`, `${escHtml(i.modalidad||'—')}`, `${escHtml(i.cobro_estado||'')}`.
- **Severidad**: HIGH
- **CWE**: CWE-79 (Cross-site Scripting — Stored)

### SEC-ADMIN-004 — XSS parcial en `loadPagos()`: `m.metodo` y otros campos no escapados
- **Archivo**: `imagiax_admin.py`, función JS `loadPagos()`
- **Descripción**: `loadPagos()` construye tbody con `m.metodo` (string método de pago de la DB), `l.creditos_total`, `l.creditos_usados`, `m.plan_id` interpolados directamente en `innerHTML` sin `escHtml()`.
- **Impacto**: Si la DB es comprometida o un pago malicioso es procesado con un `metodo` que contenga HTML, XSS en el panel admin.
- **Recomendación**: Envolver todos los valores dinámicos de base de datos en `escHtml()` antes de interpolar en `innerHTML`.
- **Severidad**: MEDIUM
- **CWE**: CWE-79 (Cross-site Scripting — Stored)

### SEC-ADMIN-005 — XSS en helpers `svcRow()` / `cfgRow()`: datos de configuración no escapados
- **Archivo**: `imagiax_admin.py`, funciones JS `svcRow()`, `cfgRow()`, `loadSistema()`
- **Descripción**: `svcRow(name, ok, desc)` y `cfgRow(k, v)` usan template literals con `${name}`, `${desc}`, `${v}` directamente en `innerHTML`. Estos valores provienen de la respuesta de `/salud` (endpoint del servidor), que incluye `comp.orthanc.url`, `d.version`, `comp.gemini.modelo`. Si un atacante puede manipular alguno de estos valores (ej. inyectando HTML en `GEMINI_MODEL` desde una variable de entorno), XSS en `loadSistema()`.
- **Impacto**: Menor que SEC-ADMIN-003 (el atacante necesita comprometer variables de entorno del servidor), pero el patrón de código inseguro es el mismo.
- **Recomendación**: Siempre `escHtml()` en valores de servidor, incluso los considerados "internos".
- **Severidad**: MEDIUM
- **CWE**: CWE-79

### SEC-ADMIN-006 — JWT de admin almacenado en `sessionStorage` (accesible via XSS)
- **Archivo**: `imagiax_admin.py`, función JS `doLogin()`, variable global `token`
- **Descripción**: `let token = sessionStorage.getItem('admin_token') || '';` — el JWT del admin se almacena en `sessionStorage` tras el login. La función `doLogin()` hace `sessionStorage.setItem('admin_token', token)` con el JWT obtenido de la respuesta JSON de `/auth/login`. `sessionStorage` es accesible a cualquier JS ejecutado en la página (DOM, extensiones, XSS).
- **Impacto**: Si alguno de los XSS anteriores (SEC-ADMIN-003/004/005) se explota, el atacante puede hacer `sessionStorage.getItem('admin_token')` y exfiltrar el JWT del administrador, tomando control total del sistema.
- **Recomendación**: Usar un httpOnly cookie para el token del panel admin en lugar de sessionStorage. El cookie ya existe (`session_token` httpOnly) — el JS del panel debería hacer requests que usen ese cookie, no un Bearer token en sessionStorage.
- **Severidad**: HIGH
- **CWE**: CWE-922 (Insecure Storage of Sensitive Information)

### SEC-ADMIN-007 — `loadAuditoria()`: campo `l.accion` interpolado sin `escHtml()`
- **Archivo**: `imagiax_admin.py`, función JS `loadAuditoria()`
- **Descripción**: `${l.accion}` es usado como contenido del span del badge de auditoría directamente en `innerHTML`. Aunque `accionBadge(l.accion)` retorna solo una clase CSS (seguro), el valor raw `l.accion` se interpola en el span content sin escape.
- **Impacto**: Los registros de auditoría provienen de la DB. Si un nombre de acción contiene HTML (ej. una acción de auditoría registrada con un valor inesperado), XSS en el log viewer del panel admin.
- **Recomendación**: `${escHtml(l.accion)}` en el contenido del span.
- **Severidad**: LOW
- **CWE**: CWE-79

### SEC-ADMIN-008 — Sin CSRF explícito en endpoints `/admin/api/*` (mitigado por Bearer)
- **Archivo**: `imagiax_admin.py`, todos los endpoints `/admin/api/`
- **Descripción**: Los endpoints admin usan `Authorization: Bearer <token>` (desde sessionStorage). No existe un token CSRF explícito. El uso de `Authorization` header desde JS efectivamente previene CSRF clásico (los formularios HTML no pueden enviar headers custom), pero si algún endpoint en el futuro acepta cookies directamente sin verificar el header, CSRF sería posible.
- **Impacto**: Riesgo bajo actualmente. Sin embargo, la ausencia de CSRF token explícito es una deuda técnica de seguridad.
- **Recomendación**: Documentar explícitamente que el Bearer token en header actúa como CSRF mitigation. Si en el futuro se migra a cookies, implementar CSRF token (double-submit cookie pattern).
- **Severidad**: INFO
- **CWE**: CWE-352 (Cross-Site Request Forgery)

---

## Hallazgos — OHIF Extension (`ohif-extension-imagiax-ai/`)

### SEC-FE-001 — JWT de usuario almacenado en `localStorage` (accesible via XSS)
- **Archivo**: `ohif-extension-imagiax-ai/src/services/imagiaxApi.ts`
- **Descripción**: `_AUTH_TOKEN_KEY = 'imagiax_auth_token_v1'` — el JWT del usuario autenticado se almacena en `localStorage` y se envía como `Authorization: Bearer ${_token}` en cada request. `localStorage` persiste entre sesiones y es accesible a cualquier JS en el dominio.
- **Prueba**: Línea `const _token = localStorage.getItem(_AUTH_TOKEN_KEY)` en `_initAuth()`. Línea `localStorage.setItem(_AUTH_TOKEN_KEY, token)` en `_storeAuth()`.
- **Impacto**: Cualquier XSS en el dominio (incluyendo OHIF viewer, extensiones de terceros) puede exfiltrar el JWT médico con acceso a informes y DICOM.
- **Recomendación**: Migrar a httpOnly cookie para el token de sesión del usuario. Coordinar con el backend (ya existe `session_token` httpOnly cookie en `imagiax_auth.py`).
- **Severidad**: HIGH
- **CWE**: CWE-922 (Insecure Storage of Sensitive Information)

### SEC-FE-002 — URL del backend hardcodeada como `http://localhost:8010` (HTTP no cifrado)
- **Archivo**: `ohif-extension-imagiax-ai/src/services/imagiaxApi.ts`, `ImagiaxAiPanel.tsx` (~línea 33)
- **Descripción**: `IMAGIAX_BACKEND = window.__IMAGIAX_CONFIG__?.backend_url || window.__IMAGIAX_API_URL__ || 'http://localhost:8010'`. Si las variables de window no están seteadas en producción, toda la comunicación con el backend ocurre sobre HTTP sin cifrar, incluyendo el envío del JWT y datos médicos.
- **Impacto**: En un entorno de producción mal configurado, tokens JWT y PHI de pacientes viajan sin cifrar.
- **Recomendación**: Cambiar el default a `https://imagiax.dev/api` o usar una variable de build-time (`process.env.REACT_APP_BACKEND_URL`). Fallar explícitamente si la URL es HTTP en contexto no-localhost.
- **Severidad**: MEDIUM
- **CWE**: CWE-319 (Cleartext Transmission of Sensitive Information)

### SEC-FE-003 — `apiV2Request()` no incluye header de autenticación
- **Archivo**: `ohif-extension-imagiax-ai/src/services/imagiaxApi.ts`, función `apiV2Request()`
- **Descripción**: `apiV2Request()` construye los headers sin incluir `Authorization`. Las requests a la API V2 (puerto 8011) con UIDs de estudios DICOM viajan sin autenticación. Contrasta con `apiRequest()` que sí incluye `Authorization: Bearer ${_token}`.
- **Prueba**: `headers: { 'Content-Type': 'application/json', ...options.headers }` — sin `Authorization`.
- **Impacto**: Si el servidor V2 (puerto 8011) no implementa su propia autenticación, cualquier cliente puede llamar estos endpoints con UIDs de estudios.
- **Recomendación**: Agregar `..._token ? { Authorization: \`Bearer ${_token}\` } : {}` a los headers de `apiV2Request()`.
- **Severidad**: HIGH
- **CWE**: CWE-306 (Missing Authentication for Critical Function)

### SEC-FE-004 — PHI del paciente persistido en `localStorage` (historia de informes)
- **Archivo**: `ohif-extension-imagiax-ai/src/ImagiaxAiPanel.tsx`
- **Descripción**: El historial de informes se persiste en `localStorage` bajo `'imagiax_history_v1'`. Cada entrada incluye: `patient.rut` (RUT chileno — ID nacional), `patient.nombre`, `patient.edad`, `patient.medicoReferencia`, `patient.tipoExamen`, `informeId`, y secciones del informe médico. Esta información persiste indefinidamente en el navegador del médico.
- **Prueba**: `useEffect(() => { localStorage.setItem('imagiax_history_v1', JSON.stringify(history)); }, [history])` y la inicialización desde `localStorage.getItem('imagiax_history_v1')`.
- **Impacto**: PHI de múltiples pacientes almacenada permanentemente en el browser. Compromiso del dispositivo del médico expone todos los informes de pacientes en texto plano. Violación potencial de regulaciones de privacidad médica (ley 20.584 Chile, HIPAA si aplica).
- **Recomendación**: No persistir PHI en localStorage. Usar solo datos mínimos (informeId) para referencias, y recuperar el informe completo del servidor cuando sea necesario. O usar cifrado simétrico con una clave derivada de la sesión.
- **Severidad**: HIGH
- **CWE**: CWE-312 (Cleartext Storage of Sensitive Information)

### SEC-FE-005 — Metadatos DICOM renderizados sin sanitización en preview HTML
- **Archivo**: `ohif-extension-imagiax-ai/src/ImagiaxAiPanel.tsx`, `MedicalReportDocument` component
- **Descripción**: `instance.PatientName`, `instance.PatientID`, `instance.StudyDescription` extraídos del DICOM se almacenan en estado `patient` y son renderizados en el componente React `MedicalReportDocument`. React renderiza estos valores como text nodes (escapados automáticamente) — por lo que el riesgo XSS en el preview es bajo. Sin embargo, los mismos valores se pasan al generador de PDF (`jsPDF`) vía `doc.text()`, lo que no es un vector HTML pero sí podría causar corrupción del PDF.
- **Impacto**: Bajo en el contexto HTML (React escapa). Riesgo potencial de injection en el PDF si `jsPDF` interpreta caracteres especiales en `doc.text()`. Principalmente un riesgo de calidad de datos.
- **Recomendación**: Sanitizar metadatos DICOM con una función que remueva caracteres de control antes de usarlos en cualquier renderizado. Documentar explícitamente que `MedicalReportDocument` usa React text nodes (no `dangerouslySetInnerHTML`).
- **Severidad**: LOW
- **CWE**: CWE-20 (Improper Input Validation)

### SEC-FE-006 — Config de OHIF expone `showErrorDetails: 'always'` en producción
- **Archivo**: `OHIF Medical Imaging Viewer/Viewers/platform/app/public/config/default.js`
- **Descripción**: `showErrorDetails: 'always'` hace que OHIF muestre detalles de error completos a todos los usuarios en todos los entornos, incluyendo producción. Puede exponer stack traces, URLs internas, o nombres de módulos.
- **Recomendación**: Cambiar a `showErrorDetails: 'dev-only'` o eliminarlo (el valor por defecto es más conservador).
- **Severidad**: MEDIUM
- **CWE**: CWE-209

### SEC-FE-007 — Configs OHIF con URLs HTTP hardcodeadas para DICOM
- **Archivo**: `local_orthanc.js`, `docker-nginx-orthanc-keycloak.js`
- **Descripción**: `local_orthanc.js` define `wadoUriRoot: 'http://localhost/dicom-web'` (HTTP). `docker-nginx-orthanc-keycloak.js` define `wadoUriRoot: 'http://127.0.0.1/pacs'` (HTTP). Estas configs se incluyen en el repo y podrían ser usadas en producción sin modificación.
- **Impacto**: DICOM data (imágenes médicas) transmitida sin cifrar. En la config de docker-nginx-orthanc.js (la más usada en deploy) se usan rutas relativas, lo que es correcto.
- **Recomendación**: Agregar un comment prominente en cada archivo de config HTTP-only indicando que son solo para desarrollo local. Agregar un check en startup del OHIF que rechace configs con URLs HTTP en dominio no-localhost.
- **Severidad**: MEDIUM
- **CWE**: CWE-319

### SEC-FE-008 — Sin headers de seguridad en `serve.json` ni `_headers` de OHIF
- **Archivo**: `OHIF Medical Imaging Viewer/Viewers/platform/app/public/serve.json`, `_headers`
- **Descripción**: `serve.json` solo contiene URL rewrites. `_headers` está vacío. El OHIF viewer estático se sirve sin ningún header de seguridad cuando se usa el servidor `serve` incluido (ej. para el container Docker de OHIF). En producción con nginx estos headers son inyectados, pero si se deploya OHIF directamente sin nginx, no hay headers.
- **Impacto**: Sin `X-Frame-Options`, `Content-Security-Policy`, o `X-Content-Type-Options` en el servidor OHIF directo.
- **Recomendación**: Agregar headers de seguridad en `_headers` (formato de Netlify/Vercel) o en la sección `headers` de `serve.json`. No depender exclusivamente de nginx.
- **Severidad**: MEDIUM
- **CWE**: CWE-1021

---

## Hallazgos — Infraestructura (Nginx + Orthanc)

### SEC-INFRA-NEW-001 — Credencial de Orthanc PACS hardcodeada en repositorio 🔴
- **Archivo**: `OHIF Medical Imaging Viewer/orthanc-imagiax/config/orthanc.json`
- **Descripción**: El archivo `orthanc.json` tiene `"RegisteredUsers": { "imagiax": "ImagiaxPACS_2026!" }` en texto plano. Este archivo está en el repositorio. Cualquier persona con acceso al repo (colaborador, fork privado comprometido) tiene la credencial del PACS.
- **Prueba**: `"imagiax": "ImagiaxPACS_2026!"` en `orthanc.json`.
- **Impacto**: Acceso directo a Orthanc (puerto 8042) para cualquiera que tenga la credencial. Orthanc tiene acceso completo a todas las imágenes DICOM de todos los pacientes. Combinado con SEC-BE-003 (Path Traversal), acceso a la API REST y scripting Lua de Orthanc.
- **Recomendación**: (1) Rotar la contraseña de Orthanc INMEDIATAMENTE. (2) Mover `RegisteredUsers` a una variable de entorno o montar el archivo como secreto en Docker. (3) Agregar `orthanc.json` a `.gitignore`.
- **Severidad**: CRITICAL
- **CWE**: CWE-798 (Use of Hard-coded Credentials)

### SEC-INFRA-NEW-002 — Orthanc con `HttpDescribeErrors: true` y `DicomCheckCalledAet: false`
- **Archivo**: `OHIF Medical Imaging Viewer/orthanc-imagiax/config/orthanc.json`
- **Descripción**: `"HttpDescribeErrors": true` hace que Orthanc incluya mensajes de error detallados en las respuestas HTTP, potencialmente exponiendo información interna. `"DicomCheckCalledAet": false` permite que cualquier equipo DICOM se conecte sin verificar el AET (Application Entity Title), lo que amplía la superficie de ataque DICOM.
- **Impacto**: Information disclosure vía errores HTTP de Orthanc. Cualquier equipo DICOM puede intentar C-STORE (subir estudios) o C-FIND (buscar estudios) sin verificación de AET.
- **Recomendación**: `"HttpDescribeErrors": false` en producción. `"DicomCheckCalledAet": true` y listar en `DicomModalities` solo los equipos autorizados.
- **Severidad**: MEDIUM
- **CWE**: CWE-209, CWE-284

### SEC-INFRA-NEW-003 — Nginx: `auth_request` al PACS sin manejo explícito de errores 5xx
- **Archivo**: `OHIF Medical Imaging Viewer/orthanc-imagiax/config/nginx-proxy.conf`
- **Descripción**: La directiva `auth_request /api/auth/verify` protege `/pacs/`. Si el backend FastAPI está caído (5xx), nginx por defecto propaga el error como 500 al cliente, lo cual podría revelar que el auth endpoint no está disponible. Más importante: no hay `auth_request_set` para propagar el usuario autenticado a Orthanc — el backend que autentica y el PACS que sirve los datos están completamente desacoplados.
- **Impacto**: Si FastAPI cae momentáneamente y nginx retorna 500 en lugar de denegar el acceso, un atacante puede intentar timing attacks para detectar el estado del backend. No permite bypass directo, pero es una debilidad de observabilidad.
- **Recomendación**: Agregar `proxy_intercept_errors on` y `error_page 500 502 503 504 =403 /403.html` para el bloque `auth_request`. Documentar que el desacoplamiento entre auth y PACS es intencional.
- **Severidad**: LOW
- **CWE**: CWE-390 (Detection of Error Condition Without Action)

### SEC-INFRA-NEW-004 — `google.js` contiene OAuth `client_id` en el repositorio
- **Archivo**: `OHIF Medical Imaging Viewer/Viewers/platform/app/public/config/google.js`
- **Descripción**: `client_id: '723928408739-k9k9r3i44j32rhu69vlnibipmmk9i57p.apps.googleusercontent.com'` — el OAuth 2.0 client_id de Google Cloud está expuesto en el repositorio. A diferencia de un client_secret, un client_id es semi-público por diseño del flujo OAuth (se envía en la URL de autorización), pero su presencia en el repo con el Google Cloud Project ID (`ohif-cloud-healthcare`) expone la configuración del proyecto GCP.
- **Impacto**: Bajo (client_id es público en OAuth flows). Sin embargo, expone el Google Cloud Project ID, que combinado con otros datos puede facilitar ataques de phishing (OAuth consent screen spoofing) o enumeración de recursos GCP.
- **Recomendación**: Si este `google.js` no se usa en producción (IMAGIAX usa Orthanc local, no GCP Healthcare API), eliminarlo o moverlo a una carpeta `examples/`. Si se usa, aceptar que el client_id es público pero verificar que el client_secret nunca esté en el repo.
- **Severidad**: LOW
- **CWE**: CWE-200 (Information Exposure)

---

## Positivos Destacados — Fase 2 ✅

- **Nginx con HSTS, X-Frame-Options, CSP y X-Content-Type-Options**: El proxy de producción tiene una configuración de headers de seguridad sólida.
- **`frame-ancestors: 'none'` en CSP**: Previene clickjacking correctamente.
- **`upgrade-insecure-requests` en CSP**: Fuerza HTTPS en recursos embebidos.
- **`auth_request` para PACS y OHIF**: Orthanc y el viewer están protegidos por el auth check de FastAPI a nivel nginx.
- **`server_tokens off`**: Versión de nginx no expuesta.
- **HTTP → HTTPS redirect**: Todo el tráfico HTTP es redirigido a HTTPS.
- **ACME challenge path aislado**: Solo `.well-known/acme-challenge/` está expuesto en HTTP.
- **`escHtml()` implementado**: La función de escape existe en el admin panel — solo falta usarla consistentemente.
- **React text nodes en `MedicalReportDocument`**: No se usa `dangerouslySetInnerHTML` en ningún lugar del panel OHIF — XSS via rendering está prevenido.
- **`_sanitize_return_to()` en Python**: La validación server-side del redirect post-login es correcta.
- **TLS 1.2 + 1.3, sin MD5/aNULL**: Ciphers correctos en nginx.

---

## Tabla Unificada de Hallazgos (Fases 1 + 2)

| ID | Severidad | Dominio | Descripción breve |
|----|-----------|---------|-------------------|
| SEC-PAY-001 | 🔴 CRITICAL | Pagos | MP Webhook sin validación en dev |
| SEC-PAY-002 | 🔴 CRITICAL | Pagos | /pagos/exito sin autenticación |
| SEC-PAY-003 | 🔴 CRITICAL | Pagos | IDOR en /tbk/cobrar |
| SEC-PAY-004 | 🔴 CRITICAL | Pagos | Monto controlado por cliente en TBK |
| SEC-BE-003 | 🔴 CRITICAL | Backend | Path Traversal en proxy DICOMweb |
| SEC-BE-002 | 🔴 CRITICAL | Backend | Proxy DICOM no verifica tokens revocados |
| SEC-BE-001 | 🔴 CRITICAL | Backend | /health/db sin auth expone info interna |
| **SEC-INFRA-NEW-001** | 🔴 **CRITICAL** | **Infra** | **Credencial Orthanc hardcodeada en repo** |
| SEC-ADMIN-003 | 🟠 HIGH | Admin | XSS en loadInformes() — patient_id/modalidad |
| SEC-ADMIN-006 | 🟠 HIGH | Admin | JWT admin en sessionStorage (exfiltrable via XSS) |
| SEC-FE-001 | 🟠 HIGH | Frontend | JWT usuario en localStorage |
| SEC-FE-003 | 🟠 HIGH | Frontend | apiV2Request() sin autenticación |
| SEC-FE-004 | 🟠 HIGH | Frontend | PHI de pacientes en localStorage |
| SEC-PAY-005 | 🟠 HIGH | Pagos | TOCTOU en acreditar_pago() (doble gasto) |
| SEC-PAY-006 | 🟠 HIGH | Pagos | Flag _IS_TEST derivado del prefijo del token |
| SEC-PAY-007 | 🟠 HIGH | Pagos | cobrar_scan en TEST mode siempre aprueba |
| SEC-INFRA-001 | 🟠 HIGH | Infra | Misma key Gemini en dev y producción |
| SEC-INFRA-002 | 🟠 HIGH | Infra | Contraseña admin predecible |
| SEC-AI-001 | 🟠 HIGH | AI/Backend | DoS via max_instances=500 |
| SEC-AI-002 | 🟠 HIGH | AI/Backend | TBK SDK síncrono bloquea event loop |
| SEC-ADMIN-001 | 🟡 MEDIUM | Admin | Sin headers de seguridad directos en handler |
| SEC-ADMIN-002 | 🟡 MEDIUM | Admin | window.onerror con alert() en producción |
| SEC-ADMIN-004 | 🟡 MEDIUM | Admin | XSS en loadPagos() — metodo/plan_id |
| SEC-ADMIN-005 | 🟡 MEDIUM | Admin | XSS en svcRow()/cfgRow() — config data |
| SEC-FE-002 | 🟡 MEDIUM | Frontend | Backend URL default HTTP localhost |
| SEC-FE-006 | 🟡 MEDIUM | Frontend | showErrorDetails:'always' en OHIF |
| SEC-FE-007 | 🟡 MEDIUM | Frontend | Configs OHIF con URLs HTTP hardcodeadas |
| SEC-FE-008 | 🟡 MEDIUM | Frontend | Sin headers de seguridad en serve.json |
| SEC-BE-006 | 🟡 MEDIUM | Backend | PHI accesible por cualquier rol |
| SEC-BE-007 | 🟡 MEDIUM | Backend | Sin CSP en backend FastAPI |
| SEC-BE-008 | 🟡 MEDIUM | Backend | CORS sin validación de wildcard |
| SEC-BE-009 | 🟡 MEDIUM | Backend | /informes/listado retorna 500 registros PHI |
| SEC-PAY-009 | 🟡 MEDIUM | Pagos | PHI tarjeta almacenada en DB (PCI-DSS) |
| SEC-PAY-010 | 🟡 MEDIUM | Pagos | external_reference malformado da crédito gratis |
| SEC-INFRA-NEW-002 | 🟡 MEDIUM | Infra | Orthanc con HttpDescribeErrors:true + AET abierto |
| SEC-AI-003 | 🟡 MEDIUM | AI | Excepción TBK en URL de redirect |
| SEC-INFRA-003 | 🟡 MEDIUM | Infra | X-Forwarded-For spoofable sin nginx |
| SEC-ADMIN-007 | 🔵 LOW | Admin | l.accion no escapado en loadAuditoria() |
| SEC-FE-005 | 🔵 LOW | Frontend | Metadatos DICOM en PDF sin sanitización |
| SEC-INFRA-NEW-003 | 🔵 LOW | Infra | auth_request sin manejo de errores 5xx |
| SEC-INFRA-NEW-004 | 🔵 LOW | Infra | OAuth client_id en repo |
| SEC-BE-010 | 🔵 LOW | Backend | Sin HSTS directo en FastAPI |
| SEC-BE-011 | 🔵 LOW | Backend | robots.txt expone paths internos |
| SEC-BE-012 | 🔵 LOW | Backend | String interpolation en SQL INTERVAL |
| SEC-AI-004 | 🔵 LOW | AI | Sin límite de tamaño en preprocesar() |
| SEC-AI-005 | 🔵 LOW | AI | Rate limiter no compartido entre workers |
| SEC-ADMIN-008 | ✅ INFO | Admin | Sin CSRF explícito (mitigado por Bearer) |
| SEC-FE-006-INFO | ✅ INFO | Frontend | dangerouslySetInnerHTML no usado (positivo) |
| SEC-PAY-011 | ✅ INFO | Pagos | MP_PUBLIC_KEY en HTML (by design) |

**Total: 8 CRITICAL — 13 HIGH — 18 MEDIUM — 10 LOW — 3 INFO = 52 hallazgos**

---

## Recomendaciones Adicionales (Fase 2)

### Crítico — Acción inmediata

8. **SEC-INFRA-NEW-001** (Credencial Orthanc en repo): Rotar contraseña de Orthanc AHORA. Mover `RegisteredUsers` a variable de entorno. Agregar `orthanc.json` a `.gitignore`. Si el repo es accesible a terceros, asumir que la credencial está comprometida.

### Alta prioridad

9. **SEC-ADMIN-003 + SEC-ADMIN-006** (XSS + token exfiltración en admin panel): Aplicar `escHtml()` a TODOS los campos de `loadInformes()`. Migrar el token admin de `sessionStorage` a httpOnly cookie.

10. **SEC-FE-001 + SEC-FE-004** (localStorage en OHIF): Migrar autenticación a httpOnly cookie. No persistir PHI en localStorage — usar solo IDs y recuperar datos del servidor.

11. **SEC-FE-003** (apiV2Request sin auth): Agregar Authorization header a `apiV2Request()`.

### Ready for Proposal (Actualizado)

**Sí** — con los 8 hallazgos CRITICAL se tiene suficiente para definir el alcance del primer cambio de remediación. Se recomienda dividir en:
1. `fix/security-critical` — SEC-BE-003, SEC-PAY-002, SEC-PAY-003, SEC-PAY-004, SEC-BE-002, SEC-INFRA-NEW-001, más rotación de credentials
2. `fix/security-high-frontend` — SEC-ADMIN-003, SEC-ADMIN-006, SEC-FE-001, SEC-FE-003, SEC-FE-004
3. `fix/security-medium` — Todos los MEDIUM en una iteración posterior
