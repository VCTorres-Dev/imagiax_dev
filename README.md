# IMAGIAX — Lectura técnica del sistema

IMAGIAX asiste el análisis radiológico desde OHIF: procesa estudios DICOM, construye contexto clínico y genera un borrador de informe para revisión médica.

- Ruta guiada para el equipo: [`docs/repo-truth/reviewer-map.md`](docs/repo-truth/reviewer-map.md)
- Mapa de componentes: [`docs/repo-truth/authority-matrix.md`](docs/repo-truth/authority-matrix.md)

---

## Flujo general del análisis

```mermaid
flowchart TD
    subgraph UI[1. Interacción clínica]
        A1[OHIF abre el estudio]
        A2[Panel IMAGIAX toma el StudyInstanceUID]
        A3[El médico puede agregar nota clínica]
        A1 --> A2 --> A3
    end

    subgraph PREP[2. Preparación del estudio]
        B1[POST generar/informe]
        B2[Orthanc localiza las instancias]
        B3[Se descargan DICOM y tags]
        B4[Se preparan imágenes para análisis]
        B5[Se extrae metadata clínica útil]
        B1 --> B2 --> B3
        B3 --> B4
        B3 --> B5
    end

    subgraph EVID[3. Evidencia computacional]
        C1[infer_multilabel calcula hallazgos]
        C2[Se resume nivel de alerta]
        C3[classify_exam identifica el tipo de examen]
        B4 --> C1 --> C2
        B5 --> C3
    end

    subgraph CTX[4. Contexto para Gemini]
        D1[_build_template_context reúne hallazgos y metadata]
        D2[Se selecciona template YAML]
        D3[assemble_prompt arma prompt y system]
        D4[Se inyecta metadata adicional]
        D5[Se agrega nota clínica saneada]
        C2 --> D1
        B5 --> D1
        C3 --> D2
        D1 --> D2 --> D3 --> D4 --> D5
        A3 --> D5
    end

    subgraph CTRL[5. Generación y controles]
        E1[Quality Gate opcional v1.1]
        E2[Gemini genera borrador estructurado]
        E3[AttentionBooster puede reexaminar regiones MSK]
        E4[ClinicalAuditEngine ajusta lenguaje o severidad]
        E5[Selective Auditor puede revisar y regenerar]
        E6[Fallback estructurado si Gemini falla]
        B4 --> E1 --> E2
        D5 --> E2
        E2 --> E3 --> E4 --> E5
        E2 -. error .-> E6
    end

    subgraph END[6. Revisión médica]
        F1[El panel muestra el borrador]
        F2[Se genera QR asociado al informe]
        F3[El médico revisa, edita, acepta o rechaza]
        F4[Se guarda la decisión]
        F5[Cuando corresponde, se genera PDF]
        E5 --> F1
        E6 --> F1
        F1 --> F2 --> F3 --> F4 --> F5
    end

    A2 --> B1
```

Punto clave: Gemini entra en la mitad del recorrido. Antes se preparan imágenes, tags, hallazgos y contexto; después se aplican controles y revisión médica.

---

## Mapa de lectura del código

| Si querés entender... | Empezá por... | Qué mirar |
|---|---|---|
| Inicio desde OHIF | `ohif-extension-imagiax-ai/src/ImagiaxAiPanel.tsx` | disparo del análisis, revisión, edición, PDF |
| Cliente frontend/backend | `ohif-extension-imagiax-ai/src/services/imagiaxApi.ts` | llamadas a `generarInforme`, `guardarDecision`, `getInformesPorEstudio` |
| Orquestación principal | `src/api/ai/router.py` | `generar_informe`, análisis del estudio, auditoría y persistencia |
| DICOM e inferencia visual | `src/infrastructure/dicom_service.py` | tensor, PNG, `infer_multilabel`, nivel de alerta |
| Clasificación y prompts | `app/ai/prompt_orchestrator.py` y `prompts/*.yaml` | template, contexto, nota clínica saneada |
| Integración Gemini | `src/infrastructure/llm_service.py` | generación, fallback, refinamiento y controles posteriores |
| Controles clínicos | `app/ai/quality_gate.py`, `app/ai/auditor.py`, `app/ai/clinical_audit_engine.py`, `app/ai/attention_booster.py` | quality gate, auditoría selectiva y reescrituras |

Si querés una ruta guiada por tiempo o perfil, usá [`docs/repo-truth/reviewer-map.md`](docs/repo-truth/reviewer-map.md).

---

## Por qué el informe se construye por etapas

```mermaid
flowchart LR
    A[Imágenes del estudio] --> F[Contexto de generación]
    B[Hallazgos del clasificador] --> F
    C[Metadata DICOM saneada] --> F
    D[Template clínico por examen] --> F
    E[Nota clínica opcional] --> F
    F --> G[Gemini]
    G --> H[Refinamiento y validación]
    H --> I[Informe revisable por el médico]
```

Porque cada capa cumple una responsabilidad distinta:

- el tipo de examen define qué template y qué reglas clínicas se usan;
- los hallazgos del modelo alimentan el contexto y el nivel de alerta;
- el quality gate puede frenar o marcar estudios subóptimos antes de seguir;
- después de Gemini, el borrador todavía puede ser refinado o auditado;
- la decisión final queda en manos del médico desde el panel.

---

## Qué insumos llegan a Gemini

Conceptualmente, Gemini recibe una combinación de:

1. imágenes derivadas del estudio DICOM;
2. etiquetas o contexto por imagen cuando están disponibles;
3. contexto clínico armado a partir de hallazgos, metadata y tipo de examen;
4. instrucciones especializadas desde templates YAML;
5. nota clínica opcional ya saneada;
6. en algunos templates, parches de alta resolución para zonas específicas.

La generación del informe no depende de una sola entrada: depende de cómo se combinan esas piezas antes y después de la llamada al modelo.

---

## Lectura base recomendada

1. `docs/repo-truth/authority-matrix.md`
2. `ohif-extension-imagiax-ai/src/ImagiaxAiPanel.tsx`
3. `ohif-extension-imagiax-ai/src/services/imagiaxApi.ts`
4. `src/api/ai/router.py`
5. `src/infrastructure/dicom_service.py`
6. `app/ai/prompt_orchestrator.py`
7. `prompts/chest_xray.yaml`
8. `src/infrastructure/llm_service.py`
9. `app/ai/quality_gate.py`
10. `app/ai/auditor.py`

Con ese recorrido se entiende cómo IMAGIAX conecta OHIF, DICOM, inferencia visual, contexto clínico, Gemini y revisión médica.
