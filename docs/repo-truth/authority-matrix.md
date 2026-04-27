# Mapa técnico de componentes

Este documento ordena las piezas principales de IMAGIAX por responsabilidad dentro del flujo de análisis.

## Matriz principal

| Capa | Archivo / grupo | Qué aporta |
|---|---|---|
| Interfaz clínica | `ohif-extension-imagiax-ai/src/ImagiaxAiPanel.tsx`, `getPanelModule.tsx`, `getCommandsModule.ts` | dispara el análisis desde OHIF, muestra el borrador y permite editar, aceptar o rechazar |
| Cliente API | `ohif-extension-imagiax-ai/src/services/imagiaxApi.ts` | encapsula llamadas a `generarInforme`, `guardarDecision`, `getInformesPorEstudio` y chequeos de salud |
| Orquestación backend | `src/api/ai/router.py` | coordina el pipeline: resuelve estudios, analiza instancias, arma contexto, llama a LLM y persiste resultados |
| Acceso a Orthanc | `app/ai/orthanc_client.py` | busca instancias, obtiene tags y descarga bytes DICOM |
| DICOM e inferencia visual | `src/infrastructure/dicom_service.py` | convierte DICOM, ejecuta inferencia multilabel y resume hallazgos/nivel de alerta |
| Clasificación y prompts | `app/ai/prompt_orchestrator.py`, `prompts/*.yaml` | clasifica el examen, selecciona templates y construye instrucciones especializadas |
| Generación del informe | `src/infrastructure/llm_service.py` | integra Gemini, fallback estructurado, refinamientos y controles posteriores |
| Controles clínicos | `app/ai/quality_gate.py`, `app/ai/auditor.py`, `app/ai/clinical_audit_engine.py`, `app/ai/attention_booster.py`, `app/ai/pipeline_context.py` | verifican calidad, auditan, reescriben cuando hace falta y trazan las fases del pipeline |
| Lógica clínica auxiliar | `src/core/medical_logic.py` | aporta reglas auxiliares para regiones y hallazgos específicos |
| Referencia integral | `servidor_api_orthanc_v1.py` | concentra una versión amplia del backend y sirve como lectura complementaria |

---

## Cómo seguir el flujo de punta a punta

1. Empezá por `ohif-extension-imagiax-ai/src/ImagiaxAiPanel.tsx` para ver cómo se inicia el análisis y cómo se cierra la revisión médica.
2. Seguí con `ohif-extension-imagiax-ai/src/services/imagiaxApi.ts` para ubicar los endpoints principales.
3. Abrí `src/api/ai/router.py` y recorré `generar_informe` como eje del pipeline.
4. Bajá a `src/infrastructure/dicom_service.py` para ver preparación DICOM e inferencia visual.
5. Revisá `app/ai/prompt_orchestrator.py` y `prompts/*.yaml` para entender cómo se arma el contexto textual.
6. Terminá en `src/infrastructure/llm_service.py` y `app/ai/**` para ver generación, controles y refinamientos.

---

## Regla de lectura

Ningún archivo explica todo el sistema por sí solo.

- `router.py` coordina el recorrido general;
- `dicom_service.py` produce la evidencia computacional;
- `prompt_orchestrator.py` y `prompts/*.yaml` convierten esa evidencia en contexto clínico utilizable;
- `llm_service.py` genera y refina el borrador;
- `ImagiaxAiPanel.tsx` cierra el loop con revisión y decisión médica.
