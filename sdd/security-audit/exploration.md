# Exploration: security-audit-backend

**Fecha**: 2026-04-03  
**Auditor**: SDD Explore Agent  
**Proyecto**: IMAGIAX — Plataforma de imágenes médicas (FastAPI + PostgreSQL + Orthanc PACS)  
**Archivos analizados**: `servidor_api_orthanc_v1.py`, `imagiax_auth.py`, `imagiax_db.py`, `imagiax_admin.py`, `pagos_mp.py`, `pagos_tbk.py`

---

## REPORTE DE SEGURIDAD — BACKEND

---

## 🔴 CRITICAL (2 hallazgos)

---

### SEC-BACK-001
- **Título**: Credenciales reales de producción en `.env` y `.env.production` en el repositorio
- **Archivo**: `.env` (root), `.env.production` (root)
- **Severidad**: CRITICAL
- **Descripción**: El archivo `.env` contiene credenciales operativas reales. Aunque `.gitignore` los excluye del VCS, estas credenciales están **en texto plano en disco**, son accesibles a cualquier proceso con acceso al filesystem, y `.env.production` contiene credenciales de producción de DigitalOcean. El hecho de que sea una "copia" del proyecto (`IMAGIAX - copia (2)`) amplifica el riesgo — estas copias de trabajo suelen terminar en backups, drives compartidos o repositorios por error.
- **Proof**:
```
# .env
JWT_SECRET=0bfbd13f411ed026f7c6888c3f41ccdb46da4c05ef1a536efdcde921c5b85a12
IMAGIAX_ADMIN_PASSWORD=[1,ox4MLIw/l0Qq?t)
POSTGRES_URL=postgresql://imagiax:imagiax2024@localhost:5432/imagiax
GEMINI_API_KEY=AIzaSyCHZ3JDxddgVQmTTirW6GGtilSG-BHfHSQ
MP_ACCESS_TOKEN=TEST-918232103812133-031511-ffc03477a731c7c5f8960e8ef21da708-196010627

# .env.production
GEMINI_API_KEY=AIzaSyCHZ3JDxddgVQmTTirW6GGtilSG-BHfHSQ  ← MISMA KEY QUE DEV
POSTGRES_URL=postgresql://imagiax:ImagiaxDemo2024@postgres:5432/imagiax
IMAGIAX_ADMIN_PASSWORD=ImagiaxAdmin2024!
MP_WEBHOOK_SECRET=18fab28c15c331d336634c052a9ecc1616fc7962335cec14b127577abe03ab45
ORTHANC_PASSWORD=imagiax2024
```
- **Impacto**: Compromiso total de la base de datos con datos PHI, acceso admin a la plataforma, acceso al PACS Orthanc, fraude financiero via MercadoPago, acceso al modelo Gemini (costo económico). La misma `GEMINI_API_KEY` en dev y producción significa que comprometer dev expone producción.
- **Recomendación**:
  1. Rotar **TODAS** las credenciales expuestas INMEDIATAMENTE (JWT_SECRET, DB passwords, Gemini API Key, MP tokens, Orthanc password)
  2. Usar secrets manager (AWS Secrets Manager, Vault, GCP Secret Manager) para producción
  3. Nunca reutilizar credenciales entre dev y prod
  4. Agregar `pre-commit` hook con `git-secrets` o `trufflehog` para prevenir futuros leaks
  5. Revisar historial de git con `git log --all -- .env` (actualmente limpio, pero verificar branches)
- **CWE**: CWE-798 (Use of Hard-coded Credentials), CWE-312 (Cleartext Storage of Sensitive Information)

---

### SEC-BACK-002
- **Título**: `/admin` panel bypassa revocación de usuarios — usuario eliminado puede acceder al dashboard
- **Archivo**: `imagiax_admin.py` — función `admin_panel()` (línea ~580)
- **Severidad**: CRITICAL
- **Descripción**: El endpoint `GET /admin` verifica el JWT usando `decode_token()` (solo firma + expiración), pero **NO consulta la tabla `revoked_users`** en PostgreSQL. La dependencia `get_current_user()` sí hace esta verificación de DB. Un usuario admin eliminado o con logout global forzado (`revoke_all_user_tokens`) sigue pudiendo cargar el dashboard `/admin` con su JWT válido hasta que expire (hasta 168 horas con "recordarme").
- **Proof**:
```python
# imagiax_admin.py - admin_panel() 
async def admin_panel(request: Request) -> Response:
    token = request.cookies.get("imagiax_session")
    try:
        user = imagiax_auth.decode_token(token)  # ← SOLO verifica firma JWT
        # ⚠️ NO llama a is_user_revoked() ni is_jti_revoked()
        if user is None or user.rol not in ("admin", "superadmin"):
            return RedirectResponse(...)
    except Exception:
        return RedirectResponse(...)
    return HTMLResponse(_ADMIN_HTML)  # ← Panel cargado sin verificar revocación

# Comparar con get_current_user que SÍ verifica:
async def get_current_user(...):
    user = decode_token(token)
    if user.jti and await is_jti_revoked(user.jti):      # ← DB check
        raise HTTPException(401, "Sesión revocada")
    if await is_user_revoked(user.user_id):               # ← DB check
        raise HTTPException(401, "Cuenta desactivada")
```
- **Impacto**: Un empleado admin despedido o una cuenta comprometida revocada sigue accediendo al dashboard (y cargando el SPA completo) durante el tiempo de vida del JWT. Aunque las llamadas API subsiguientes sí serían bloqueadas, el HTML se sirve y expone la arquitectura interna.
- **Recomendación**:
```python
# Reemplazar en admin_panel():
async def admin_panel(request: Request) -> Response:
    token = request.cookies.get("imagiax_session")
    if not token:
        return RedirectResponse(url="/login?returnTo=/admin", status_code=303)
    try:
        user = await imagiax_auth.get_current_user(request=request)  # ← USA get_current_user
        if user.rol not in ("admin", "superadmin"):
            return RedirectResponse(url="/login?returnTo=/admin", status_code=303)
    except HTTPException:
        return RedirectResponse(url="/login?returnTo=/admin", status_code=303)
    return HTMLResponse(_ADMIN_HTML)
```
- **CWE**: CWE-613 (Insufficient Session Expiration), CWE-287 (Improper Authentication)

---

## 🟠 HIGH (5 hallazgos)

---

### SEC-BACK-003
- **Título**: IDOR en `/informes/{informe_id}/ver` — cualquier usuario autenticado accede a informes ajenos (PHI)
- **Archivo**: `servidor_api_orthanc_v1.py` — `ver_informe()` (línea ~2223)
- **Severidad**: HIGH
- **Descripción**: El endpoint `GET /informes/{informe_id}/ver` verifica autenticación pero **no verifica que el informe pertenezca al usuario que hace la solicitud**. Cualquier médico autenticado puede enumerar UUIDs de informes y acceder a datos PHI de pacientes de otros médicos: nombre del paciente, informe radiológico completo, nivel de alerta diagnóstica.
- **Proof**:
```python
@app.get("/informes/{informe_id}/ver", response_class=HTMLResponse)
async def ver_informe(informe_id: str, user: UserPayload = Depends(get_current_user)):
    """Página de verificación de informe con PHI. Requiere autenticación."""
    informe = await imagiax_db.get_informe_por_id(informe_id)  # ← Sin filtro de usuario
    # ... retorna patient_name, informe_generado, nivel de alerta (PHI completa)
```
- **Impacto**: Violación HIPAA. Un médico puede leer los informes radiológicos de pacientes de otros médicos simplemente enumerando UUIDs. Los UUIDs son `gen_random_uuid()` (v4), lo cual tiene entropía suficiente, pero el endpoint de QR sugiere que estos links se comparten externamente.
- **Recomendación**:
```python
async def ver_informe(informe_id: str, user: UserPayload = Depends(get_current_user)):
    informe = await imagiax_db.get_informe_por_id(informe_id)
    if not informe:
        return HTMLResponse(..., status_code=404)
    # Verificar ownership (médicos solo ven los suyos; admin ve todos)
    if user.rol not in ("admin", "superadmin"):
        if informe.get("usuario_id") != user.user_id:
            return HTMLResponse(..., status_code=403)
```
- **CWE**: CWE-639 (Authorization Bypass Through User-Controlled Key), CWE-200 (Exposure of Sensitive Information)

---

### SEC-BACK-004
- **Título**: Rate limiting en memoria — no persistente entre workers, bypasseable con múltiples IPs, sin protección en endpoints críticos
- **Archivo**: `servidor_api_orthanc_v1.py` — `_RateLimiter`, `_analysis_limiter`, `_general_limiter` (líneas ~62-80, 1050-1065); `imagiax_auth.py` — `_login_attempts` dict (líneas ~47-85)
- **Severidad**: HIGH
- **Descripción**: Todos los rate limiters son dictionaries en memoria:
  1. **No persisten entre reinicios**: un restart limpia todos los contadores de intentos de login
  2. **No comparten estado entre workers**: con múltiples uvicorn workers o múltiples pods, cada instancia tiene su propio contador — un atacante puede hacer N×max_attempts antes del bloqueo
  3. **Los endpoints `/auth/login`, `/auth/register`, `/auth/change-password` NO están protegidos por `rate_limit_middleware`** — solo `/analizar/` y `/generar/` lo están
  4. **`/pacs/dicom-web/*`** (proxy DICOM completo) no tiene rate limiting — vulnerable a data exfiltration masiva
  5. El limiter de login se indexa por email, no por IP — un atacante con muchos emails puede hacer brute-force sin límite por IP
- **Proof**:
```python
# Solo /analizar/ y /generar/ están rate-limited
_RATE_LIMITED_PREFIXES = ("/analizar/", "/generar/")  # ← /auth/ no está incluido

# El rate limiter de login usa email como key
_login_attempts: dict[str, list[float]] = {}  # email → [timestamps] (EN MEMORIA)
# Un restart limpia todo
```
- **Impacto**: Brute-force de contraseñas sin límite real en producción multi-worker. Exfiltración masiva de DICOMs vía `/pacs/dicom-web/*`.
- **Recomendación**: Migrar a Redis para rate limiting (e.g. `slowapi` con backend Redis). Agregar rate limit a `/auth/login`, `/auth/register`. Agregar rate limit al proxy `/pacs/dicom-web/*`. Implementar CAPTCHA progresivo tras 3 intentos fallidos.
- **CWE**: CWE-307 (Improper Restriction of Excessive Authentication Attempts), CWE-770 (Allocation of Resources Without Limits)

---

### SEC-BACK-005
- **Título**: Múltiples endpoints exponen errores internos con detalles de implementación en producción
- **Archivo**: `servidor_api_orthanc_v1.py` (líneas 612, 1132, 2004, 2125, 2184, 2212, 2332); `imagiax_admin.py` (línea 148)
- **Severidad**: HIGH
- **Descripción**: A pesar de existir un handler global `_unhandled_exception_handler` que oculta stack traces en producción, **los HTTPException explícitos con `detail=f"...{exc}..."` se pasan directamente al cliente** en todos los ambientes. Estos mensajes exponen: nombres de tablas, estructura de queries, mensajes de error de asyncpg, y paths internos.
- **Proof**:
```python
# servidor_api_orthanc_v1.py línea 612
raise HTTPException(status_code=422, detail=f"No se pudo procesar DICOM: {error}")
# ← Expone errores pydicom con metadata del archivo DICOM

# línea 1132 (endpoint /health/db, sin auth)
raise HTTPException(status_code=503, detail=f"PostgreSQL no responde: {exc!s}")
# ← Expone versión de asyncpg, nombre del host DB, error message completo

# línea 2125
raise HTTPException(status_code=500, detail=f"Error al guardar informe en base de datos: {exc}")
# ← Expone errores asyncpg con estructura de tabla 'informes'

# imagiax_admin.py línea 148
raise HTTPException(status_code=500, detail=f"Error al obtener estadísticas: {exc}") from exc
# ← Expone internals de la DB al admin (menor riesgo pero sigue siendo mala práctica)
```
- **Impacto**: Un atacante puede obtener información de la arquitectura interna: nombres de tablas, constraints de DB, versión de asyncpg, paths de red, configuración interna. Facilita ataques más sofisticados.
- **Recomendación**: Crear un helper `_safe_error(exc, public_msg)` que loguea `exc` internamente pero retorna solo `public_msg` en producción:
```python
def _http_error(status: int, public_msg: str, exc: Exception | None = None) -> HTTPException:
    if exc:
        logger.error("%s: %s", public_msg, exc, exc_info=True)
    if _IS_PROD:
        return HTTPException(status_code=status, detail=public_msg)
    return HTTPException(status_code=status, detail=f"{public_msg}: {exc}")
```
- **CWE**: CWE-209 (Generation of Error Message Containing Sensitive Information)

---

### SEC-BACK-006
- **Título**: `/health/db` público expone información de conectividad de base de datos sin autenticación
- **Archivo**: `servidor_api_orthanc_v1.py` — `health_db()` (línea 1105)
- **Severidad**: HIGH
- **Descripción**: El endpoint `GET /health/db` no requiere autenticación y en caso de error expone el mensaje completo de asyncpg incluyendo el host de PostgreSQL, el puerto, el nombre de la base de datos, y el error específico. La documentación del código dice explícitamente "sin JWT".
- **Proof**:
```python
@app.get("/health/db")  # ← Sin Depends(get_current_user)
async def health_db() -> dict[str, Any]:
    """Readiness de PostgreSQL para diagnóstico local (sin JWT)."""
    ...
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"PostgreSQL no responde: {exc!s}",  # ← Expone: host, puerto, DB
        ) from exc
```
- **Impacto**: Reconocimiento de infraestructura. Un atacante puede confirmar si la DB está corriendo, obtener el host/puerto objetivo, y obtener versión de asyncpg sin autenticarse. En la respuesta exitosa también expone `pool_size` y `pool_free` (aunque esto está en `/salud` que sí requiere JWT).
- **Recomendación**: Agregar `Depends(get_current_user)` o al menos `Depends(require_role("admin"))`. Si debe ser público, limitar la respuesta a `{"ok": true}` o `{"ok": false}` sin detalles de error.
- **CWE**: CWE-200 (Exposure of Sensitive Information to an Unauthorized Actor)

---

### SEC-BACK-007
- **Título**: Ausencia de Content-Security-Policy en el panel admin — riesgo XSS escalado
- **Archivo**: `imagiax_admin.py` — `_ADMIN_HTML_SOURCE`, `_LOGIN_HTML` (todo el HTML inline)
- **Severidad**: HIGH
- **Descripción**: El panel `/admin` y la pantalla `/login` **no tienen header `Content-Security-Policy`**. El panel carga GSAP desde CDN externo (`cdn.jsdelivr.net`) y Google Fonts, sin ningún hash de integridad ni política de restricción de fuentes. Dado que el panel maneja JWT en cookies y datos PHI, cualquier XSS permitiría exfiltración completa de la sesión de administrador.
- **Proof**:
```python
# imagiax_admin.py - ningún CSP header
CSP en admin HTML? False  # verificado programáticamente

# Carga CDN sin SRI:
<script src="https://cdn.jsdelivr.net/npm/gsap@3.13/dist/gsap.min.js" defer crossorigin="anonymous">
# ← Sin integrity="sha256-..." → si el CDN es comprometido, se ejecuta código arbitrario
```
- **Impacto**: Sin CSP, un XSS en cualquier parte del panel (ej: un nombre de usuario malicioso renderizado sin escape, o una inyección via `informe_final`) ejecuta JS arbitrario con acceso a cookies y DOM. Escalada de un XSS a Account Takeover.
- **Recomendación**:
```python
# En los headers de admin_panel() y login_panel():
headers = {
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' https://cdn.jsdelivr.net 'nonce-{nonce}'; "
        "style-src 'self' https://fonts.googleapis.com 'nonce-{nonce}'; "
        "font-src https://fonts.gstatic.com; "
        "connect-src 'self'; "
        "img-src 'self' data:; "
        "frame-ancestors 'none';"
    ),
    ...
}
```
Agregar SRI (Subresource Integrity) a todos los recursos CDN.
- **CWE**: CWE-116 (Improper Encoding or Escaping of Output), CWE-79 (XSS)

---

## 🟡 MEDIUM (6 hallazgos)

---

### SEC-BACK-008
- **Título**: Rate limit de login indexado solo por email — permite brute-force por IP con emails distintos
- **Archivo**: `imagiax_auth.py` — `_check_login_rate()` (líneas 55-68)
- **Severidad**: MEDIUM
- **Descripción**: El rate limiter de login usa el email como clave. Un atacante con una lista de emails válidos (o enumerados) puede intentar N×max_attempts contraseñas en 5 minutos usando diferentes emails, sin nunca ser bloqueado desde su IP.
- **Proof**:
```python
def _check_login_rate(email: str) -> bool:
    key = email.lower().strip()  # ← Solo indexado por email, NO por IP
    attempts = _login_attempts.get(key, [])
    return len(attempts) < _LOGIN_MAX_ATTEMPTS  # 5 intentos por email
```
- **Recomendación**: Combinar clave email + IP: `key = f"{email.lower().strip()}:{client_ip}"`. Agregar también un limiter global por IP (e.g., 20 intentos totales desde una IP en 5 minutos).
- **CWE**: CWE-307

---

### SEC-BACK-009
- **Título**: `is_jti_revoked()` tiene lógica redundante — siempre retorna `True` si hay JTI en `revoked_tokens`
- **Archivo**: `imagiax_auth.py` — `is_jti_revoked()` (líneas 375-396)
- **Severidad**: MEDIUM
- **Descripción**: La función verifica si el usuario está en `revoked_users` y si lo está retorna `True`; si no está, también retorna `True`. La bifurcación es inútil y puede confundir futuros mantenedores o causar bugs si se intenta diferenciar los casos. El código también tiene un bug conceptual: si el JTI está en `revoked_tokens`, retorna `True` independientemente de si el usuario fue revocado globalmente (pero el `get_current_user` llama a `is_user_revoked` por separado, mitigándolo).
- **Proof**:
```python
async def is_jti_revoked(jti: str) -> bool:
    row = await conn.fetchrow(...)
    if row:
        user_revoked = await conn.fetchval(...)
        if user_revoked:
            return True
        return True  # ← SIEMPRE retorna True aquí — la bifurcación no diferencia nada
    return False
```
- **Recomendación**: Simplificar a `return True` si `row` existe, eliminar el doble check inútil. Si la intención era retornar distinto valor según el tipo de revocación, documentar y implementar correctamente.
- **CWE**: CWE-670 (Always-Incorrect Control Flow Implementation)

---

### SEC-BACK-010
- **Título**: Email enumeration via `/auth/register` — respuesta 409 confirma emails existentes
- **Archivo**: `imagiax_auth.py` — `create_user()` (línea 458); `imagiax_admin.py` — `create_usuario()`
- **Severidad**: MEDIUM
- **Descripción**: Cuando se intenta crear un usuario con un email ya registrado, la API retorna `HTTP 409 "El email ya está registrado"`. Aunque `/auth/register` requiere rol `admin`, esto es un patrón peligroso que filtra información. Más relevante: si un admin comprometido quiere enumerar qué emails están registrados antes de un ataque, puede usarlo.
- **Proof**:
```python
except Exception as exc:
    if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
        raise HTTPException(status_code=409, detail="El email ya está registrado")
        # ← Confirma existencia del email
```
- **Impacto**: Moderado — solo explotable por admins. Pero sigue siendo un patrón de diseño inseguro.
- **Recomendación**: En contexto admin puede ser aceptable devolver 409 para UX. Sin embargo, documentar claramente que este endpoint no es público y nunca exponer este mensaje en un contexto de auto-registro.
- **CWE**: CWE-203 (Observable Discrepancy)

---

### SEC-BACK-011
- **Título**: Sin validación del parámetro `estado` en filtros de informes — datos no sensibles pero inconsistente
- **Archivo**: `imagiax_db.py` — `get_todos_informes()` (líneas 546-555); `imagiax_admin.py` — `admin_informes()`
- **Severidad**: MEDIUM
- **Descripción**: El parámetro `estado` se pasa directamente a la query SQL como valor parameterizado (`$1`) — esto es **safe contra SQL injection**. Sin embargo, no se valida contra `ESTADOS_INFORME_VALIDOS` antes de ejecutar la query. Un valor inválido como `estado=inexistente` simplemente retorna lista vacía sin error, lo que puede causar confusión o masking de bugs. La constante `ESTADOS_INFORME_VALIDOS` existe pero no se usa para validar inputs.
- **Proof**:
```python
ESTADOS_INFORME_VALIDOS = ("pendiente", "aceptado", "rechazado", "editado_aceptado")

# Pero en get_todos_informes:
if estado:
    conditions.append(f"estado = ${idx}")
    params.append(estado)  # ← estado no validado contra ESTADOS_INFORME_VALIDOS
```
- **Recomendación**:
```python
if estado and estado not in ESTADOS_INFORME_VALIDOS:
    raise ValueError(f"Estado inválido: {estado}. Válidos: {ESTADOS_INFORME_VALIDOS}")
```
- **CWE**: CWE-20 (Improper Input Validation)

---

### SEC-BACK-012
- **Título**: `/pagos/exito` y `/pagos/suscripcion/exito` procesan créditos sin autenticación del usuario
- **Archivo**: `pagos_mp.py` — `pago_exitoso()` (línea 803), `suscripcion_exitosa()` (línea 878)
- **Severidad**: MEDIUM
- **Descripción**: Los callbacks de MercadoPago `GET /pagos/exito` y `GET /pagos/suscripcion/exito` son públicos (sin autenticación). Cuando llegan, acreditan créditos basándose en el `external_reference` parseado del pago de MP. Un atacante que conozca la estructura del `external_reference` podría intentar manipular el `usuario_id` o `cliente_id` en la referencia para acreditar créditos a cuentas arbitrarias. La validación real debería venir del webhook firmado, no del callback de redirect.
- **Proof**:
```python
@router.get("/pagos/exito", response_class=HTMLResponse)  # ← Sin autenticación
async def pago_exitoso(payment_id: str = Query(None), ...):
    # Verifica con API de MP - pero external_reference viene de la URL/pago
    ext_ref = _parse_external_reference(payment_data.get("external_reference", ""))
    cliente_id = ext_ref.get("cliente_id")
    ext_uid = ext_ref.get("usuario_id")
    # Acredita créditos basado en ext_uid del external_reference
```
- **Recomendación**: Los callbacks de redirect NUNCA deben ser los que acrediten créditos — solo deben mostrar UI. La lógica real de acreditación debe estar EXCLUSIVAMENTE en el webhook firmado (`/pagos/webhook`). El callback debe solo verificar el estado y redirigir, sin mutar datos.
- **CWE**: CWE-345 (Insufficient Verification of Data Authenticity)

---

### SEC-BACK-013
- **Título**: `upsert_bootstrap_superadmin_from_env()` no está desactivado en producción correctamente
- **Archivo**: `imagiax_auth.py` — `upsert_bootstrap_superadmin_from_env()` (líneas ~360-395)
- **Severidad**: MEDIUM
- **Descripción**: La función está diseñada para solo correr en desarrollo (`APP_ENV != production`). Sin embargo, en `.env.production` no se ve `APP_ENV=production` configurado explícitamente. Si `APP_ENV` no está en el entorno de producción, la función correrá **en producción**, haciendo upsert de la contraseña admin desde el `.env` en cada arranque del servidor — sobrescribiendo contraseñas rotadas.
- **Proof**:
```python
async def upsert_bootstrap_superadmin_from_env() -> None:
    if os.getenv("APP_ENV", "development").strip().lower() == "production":
        return  # ← Solo skipea si APP_ENV="production"
    # Si APP_ENV no está configurado → CORRE EN PRODUCCIÓN
    pw_hash = hash_password(admin_password)
    await conn.execute("""
        INSERT INTO usuarios ... DO UPDATE SET password_hash = EXCLUDED.password_hash
        ...
    """)  # ← Sobrescribe contraseña admin en cada boot
```
- **Recomendación**: Verificar que `APP_ENV=production` esté en el `.env.production` de DigitalOcean. Considerar agregar `IMAGIAX_ADMIN_UPSERT_FROM_ENV=0` por defecto en `.env.production` como segunda línea de defensa.
- **CWE**: CWE-1188 (Initialization of a Resource with an Insecure Default)

---

## 🔵 LOW (4 hallazgos)

---

### SEC-BACK-014
- **Título**: `HTTP_TIMEOUT` configurado en 2 segundos — riesgo de DoS por timeout en análisis legítimo
- **Archivo**: `.env` — `IMAGIAX_HTTP_TIMEOUT=2`; `servidor_api_orthanc_v1.py` — línea ~104
- **Severidad**: LOW
- **Descripción**: El timeout HTTP para comunicación con Orthanc es de **2 segundos** en dev. Para estudios DICOM grandes (multi-frame, CT), descargar el archivo DICOM puede tardar varios segundos. Aunque el proxy DICOMweb usa timeout 300s, el cliente de análisis usa `HTTP_TIMEOUT=2`, lo que puede causar timeouts artificiales que exponen mensajes de error con información interna.
- **Recomendación**: `IMAGIAX_HTTP_TIMEOUT=30` para dev, y documentar valor recomendado para producción.
- **CWE**: CWE-400

---

### SEC-BACK-015
- **Título**: `Permissions-Policy` demasiado restrictiva puede romper funcionalidad de imagen médica
- **Archivo**: `servidor_api_orthanc_v1.py` — `security_headers_middleware()` (línea ~878)
- **Severidad**: LOW
- **Descripción**: El middleware agrega `Permissions-Policy: camera=(), microphone=(), geolocation=()` lo cual es correcto. Sin embargo, no incluye restricciones para `fullscreen`, `display-capture`, o `usb` (relevante para dispositivos médicos DICOM que usan WebUSB). No es una vulnerabilidad pero es una política incompleta.
- **Recomendación**: Agregar `fullscreen=(self)` para permitir fullscreen en el visor DICOM. Considerar `display-capture=()` y `usb=()`.
- **CWE**: N/A

---

### SEC-BACK-016
- **Título**: El cleanup de `revoked_tokens` corre cada 24h — tabla puede crecer descontroladamente con ataques
- **Archivo**: `servidor_api_orthanc_v1.py` — `_periodic_cleanup()` (línea 641)
- **Severidad**: LOW
- **Descripción**: La limpieza de tokens revocados expirados corre cada 24 horas. Un atacante que haga logout masivo con tokens falsos no puede insertar en `revoked_tokens` (requiere JTI real), pero un escenario de muchos logouts legítimos puede acumular entradas. El intervalo de 24h es aceptable pero podría ser más frecuente.
- **Recomendación**: Reducir intervalo a 4-6 horas, o triggear limpieza cuando la tabla supere un umbral.
- **CWE**: CWE-770

---

### SEC-BACK-017
- **Título**: CORS configurado correctamente pero `allow_credentials=True` con orígenes en HTTP sin HTTPS
- **Archivo**: `servidor_api_orthanc_v1.py` — líneas 763-768
- **Severidad**: LOW
- **Descripción**: En desarrollo `CORS_ORIGINS=http://localhost:3000,http://127.0.0.1:3000` con `allow_credentials=True`. Las cookies `SameSite=Strict` están correctamente configuradas, mitigando CSRF. Pero en producción cloud, si algún origen es HTTP (no HTTPS), las cookies `Secure` no se enviarían y habría riesgo de intercepción.
- **Proof**:
```python
# .env.production
CORS_ORIGINS=https://imagiax.dev,https://www.imagiax.dev  # ← Correcto (HTTPS)
# Pero ORTHANC_BASE_URL=http://orthanc:8042 (interno - OK)
```
- **Recomendación**: Verificar que en producción todos los orígenes CORS sean HTTPS. Agregar HSTS en nginx (ya mencionado en el código como "nginx lo maneja").
- **CWE**: CWE-319

---

## ℹ️ INFO (5 observaciones)

---

### SEC-BACK-INFO-001
- **Título**: SQL queries en general son seguras (parametrizadas correctamente)
- **Descripción**: Contrariamente al riesgo documentado en el contexto, el análisis exhaustivo muestra que **NO hay SQL injection** en el codebase. Los patrones `f"""SELECT ... {where}"""` parecen peligrosos pero el `{where}` string se construye con nombres de columna **hardcodeados** (`a.usuario_id = $1`, `a.accion = $2`) y los valores van siempre en `params`. asyncpg parametriza todo. El `str(_AUDIT_RETENTION_DAYS)` se convierte a número entero en startup y se pasa como `$1` al cast `::INTERVAL`.
- **Acción**: Sin acción requerida. Considerar refactorizar a queries con ORM o SQLAlchemy Core para mayor claridad.

### SEC-BACK-INFO-002
- **Título**: bcrypt con 12 rounds es apropiado para datos médicos PHI
- **Descripción**: `CryptContext(schemes=["bcrypt"], bcrypt__rounds=12)` es configuración correcta. 12 rounds = ~300ms por hash en hardware moderno, suficiente para PHI. La validación de complejidad de contraseñas (12 chars, uppercase, digit, special) es robusta.
- **Acción**: Considerar aumentar a 14 rounds si el hardware lo permite (duplica el tiempo de hash).

### SEC-BACK-INFO-003
- **Título**: JWT con HS256 — considerar RS256 para arquitectura multi-servicio
- **Descripción**: HS256 es aceptable para una aplicación single-service. Si en el futuro se agregan microservicios o tokens externos que necesiten verificar JWT sin conocer el secreto, RS256/ES256 serían necesarios.
- **Acción**: Documentar la limitación. No requiere acción inmediata.

### SEC-BACK-INFO-004
- **Título**: Cookie `imagiax_session` tiene `SameSite=Strict` y `HttpOnly` correctamente configurados
- **Descripción**: La configuración de cookies es correcta para prevenir CSRF y XSS-based session hijacking. El flag `Secure` se activa automáticamente cuando `IMAGIAX_BASE_URL` comienza con `https://`.
- **Acción**: Verificar que en producción `IMAGIAX_BASE_URL=https://imagiax.dev/api` (sí está en `.env.production`). ✅

### SEC-BACK-INFO-005
- **Título**: Audit logging cubre las acciones críticas pero le faltan algunos eventos
- **Descripción**: Las acciones `login`, `crear_usuario`, `eliminar_usuario`, `admin_reset_password`, `cambiar_password`, `decision_informe` están auditadas. Faltan: `logout_forzado` cuando se revocan todos los tokens de un usuario, `failed_login` (solo se registra el intento sin éxito explícito), y cambios a `activo` (desactivación de cuentas).
- **Acción**: Agregar audit log en `revoke_all_user_tokens()` y en intentos de login fallidos con IP.

---

## Resumen Ejecutivo

| Severidad | Cantidad | Estado |
|-----------|----------|--------|
| CRITICAL  | 2        | ⚠️ Requiere acción inmediata |
| HIGH      | 5        | ⚠️ Requiere acción antes de producción |
| MEDIUM    | 6        | 📋 Planificar para próximo sprint |
| LOW       | 4        | 📝 Backlog técnico |
| INFO      | 5        | ℹ️ Mejoras opcionales |

### Puntos fuertes del codebase ✅
- SQL queries parametrizadas correctamente (no hay SQLi real)
- bcrypt 12 rounds con política de complejidad robusta
- JWT con revocación implementada (revoked_tokens + revoked_users)
- RBAC funcional con superadmin como nivel superior inmutable
- CORS con orígenes explícitos, cookies HttpOnly+Strict
- Error handler global oculta stack traces en producción
- Rate limiting de login funcional (aunque con las limitaciones documentadas)
- `_sanitize_return_to()` previene open redirect

### Prioridades de remediación
1. **HOY**: Rotar TODAS las credenciales de `.env` y `.env.production` (SEC-BACK-001)
2. **HOY**: Parchear `/admin` panel para usar `get_current_user` (SEC-BACK-002)
3. **ANTES DE PRODUCCIÓN**: Agregar ownership check en `/informes/{id}/ver` (SEC-BACK-003)
4. **ANTES DE PRODUCCIÓN**: Migrar rate limiting a Redis (SEC-BACK-004)
5. **ANTES DE PRODUCCIÓN**: Sanitizar mensajes de error en producción (SEC-BACK-005)
6. **SPRINT ACTUAL**: Agregar auth a `/health/db` (SEC-BACK-006)
7. **SPRINT ACTUAL**: Agregar CSP al panel admin (SEC-BACK-007)

---

## Affected Areas
- `servidor_api_orthanc_v1.py` — Endpoints API, CORS, rate limiting, error handling
- `imagiax_auth.py` — JWT, rate limiting login, revocación, SQL dinámico en audit
- `imagiax_db.py` — SQL dinámico en informes (safe pero sin validación de inputs)
- `imagiax_admin.py` — Panel admin, bypass de revocación, sin CSP
- `pagos_mp.py` — Callbacks de pago sin auth, webhook signature
- `.env` / `.env.production` — Credenciales en texto plano

---

## Ready for Proposal
**Sí** — con los 7 hallazgos críticos/high identificados y priorizados, hay suficiente información para crear una propuesta de cambio estructurada.
