"""
app.ai.clinical_audit_engine — Clinical Audit Engine for IMAGIAX Global

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PROTOCOLO DE INERCIA SEMÁNTICA — Fundamento del Motor de Auditoría
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Los Modelos de Lenguaje-Visión Grandes (MLLMs) como Gemini exhiben un fenómeno
conocido como "Inercia Semántica": al procesar imágenes radiológicas con ruido
visual o artefactos técnicos, el modelo tiende a sobre-interpretar sombras,
superposiciones y variaciones de densidad como indicios de patología. Esta
tendencia es un prior estadístico del entrenamiento — el modelo fue expuesto a
grandes volúmenes de imágenes patológicas etiquetadas, por lo que su sesgo
base es ENCONTRAR hallazgos antes que declarar normalidad.

El problema se manifiesta especialmente en:
  1. Estudios normales o casi-normales donde el modelo inventa vocabulario
     diagnóstico para "justificar" una narrativa radiológica (alucinación visual).
  2. Estudios con señal inespecífica (opacidades laminares, sombras técnicas)
     donde el modelo escala la severidad del diagnóstico sin evidencia real.
  3. Mutación de vocabulario: cuando se bloquea vocabulario agudo (neumonía,
     consolidación), el modelo muta a vocabulario crónico (fibrosis, EPI) o
     morfológico de alta severidad (adenopatías, ensanchamiento hiliar lobulado)
     para evadir restricciones textuales.

Este motor actúa como un VETO basado en evidencia técnica cuantitativa:
  - Para tórax: ResNet50 entrenado en NIH ChestX-ray14 (>100.000 radiografías)
    provee una señal objetiva de presencia/ausencia de patología parenquimatosa.
  - Para otras regiones sin ResNet disponible: el motor no veta (AUDIT_DISABLED)
    porque actuar sin evidencia cuantitativa podría suprimir hallazgos reales.

La lógica es asimétrica por diseño (Fail-Safe):
  - Falso positivo del MLLM (alucina patología) → el árbitro lo elimina.
  - Falso negativo del ResNet (no ve algo real) → el árbitro NO actúa.
  - Sin ResNet para la región → AUDIT_DISABLED (confiamos en Gemini Pro + revisión del radiólogo).
  Es preferible la seguridad clínica (no reportar alucinaciones) sobre la
  sensibilidad máxima, que es responsabilidad del radiólogo firmante.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Responsabilidades de este módulo (Single Responsibility):
  1. Clasificar la señal ResNet por región anatómica (ChestSignal / futuro MSKSignal).
  2. Auditar el informe generado contra esa señal (post-generación).
  3. Reescribir o forzar template cuando hay discrepancia.

Responsabilidad EXPLÍCITAMENTE excluida:
  - Construir el prompt de Gemini (eso es responsabilidad de llm_service.py).
  - classify_chest_signal() expone el resultado de la clasificación para que
    llm_service.py construya el prompt correcto — SIN lógica de generación aquí.

Uso:
    from app.ai.clinical_audit_engine import (
        ClinicalAuditEngine, BodyPart, ChestSignal,
        classify_chest_signal, normalize_body_part,
    )

    # Pre-generación: clasificar señal para construir el prompt
    signal = classify_chest_signal(ai_resultados)

    # Post-generación: auditar el informe
    engine = ClinicalAuditEngine()
    audited_report, was_intervened = await engine.validate(
        report=gemini_report,
        ai_results=ai_resultados,
        body_part=BodyPart.CHEST,
        gemini_client=client,
        proyecciones_txt="posteroanterior y lateral",
    )
"""

from __future__ import annotations

import asyncio
import logging
from enum import Enum
from typing import Any

from google import genai

from core.constants import strip_xml_tags as _strip_xml_tags
from app.core.config import settings

logger = logging.getLogger("imagiax.audit")


# ============================================================
# BodyPart Enum — clave de routing para los diccionarios
# ============================================================

class BodyPart(str, Enum):
    """Región anatómica que determina qué vocabulario de auditoría aplicar.

    Mapeo desde ExamRegion del prompt_orchestrator:
        ExamRegion.CHEST                              → BodyPart.CHEST
        ExamRegion.KNEE / SHOULDER / WRIST /
        ExamRegion.HAND / LUMBAR_SPINE / CERVICAL_SPINE → BodyPart.SKELETAL
        ExamRegion.PELVIS                             → BodyPart.PELVIS
        ExamRegion.BRAIN                              → BodyPart.BRAIN
        ExamRegion.ABDOMEN                            → BodyPart.ABDOMEN
        ExamRegion.GENERIC                            → BodyPart.UNKNOWN
    """

    CHEST    = "chest"
    SKELETAL = "skeletal"   # MSK general: rodilla, hombro, muñeca, mano, columna
    PELVIS   = "pelvis"
    BRAIN    = "brain"
    ABDOMEN  = "abdomen"
    UNKNOWN  = "unknown"    # Sin clasificación — audit deshabilitado (Fail-Safe)


# ============================================================
# ChestSignal — clasificación de la señal ResNet para tórax
# ============================================================

class ChestSignal(str, Enum):
    """Clasificación de la señal ResNet para estudios de tórax.

    Este enum es la interfaz entre el motor de auditoría y llm_service.py.
    llm_service.py lo usa para construir el bloque de prompt correcto;
    el engine lo usa internamente para determinar si debe actuar.

    PATHOLOGICAL:
        ResNet detectó ≥1 patología grave con ≥50% de confianza.
        (Consolidation, Pneumonia, Effusion, Edema, Pneumothorax, Mass,
        Lung Lesion, Enlarged Cardiomediastinum)
        → El árbitro NO actúa (Gemini puede reportar libremente).

    GENERIC_SIGNAL:
        ResNet sólo detectó etiquetas genéricas (Lung Opacity, Atelectasis,
        Infiltration) — super-clases del dataset NIH que incluyen hallazgos
        menores. Sin patología grave confirmada.
        → El árbitro actúa en modo VETO + llm_service inyecta Puerta Semántica.

    NORMAL:
        ResNet no detectó hallazgos positivos (hallazgos_estudio=[]).
        → El árbitro actúa en modo VETO + llm_service inyecta RESTRICCIÓN ACTIVA.

    NOT_APPLICABLE:
        No se ejecutó ResNet para este estudio (región no es CHEST o modalidad no es XRAY).
        → AuditMode.DISABLED, llm_service no inyecta restricciones.
    """

    PATHOLOGICAL    = "pathological"
    GENERIC_SIGNAL  = "generic_signal"
    NORMAL          = "normal"
    NOT_APPLICABLE  = "not_applicable"


# ============================================================
# Constantes ResNet — toda la lógica diagnóstica centralizada aquí
# ============================================================

# Patologías graves del dataset NIH ChestX-ray14 que invalidan el modo cuasi-normal.
# Si alguna aparece con ≥50% en hallazgos_estudio → señal PATHOLOGICAL → árbitro inactivo.
PATOLOGIAS_GRAVES_CHEST: frozenset[str] = frozenset({
    "Consolidation",
    "Pneumonia",
    "Effusion",
    "Edema",
    "Pneumothorax",
    "Mass",
    "Lung Lesion",
    "Enlarged Cardiomediastinum",
})

# Etiquetas genéricas del dataset NIH ChestX-ray14.
# Son super-clases que incluyen atelectasias laminares, opacidades retrocardiacas
# y sombras técnicas. Cuando son el único hallazgo positivo → señal GENERIC_SIGNAL.
ETIQUETAS_GENERICAS_CHEST: frozenset[str] = frozenset({
    "Lung Opacity",
    "Atelectasis",
    "Infiltration",
})


# ============================================================
# Vocabulario de Alta Severidad por región
# ============================================================

# Cada entrada es una tupla de substrings en minúsculas.
# Si ALGUNO aparece en el informe (sin estar cubierto por la whitelist en la
# misma línea), el árbitro detecta una discrepancia y actúa.

SEVERIDAD_ALTA: dict[BodyPart, tuple[str, ...]] = {

    BodyPart.CHEST: (
        # ── Patología aguda parenquimatosa ─────────────────────────────────
        "neumonía", "neumonia",
        "consolidación", "consolidacion",
        "derrame pleural", "derrame",
        "infiltrado",
        "masa pulmonar", "masa ",
        "proceso neumónico", "proceso neumonico",
        "proceso infeccioso", "proceso inflamatorio",
        "edema pulmonar",
        "insuficiencia cardíaca", "insuficiencia cardiaca",
        "tromboembolismo", "tep ",
        # ── Patología crónica / intersticial (vocabulario de mutación) ─────
        "enfermedad pulmonar intersticial", "epi ", " epi,", "(epi)",
        "fibrosis pulmonar", "fibrosis ",
        "patrón intersticial", "patron intersticial",
        "patrón reticular", "patron reticular",
        "vidrio esmerilado", "opacidades en vidrio",
        "atrapamiento aéreo", "atrapamiento aereo",
        "enfisema pulmonar", "enfisema ",
        "bronquiectasias", "bronquiectasia",
        "nódulo pulmonar", "nodulo pulmonar",
        "nódulos pulmonares", "nodulos pulmonares",
        "opacidad intersticial", "opacidades intersticiales",
        "engrosamiento intersticial", "engrosamientos intersticiales",
        "patrón micronodular", "patron micronodular",
        "lesión ocupante", "lesion ocupante",
        "masa mediastínica", "masa mediastinica",
        # ── Vocabulario morfológico de alta severidad (evasión del modelo) ─
        # El modelo usa estos términos para inferir patología sin diagnóstico
        # explícito (ej. "hilios de contornos lobulados" → sarcoide implícito).
        "adenopatías", "adenopatias", "adenopatía", "adenopatia",
        "ensanchamiento hiliar", "hilios ensanchados", "hilios aumentados de tamaño",
        "contornos lobulados", "contorno lobulado",
        "masa hiliar", "masas hiliares",
        "ocupación de senos costofrénicos", "senos costofrénicos ocupados",
        "senos costo-frénicos ocupados",
    ),

    BodyPart.SKELETAL: (
        # En MSK el árbitro opera en modo DESCRIPTIVE (sin ResNet validado).
        # Estos términos se convierten a lenguaje morfológico, no se eliminan.
        "fractura", "fractura desplazada", "fractura en tallo verde",
        "luxación", "luxacion", "subluxación", "subluxacion",
        "metástasis", "metastasis", "metástasis ósea", "metastasis osea",
        "lisis ósea", "lisis osea", "lesión lítica", "lesion litica",
        "lesión blástica", "lesion blastica",
        "neoplasia", "tumor óseo", "tumor oseo",
        "osteomielitis",
        "artritis séptica", "artritis septica",
    ),

    BodyPart.PELVIS: (
        "fractura de cadera", "fractura de fémur", "fractura de femur",
        "fractura pertrocantérea", "fractura pertrocanterica",
        "fractura subcapital",
        "luxación de cadera", "luxacion de cadera",
        "metástasis", "metastasis",
        "lisis ósea", "lesión lítica", "lesion litica",
        "masa pélvica", "masa pelvica",
        "calcificación patológica", "calcificacion patologica",
    ),

    BodyPart.BRAIN: (
        "hemorragia", "sangrado cerebral",
        "infarto cerebral", "accidente cerebrovascular", "acv",
        "tumor cerebral", "neoplasia cerebral", "metástasis cerebral",
        "absceso cerebral",
        "hidrocefalia",
        "edema cerebral", "efecto de masa",
    ),

    BodyPart.ABDOMEN: (
        "perforación", "perforacion",
        "obstrucción intestinal", "obstruccion intestinal",
        "masa abdominal",
        "neumoperitoneo",
        "colección", "coleccion",
    ),

    BodyPart.UNKNOWN: (),  # Audit deshabilitado — vocabulario vacío
}


# ============================================================
# Hallazgos Permitidos por región (whitelist de contexto)
# ============================================================

# Si una línea del informe contiene un término de SEVERIDAD_ALTA PERO TAMBIÉN
# contiene un término de esta whitelist, la línea se considera contexto válido
# (hallazgo real, no alucinación) y no dispara el árbitro.

HALLAZGOS_PERMITIDOS: dict[BodyPart, tuple[str, ...]] = {

    BodyPart.CHEST: (
        # Hallazgos extraparenquimatosos crónicos clínicamente reales
        "calcificaciones", "calcificación", "calcificacion",
        "ateromatosis", "ateromatosis aórtica", "ateromatosis aortica",
        "elongación aórtica", "elongacion aortica", "elongación", "elongacion",
        "ectasia aórtica", "ectasia aortica",
        "osteofito", "osteofitos", "rodetes osteofíticos", "rodetes osteofiticos",
        "cambios degenerativos", "cambio degenerativo",
        "escoliosis", "cifosis", "lordosis",
        "trama peribroncovascular", "trama broncovascular", "aumento de la trama",
        "trama hiliar", "hilios prominentes", "prominencia hiliar",
        "marcas vasculares", "marcas bronchovasculares", "marcas broncovasculares",
        "refuerzo hiliar", "refuerzo peribroncovascular",
        "hernia hiatal", "hernia de hiato",
        "marcapasos", "catéter", "cateter", "dispositivo", "electrodo",
        "engrosamiento pleural", "bandas pleurales", "pleura engrosada",
        "atelectasia lamelar", "atelectasia subsegmentaria", "atelectasias laminares",
        "opacidades retrocardiacas", "opacidades retrocardiaca",
    ),

    BodyPart.SKELETAL: (
        "cambios degenerativos", "cambio degenerativo",
        "osteofito", "osteofitos", "esclerosis subcondral",
        "pinzamiento del espacio articular",
        "calcificación periarticular", "calcificaciones periarticulares",
        "densidad conservada", "trama trabecular conservada",
        "sin desplazamiento", "alineación conservada",
        "partes blandas sin alteración",
    ),

    BodyPart.PELVIS: (
        "calcificaciones vasculares", "calcificaciones pélvicas",
        "cambios degenerativos", "osteofitos",
        "esclerosis subcondral",
        "prótesis", "implante", "tornillo", "placa de osteosíntesis",
    ),

    BodyPart.BRAIN: (
        "atrofia cortical", "cambios isquémicos crónicos",
        "leucoaraiosis", "gliosis",
        "calcificaciones basales", "calcificaciones de los ganglios",
        "variante anatómica", "variante de la normalidad",
    ),

    BodyPart.ABDOMEN: (
        "calcificaciones", "litiasis biliar", "litiasis renal",
        "cambios degenerativos vertebrales",
        "gas en asas colónicas", "patrón aéreo normal",
    ),

    BodyPart.UNKNOWN: (),
}


# ============================================================
# Template de Normalidad — función generadora (no dict estático)
# ============================================================

# Los textos base por región. Se exponen sólo a través de
# get_template_normalidad() para permitir extensión sin tocar este módulo.

_TEMPLATE_BASE: dict[BodyPart, str] = {

    BodyPart.CHEST: """\
TÉCNICA
Estudio radiográfico de tórax en proyección {proyecciones}.

INDICACIÓN CLÍNICA
No proporcionada.

HALLAZGOS
- Campos pulmonares con transparencia conservada, sin opacidades focales ni difusas.
- Silueta cardíaca de tamaño normal, índice cardiotorácico dentro de límites normales.
- Mediastino centrado, de ancho normal. Hilios de aspecto habitual.
- Senos costofrénicos libres bilateralmente. Diafragmas de contorno regular.
- Estructuras óseas sin lesiones evidentes.
- Partes blandas sin alteraciones significativas.

IMPRESIÓN DIAGNÓSTICA
Radiografía de tórax sin hallazgos patológicos significativos agudos.
El modelo de visión técnica confirmó ausencia de patología parenquimatosa.

OBSERVACIONES VISUALES ADICIONALES
Sin observaciones adicionales.

RECOMENDACIONES
Correlacionar con el cuadro clínico del paciente.

COMPARACIÓN
Sin estudios previos disponibles para comparación.

LIMITACIONES
La radiografía de tórax tiene limitaciones inherentes para evaluar el mediastino
profundo, lesiones subcentimétricas y estructuras posteriores. Hallazgos equívocos
requieren correlación clínica o estudios complementarios según indicación.""",

    BodyPart.SKELETAL: """\
TÉCNICA
Estudio radiográfico en proyección {proyecciones}.

INDICACIÓN CLÍNICA
No proporcionada.

HALLAZGOS
- Alineación ósea conservada. Sin deformidades evidentes.
- Densidad ósea conservada. Trama trabecular de aspecto normal.
- Superficies articulares de contorno regular. Espacio articular conservado.
- Partes blandas sin aumento de volumen ni calcificaciones patológicas evidentes.

IMPRESIÓN DIAGNÓSTICA
Estudio radiográfico sin hallazgos patológicos agudos significativos en la región evaluada.

OBSERVACIONES VISUALES ADICIONALES
Sin observaciones adicionales.

RECOMENDACIONES
Correlacionar con el cuadro clínico. Si persiste la sintomatología, considerar
estudios complementarios según indicación clínica.""",

    BodyPart.PELVIS: """\
TÉCNICA
Estudio radiográfico de pelvis en proyección {proyecciones}.

INDICACIÓN CLÍNICA
No proporcionada.

HALLAZGOS
- Alineación pélvica conservada, sin asimetrías evidentes.
- Cabezas femorales de aspecto habitual, espacios articulares de la cadera conservados.
- Densidad ósea conservada. Acetábulos de contorno regular.
- Partes blandas sin alteraciones significativas evidentes.

IMPRESIÓN DIAGNÓSTICA
Pelvis sin hallazgos patológicos agudos significativos.

OBSERVACIONES VISUALES ADICIONALES
Sin observaciones adicionales.

RECOMENDACIONES
Correlacionar con el cuadro clínico del paciente.""",

    BodyPart.BRAIN: """\
TÉCNICA
Estudio de imagen cerebral en {proyecciones}.

INDICACIÓN CLÍNICA
No proporcionada.

HALLAZGOS
- Parénquima cerebral sin lesiones focales de densidad/señal anormal.
- Sistema ventricular de tamaño y morfología normal.
- Surcos y cisuras de distribución habitual para la edad.
- Sin evidencia de sangrado extra o intraaxial.
- Estructuras de la línea media centradas.

IMPRESIÓN DIAGNÓSTICA
Estudio de imagen cerebral sin hallazgos patológicos agudos significativos.

OBSERVACIONES VISUALES ADICIONALES
Sin observaciones adicionales.

RECOMENDACIONES
Correlacionar con el cuadro clínico.""",

    BodyPart.ABDOMEN: """\
TÉCNICA
Estudio radiográfico de abdomen en {proyecciones}.

INDICACIÓN CLÍNICA
No proporcionada.

HALLAZGOS
- Distribución aérea intestinal de aspecto normal, sin niveles hidroaéreos patológicos.
- Sin evidencia de neumoperitoneo libre subdiafragmático.
- Siluetas de órganos sólidos sin alteraciones evidentes.
- Estructuras óseas sin lesiones evidentes.

IMPRESIÓN DIAGNÓSTICA
Radiografía de abdomen sin hallazgos patológicos agudos significativos.

OBSERVACIONES VISUALES ADICIONALES
Sin observaciones adicionales.

RECOMENDACIONES
Correlacionar con el cuadro clínico del paciente.""",

    BodyPart.UNKNOWN: """\
TÉCNICA
Estudio radiológico en {proyecciones}.

INDICACIÓN CLÍNICA
No proporcionada.

HALLAZGOS
Estudio analizado. Sin hallazgos patológicos evidentes en la región evaluada.

IMPRESIÓN DIAGNÓSTICA
Sin hallazgos patológicos agudos significativos. Correlacionar con clínica.

RECOMENDACIONES
Correlacionar con el cuadro clínico del paciente.""",
}


def get_template_normalidad(body_part: BodyPart, proyecciones: str) -> str:
    """Genera el template de normalidad para la región y proyección dadas.

    Función generadora — preferida sobre acceso directo al dict _TEMPLATE_BASE
    porque permite extensión futura (variables adicionales, lógica condicional)
    sin modificar los llamadores.

    Args:
        body_part: Región anatómica del estudio.
        proyecciones: Texto de proyecciones (ej. "posteroanterior y lateral").

    Returns:
        String del informe de normalidad, listo para devolver al usuario.
    """
    base = _TEMPLATE_BASE.get(body_part, _TEMPLATE_BASE[BodyPart.UNKNOWN])
    return base.format(proyecciones=proyecciones)


# ============================================================
# AuditMode — comportamiento del árbitro según región y ResNet
# ============================================================

class AuditMode(str, Enum):
    """Modo de operación del motor de auditoría.

    VETO:
        ResNet cuantitativo disponible y confiable para esta región.
        El árbitro puede ELIMINAR diagnósticos que contradicen el resultado técnico.
        Actualmente sólo para CHEST con ResNet50 NIH ChestX-ray14.

    DESCRIPTIVE:
        Sin ResNet confiable, pero el árbitro CONVIERTE diagnósticos definitivos
        a lenguaje descriptivo morfológico. Reduce riesgo sin silenciar hallazgos.
        Usado para SKELETAL, PELVIS cuando no hay modelo técnico validado.

    HEDGING_DESCRIPTIVE (v14 — CDI alto):
        Activado automáticamente cuando el CDI supera el umbral (≥ 2 términos de
        alta severidad simultáneos en regiones sin ResNet validado).
        Más restrictivo que DESCRIPTIVE: aplica la taxonomía de incertidumbre
        calibrada completa y añade la cláusula de correlación clínica obligatoria.
        Principio: alta divergencia entre señal técnica y texto diagnóstico indica
        riesgo de sobrediagnóstico; el árbitro eleva el nivel de hedging.

    DISABLED (Fail-Safe):
        Sin base de evidencia cuantitativa. El árbitro NO actúa.
        Principio: es preferible no vetar que vetar sin evidencia
        (subdiagnóstico de hemorragia cerebral > sobrediagnóstico de normal).
        Usado para BRAIN, ABDOMEN y UNKNOWN.
    """

    VETO                 = "veto"
    DESCRIPTIVE          = "descriptive"
    HEDGING_DESCRIPTIVE  = "hedging_descriptive"  # v14 — CDI alto
    DISABLED             = "disabled"


# Mapeo BodyPart → AuditMode por defecto.
# Cuando ResNet confirma patología grave, el árbitro se bypassa independientemente.
_DEFAULT_AUDIT_MODE: dict[BodyPart, AuditMode] = {
    BodyPart.CHEST:    AuditMode.VETO,
    BodyPart.SKELETAL: AuditMode.DESCRIPTIVE,
    BodyPart.PELVIS:   AuditMode.DESCRIPTIVE,
    BodyPart.BRAIN:    AuditMode.DISABLED,
    BodyPart.ABDOMEN:  AuditMode.DISABLED,
    BodyPart.UNKNOWN:  AuditMode.DISABLED,
}


# ============================================================
# CDI — Confidence Divergence Index (v14)
# ============================================================
#
# El CDI mide la divergencia entre la señal técnica cuantitativa (ResNet)
# y el contenido diagnóstico del informe generado por Gemini.
#
# Implementación:
#   CDI = len(terminos_alta_severidad_encontrados) / CDI_MAX_REFERENCIA
#
# Fundamento: en regiones sin ResNet validado (SKELETAL, PELVIS), la presencia
# de múltiples términos diagnósticos de alta severidad indica que el modelo
# de lenguaje está sobre-interpretando sin respaldo cuantitativo — que es
# exactamente la definición operacional de alta divergencia.
# Cuando CDI >= CDI_THRESHOLD, el árbitro escala de DESCRIPTIVE a
# HEDGING_DESCRIPTIVE (lenguaje de incertidumbre calibrada más restrictivo).
#
# Nota de diseño: el threshold de 0.6 del paper original asume logits de
# softmax de Gemini (no disponibles vía API). Esta implementación usa un
# proxy basado en conteo de términos, calibrado empíricamente:
#   - 1 término   → CDI = 0.33  (< 0.6 → DESCRIPTIVE normal)
#   - 2 términos  → CDI = 0.67  (> 0.6 → escala a HEDGING)
#   - 3+ términos → CDI = 1.0   (máximo → HEDGING garantizado)
# El denominador CDI_MAX_REFERENCIA = 3 equivale a "3 términos graves = CDI máximo".

CDI_THRESHOLD: float = 0.6          # umbral de activación HEDGING_DESCRIPTIVE
CDI_MAX_REFERENCIA: int = 3         # denominador para normalizar el proxy CDI

# Regiones donde el CDI puede escalar a HEDGING_DESCRIPTIVE.
# CHEST está excluido: tiene ResNet → el veto es cuantitativo, no proxy.
CDI_ELIGIBLE_REGIONS: frozenset[BodyPart] = frozenset({
    BodyPart.SKELETAL,
    BodyPart.PELVIS,
})


def calculate_cdi(terminos_encontrados: list[str]) -> float:
    """Calcula el Índice de Divergencia (CDI) como proxy de señal técnica vs LLM.

    CDI = len(terminos) / CDI_MAX_REFERENCIA, clamped a [0.0, 1.0].

    Args:
        terminos_encontrados: Lista de términos de alta severidad detectados por
            _informe_contiene_severidad_alta() en el informe generado.

    Returns:
        Float en [0.0, 1.0]. Un valor >= CDI_THRESHOLD indica divergencia alta
        y activa el modo HEDGING_DESCRIPTIVE.
    """
    if not terminos_encontrados:
        return 0.0
    return min(len(terminos_encontrados) / CDI_MAX_REFERENCIA, 1.0)


# ============================================================
# CFO — Critical Findings Observer (v14 — cimientos para v15)
# ============================================================
#
# El CFO es un módulo de vigilancia pasiva que detecta hallazgos de alta
# letalidad en el informe generado y los registra para alertas futuras.
# En v14 NO interviene ni modifica el informe — solo genera logs estructurados.
# En v15, estos logs alimentarán la Alerta Roja en la interfaz del radiólogo.
#
# High Lethality Taxonomy (HLT): diagnósticos cuya omisión puede ser fatal.
# Deben ser detectados incluso cuando el árbitro los suavice a DESCRIPTIVE.

HLT_TAXONOMY: frozenset[str] = frozenset({
    # Urgencias torácicas
    "neumotórax", "neumotorax", "pneumotórax",
    "hemotórax", "hemotorax",
    "taponamiento cardíaco", "taponamiento cardiaco",
    "tromboembolismo pulmonar", "embolia pulmonar", "tep ",
    # Urgencias MSK
    "fractura desplazada", "fractura inestable",
    "fractura vertebral", "aplastamiento vertebral",
    "luxación", "luxacion",
    "fractura de cadera", "fractura pertrocantérea",
    # Urgencias neurológicas / vasculares
    "hemorragia", "hematoma subdural", "hematoma epidural",
    "infarto cerebral", "accidente cerebrovascular",
    "disección aórtica", "diseccion aortica",
    # Masa / neoplasia con urgencia
    "masa expansiva", "efecto de masa",
    "tumor maligno", "neoplasia maligna",
    "metástasis", "metastasis",
})


def scan_cfo(report: str, body_part: BodyPart, pipeline_id: str = "") -> list[str]:
    """Escanea el informe en busca de hallazgos de alta letalidad (CFO — v14).

    Función PASIVA: no modifica el informe, solo registra en logs estructurados.
    Los hallazgos detectados se emitirán como alertas en v15.

    Args:
        report: Texto del informe (original o reescrito).
        body_part: Región anatómica del estudio.
        pipeline_id: ID de la pipeline para logs (opcional).

    Returns:
        Lista de términos HLT encontrados (vacía si ninguno).
        En v14 el llamador puede ignorar este valor — el log es el artefacto.
    """
    lower = report.lower()
    encontrados = [t for t in HLT_TAXONOMY if t in lower]

    if encontrados:
        logger.warning(
            "[CFO][PIPELINE:%s] Hallazgos de alta letalidad detectados — región=%s términos=%s. "
            "[v14: log only — v15 activará Alerta Roja en UI]",
            pipeline_id or "N/A",
            body_part.value,
            encontrados,
        )
    else:
        logger.debug(
            "[CFO][PIPELINE:%s] Sin hallazgos HLT en región=%s.",
            pipeline_id or "N/A",
            body_part.value,
        )

    return encontrados


# ============================================================
# classify_chest_signal() — interfaz pre-generación
# ============================================================

def classify_chest_signal(ai_resultados: dict[str, Any]) -> ChestSignal:
    """Clasifica la señal ResNet para estudios de tórax.

    Función PURA — no tiene efectos secundarios, no llama a Gemini.
    Devuelve un ChestSignal que llm_service.py usa para:
      1. Construir el bloque de contexto del prompt (hallazgos_confirmados_txt).
      2. Activar o no la Puerta Semántica CTA.

    El motor de auditoría (ClinicalAuditEngine.validate) consume
    internamente la misma lógica a través de _determinar_resnet_es_normal().

    Args:
        ai_resultados: Dict de resultados de la pipeline de IA.
            Campos esperados: hallazgos_estudio (list[dict]).

    Returns:
        ChestSignal clasificando la señal ResNet.
    """
    hallazgos = ai_resultados.get("hallazgos_estudio", [])
    hallazgos_positivos = [
        h for h in hallazgos
        if h.get("probabilidad_pct_max", 0) >= 30    # v36c: lowered from 50 to catch subtle findings
    ]

    if not hallazgos_positivos:
        return ChestSignal.NORMAL

    hallazgos_graves = [
        h for h in hallazgos_positivos
        if h.get("patologia") in PATOLOGIAS_GRAVES_CHEST
    ]
    if hallazgos_graves:
        return ChestSignal.PATHOLOGICAL

    # Solo etiquetas genéricas (Lung Opacity, Atelectasis, Infiltration)
    solo_genericos = all(
        h.get("patologia") in ETIQUETAS_GENERICAS_CHEST
        for h in hallazgos_positivos
    )
    if solo_genericos:
        return ChestSignal.GENERIC_SIGNAL

    # Hallazgos positivos que no son ni graves ni genéricos (caso edge)
    return ChestSignal.PATHOLOGICAL


# ============================================================
# Helpers internos de auditoría
# ============================================================

def _informe_contiene_severidad_alta(
    informe: str,
    body_part: BodyPart,
) -> list[str]:
    """Retorna los términos de alta severidad encontrados en el informe.

    Lógica de whitelist por línea: si una línea contiene un término de alta
    severidad PERO también contiene un hallazgo permitido para la región,
    esa línea se omite (contexto legítimo, no alucinación).

    Args:
        informe: Texto del informe en español.
        body_part: Región anatómica — determina el vocabulario de referencia.

    Returns:
        Lista de términos problemáticos encontrados (vacía = informe ok).
    """
    terminos_problematicos = SEVERIDAD_ALTA.get(body_part, ())
    terminos_permitidos    = HALLAZGOS_PERMITIDOS.get(body_part, ())

    if not terminos_problematicos:
        return []

    terminos_encontrados: list[str] = []
    lower_full = informe.lower()

    for termino in terminos_problematicos:
        if termino not in lower_full:
            continue
        for linea in lower_full.splitlines():
            if termino not in linea:
                continue
            linea_cubierta = any(p in linea for p in terminos_permitidos)
            if not linea_cubierta:
                terminos_encontrados.append(termino)
                break

    return terminos_encontrados


def _determinar_resnet_es_normal(
    ai_resultados: dict[str, Any],
    body_part: BodyPart,
) -> bool:
    """¿Indica ResNet que el estudio es normal para esta región?

    Para CHEST: evalúa hallazgos_estudio contra PATOLOGIAS_GRAVES_CHEST.
    Para otras regiones: devuelve True (sin ResNet = tratado como normal
    a efectos del árbitro; el AuditMode limita qué acción toma).

    Returns:
        True  → árbitro puede actuar (ResNet dice normal o no disponible).
        False → ResNet detectó patología grave confirmada, árbitro inactivo.
    """
    if body_part != BodyPart.CHEST:
        return True  # Sin ResNet para esta región — AuditMode controla la acción

    signal = classify_chest_signal(ai_resultados)
    return signal != ChestSignal.PATHOLOGICAL


# ============================================================
# Prompts del árbitro (Flash rewriter)
# ============================================================

def _build_prompt_arbitro_veto(
    informe_original: str,
    terminos_encontrados: list[str],
) -> str:
    """Prompt de reescritura para el árbitro en modo VETO (tórax)."""
    terminos_str = ", ".join(f"'{t}'" for t in terminos_encontrados)
    sep = "\n"
    return (
        "El siguiente es un informe radiologico de torax preliminar:" + sep + sep
        + "---" + sep + informe_original + sep + "---" + sep + sep
        + "PROBLEMA: El algoritmo de vision tecnica (ResNet50, entrenado en >100.000 "
        "radiografias) analizo este estudio y confirmo con alta confianza que es NORMAL — "
        "no hay patologia parenquimatosa pulmonar. Sin embargo, el informe preliminar "
        "menciona los siguientes terminos que constituyen alucinaciones visuales: "
        + terminos_str + "." + sep + sep
        + "TAREA: Reescribe el informe corrigiendo SOLO el parenchima pulmonar." + sep
        + "CONSERVA (no elimines):" + sep
        + "  - Hallazgos cardiovasculares y aorticos: ateromatosis, elongacion aortica, "
        "calcificaciones en cayado, ectasia." + sep
        + "  - Hallazgos oseos y vertebrales: osteofitos, cambios degenerativos, escoliosis." + sep
        + "  - Hallazgos vasculares hiliares: aumento de trama peribroncovascular, "
        "prominencia hiliar descriptiva." + sep
        + "  - Hallazgos pleurales menores: engrosamientos pleurales, bandas." + sep
        + "  - Atelectasias laminares o subsegmentarias (hallazgo menor, no patologia mayor)." + sep
        + "  - Opacidades retrocardiacas posiblemente atelectasicas." + sep
        + "  - Dispositivos medicos visibles (marcapasos, cateter, etc.)." + sep + sep
        + "ELIMINA COMPLETAMENTE del parenchima pulmonar:" + sep
        + "  - Los terminos listados: " + terminos_str + "." + sep
        + "  - Cualquier diagnostico de patologia pulmonar parenquimatosa aguda o cronica." + sep + sep
        + "Conserva el mismo formato de secciones (TECNICA, INDICACION CLINICA, HALLAZGOS, "
        "IMPRESION DIAGNOSTICA, OBSERVACIONES VISUALES ADICIONALES, RECOMENDACIONES, etc.)." + sep
        + "Responde UNICAMENTE con el informe reescrito, sin explicaciones adicionales."
    )


def _build_prompt_arbitro_descriptive(
    informe_original: str,
    terminos_encontrados: list[str],
    body_part: BodyPart,
) -> str:
    """Prompt de reescritura para el árbitro en modo DESCRIPTIVE (MSK/Pelvis).

    NO elimina el hallazgo — lo convierte a lenguaje descriptivo morfológico
    para que el radiólogo firmante evalúe directamente.
    """
    terminos_str = ", ".join(f"'{t}'" for t in terminos_encontrados)
    region_nombre = {
        BodyPart.SKELETAL: "musculoesquelética",
        BodyPart.PELVIS:   "pélvica",
    }.get(body_part, "radiológica")

    sep = "\n"
    return (
        "El siguiente es un informe radiologico de region " + region_nombre + " preliminar:"
        + sep + sep
        + "---" + sep + informe_original + sep + "---" + sep + sep
        + "PROBLEMA: No se dispone de un modelo cuantitativo validado para esta region. "
        "El informe contiene diagnosticos definitivos que no pueden confirmarse por analisis "
        "tecnico automatico: " + terminos_str + "." + sep + sep
        + "TAREA: Reescribe convirtiendo los diagnosticos definitivos a lenguaje DESCRIPTIVO "
        "morfologico." + sep + sep
        + "REGLA DE CONVERSION:" + sep
        + "  - NO uses diagnosticos definitivos ('fractura', 'luxacion', 'metastasis')." + sep
        + "  - USA lenguaje descriptivo: 'solucion de continuidad que requiere correlacion "
        "clinica', 'aumento de volumen de partes blandas', 'alteracion de la densidad osea "
        "que amerita evaluacion complementaria'." + sep
        + "  - Conserva toda la descripcion morfologica objetiva (localizacion, extension, "
        "caracteristicas visuales)." + sep
        + "  - En IMPRESION DIAGNOSTICA usa: 'Hallazgos que requieren evaluacion clinica "
        "directa y eventual estudio complementario (TC/RM)'." + sep + sep
        + "Conserva el formato de secciones existente." + sep
        + "Responde UNICAMENTE con el informe reescrito, sin explicaciones adicionales."
    )


def _build_prompt_arbitro_hedging(
    informe_original: str,
    terminos_encontrados: list[str],
    body_part: BodyPart,
    cdi_value: float,
) -> str:
    """Prompt de reescritura para el árbitro en modo HEDGING_DESCRIPTIVE (v14).

    Activado cuando CDI >= CDI_THRESHOLD: divergencia alta entre señal técnica
    y diagnóstico LLM. Aplica la taxonomía de incertidumbre calibrada completa:
    - Sustituciones morfológicas más estrictas que DESCRIPTIVE
    - Cláusula RCP (correlación clínica obligatoria) siempre presente
    - Lenguaje de hallazgos sugerentes, no confirmados

    Args:
        informe_original: Texto del informe a reescribir.
        terminos_encontrados: Términos de alta severidad detectados.
        body_part: Región anatómica.
        cdi_value: Valor CDI calculado (incluido en el log interno del prompt).
    """
    terminos_str = ", ".join(f"'{t}'" for t in terminos_encontrados)
    region_nombre = {
        BodyPart.SKELETAL: "musculoesquelética",
        BodyPart.PELVIS:   "pélvica",
    }.get(body_part, "radiológica")

    sep = "\n"
    return (
        "El siguiente es un informe radiologico de region " + region_nombre + " preliminar:"
        + sep + sep
        + "---" + sep + informe_original + sep + "---" + sep + sep
        + "CONTEXTO DE INCERTIDUMBRE (CDI=" + f"{cdi_value:.2f}" + "): "
        "Se detectaron multiples terminos diagnosticos de alta severidad sin respaldo "
        "de un modelo cuantitativo validado para esta region. Esto indica alta divergencia "
        "entre el analisis visual del modelo y la certeza tecnica disponible: "
        + terminos_str + "." + sep + sep
        + "TAREA: Reescribe el informe aplicando la TAXONOMIA DE INCERTIDUMBRE CALIBRADA." + sep + sep
        + "SUSTITUCIONES OBLIGATORIAS (Taxonomia de Hedging v14):" + sep
        + "  - 'Fractura' / 'fractura'              → 'Imagen sugerente de solucion de continuidad'" + sep
        + "  - 'Fractura desplazada'                → 'Discontinuidad cortical focal con posible desplazamiento'" + sep
        + "  - 'Luxacion' / 'luxación'              → 'Alteracion de la congruencia articular sugerente de luxacion'" + sep
        + "  - 'Masa' / 'masa '                     → 'Aumento de densidad de bordes inespecificos'" + sep
        + "  - 'Metastasis' / 'metástasis'          → 'Lesion de densidad heterogenea que requiere caracterizacion'" + sep
        + "  - 'Lesion litica' / 'lesion blastica'  → 'Alteracion focal de la trama osea de etiologia a determinar'" + sep
        + "  - 'Neoplasia'                          → 'Proceso expansivo de caracterizacion pendiente'" + sep
        + "  - 'Osteomielitis'                      → 'Cambios oseos sugerentes de proceso inflamatorio/infeccioso'" + sep + sep
        + "REGLAS ADICIONALES DE HEDGING:" + sep
        + "  - Sustituye TODA certeza diagnostica por lenguaje sugerente: "
        "'imagen compatible con', 'hallazgo sugerente de', 'patron morfologico que no excluye'." + sep
        + "  - Conserva la descripcion morfologica objetiva (localizacion, extension, densidad)." + sep
        + "  - NO omitas ningun hallazgo — el objetivo es calibrar la certeza, no suprimir." + sep + sep
        + "CLAUSULA RCP OBLIGATORIA (debe aparecer en RECOMENDACIONES):" + sep
        + "  Agrega siempre: 'Se sugiere correlacion con examen fisico y proyecciones adicionales "
        "por inconsistencia en la senal tecnica. Evaluacion directa por especialista recomendada.'." + sep + sep
        + "Conserva el formato de secciones existente (TECNICA, HALLAZGOS, IMPRESION DIAGNOSTICA, "
        "RECOMENDACIONES, etc.)." + sep
        + "Responde UNICAMENTE con el informe reescrito, sin explicaciones adicionales."
    )


# ============================================================
# Parámetros de inferencia v14
# ============================================================

# Temperatura del árbitro Flash (reescritor) — baja para máxima fidelidad al prompt.
AUDIT_TEMPERATURE: float = 0.1

# Temperatura del generador principal Gemini Pro — ligeramente más alta para fluidez.
MAIN_TEMPERATURE: float = 0.2


# ============================================================
# ClinicalAuditEngine
# ============================================================

class ClinicalAuditEngine:
    """Motor de auditoría clínica centralizado para IMAGIAX Global.

    Stateless — instanciar una vez como singleton en llm_service.py.

    Implementa el veto basado en evidencia ResNet50 para tórax (AuditMode.VETO)
    y el modo descriptivo para MSK/pelvis sin ResNet (AuditMode.DESCRIPTIVE).
    Para regiones sin base cuantitativa: AuditMode.DISABLED (Fail-Safe).
    """

    def __init__(self) -> None:
        self._flash_model: str = settings.GEMINI_MODEL

    async def validate(
        self,
        *,
        report: str,
        ai_results: dict[str, Any],
        body_part: BodyPart,
        gemini_client: genai.Client,
        proyecciones_txt: str = "estándar",
    ) -> tuple[str, bool]:
        """Valida y corrige el informe generado por Gemini según la evidencia técnica.

        Flujo:
            1. Determina AuditMode para la región (Fail-Safe: DISABLED si sin ResNet).
            2. Si DISABLED → devuelve sin cambios.
            3. Determina si ResNet es normal/genérico para la región.
            4. Si ResNet detectó patología grave → devuelve sin cambios.
            5. Busca vocabulario de alta severidad (con whitelist por línea).
            6. Sin términos problemáticos → devuelve sin cambios.
            7. Con términos:
               - VETO: reescribe eliminando patología, o fuerza template.
               - DESCRIPTIVE: reescribe convirtiendo a lenguaje morfológico.

        Args:
            report: Texto del informe generado por Gemini Pro.
            ai_results: Resultados de la pipeline de IA (hallazgos_estudio, etc.).
            body_part: Región anatómica del estudio.
            gemini_client: Cliente Gemini para reescritura con Flash.
            proyecciones_txt: Proyecciones del estudio (para template de normalidad).

        Returns:
            (informe_final, fue_intervenido)
        """
        audit_mode = _DEFAULT_AUDIT_MODE.get(body_part, AuditMode.DISABLED)

        if audit_mode == AuditMode.DISABLED:
            logger.debug("[AUDIT] DISABLED para %s — pasa sin modificar.", body_part.value)
            return report, False

        resnet_es_normal = _determinar_resnet_es_normal(ai_results, body_part)
        if not resnet_es_normal:
            logger.debug("[AUDIT] ResNet detectó patología grave en %s — inactivo.", body_part.value)
            return report, False

        terminos = _informe_contiene_severidad_alta(report, body_part)
        if not terminos:
            # CFO: escanear igual — hallazgos HLT pueden estar en informe sin disparar árbitro
            scan_cfo(report, body_part)
            return report, False

        # ── CDI: escalamiento a HEDGING_DESCRIPTIVE (v14) ─────────────────
        if audit_mode == AuditMode.DESCRIPTIVE and body_part in CDI_ELIGIBLE_REGIONS:
            cdi = calculate_cdi(terminos)
            if cdi >= CDI_THRESHOLD:
                audit_mode = AuditMode.HEDGING_DESCRIPTIVE
                logger.warning(
                    "[AUDIT] CDI=%.2f >= %.2f → escalando a HEDGING_DESCRIPTIVE en %s.",
                    cdi, CDI_THRESHOLD, body_part.value,
                )

        logger.warning(
            "[AUDIT] Discrepancia — región=%s modo=%s términos=%s. Reescribiendo con Flash.",
            body_part.value, audit_mode.value, terminos,
        )

        if audit_mode == AuditMode.VETO:
            informe_final, fue_intervenido = await self._rewrite_veto(
                report=report,
                terminos_encontrados=terminos,
                body_part=body_part,
                gemini_client=gemini_client,
                proyecciones_txt=proyecciones_txt,
            )
        elif audit_mode == AuditMode.HEDGING_DESCRIPTIVE:
            cdi_value = calculate_cdi(terminos)
            informe_final, fue_intervenido = await self._rewrite_hedging(
                report=report,
                terminos_encontrados=terminos,
                body_part=body_part,
                gemini_client=gemini_client,
                cdi_value=cdi_value,
            )
        else:
            # AuditMode.DESCRIPTIVE
            informe_final, fue_intervenido = await self._rewrite_descriptive(
                report=report,
                terminos_encontrados=terminos,
                body_part=body_part,
                gemini_client=gemini_client,
            )

        # ── CFO: escanear el informe final (post-reescritura) ─────────────
        scan_cfo(informe_final, body_part)

        return informe_final, fue_intervenido

    async def _rewrite_veto(
        self,
        *,
        report: str,
        terminos_encontrados: list[str],
        body_part: BodyPart,
        gemini_client: genai.Client,
        proyecciones_txt: str,
    ) -> tuple[str, bool]:
        """Modo VETO: elimina diagnósticos sin evidencia técnica ResNet."""
        prompt = _build_prompt_arbitro_veto(report, terminos_encontrados)
        try:
            resp = await asyncio.wait_for(
                gemini_client.aio.models.generate_content(
                    model=self._flash_model,
                    contents=[prompt],
                    config=genai.types.GenerateContentConfig(
                        system_instruction=(
                            "Eres un editor de informes radiologicos. Tu unica funcion es "
                            "reescribir informes para eliminar diagnosticos incorrectos detectados "
                            "por discrepancia con el analisis cuantitativo. Sos conciso y preciso."
                        ),
                        temperature=AUDIT_TEMPERATURE,
                        max_output_tokens=2048,
                        thinking_config=genai.types.ThinkingConfig(thinking_budget=0),
                    ),
                ),
                timeout=45.0,
            )
            reescrito = _strip_xml_tags((resp.text or "").strip())
            if not reescrito:
                raise ValueError("Flash devolvió respuesta vacía")

            residuales = _informe_contiene_severidad_alta(reescrito, body_part)
            if residuales:
                logger.error(
                    "[AUDIT] VETO — Flash no resolvió residuales: %s. Forzando template.",
                    residuales,
                )
                return get_template_normalidad(body_part, proyecciones_txt), True

            logger.info("[AUDIT] VETO exitoso para %s.", body_part.value)
            return reescrito, True

        except Exception as exc:
            logger.error("[AUDIT] VETO error en %s: %s. Forzando template.", body_part.value, exc)
            return get_template_normalidad(body_part, proyecciones_txt), True

    async def _rewrite_descriptive(
        self,
        *,
        report: str,
        terminos_encontrados: list[str],
        body_part: BodyPart,
        gemini_client: genai.Client,
    ) -> tuple[str, bool]:
        """Modo DESCRIPTIVE: convierte diagnósticos a lenguaje morfológico."""
        prompt = _build_prompt_arbitro_descriptive(report, terminos_encontrados, body_part)
        try:
            resp = await asyncio.wait_for(
                gemini_client.aio.models.generate_content(
                    model=self._flash_model,
                    contents=[prompt],
                    config=genai.types.GenerateContentConfig(
                        system_instruction=(
                            "Eres un editor de informes radiologicos especializado en "
                            "convertir diagnosticos definitivos a lenguaje descriptivo "
                            "morfologico cuando no hay respaldo cuantitativo. "
                            "Preservas los hallazgos, cambias solo el nivel de certeza diagnostica."
                        ),
                        temperature=AUDIT_TEMPERATURE,
                        max_output_tokens=2048,
                        thinking_config=genai.types.ThinkingConfig(thinking_budget=0),
                    ),
                ),
                timeout=45.0,
            )
            reescrito = _strip_xml_tags((resp.text or "").strip())
            if not reescrito:
                raise ValueError("Flash devolvió respuesta vacía en DESCRIPTIVE")

            logger.info("[AUDIT] DESCRIPTIVE exitoso para %s.", body_part.value)
            return reescrito, True

        except Exception as exc:
            logger.error(
                "[AUDIT] DESCRIPTIVE error en %s: %s. Devolviendo original.",
                body_part.value, exc,
            )
            # En DESCRIPTIVE NO forzamos template: subdiagnóstico de fractura
            # es más peligroso que un informe con diagnóstico probable.
            return report, False

    async def _rewrite_hedging(
        self,
        *,
        report: str,
        terminos_encontrados: list[str],
        body_part: BodyPart,
        gemini_client: genai.Client,
        cdi_value: float,
    ) -> tuple[str, bool]:
        """Modo HEDGING_DESCRIPTIVE (v14): aplica taxonomía de incertidumbre calibrada.

        Activado cuando CDI >= CDI_THRESHOLD. Más restrictivo que DESCRIPTIVE:
        - Sustituciones morfológicas estrictas (taxonomía v14).
        - Cláusula RCP (correlación clínica obligatoria) siempre presente.
        - Lenguaje de hallazgos sugerentes, nunca confirmados.

        Fail-Safe: si Flash falla, devuelve el original (igual que DESCRIPTIVE —
        subdiagnóstico de fractura es más peligroso que sobrediagnóstico probable).
        """
        prompt = _build_prompt_arbitro_hedging(
            report, terminos_encontrados, body_part, cdi_value
        )
        try:
            resp = await asyncio.wait_for(
                gemini_client.aio.models.generate_content(
                    model=self._flash_model,
                    contents=[prompt],
                    config=genai.types.GenerateContentConfig(
                        system_instruction=(
                            "Eres un editor de informes radiologicos especializado en "
                            "aplicar lenguaje de incertidumbre calibrada. Tu funcion es "
                            "reescribir diagnosticos definitivos como hallazgos sugerentes, "
                            "preservando toda la descripcion morfologica objetiva y agregando "
                            "la clausula de correlacion clinica obligatoria."
                        ),
                        temperature=AUDIT_TEMPERATURE,
                        max_output_tokens=2048,
                        thinking_config=genai.types.ThinkingConfig(thinking_budget=0),
                    ),
                ),
                timeout=45.0,
            )
            reescrito = _strip_xml_tags((resp.text or "").strip())
            if not reescrito:
                raise ValueError("Flash devolvió respuesta vacía en HEDGING_DESCRIPTIVE")

            logger.info(
                "[AUDIT] HEDGING_DESCRIPTIVE exitoso para %s (CDI=%.2f).",
                body_part.value, cdi_value,
            )
            return reescrito, True

        except Exception as exc:
            logger.error(
                "[AUDIT] HEDGING_DESCRIPTIVE error en %s (CDI=%.2f): %s. Devolviendo original.",
                body_part.value, cdi_value, exc,
            )
            return report, False


# ============================================================
# normalize_body_part() — resolución multicapa (API pública)
# ============================================================

def normalize_body_part(
    *,
    exam_region: str | None = None,
    template_id: str | None = None,
    custom_system_instruction: str | None = None,
    modalidad: str | None = None,
) -> BodyPart:
    """Resuelve el BodyPart de auditoría desde múltiples fuentes, con fallback robusto.

    Prioridad de resolución (multicapa):
        1. exam_region — ExamRegion.value del prompt_orchestrator (más confiable).
        2. template_id — ID del template YAML activo.
        3. custom_system_instruction — keywords en el system prompt ensamblado.
        4. modalidad DICOM — fallback grueso por tipo de estudio.
        5. UNKNOWN — Fail-Safe: audit deshabilitado.

    Nunca lanza excepción.

    Args:
        exam_region: Valor string de ExamRegion (ej. "chest", "knee", "pelvis").
        template_id: ID del template YAML (ej. "chest_xray", "msk_xray").
        custom_system_instruction: System instruction ensamblada por el orquestador.
        modalidad: Modalidad DICOM raw (DX, CR, CT, MR, etc.).

    Returns:
        BodyPart correspondiente.
    """
    # ── 1. Por ExamRegion ─────────────────────────────────────────────────────
    _REGION_MAP: dict[str, BodyPart] = {
        "chest":          BodyPart.CHEST,
        "knee":           BodyPart.SKELETAL,
        "shoulder":       BodyPart.SKELETAL,
        "wrist":          BodyPart.SKELETAL,
        "hand":           BodyPart.SKELETAL,
        "lumbar_spine":   BodyPart.SKELETAL,
        "cervical_spine": BodyPart.SKELETAL,
        "pelvis":         BodyPart.PELVIS,
        "brain":          BodyPart.BRAIN,
        "abdomen":        BodyPart.ABDOMEN,
        "generic":        BodyPart.UNKNOWN,
    }
    if exam_region:
        bp = _REGION_MAP.get(exam_region.lower())
        if bp is not None:
            return bp

    # ── 2. Por template_id ────────────────────────────────────────────────────
    _TEMPLATE_MAP: dict[str, BodyPart] = {
        "chest_xray":       BodyPart.CHEST,
        "msk_xray":         BodyPart.SKELETAL,
        "lumbar_spine_xray":BodyPart.SKELETAL,
        "pelvis_hip_xray":  BodyPart.PELVIS,
        "bone_age_xray":    BodyPart.SKELETAL,
        "brain_ct":         BodyPart.BRAIN,
        "generic_fallback": BodyPart.UNKNOWN,
    }
    if template_id:
        bp = _TEMPLATE_MAP.get(template_id)
        if bp is not None:
            return bp

    # ── 3. Por custom_system_instruction (keywords) ───────────────────────────
    if custom_system_instruction:
        csi = custom_system_instruction.lower()
        if any(kw in csi for kw in ("torácic", "toracic", "tórax", "torax", "pulmonar")):
            return BodyPart.CHEST
        if any(kw in csi for kw in ("rodilla", "hombro", "columna", "muñeca", "mano", "cadera")):
            return BodyPart.SKELETAL
        if "pelvis" in csi:
            return BodyPart.PELVIS
        if any(kw in csi for kw in ("cerebr", "craneal", "cráneo", "craneo")):
            return BodyPart.BRAIN

    # ── 4. Por modalidad DICOM (fallback grueso) ──────────────────────────────
    # DX/CR sin otro contexto → asumimos tórax (modalidad más común en producción)
    if modalidad in ("DX", "CR") and custom_system_instruction is None:
        return BodyPart.CHEST

    # ── 5. Fail-Safe ──────────────────────────────────────────────────────────
    return BodyPart.UNKNOWN


# Alias de compatibilidad (nombre antiguo usado en llm_service.py antes del rename)
resolve_body_part = normalize_body_part
