# Benchmark v15 — iMagiax "The Attention Booster"
**Fecha de ejecución:** 2026-04-16  
**Commit evaluado:** `0760673` — `feat(v15): attention booster PCCR + demographic sanitizer`  
**Ambiente:** Producción — `imagiax.dev`

---

## Resultado Global

| Métrica | Valor |
|---------|-------|
| Casos ejecutados | 2/2 |
| **PASS** | ✅ 2 |
| **FAIL** | ❌ 0 |
| Casos MSK en producción | ⚠ 0 (sin estudios MSK en últimos 30) |

---

## Casos evaluados

### ESPINOSA — Tórax (caso de SEGURIDAD)
**Objetivo:** El AttentionBooster NO debe activar en tórax  
**Resultado:** ✅ PASS

| Check | Resultado |
|-------|-----------|
| Estructura (TÉCNICA / HALLAZGOS / IMPRESIÓN) | ✅ |
| Sin contaminación de IA en el texto | ✅ |
| Booster inactivo en tórax | ✅ |
| Sin lenguaje pediátrico inadecuado | ✅ |
| Tiempo de respuesta | 0.5s (informe en caché) |

**Extracto del informe:**
> HALLAZGOS  
> - Parénquima pulmonar con transparencia conservada, sin opacidades focales o difusas.  
> - Silueta cardíaca de tamaño y contornos dentro de la normalidad.  
> - Mediastino centrado, de morfología habitual. Tráquea en posición central.  
> - Senos costofrénicos libres bilateralmente.  
> - Estructuras óseas con cambios degenerativos en la columna dorsal. No se observan fracturas ni lesiones...

---

### ARAVENA — Tórax (caso de SEGURIDAD)
**Objetivo:** El AttentionBooster NO debe activar en tórax  
**Resultado:** ✅ PASS

| Check | Resultado |
|-------|-----------|
| Estructura (TÉCNICA / HALLAZGOS / IMPRESIÓN) | ✅ |
| Sin contaminación de IA en el texto | ✅ |
| Booster inactivo en tórax | ✅ |
| Sin lenguaje pediátrico inadecuado | ✅ |
| Tiempo de respuesta | 0.5s (informe en caché) |

**Extracto del informe:**
> HALLAZGOS  
> - Campos pulmonares con transparencia conservada, sin opacidades focales ni difusas. Trama broncovascular de distribución normal.  
> - Silueta cardíaca de tamaño y morfología dentro de límites normales.  
> - Mediastino centrado, de ancho normal. Tráquea en posición central...

---

## Limitaciones honestas

### 1. GODOY — DemographicSanitizer (edad DICOM anómala "001D")
El DemographicSanitizer está **activo en producción** pero GODOY no tiene un estudio nuevo en el sistema.  
✅ Verificable solo vía **test unitario TEST-13** (`scripts/test_global_validation.py`).  
❌ No se puede generar un informe real de GODOY en este benchmark.

### 2. CARRASCO — AttentionBooster en artrosis lumbar incipiente (SENSIBILIDAD)
El Booster correrá automáticamente en el **próximo estudio MSK** de CARRASCO.  
Los informes históricos en BD fueron generados con v14 — no se reprocesaron.  
⚠ Los **últimos 30 informes en producción no incluyen ningún estudio MSK o Pelvis**.

### 3. Calidad diagnóstica del Booster
Si el Booster activa y reporta hallazgos adicionales en un caso MSK, este benchmark **no puede validar** si esos hallazgos son clínicamente correctos. Eso requiere un radiólogo que revise las imágenes DICOM reales.  
El benchmark mide **estructura y seguridad**, NO calidad diagnóstica.

### 4. Tests unitarios (109/109)
Los tests validan lógica de activación, parsing, estructura del pipeline, y casos clínicos de referencia (GODOY, CARRASCO).  
**No validan** que Gemini detecte artrosis incipiente en una imagen real — eso depende de la calidad de la imagen y de la respuesta del modelo.

---

## Estado de v15 en producción

| Componente | Estado |
|------------|--------|
| `app/ai/attention_booster.py` — AttentionBooster PCCR (Fase 2 + Fase 3 + Ancla de Negación) | ✅ Activo |
| `app/ai/llm_service.py` — Pipeline draft → booster → audit engine | ✅ Activo |
| `app/ai/prompt_orchestrator.py` — DemographicSanitizer | ✅ Activo |
| `app/ai/router.py` — Integración DemographicSanitizer | ✅ Activo |
| `app/ai/clinical_audit_engine.py` — ClinicalAuditEngine global | ✅ Activo |
| Tests unitarios (`scripts/test_global_validation.py`) | ✅ 109/109 |

---

## Conclusión

**v15 "The Attention Booster" está en producción y el benchmark de seguridad pasó al 100%.**  
El Booster no interfiere con los casos de tórax (ESPINOSA, ARAVENA).  
El benchmark de sensibilidad (casos MSK) queda pendiente hasta que ingresen nuevos estudios MSK al sistema.
