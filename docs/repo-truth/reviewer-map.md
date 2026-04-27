# Guía de revisión para el equipo

Esta guía ordena el branch para que cada persona pueda revisar lo que necesita sin perderse en archivos secundarios.

## Objetivo del branch

Este paquete sirve para revisar el progreso técnico de IMAGIAX:

- cómo entra un estudio desde OHIF;
- cómo el backend consulta DICOM/Orthanc;
- cómo se calcula evidencia con IA;
- cómo se arma el contexto para Gemini;
- cómo los prompts influyen en el informe;
- qué parte está productiva y qué parte está en transición.

No es una rama de despliegue. No intenta incluir datos reales, secretos, archivos de host ni configuración productiva sensible.

---

## Ruta rápida: 20 minutos

Para una revisión general, leer en este orden:

1. `README.md`
2. `docs/repo-truth/authority-matrix.md`
3. `src/api/ai/router.py` — buscar `MAX_GEMINI_IMAGES` y la sección de `generar_informe`.
4. `src/infrastructure/llm_service.py` — revisar cómo se arma la llamada a Gemini.
5. `prompts/chest_xray.yaml` — ejemplo claro de prompt clínico especializado.

Con eso se entiende la arquitectura principal y la razón de usar un flujo por etapas.

---

## Ruta completa: 60–90 minutos

Para una revisión más profunda:

1. `docs/repo-truth/authority-matrix.md`
2. `servidor_api_orthanc_v1.py`
3. `src/api/ai/router.py`
4. `src/infrastructure/llm_service.py`
5. `app/ai/quality_gate.py`
6. `app/ai/auditor.py`
7. `app/ai/clinical_audit_engine.py`
8. `app/ai/attention_booster.py`
9. `app/ai/prompt_orchestrator.py`
10. `prompts/*.yaml`
11. `ohif-extension-imagiax-ai/src/services/imagiaxApi.ts`
12. `ohif-extension-imagiax-ai/src/ImagiaxAiPanel.tsx`

---

## Qué revisar por perfil

### Si querés entender el producto

- `README.md`
- `docs/repo-truth/authority-matrix.md`
- `prompts/chest_xray.yaml`
- `ohif-extension-imagiax-ai/src/ImagiaxAiPanel.tsx`

Foco: qué problema resuelve, cómo se ve el flujo y dónde interviene el médico.

### Si querés entender backend/IA

- `src/api/ai/router.py`
- `src/infrastructure/llm_service.py`
- `src/infrastructure/dicom_service.py`
- `src/core/medical_logic.py`
- `app/ai/**`

Foco: pipeline, imágenes, Gemini, reglas clínicas, auditoría y fallback.

### Si querés entender prompts

- `prompts/chest_xray.yaml`
- `prompts/lumbar_spine_xray.yaml`
- `prompts/pelvis_hip_xray.yaml`
- `prompts/auditor.yaml`
- `prompts/quality_gate.yaml`

Foco: por qué los prompts son especializados y por qué un prompt general no alcanza.

### Si querés entender frontend/OHIF

- `ohif-extension-imagiax-ai/src/services/imagiaxApi.ts`
- `ohif-extension-imagiax-ai/src/ImagiaxAiPanel.tsx`
- `ohif-extension-imagiax-ai/src/getPanelModule.tsx`
- `ohif-extension-imagiax-ai/src/getCommandsModule.ts`

Foco: cómo se dispara el análisis, cómo se muestra el informe y cómo se registra la decisión médica.

---

## Archivos incluidos solo como contexto

| Archivo / carpeta | Motivo |
|---|---|
| `docs/repo-curation/**` | Evidencia de por qué el backend modular sigue en transición |
| `tests/**` | Muestran expectativas y contratos, no son el foco principal de lectura |
| `servidor_api_orthanc_v1.py` | Importante porque es el runtime productivo documentado, aunque el flujo modular sea más nuevo |
| `requirements.txt` | Ayuda a entender dependencias principales |

---

## Qué se omitió a propósito

| Omitido | Por qué |
|---|---|
| `.env*` | Secretos/configuración local |
| DICOM/datasets/dumps SQL | Riesgo de datos sensibles y tamaño innecesario |
| `_quarantine`, `.sdd-temp`, `_backup` | Artefactos de trabajo, no código de lectura |
| Docker prod/nginx/Orthanc config real | Superficie sensible de operación/hosting |
| Auth/pagos completos | No son necesarios para entender el flujo IA/Gemini actual |
| Imágenes/logos/binarios | No aportan al análisis técnico; el logo del PDF puede agregarse luego como asset demostrativo si se necesita |

---

## Nota sobre el logo del PDF

El panel usa una referencia visual para simular cómo se vería un informe clínico final. En este branch no se prioriza incluir el asset binario real porque el objetivo es revisar el flujo técnico. Si más adelante se quiere una demo visual del PDF, se puede agregar un placeholder no sensible.

---

## Cómo leer los prompts sin confundirse

Los prompts no son “texto decorativo”. Son parte de la lógica clínica del sistema.

Al revisarlos, mirar:

1. qué tipo de examen cubren;
2. qué instrucciones recibe Gemini;
3. qué reglas evitan falsos positivos;
4. qué estructura debe tener el informe;
5. qué cosas se prohíben explícitamente;
6. cómo se complementan con auditoría y quality gate.

La pregunta correcta no es “¿por qué no hay un prompt único?”, sino:

> ¿Qué responsabilidad cumple cada etapa para reducir ambigüedad y mejorar trazabilidad?

---

## Resultado esperado de la revisión

Después de revisar este branch, el equipo debería poder responder:

- qué código corre hoy según la autoridad documentada;
- qué código representa el flujo modular más nuevo;
- cómo se arma el contexto para Gemini;
- por qué hay prompts por tipo de examen;
- cómo interviene el médico antes de cerrar un informe;
- qué está pendiente antes de hablar de deploy o ejecución local completa.
