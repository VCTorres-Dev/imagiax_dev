# Exploration: Current AI Analysis Pipeline for Prompt Orchestrator

**Fecha:** 2026-04-05  
**Proyecto:** IMAGIAX — imagiax.dev  
**Objetivo:** Mapear la arquitectura actual del pipeline de análisis IA para diseñar un prompt orchestrator.

---

## A. Current Pipeline Architecture

### Diagrama de flujo completo

```
DICOM Upload (Orthanc PACS)
       │
       ▼
POST /generar/informe  ←── médico hace click en "Generar Informe"
       │
       ├── [Paso 0] Deduplicación: ¿existe informe <2min para este estudio? → retorna cacheado
       ├── [Paso 1a] Verificar créditos / métodos de pago
       │
       ├── [Paso 2] Orthanc: find_instances_by_query(StudyInstanceUID)
       │           └── retorna lista de orthanc_instance_ids
       │
       ├── [Paso 3] Por cada instancia (máx 20):
       │   │
       │   ├── get_instance_dicom_bytes(orthanc_id)   → bytes DICOM raw
       │   ├── get_instance_tags(orthanc_id)          → dict de tags DICOM
       │   │
       │   └── analyze_single_instance(orthanc_id)
       │       ├── dicom_to_tensor(dicom_bytes)
       │       │   ├── pydicom.dcmread → pixel_array
       │       │   ├── apply_modality_lut (RescaleSlope/Intercept)
       │       │   ├── MONOCHROME1 correction
       │       │   ├── robust percentile normalization (p1/p99)
       │       │   ├── CLAHE (clipLimit=2.0, 8x8)
       │       │   ├── resize 512×512 (anti_aliasing)
       │       │   └── TXV normalization: pixel * 2048 - 1024
       │       │
       │       └── infer_multilabel(tensor)   [ResNet50-512-all]
       │           ├── model.forward(tensor)  → 18 probabilidades [0,1]
       │           ├── compare vs UMBRALES (0.5 para 16 patologías, 0.6 para 2)
       │           └── retorna: nivel_alerta, hallazgos[], sin_hallazgos, todas_patologias{}
       │
       ├── [Paso 4] Agregar resultados por estudio:
       │   ├── max_by_pathology: máximo probabilidad por patología entre instancias
       │   ├── study_findings: patologías que superan umbral
       │   └── nivel_alerta: NORMAL / BAJA / MEDIA / ALTA
       │
       ├── [Paso 5] Clasificar instancias por proyección (ViewPosition / SeriesDescription)
       │   ├── PA = ViewPosition in {PA, AP}
       │   ├── LAT = ViewPosition in {LL, RL, LAT, LATERAL}
       │   └── Fallback: buscar "PA"/"LAT" en SeriesDescription
       │
       ├── [Paso 6] Recolectar imágenes para Gemini:
       │   ├── ordered_for_gemini = PA_insts + LAT_insts + OTHER_insts  (máx 2)
       │   └── _dicom_to_png_bytes(raw) → PNG 8-bit normalizado (máx 1536px)
       │
       ├── [Paso 7] generar_informe_llm(ai_resultados, patient_meta, dicom_images)
       │   ├── Construye prompt (ver Sección B)
       │   ├── Gemini Vision: images + prompt
       │   ├── system_instruction: "Eres un médico radiólogo con 20 años..."
       │   ├── temperature=0.3, max_output_tokens=4096
       │   ├── Retry: 4 intentos con backoff exponencial (5s, 10s, 20s)
       │   └── Fallback: template estático si Gemini falla
       │
       ├── [Paso 8] Cobro (Créditos > Oneclick > MP Card)
       └── [Paso 9] Persistir en PostgreSQL + retornar JSON
```

### Componentes del pipeline

| Componente | Archivo | Rol |
|---|---|---|
| `imagiax_model.py` | `imagiax_model.py` | Define NIH_PATOLOGIAS, UMBRALES, get_model(), preprocesar() (para JPG/PNG) |
| `servidor_api_v6.py` | `servidor_api_v6.py` | Re-exporta DEVICE, NIH_PATOLOGIAS, UMBRALES, get_model() |
| `servidor_api_orthanc_v1.py` | `servidor_api_orthanc_v1.py` | Pipeline completo: DICOM → ML → Gemini → informe |

> **NOTA CRÍTICA**: `servidor_api_orthanc_v1.py` importa el modelo de `servidor_api_v6.py` (línea 57), NO de `imagiax_model.py`. Ambos definen el mismo modelo, pero el servidor v6 es quien realmente se usa en producción.

---

## B. The Current Gemini Prompt

### Prompt COMPLETO (copiado verbatim del código, líneas 1137–1194)

```python
prompt = f"""Eres el radiólogo que firma este informe. Redáctalo en español profesional.
{edad_txt}
HALLAZGOS CONFIRMADOS POR ANÁLISIS CUANTITATIVO:
{hallazgos_confirmados_txt}{hallazgos_leves_bloque}

INSTRUCCIONES:

1. SECCIÓN HALLAZGOS — Revisión sistemática completa del tórax.
   Describe CADA estructura usando lo que ves en la imagen:
     a) Campos pulmonares: transparencia, trama broncovascular, presencia de opacidades o consolidaciones.
     b) Silueta cardíaca: tamaño relativo (índice cardiotorácico), contornos.
     c) Mediastino: ancho, contornos, tráquea (posición y calibre).
     d) Aorta torácica: calibre, trayecto del cayado, calcificaciones en la pared.
     e) Hilios pulmonares: tamaño, densidad, simetría.
     f) Senos costofrénicos: libres u ocupados.
     g) Diafragmas: contornos, posición, presencia de gas o tejido supradiafragmático anormal (hernia hiatal).
     h) Estructuras óseas: costillas, columna dorsal, clavículas — fracturas, lesiones líticas/blásticas, cambios degenerativos.
     i) Partes blandas: pared torácica, tejido subcutáneo.

   REGLA PARA HALLAZGOS CONFIRMADOS: descríbelos con localización y aspecto visual detallado.
   REGLA PARA ESTRUCTURAS SIN HALLAZGO CONFIRMADO: describe lo que realmente ves en la imagen con lenguaje morfológico.
     - Si la estructura se ve normal → "de aspecto habitual", "sin alteraciones evidentes", "libres".
     - Si la estructura se ve anormal → descríbela tal cual la ves, SIN diagnóstico, y agrégala también en OBSERVACIONES.

2. SECCIÓN OBSERVACIONES VISUALES ADICIONALES — Hallazgos visuales NO confirmados por el algoritmo.
   El análisis cuantitativo solo detecta 18 patologías parenquimatosas. NO detecta:
   elongación/ateromatosis aórtica, hernia hiatal, engrosamiento intersticial difuso,
   escoliosis, cambios degenerativos, dispositivos médicos, nódulos pequeños, ni otros hallazgos incidentales.
   Si al examinar la imagen en la sección anterior encontraste CUALQUIER anomalía visual
   que no está en la lista confirmada, repórtala aquí como:
   "Se observa [descripción morfológica]. Hallazgo a correlacionar clínicamente."
   Si no hay nada adicional: "Sin observaciones adicionales."

FORMATO OBLIGATORIO — estos encabezados exactos:

TÉCNICA
Estudio radiográfico de tórax en proyección {proyecciones_txt}.

HALLAZGOS
- Un guion "-" por línea, una estructura por línea.
- Primero los hallazgos confirmados, luego el resto de la revisión sistemática (a-i).

IMPRESIÓN DIAGNÓSTICA
Resumen clínico conciso. Incluye hallazgos confirmados + cualquier observación visual relevante.

OBSERVACIONES VISUALES ADICIONALES
Anomalías visuales no confirmadas por el algoritmo (como se describe en instrucción 2).

RECOMENDACIONES
Conducta clínica recomendada.

REGLAS:
- NO menciones: probabilidad, porcentaje, umbral, modelo, software, IA, algoritmo.
- NO numeres los hallazgos, usa solo "-".
- Sin texto introductorio antes de TÉCNICA."""
```

### System instruction (verbatim, líneas 1196–1205)

```python
system_instruction = (
    "Eres un médico radiólogo con 20 años de experiencia. "
    "Redactas informes radiológicos completos en español. "
    "Realizas una revisión sistemática de TODAS las estructuras torácicas en cada imagen. "
    "Describes lo que ves de forma objetiva y morfológica. "
    "El análisis cuantitativo automatizado solo cubre 18 patologías parenquimatosas. "
    "Tú eres responsable de detectar visualmente todo lo que el algoritmo no cubre: "
    "cambios vasculares, hernias, cambios intersticiales difusos, fracturas sutiles, "
    "dispositivos, cambios degenerativos, y cualquier otro hallazgo incidental. "
    "NUNCA mencionas inteligencia artificial, algoritmos, modelos ni probabilidades. "
    "El informe comienza directamente con TÉCNICA."
)
```

### Análisis del prompt actual

**Fortalezas:**
- Revisión sistemática explícita de 9 estructuras (a-i): obliga cobertura completa
- Distinción confirmados vs. observaciones visuales adicionales: intenta separar ML-output de visión
- Regla de no mencionar IA/algoritmo: protege la credibilidad clínica
- Calibración etaria: si `patient_age` está disponible, ajusta expectativas (pediátrico <18 años)
- Formato estructurado obligatorio: TÉCNICA / HALLAZGOS / IMPRESIÓN / OBSERVACIONES / RECOMENDACIONES

**Debilidades:**
- **HARD-CODED como tórax**: la sección de hallazgos (a-i) describe tórax exclusivamente (campos pulmonares, silueta cardíaca, aorta, senos costofrénicos, etc.)
- **No usa Modality**: el prompt no sabe si es CR, DX, CT, MR. Un CT de tórax tiene una revisión radicalmente diferente a un RX
- **No usa BodyPartExamined**: podría recibir una columna cervical o una pelvis y el prompt seguiría describiendo tórax
- **No usa PatientSex**: el sexo no se extrae en absolute en el pipeline actual
- **Hallazgos pre-filtrados desde ResNet50**: el modelo solo corre bien en tórax PA/AP. Si llega una imagen de rodilla, generará scores de NIH sin sentido que se pasan como "confirmados" a Gemini
- **Paranoia inducida por el ML**: cuando ResNet detecta algo con probabilidad 50-60%, va como "CONFIRMADO" al prompt, inflando falsos positivos. El modelo no sabe que está mirando una imagen de cadera
- **Sin contexto del tipo de examen**: "Rx tórax de control post-operatorio" vs "Rx tórax urgencia" implican radiólogos con enfoque diferente
- **edad_txt es opcional**: si PatientAge no está en el DICOM (cosa común), el prompt pierde esta calibración

**Metadatos que actualmente usa el prompt:**
- `patient_age` → calibración etaria (si disponible)
- `proyecciones_txt` → texto "posteroanterior", "posteroanterior y lateral", etc.
- `hallazgos_confirmados_txt` → patologías sobre umbral del ResNet50
- **NO usa**: Modality, BodyPartExamined, PatientSex, StudyDescription, modalidad de examen

---

## C. The ML Model Analysis

### Arquitectura exacta

**Modelo:** `ResNet50-512-all` de TorchXRayVision  
**Fuente:** `torchxrayvision.models.ResNet(weights="resnet50-res512-all")`  
**Tamaño:** ~90 MB (descarga desde HuggingFace/TXV la primera vez)  
**Dispositivo:** CPU (sin GPU en servidor actual) o CUDA si disponible  
**Inference pipeline:** sigmoid + op_norm internamente (NO aplicar sigmoid extra)

### Las 18 clases NIH (NIH_PATOLOGIAS)

```python
NIH_PATOLOGIAS = [
    "Atelectasis",              # Atelectasia
    "Consolidation",            # Consolidación pulmonar
    "Infiltration",             # Infiltrado pulmonar
    "Pneumothorax",             # Neumotórax
    "Edema",                    # Edema pulmonar
    "Emphysema",                # Enfisema pulmonar
    "Fibrosis",                 # Fibrosis pulmonar
    "Effusion",                 # Derrame pleural
    "Pneumonia",                # Neumonía
    "Pleural_Thickening",       # Engrosamiento pleural
    "Cardiomegaly",             # Cardiomegalia
    "Nodule",                   # Nódulo pulmonar
    "Mass",                     # Masa pulmonar
    "Hernia",                   # Hernia diafragmática
    "Lung Lesion",              # Lesión pulmonar    ← umbral 0.6 (op_thresh=NaN)
    "Fracture",                 # Fractura
    "Lung Opacity",             # Opacidad pulmonar
    "Enlarged Cardiomediastinum",# Ensanchamiento mediastínico ← umbral 0.6 (op_thresh=NaN)
]
```

**Umbrales:** 0.5 para 16 patologías, 0.6 para "Lung Lesion" y "Enlarged Cardiomediastinum" (tienen `op_thresh=NaN` en el modelo, por lo que op_norm devuelve siempre 0.5 → el umbral elevado evita falsos positivos constantes)

### Benchmarks declarados en el código

| Patología | AUC ResNet50-512 | AUC DenseNet121-224 (anterior) |
|---|---|---|
| Pneumothorax | ~0.91 | 0.75 |
| Emphysema | ~0.88 | 0.73 |
| Cardiomegaly | ~0.90 | 0.88 |

### Cómo se formatea y pasa a Gemini

1. `infer_multilabel(tensor)` retorna:
   ```json
   {
     "nivel_alerta": "MEDIA",
     "hallazgos": [
       {"patologia": "Pneumonia", "probabilidad_pct": 67.3},
       {"patologia": "Effusion", "probabilidad_pct": 52.1}
     ],
     "sin_hallazgos": false,
     "todas_patologias": {
       "Atelectasis": {"probabilidad_pct": 23.4, "positivo": false, "umbral_pct": 50.0},
       ...
     }
   }
   ```

2. Para estudios multi-instancia, se hace **max-pooling por patología** entre instancias del mismo estudio.

3. Solo las patologías que superan el umbral se pasan como "CONFIRMADOS" al prompt.

4. El texto exacto que recibe Gemini es:
   ```
   HALLAZGOS CONFIRMADOS POR ANÁLISIS CUANTITATIVO:
     - Neumonía (CONFIRMADO — hallazgo principal)
     - Derrame pleural (CONFIRMADO — hallazgo principal)
   ```
   O si no hay ninguno:
   ```
     - Ningún hallazgo patológico significativo confirmado (estudio dentro de límites normales)
   ```

### Evaluación honesta: ¿agrega valor o ruido?

**Valor real que aporta:**
- En Rx de tórax PA/AP con buena calidad: AUC 0.88-0.91 para las patologías críticas (neumotórax, enfisema, cardiomegalia) es clínicamente útil como segundo criterio
- Da a Gemini un "ancla cuantitativa" que reduce aleatoriedad del informe
- Detecta patologías que podrían pasarse en una revisión rápida

**Ruido que genera:**
- **El modelo es CHEST-ONLY**: fue entrenado exclusivamente en NIH ChestX-ray14 (radiografías de tórax). Si llega cualquier otra modalidad o parte del cuerpo, los scores son basura estadística
- **No hay validación de body part en el pipeline**: el servidor pasa cualquier DICOM al modelo sin verificar que sea tórax
- **Umbrales fijos con falsos positivos**: con threshold 0.5, en una Rx normal se pueden activar 2-3 patologías por ruido estadístico, y todas van como "CONFIRMADAS" a Gemini
- **Inflación de paranoia**: Gemini recibe hallazgos "confirmados" que en realidad son ruido del modelo, y los toma en serio porque el prompt los presenta como certezas
- **Sin calibración por calidad de imagen**: una Rx de mala calidad, rotada, o pediátrica genera scores igualmente "confiados"

---

## D. Available Patient Metadata

### Tags DICOM que actualmente se EXTRAEN (en `analyze_single_instance`)

```python
{
    "StudyInstanceUID": tags.get("StudyInstanceUID"),
    "SeriesInstanceUID": tags.get("SeriesInstanceUID"),
    "SOPInstanceUID": tags.get("SOPInstanceUID"),
    "Modality": tags.get("Modality"),           # DX, CR, CT, MR, etc.
    "ViewPosition": tags.get("ViewPosition"),   # PA, AP, LAT, LL, RL
    "BodyPartExamined": tags.get("BodyPartExamined"), # CHEST, KNEE, SPINE, etc.
    "StudyDate": tags.get("StudyDate"),         # YYYYMMDD
    "PatientID": tags.get("PatientID"),
    "PatientName": tags.get("PatientName"),     # Apellido^Nombre
    "PatientAge": tags.get("PatientAge"),       # "081Y", "013Y", etc.
    "PatientBirthDate": tags.get("PatientBirthDate"), # YYYYMMDD (fallback para edad)
    "AccessionNumber": tags.get("AccessionNumber"),
    "InstanceNumber": tags.get("InstanceNumber"),
    "SeriesDescription": tags.get("SeriesDescription"), # "Torax PA", etc.
}
```

### Tags DICOM que se extraen PERO NO LLEGAN al prompt de Gemini

| Tag | Disponible en `analyzed[0]["dicom"]` | Pasa al prompt |
|---|---|---|
| `Modality` | ✅ | ❌ Solo se usa para persistir en DB |
| `ViewPosition` | ✅ | ❌ Solo para clasificar PA/LAT |
| `BodyPartExamined` | ✅ | ❌ Ignorado completamente en el prompt |
| `SeriesDescription` | ✅ | ❌ Solo para clasificar PA/LAT |
| `PatientBirthDate` | ✅ | ❌ Solo para calcular edad si falta PatientAge |

### Tags DICOM que NO se extraen actualmente

| Tag DICOM | Descripción | Valor para el orquestador |
|---|---|---|
| `PatientSex` (0010,0040) | M/F/O | Crítico: normal values para tórax, pelvis, mama difieren por sexo |
| `StudyDescription` (0008,1030) | "RX Tórax Control", "TC Abdomen urgencia" | Podría usarse para routing sin ML |
| `ReferringPhysicianName` (0008,0090) | Médico que solicitó | Contexto clínico |
| `RequestedProcedureDescription` (0032,1060) | "Radiografía de tórax PA y lateral" | Contexto del examen |
| `ImageComments` (0020,4000) | Comentarios libres | Contexto adicional |
| `KVP` (0018,0060) | Kilovoltaje | Calidad técnica |
| `ExposureInuAs` | Exposición | Calidad técnica |
| `ProtocolName` (0018,1030) | Protocolo usado | Tipo de examen CT/MR |
| `InstitutionName` (0008,0080) | Hospital/clínica | Contexto institucional |

### Flujo desde Orthanc al prompt

```
Orthanc /instances/{id}/simplified-tags
        │
        ▼ (dict of tags)
analyze_single_instance()
        │
        ▼ (dicom{} dict con 13 tags)
generar_informe() [extrae de analyzed[0]["dicom"]]
        │
        ├── patient_id ─────────────────► DB (no al prompt)
        ├── patient_name ───────────────► DB (no al prompt)
        ├── study_date ─────────────────► DB (no al prompt)
        ├── modalidad ──────────────────► DB (no al prompt)
        ├── patient_age ────────────────► PROMPT (edad_txt si disponible)
        ├── PatientBirthDate (fallback) ► cálculo de edad → PROMPT
        ├── ViewPosition ───────────────► proyecciones_txt → PROMPT
        └── SeriesDescription ──────────► proyecciones_txt → PROMPT

ResNet50 hallazgos ──────────────────────► hallazgos_confirmados_txt → PROMPT
```

---

## E. Key Findings for the Orchestrator Decision

### Información disponible para tomar decisiones de routing

El orquestador tendría acceso a todos estos datos **antes** de llamar a Gemini:

| Dato | Fuente | Disponibilidad |
|---|---|---|
| `Modality` | DICOM tag | ✅ Siempre presente (DX, CR, CT, MR, US, etc.) |
| `BodyPartExamined` | DICOM tag | ⚠️ A veces ausente o inconsistente |
| `ViewPosition` | DICOM tag | ⚠️ Solo en Rx proyectivas (PA, LAT, AP) |
| `SeriesDescription` | DICOM tag | ✅ Muy frecuente, texto libre |
| `StudyDescription` | DICOM tag | ⚠️ No extraído actualmente, hay que agregar |
| `PatientAge` | DICOM tag + fallback | ⚠️ Disponible si DICOM está bien formateado |
| `PatientSex` | DICOM tag | ❌ No extraído actualmente, hay que agregar |
| `PatientBirthDate` | DICOM tag | ✅ Como fallback para edad |

### Limitaciones actuales que el orquestador resuelve

1. **El modelo ResNet50 solo es válido para Rx de tórax** → el orquestador puede evitar correrlo (o ignorar sus resultados) para TC, MR, US, o para Rx de otras partes del cuerpo

2. **El prompt actual es hard-coded para tórax** → 9 estructuras torácicas descritas literalmente en el prompt. Un examen de columna, pelvis o extremidades recibiría un informe de tórax

3. **PatientSex nunca llega a Gemini** → Para mamografías, ginecología, urología, el sexo es información crítica. Actualmente no se extrae

4. **StudyDescription no se usa** → Contiene frecuentemente el tipo de examen en texto libre ("Control post-operatorio", "Urgencia fractura", "Seguimiento tumor")

5. **Falsos positivos inflados por threshold 0.5** → En imágenes que no son tórax o de baja calidad, el modelo genera scores de 50-55% que se presentan como "CONFIRMADOS" a Gemini. El orquestador podría simplemente NO correr el modelo si el body part no es tórax

6. **Sin diferenciación de urgencia vs. rutina** → Mismo prompt para una Rx urgencia que para control anual

### Riesgos y consideraciones

**Riesgo 1 — Complejidad del routing:**  
BodyPartExamined es frecuentemente inconsistente en DICOMs del mundo real. Algunos PACS lo llenan mal, otros no lo llenan. El orquestador debe tener fallback combinando Modality + SeriesDescription + StudyDescription.

**Riesgo 2 — Cobertura de casos:**  
Si el orquestador desconoce el tipo de examen (body part ambiguo, tags vacíos), debe tener un prompt "genérico" de alta calidad como fallback. No puede simplemente fallar.

**Riesgo 3 — Eliminación del modelo ML:**  
Si se elimina ResNet50, Gemini Vision queda como único "ojo". Gemini 2.5 Flash tiene capacidades de visión decentes, pero en ausencia del "ancla cuantitativa" podría ser más variable. Hay que evaluar si la calidad del informe mejora o empeora sin el modelo.

**Riesgo 4 — Alucinación sin ground-truth:**  
El modelo ML actual actúa como "referee": si dice que no hay neumotórax (score < 50%), Gemini raramente lo inventa. Sin ese referee, Gemini podría ser más propenso a alucinar hallazgos en imágenes difíciles.

**Riesgo 5 — Costo de tokens:**  
Prompts especializados por body part pueden ser más largos y detallados → mayor costo por inferencia. Hay que balancear especialización vs. costo.

**Riesgo 6 — Mantenimiento:**  
Múltiples prompts especializados implica mayor superficie de mantenimiento. Cambios médicamente importantes deben replicarse en todos los prompts.

---

## Resumen ejecutivo para la decisión

### ¿Qué tiene el sistema hoy?
- Un modelo ResNet50 chest-only que produce 18 scores para CUALQUIER imagen DICOM (incluso si no es tórax)
- Un prompt único y hard-coded para tórax que recibe esos scores como "confirmados"
- Metadata DICOM disponible (Modality, BodyPartExamined, ViewPosition) pero mayormente ignorada
- `PatientSex` completamente ausente del pipeline
- `StudyDescription` completamente ausente del pipeline

### ¿Qué ganaría el orquestador?
1. **Precisión**: Gemini recibe instrucciones específicas para lo que realmente está mirando
2. **Sin ruido ML**: Para exámenes no-tórax, eliminar los "confirmados" espurios del ResNet50
3. **Cobertura real**: Poder generar informes de Rx rodilla, columna, pelvis, TC, etc.
4. **Edad y sexo**: Calibrar el informe con información demográfica completa
5. **Contexto clínico**: Usar StudyDescription para adaptar el foco del informe

### Approach recomendado para el cambio
1. Extraer tags adicionales: `PatientSex`, `StudyDescription`, `RequestedProcedureDescription`
2. Crear un módulo `prompt_orchestrator.py` que reciba la metadata y retorne el prompt adecuado
3. Definir reglas de routing: Modality + BodyPartExamined → categoría de examen
4. Escribir prompts especializados por categoría (empezar con las más frecuentes)
5. Mantener ResNet50 SOLO para exámenes de tórax (sigue siendo útil ahí), eliminarlo para el resto
6. Prompt genérico como fallback cuando el routing no puede determinar la categoría

### Ready for Proposal
Sí — la exploración está completa. Los datos confirman que el cambio propuesto (eliminar modelo para casos no-tórax, crear orquestador de prompts basado en metadata) es tanto factible como necesario. La información de routing ya está en el DICOM; solo falta extraerla y usarla.
