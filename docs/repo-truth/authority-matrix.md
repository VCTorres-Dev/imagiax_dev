# Matriz de autoridad técnica

Esta matriz existe para evitar confusiones entre código productivo, código modular, trabajo local pendiente y documentación.

## Resumen

| Área | Estado | Qué significa |
|---|---|---|
| `servidor_api_orthanc_v1.py` | Runtime productivo documentado | Docker/deploy siguen apuntando a este backend monolítico |
| `src/api/main.py` | Shadow / no cutover | Existe como target modular, pero el gate sigue RED/HOLD |
| `src/api/ai/router.py` | Modular target / antes local-untracked | Flujo IA más moderno, incluye `MAX_GEMINI_IMAGES = 10` |
| `src/infrastructure/llm_service.py` | Modular target / antes local-untracked | Servicio LLM moderno: multi-imagen, fallback, capas clínicas |
| `app/ai/**` | Evolución modular previa | Quality gate, auditoría, booster, contexto, orquestación |
| `prompts/*.yaml` | Prompt set clínico sensible | Útil para lectura; no asumir producción sin validar ruta activa |
| `ohif-extension-imagiax-ai/src/**` | Canonical OHIF extension | Fuente principal para lectura del panel |
| `OHIF Medical Imaging Viewer/Viewers/**` | Mirror OHIF runtime/local | Tiene drift documentado; no mezclar sin verificar |

---

## Runtime productivo documentado

La autoridad productiva documentada es:

```text
servidor_api_orthanc_v1:app
```

Evidencia revisada:

- `Dockerfile` usa `uvicorn servidor_api_orthanc_v1:app`.
- `deploy_production.sh` declara que conserva esa autoridad.
- `docker-compose.prod.yml` construye `fastapi` desde el Dockerfile raíz.
- `docs/repo-curation/runtime-cutover-parity-gate.md` mantiene el cutover en `RED / HOLD`.

Lectura honesta:

> Producción puede estar estable porque sigue usando el monolito. Eso no significa que todo GitHub/local estuviera alineado.

---

## Modular target / flujo más nuevo

El flujo modular más nuevo vive principalmente en:

```text
src/api/ai/router.py
src/infrastructure/llm_service.py
src/core/medical_logic.py
app/ai/**
prompts/*.yaml
```

Puntos relevantes:

- `src/api/ai/router.py` define `MAX_GEMINI_IMAGES = 10`.
- `src/infrastructure/llm_service.py` arma llamadas multi-imagen a Gemini.
- `app/ai/quality_gate.py`, `auditor.py`, `clinical_audit_engine.py` y `attention_booster.py` agregan capas de control clínico.
- `prompts/*.yaml` contienen reglas clínicas por tipo de examen.

Lectura honesta:

> Este código es clave para entender hacia dónde va el sistema, pero no debe venderse como runtime productivo hasta cerrar cutover y validación.

---

## Estado de publicación antes de este paquete

Antes de preparar este branch:

| Archivo / grupo | Estado detectado |
|---|---|
| `src/api/ai/router.py` | Local, no trackeado |
| `src/infrastructure/llm_service.py` | Local, no trackeado |
| `app/ai/**` | En HEAD local y/o modificado, no en remotos principales |
| `prompts/*.yaml` | En HEAD local y/o modificados, no en remotos principales |
| `servidor_api_orthanc_v1.py` | En remotos, pero no limpio en HEAD local |
| `share/local-onboarding-package` | Limpio para onboarding, incompleto para lectura técnica actual |

---

## Regla para leer este branch

Usar estas etiquetas mentales:

- **Autoridad productiva**: lo que deploy/Docker declaran como activo hoy.
- **Target modular**: lo que representa el flujo moderno, pero requiere cutover.
- **Lectura técnica**: código incluido para entender arquitectura, no necesariamente para ejecutar local perfecto.
- **Sensible**: prompts, credenciales, datos clínicos o configuraciones que no se publican casualmente.

---

## Qué NO afirma este paquete

- No afirma que local levante completo en todas las máquinas.
- No afirma que el modular ya esté en producción.
- No afirma que todos los prompts estén validados productivamente.
- No autoriza deploy ni cutover.
- No reemplaza auditoría de secretos/PHI antes de publicar.
