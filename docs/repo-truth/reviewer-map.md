# Guía de revisión para el equipo

Esta guía ordena el branch para que cada persona pueda revisar el flujo técnico de IMAGIAX sin perderse en archivos secundarios.

## Qué vas a poder revisar acá

Este paquete permite revisar:

- cómo entra un estudio desde OHIF;
- cómo el backend obtiene imágenes y tags DICOM;
- cómo se generan hallazgos y nivel de alerta;
- cómo se arma el contexto para Gemini;
- cómo el médico revisa, edita, acepta o rechaza el informe.

---

## Ruta rápida: 20 minutos

Para una revisión general, leer en este orden:

1. `README.md`
2. `docs/repo-truth/authority-matrix.md`
3. `src/api/ai/router.py` — revisar `generar_informe`.
4. `src/infrastructure/llm_service.py` — revisar generación, fallback y refinamiento.
5. `ohif-extension-imagiax-ai/src/ImagiaxAiPanel.tsx` — revisar revisión médica y cierre.

> Si querés ver un template clínico concreto, abrí `prompts/chest_xray.yaml`.

---

## Ruta completa: 60–90 minutos

Para una revisión más profunda:

1. `docs/repo-truth/authority-matrix.md`
2. `ohif-extension-imagiax-ai/src/services/imagiaxApi.ts`
3. `src/api/ai/router.py`
4. `src/infrastructure/dicom_service.py`
5. `app/ai/prompt_orchestrator.py`
6. `prompts/*.yaml`
7. `src/infrastructure/llm_service.py`
8. `app/ai/quality_gate.py`
9. `app/ai/auditor.py`
10. `app/ai/clinical_audit_engine.py`
11. `app/ai/attention_booster.py`
12. `ohif-extension-imagiax-ai/src/ImagiaxAiPanel.tsx`
13. `servidor_api_orthanc_v1.py`

---

## Qué revisar por perfil

### Si querés entender el flujo clínico-producto

- `README.md`
- `ohif-extension-imagiax-ai/src/ImagiaxAiPanel.tsx`
- `ohif-extension-imagiax-ai/src/services/imagiaxApi.ts`
- `prompts/chest_xray.yaml`

Foco: cómo entra el estudio, cómo aparece el borrador y en qué punto interviene el médico.

### Si querés entender backend/IA

- `src/api/ai/router.py`
- `app/ai/orthanc_client.py`
- `src/infrastructure/llm_service.py`
- `src/infrastructure/dicom_service.py`
- `src/core/medical_logic.py`
- `app/ai/**`

Foco: pipeline, DICOM, inferencia visual, Gemini, reglas clínicas, auditoría y fallback.

### Si querés entender prompts

- `app/ai/prompt_orchestrator.py`
- `prompts/chest_xray.yaml`
- `prompts/lumbar_spine_xray.yaml`
- `prompts/pelvis_hip_xray.yaml`
- `prompts/auditor.yaml`
- `prompts/quality_gate.yaml`

Foco: cómo cambia el lenguaje clínico según el examen y cómo se arma el contexto que llega al modelo.

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
| `docs/repo-curation/**` | Evidencia técnica complementaria y reportes de revisión |
| `tests/**` | Muestran expectativas y contratos, no son el foco principal de lectura |
| `servidor_api_orthanc_v1.py` | Referencia integral del backend para contrastar responsabilidades |
| `requirements.txt` | Ayuda a entender dependencias principales |

---

## Cómo leer los prompts sin confundirse

Los prompts no son “texto decorativo”. Son parte de la lógica clínica del sistema.

Al revisarlos, mirar:

1. qué tipo de examen cubren;
2. qué instrucciones recibe Gemini;
3. qué reglas evitan falsos positivos;
4. qué estructura debe tener el informe;
5. qué cosas se prohíben explícitamente;
6. cómo se complementan con quality gate, auditoría y refinamiento.

La pregunta correcta no es “¿por qué no hay un prompt único?”, sino:

> ¿Qué responsabilidad cumple cada etapa para reducir ambigüedad y mejorar trazabilidad?

---

## Resultado esperado de la revisión

Después de revisar este branch, el equipo debería poder responder:

- cómo entra un estudio desde OHIF al pipeline;
- qué archivos concentran DICOM, inferencia visual, prompts y Gemini;
- cómo se arma el contexto para Gemini;
- por qué hay prompts por tipo de examen;
- cómo interviene el médico antes de cerrar un informe;
- qué controles adicionales se aplican antes de entregar el borrador.
