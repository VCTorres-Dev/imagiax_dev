# Exploration: Structured JSON Logging — IMAGIAX

**Date**: 2026-04-04  
**Change**: `structured-logging`  
**Persistence**: engram (project: imagiax_dev, topic_key: sdd/structured-logging/explore)

---

## Current State

El codebase usa **tres estilos de logging distintos** sin coordinación:

### 1. `servidor_api_orthanc_v1.py`
```python
# Líneas 54-55
logger = logging.getLogger("imagiax")
logger.setLevel(logging.INFO)
```
- Crea el logger pero **NUNCA configura handler ni formatter**
- Propaga al root logger de Python → stderr con formato texto plano por defecto
- Usa `print()` en startup para Orthanc, DB y Pagos (líneas 508–541)
- El middleware HTTP (`rate_limit_middleware`) **NO loguea requests** — solo aplica rate limit
- Usa `logger.info/warning/error` para deduplicación, pagos, Gemini

### 2. `pagos_mp.py` + `pagos_tbk.py`
```python
logger = logging.getLogger("imagiax.pagos")  # pagos_mp
logger = logging.getLogger("imagiax.tbk")    # pagos_tbk
```
- Correcto como jerarquía (`imagiax` es el padre)
- Bien utilizado: info en transacciones, warning en rechazos, error en excepciones
- **Sin handler propio** → propagate al root sin formato JSON
- ⚠️ `pagos_tbk.py:131`: `req.username` aparece en log — dato personal (email médico)

### 3. `imagiax_db.py`
- Solo `print()` para conexión/schema (3 líneas)
- Sin `import logging`

### 4. `imagiax_model.py`
- Un solo `print()` al cargar ResNet50

### 5. `imagiax_auth.py`
- **CERO** logging module
- Tiene DB audit log en tabla `sesiones_audit` (correcto para compliance)
- Solo `print()` para bootstrap de admin

### 6. `imagiax_admin.py`
- Sin logging en absoluto

### Uvicorn
- Corre con `log_level="info"` default
- Genera access log en **texto plano** a stdout (método + path + status)
- Sin `--log-config` → no hay JSON en access logs

### Docker
- Sin log driver configurado en `docker-compose.prod.yml`
- Usa `json-file` driver por defecto (captura stdout/stderr)
- Los logs llegan a Docker pero mezclados texto plano + algunos JSON inexistentes

### Requirements.txt
- `structlog` — **NO instalado**
- `python-json-logger` — **NO instalado**
- `asgi-correlation-id` — **NO instalado**

### PHI Status (CRÍTICO)
| Dato | Aparece en logs | Riesgo |
|------|-----------------|--------|
| `study_instance_uid` | Sí (varios) | BAJO — no es PHI directo |
| PatientName/PatientID | NO | OK |
| `req.username` (email médico) | Sí (`pagos_tbk.py:131`) | MEDIO — dato personal GDPR |
| Datos de tarjeta | NO (solo token[:16]) | OK |
| Emails de pacientes | NO | OK |

---

## Affected Areas

- `servidor_api_orthanc_v1.py` — logging setup, middleware, startup prints
- `imagiax_db.py` — 3 prints de conexión
- `imagiax_auth.py` — sin logging module, bootstrap prints
- `imagiax_model.py` — print de carga de modelo
- `imagiax_admin.py` — sin logging
- `pagos_mp.py` — bien estructurado, falta formato JSON + correlation
- `pagos_tbk.py` — bien estructurado, falta formato JSON + correlation + username en log
- `requirements.txt` — agregar dependencias
- `Dockerfile` — sin cambios (uvicorn ya va a stdout → Docker captura)

---

## Approaches

### Option A: `python-json-logger` + `asgi-correlation-id` ✅ RECOMENDADO
**Drop-in sobre el sistema de logging existente.**

```python
# logging_config.py (nuevo módulo)
import logging, re
from pythonjsonlogger import jsonlogger
from asgi_correlation_id import CorrelationIdFilter

class PHISanitizer(logging.Filter):
    _PHI_PATTERN = re.compile(
        r'(PatientName|PatientID|patient_name|paciente_nombre)\s*[:=]\s*\S+',
        re.IGNORECASE
    )
    def filter(self, record):
        record.msg = self._PHI_PATTERN.sub(r'\1=[REDACTED]', str(record.msg))
        return True

def setup_logging():
    handler = logging.StreamHandler()
    formatter = jsonlogger.JsonFormatter(
        "%(asctime)s %(name)s %(levelname)s %(message)s %(correlation_id)s",
        rename_fields={
            "asctime": "timestamp",
            "name": "logger", 
            "levelname": "level",
            "correlation_id": "request_id"
        },
    )
    handler.addFilter(CorrelationIdFilter(uuid_length=32))
    handler.addFilter(PHISanitizer())
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    logging.getLogger("uvicorn.access").propagate = False  # evitar duplicados
```

```python
# En servidor_api_orthanc_v1.py — lifespan o top-level
from logging_config import setup_logging
from asgi_correlation_id import CorrelationIdMiddleware
setup_logging()
app.add_middleware(CorrelationIdMiddleware)
```

- **Pros**: drop-in, mínimo cambio, jerarquía `imagiax.*` ya correcta, cero refactor de `logger.info()` existentes, bien conocido en ecosistema Python
- **Cons**: no tiene context binding async-safe para campos arbitrarios (e.g. user_id por request)
- **Effort**: Low (~2-3h)

---

### Option B: `structlog` (pipeline processor)
**Reemplaza el sistema de logging con pipeline de processors.**

- **Pros**: context binding async-safe via `contextvars`, PHI filter como processor, tipado correcto, muy extensible
- **Cons**: requiere reescribir TODOS los `logger.info()/warning()/error()` existentes en los 7 archivos, nuevo paradigma para el equipo, ~5x más trabajo
- **Effort**: High (~1-2 días)

---

### Option C: Custom `JsonFormatter` sobre stdlib logging
**Escribir propio formatter sin dependencias.**

- **Pros**: cero dependencias nuevas
- **Cons**: más código propio para mantener, fácil olvidar campos, correlation ID requiere solución propia con contextvars
- **Effort**: Medium (mismo trabajo que A pero con peor resultado)

---

## Recommendation

**Option A: python-json-logger + asgi-correlation-id**

El codebase ya tiene la jerarquía de loggers correcta (`imagiax` → `imagiax.tbk` → `imagiax.pagos`). Solo falta:
1. Un `JsonFormatter` global en el root handler
2. `CorrelationIdMiddleware` para inyectar `X-Request-ID` en cada request
3. `PHISanitizer` filter como salvaguarda
4. Migrar `print()` de startup a `logger.info()`
5. Agregar un **request logging middleware** que loguee método + path + status_code + duration_ms + user_id

### Salida JSON esperada
```json
{
  "timestamp": "2026-04-04T21:30:00.123Z",
  "level": "INFO",
  "logger": "imagiax",
  "message": "Informe generado",
  "request_id": "a1b2c3d4-e5f6-...",
  "study_id": "1.2.840.10008.5.1.4...",
  "duration_ms": 2341,
  "user_id": "uuid-aqui",
  "role": "medico"
}
```

### Dependencias a agregar en requirements.txt
```
python-json-logger==2.0.7
asgi-correlation-id==4.3.2
```

---

## Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| PHI en logs futuros | CRÍTICO | PHISanitizer filter + code review policy |
| `req.username` en pagos_tbk.py:131 | MEDIO | Ofuscar o eliminar del log |
| uvicorn access log duplicado | BAJO | `logging.getLogger("uvicorn.access").propagate = False` |
| prints de startup sin JSON format | BAJO | Migrar a `logger.info()` |
| CRLF en `imagiax_db.py` | BAJO | Editar con cuidado de no cambiar line endings |
| Docker log rotation | BAJO | Configurar `max-size`/`max-file` en docker-compose (ya manejado por json-file driver) |

---

## Ready for Proposal

**Yes** — suficiente información para escribir la propuesta.

**Next recommended**: `sdd-propose` con:
- Approach: Option A (python-json-logger + asgi-correlation-id)  
- Nuevo archivo: `logging_config.py`  
- Nuevo middleware: `RequestLoggingMiddleware` en servidor_api_orthanc_v1.py  
- PHI sanitizer filter  
- Migración de prints a logger  
- Update requirements.txt
