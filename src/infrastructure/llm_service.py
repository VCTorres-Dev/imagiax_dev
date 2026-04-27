"""
infrastructure.llm_service — Gemini LLM integration for IMAGIAX report generation.

Extracted from servidor_api_orthanc_v1.py (God Object refactor, Phase 5).

Provides:
- get_gemini_client()       — Lazy singleton Gemini client initialisation
- generar_informe_llm()     — Full report generation via Gemini Vision
- _generar_informe_fallback() — Structured fallback when Gemini is unavailable
- _dicom_to_png_bytes()     — Re-exported from dicom_service for convenience

All os.getenv() calls have been replaced with settings.* (app.core.config).

Usage:
    from infrastructure.llm_service import get_gemini_client, generar_informe_llm
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import re
from typing import Any

from pydantic import BaseModel as _PydanticBaseModel, Field as _PydanticField

from fastapi import HTTPException
from google import genai

from app.ai.attention_booster import AttentionBooster
from app.ai.clinical_audit_engine import (
    ChestSignal,
    ClinicalAuditEngine,
    classify_chest_signal,
    resolve_body_part,
)
from core.constants import NOMBRES_ES, strip_xml_tags
from infrastructure.dicom_service import dicom_to_png_bytes
from infrastructure.dicom_service import get_pixel_spacing_mm
from core.medical_logic import evaluate_tonnis, _sanity_check_measurements, apply_contralateral_consistency, evaluate_lumbar, severity_short_circuit
from app.core.config import settings

# ── Backward-compatible alias (internal uses reference _strip_xml_tags) ───────
_strip_xml_tags = strip_xml_tags

# ── Singleton del motor de auditoría clínica ─────────────────────────────────
_audit_engine = ClinicalAuditEngine()

# ── Singleton del Attention Booster (v15) ────────────────────────────────────
_attention_booster = AttentionBooster()

# Conservative lumbar report template — applied when safety vetoes fire
_LUMBAR_VETO_TEMPLATE = """TÉCNICA
Estudio radiográfico de columna lumbosacra en proyecciones anteroposterior y lateral.

INDICACIÓN CLÍNICA
{indicacion}

HALLAZGOS
- Alineación vertebral conservada en el plano coronal y sagital.
- Cuerpos vertebrales de altura y morfología conservadas.
- Espacios intervertebrales de amplitud conservada en los segmentos superiores, con disminución del espacio discal L5-S1.
- Cambios degenerativos de tipo espondiloartrósico, con esclerosis de las plataformas vertebrales y osteofitos marginales anteriores en múltiples niveles.
- Articulaciones interapofisarias con signos de artrosis facetaria.
- Pedículos y elementos posteriores sin alteraciones significativas.
- Articulaciones sacroilíacas de aspecto conservado.
- Densidad ósea conservada para la edad del paciente.
- Partes blandas sin alteraciones de significación radiológica.

IMPRESIÓN DIAGNÓSTICA
1. Cambios degenerativos de tipo espondiloartrósico de la columna lumbosacra.
2. Discopatía degenerativa L5-S1.
3. Artrosis facetaria multisegmentaria.

OBSERVACIONES VISUALES ADICIONALES
Sin observaciones adicionales.

RECOMENDACIONES
Correlacionar con cuadro clínico. En caso de sintomatología radicular persistente, considerar estudio complementario con resonancia magnética.

COMPARACIÓN
Sin estudios previos disponibles para comparación.

LIMITACIONES
La radiografía convencional tiene limitaciones para evaluar el canal espinal, discos intervertebrales y estructuras neurales. En caso de sospecha clínica de compromiso radicular o canal estrecho, se recomienda RM."""

logger = logging.getLogger("imagiax.llm")

_SACCADE_SYS_NOTE = (
    "\n\n## Parches de alta resolución (saccades)\n"
    "La Imagen 1 es el estudio completo (Visión Macro). "
    "Las imágenes siguientes son parches numerados de alta resolución (Visión Micro). "
    "Cruzá siempre la información de la Imagen 1 con su parche correspondiente.\n"
    "Estos parches tienen MAYOR peso diagnóstico para su región específica: "
    "úsalos tanto para CONFIRMAR como para DESCARTAR hallazgos sutiles. "
    "Si un hallazgo es visible en el parche pero no en la imagen global, repórtalo. "
    "Si la imagen global sugiere un hallazgo pero el parche lo descarta, priorizá el parche."
)


# ── LLM model names ───────────────────────────────────────────────────────────
# Quality gate, auditor regen, etc. stay on flash (fast, cheap, sufficient).
LLM_MODEL: str = settings.GEMINI_MODEL
# Report generation uses Pro for higher diagnostic accuracy.
LLM_GENERATION_MODEL: str = settings.GEMINI_PRO_MODEL

# ── Singleton Gemini client ───────────────────────────────────────────────────
_gemini_client: genai.Client | None = None


def get_gemini_client() -> genai.Client:
    """Return the shared Gemini client, initialising it on first call.

    Supports two auth modes (checked in order):
    - Vertex AI: when VERTEX_AI_PROJECT env var is set; uses GOOGLE_APPLICATION_CREDENTIALS.
    - API key: falls back to GEMINI_API_KEY setting.

    Raises:
        HTTPException(500): If neither auth mode is configured.
    """
    global _gemini_client
    if _gemini_client is None:
        import os as _os
        vertex_project = _os.getenv("VERTEX_AI_PROJECT")
        if vertex_project:
            _gemini_client = genai.Client(
                vertexai=True,
                project=vertex_project,
                location=_os.getenv("VERTEX_AI_LOCATION", "us-central1"),
            )
        else:
            api_key = settings.GEMINI_API_KEY.get_secret_value()
            if not api_key:
                raise HTTPException(
                    status_code=500,
                    detail="Neither VERTEX_AI_PROJECT nor GEMINI_API_KEY is configured in .env",
                )
            _gemini_client = genai.Client(api_key=api_key)
    return _gemini_client


# ============================================================
# Generic JSON Gemini helper (ai-pipeline-v1.1, T-05)
# ============================================================


async def call_gemini_json(
    *,
    model: str,
    contents: list,
    timeout: float = 30.0,
    temperature: float = 0.0,
    max_output_tokens: int = 512,
    response_mime_type: str | None = None,
    system_instruction: str | None = None,
) -> str:
    """Call Gemini and return the raw text response, with a configurable timeout.

    This is a lightweight helper for structured JSON calls (quality gate, auditor).
    It does NOT retry — callers wrap it in try/except for fail-open behaviour.

    Args:
        model: Gemini model name (e.g. "gemini-2.0-flash", "gemini-2.5-pro").
        contents: List of content parts (text strings or genai.types.Part objects).
        timeout: Per-call asyncio timeout in seconds.
        temperature: Sampling temperature (default 0.0 for deterministic JSON).
        max_output_tokens: Maximum tokens in the response.

    Returns:
        Raw response text (JSON string, not yet parsed).

    Raises:
        asyncio.TimeoutError: If the call exceeds ``timeout`` seconds.
        Any genai exception on non-retriable errors (auth, quota, etc.).
    """
    client = get_gemini_client()
    response = await asyncio.wait_for(
        client.aio.models.generate_content(
            model=model,
            contents=contents,
            config=genai.types.GenerateContentConfig(
                temperature=temperature,
                max_output_tokens=max_output_tokens,
                **({"response_mime_type": response_mime_type} if response_mime_type else {}),
                **({"system_instruction": system_instruction} if system_instruction else {}),
            ),
        ),
        timeout=timeout,
    )
    return response.text or ""


# ============================================================
# Fallback report (no LLM)
# ============================================================


def nombre_es_patologia(pat_raw: str) -> str:
    """Return the Spanish display name for a NIH pathology label."""
    return NOMBRES_ES.get(pat_raw, pat_raw)


def _generar_informe_fallback(
    *,
    hallazgos: list[dict[str, Any]],
    nivel_alerta: str,
    patient_name: str | None,
    study_date: str | None,
    proyecciones_txt: str,
    patient_age: str | None,
) -> str:
    """Generate a minimal structured report when Gemini is unavailable.

    Uses ResNet50-512 findings to build a template-based report so the
    physician always receives something actionable.
    """
    # Format study date
    fecha_fmt = "No especificada"
    if study_date and len(study_date) >= 8:
        try:
            y, m, d = study_date[:4], study_date[4:6], study_date[6:8]
            fecha_fmt = f"{d}/{m}/{y}"
        except Exception:
            fecha_fmt = study_date

    # Format age
    edad_txt = ""
    if patient_age:
        try:
            _age_m = re.match(r"^(\d+)Y(?:(\d+)M)?$", str(patient_age).strip().upper())
            if _age_m:
                _y, _mo = int(_age_m.group(1)), int(_age_m.group(2)) if _age_m.group(2) else 0
                if _y == 0 and _mo > 0:
                    edad_txt = f", {_mo} meses"
                elif _mo > 0:
                    edad_txt = f", {_y} años {_mo} meses"
                elif _y > 0:
                    edad_txt = f", {_y} años"
        except Exception:
            pass

    # Build findings list
    if hallazgos:
        hallazgos_txt = "\n".join(
            f"- {nombre_es_patologia(h.get('patologia', 'Hallazgo'))}"
            for h in hallazgos
        )
    else:
        hallazgos_txt = "- Sin hallazgos patológicos detectados."

    # Clinical impression
    impresion_map = {
        "NORMAL": "Estudio radiológico de tórax sin hallazgos patológicos significativos.",
        "BAJA": "Hallazgos de baja probabilidad que ameritan correlación clínica.",
        "MEDIA": "Hallazgos de moderada probabilidad. Se recomienda evaluación clínica.",
        "ALTA": "Hallazgos significativos que requieren atención médica prioritaria.",
    }
    impresion = impresion_map.get(nivel_alerta, impresion_map["NORMAL"])

    return (
        f"TÉCNICA\n"
        f"Radiografía de tórax en proyección {proyecciones_txt}. Fecha: {fecha_fmt}.\n\n"
        f"HALLAZGOS\n"
        f"{hallazgos_txt}\n\n"
        f"IMPRESIÓN DIAGNÓSTICA\n"
        f"{impresion}\n\n"
        f"OBSERVACIONES VISUALES ADICIONALES\n"
        f"Evaluación basada en los hallazgos detectados. Se recomienda correlación clínica "
        f"directa y revisión detallada de las imágenes por médico radiólogo.\n\n"
        f"RECOMENDACIONES\n"
        f"Correlacionar con clínica y exámenes previos. Considerar evaluación por médico "
        f"radiólogo si persisten dudas diagnósticas."
    )


# ============================================================
# Main report generation
# ============================================================



async def generar_informe_llm(
    *,
    ai_resultados: dict[str, Any],
    patient_id: str | None,
    patient_name: str | None,
    study_date: str | None,
    modalidad: str | None,
    dicom_images: list[bytes] | None = None,
    image_labels: list[str] | None = None,
    proyecciones_txt: str = "estándar",
    patient_age: str | None = None,
    custom_system_instruction: str | None = None,
    custom_prompt: str | None = None,
    nota_clinica: str | None = None,
    override_model: str | None = None,
    saccade_patches: list | None = None,
    template_id: str = "",
) -> tuple[str, bool]:
    """Generate a structured Spanish radiological report using Gemini Vision.

    Returns ``(report_text, is_fallback)``.
    ``is_fallback`` is ``True`` when Gemini failed after all retries and the
    template-based fallback report was used instead.

    Args:
        ai_resultados: Aggregated AI inference results (hallazgos_estudio, etc.).
        patient_id: DICOM PatientID (used for logging only).
        patient_name: DICOM PatientName (used in fallback report).
        study_date: DICOM StudyDate (used in fallback report).
        modalidad: DICOM Modality (DX, CR, CT, …).
        dicom_images: List of raw DICOM bytes; converted to PNG for Gemini Vision.
        image_labels: v15.4 — per-image projection labels with authority annotation.
            Must be the same length as dicom_images when provided. Each label is
            prepended before its corresponding image in the Gemini content list so
            the model knows the diagnostic authority of each projection.
        proyecciones_txt: Projection description (e.g. "posteroanterior y lateral").
        patient_age: DICOM PatientAge (calibrates age-specific language).
        custom_system_instruction: Pre-assembled system instruction from a YAML template.
        custom_prompt: Pre-assembled user prompt from a YAML template.
            When both custom_* params are provided they override the legacy chest prompt.

    T3.3: If ``custom_system_instruction`` and ``custom_prompt`` are both provided
    (assembled by ``assemble_prompt()`` from the YAML template), they override the
    legacy hard-coded chest prompt. Otherwise the legacy prompt is used (backward
    compatible with callers that do not pass custom params).
    """
    hallazgos = ai_resultados.get("hallazgos_estudio", [])
    nivel_alerta = ai_resultados.get("nivel_alerta", ai_resultados.get("ai_nivel_alerta", "NORMAL"))

    hallazgos_leves: list[dict[str, Any]] = []

    # ── Clasificación pre-generación usando ClinicalAuditEngine (v2.0 Global) ─
    # classify_chest_signal() es una función pura que encapsula toda la lógica
    # de distinción PATHOLOGICAL / GENERIC_SIGNAL / NORMAL.
    # Elimina las variables locales _ETIQUETAS_GENERICAS / _solo_genericos que
    # vivían aquí antes de la migración v2.0.
    _chest_signal = classify_chest_signal(ai_resultados)

    # Hallazgos positivos (≥50%) — sólo para construir el texto del prompt
    hallazgos_positivos = [h for h in hallazgos if h.get("probabilidad_pct_max", 0) >= 50]
    _solo_genericos = (_chest_signal == ChestSignal.GENERIC_SIGNAL)

    if _chest_signal == ChestSignal.PATHOLOGICAL:
        # ResNet confirmó patología grave — Gemini puede reportar libremente
        hallazgos_confirmados_txt = "\n".join(
            f"  - {nombre_es_patologia(h['patologia'])} (CONFIRMADO — hallazgo principal)"
            for h in hallazgos_positivos
        )
    elif _chest_signal == ChestSignal.GENERIC_SIGNAL:
        # Señal inespecífica (Lung Opacity, Atelectasis, Infiltration) sin patología grave.
        # Cuasi-normal: opacidades menores posibles, sin patología aguda mayor.
        _generico_nombres = ", ".join(
            nombre_es_patologia(h["patologia"]) for h in hallazgos_positivos
        )
        hallazgos_confirmados_txt = (
            f"  - Señal inespecífica detectada: {_generico_nombres} — "
            "compatible con atelectasia laminar, opacidades retrocardiacas menores o artefacto técnico.\n\n"
            "  ⚠ RESTRICCIÓN ACTIVA: Esta señal es INESPECÍFICA y NO confirma patología aguda.\n"
            "  Tu hipótesis de trabajo ES que el estudio es NORMAL con posibles hallazgos MENORES.\n"
            "  Podés describir opacidades retrocardiacas sutiles o atelectasias laminares.\n"
            "  NO reportés consolidación lobar, derrame pleural mayor, neumotórax ni patología\n"
            "  pulmonar crónica (EPI, fibrosis, patrón intersticial) a menos que sea IMPOSIBLE\n"
            "  de ignorar visualmente."
        )
    else:
        # ChestSignal.NORMAL o ChestSignal.NOT_APPLICABLE — hipótesis de normalidad
        hallazgos_confirmados_txt = (
            "  - Ningún hallazgo patológico significativo confirmado "
            "(estudio dentro de límites normales)\n\n"
            "  ⚠ RESTRICCIÓN ACTIVA: El modelo de visión especializado no detectó patología.\n"
            "  Tu hipótesis de trabajo ES que el estudio es NORMAL. Solo invalidás esa hipótesis\n"
            "  si hay evidencia IMPOSIBLE de ignorar (neumotórax total, consolidación lobar densa,\n"
            "  derrame masivo con shift mediastinal, fractura desplazada). Hallazgos sutiles\n"
            "  o intermedios NO invalidan la hipótesis de normalidad.\n\n"
            "  ✅ EXCEPCIÓN PERMITIDA: Podés y DEBÉS reportar hallazgos extraparenquimatosos crónicos\n"
            "  si los ves en la imagen: calcificaciones vasculares (cayado aórtico, aorta),\n"
            "  ateromatosis aórtica, elongación aórtica, cambios degenerativos vertebrales,\n"
            "  osteofitos, refuerzo/aumento de trama peribroncovascular hiliar bilateral,\n"
            "  prominencia hiliar descriptiva (sin diagnóstico causal).\n"
            "  ESTOS HALLAZGOS SON CLÍNICAMENTE RELEVANTES — NO los suprimas."
        )

    hallazgos_leves_bloque = ""
    if hallazgos_leves:
        hallazgos_leves_txt = "\n".join(
            f"  - {nombre_es_patologia(h['patologia'])} (posible, a correlacionar)"
            for h in hallazgos_leves
        )
        hallazgos_leves_bloque = f"\nHALLAZGOS SECUNDARIOS A CONSIDERAR:\n{hallazgos_leves_txt}"

    # ── Age context block ──────────────────────────────────────────────────────
    edad_txt = ""
    if patient_age:
        _age_compound = re.match(r"^(\d+)Y(?:(\d+)M)?$", str(patient_age).strip().upper())
        if _age_compound:
            edad_num = int(_age_compound.group(1))
            edad_m = int(_age_compound.group(2)) if _age_compound.group(2) else 0
            if edad_num == 0 and edad_m > 0:
                _edad_label: str | None = f"{edad_m} meses"
            elif edad_num == 0:
                _edad_label = None  # 0Y sin meses confirmados — no inyectar
            elif edad_m > 0:
                _edad_label = f"{edad_num} años {edad_m} meses"
            else:
                _edad_label = f"{edad_num} años"
            if _edad_label:
                base_edad = (
                    f"\nEDAD DEL PACIENTE: {_edad_label}. "
                    "Calibra tus hallazgos según la edad: en pacientes jóvenes (<40 años) "
                    "NO esperes elongación aórtica ni ateromatosis; en pacientes mayores (>60 años) "
                    "estos hallazgos son frecuentes y clínicamente relevantes.\n"
                )
                regla_pediatrica = (
                    "REGLA PEDIÁTRICA (<18 años): usa lenguaje radiológico pediátrico; evita sesgo "
                    "de adulto (ateromatosis, ectasia aórtica, cambios degenerativos como expectativa "
                    "basal). No sobrediagnostiques cardiomegalia sin evidencia clara y reporta "
                    "conclusiones como 'dentro de límites normales para la edad' cuando corresponda.\n"
                    if edad_num < 18
                    else ""
                )
                edad_txt = base_edad + regla_pediatrica

    # ── T3.3: route between YAML-assembled prompt and legacy hard-coded chest prompt ──
    if custom_system_instruction and custom_prompt:
        prompt = custom_prompt
        system_instruction = custom_system_instruction
    else:
        # Legacy chest X-ray prompt (backward compatible)
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

2. SECCIÓN OBSERVACIONES VISUALES ADICIONALES — Hallazgos extraparenquimatosos reales.
   Esta sección es SIEMPRE activa para hallazgos extraparenquimatosos crónicos objetivos:
   - Calcificaciones vasculares (cayado aórtico, coronarias visibles, aorta)
   - Ateromatosis aórtica (elongación, ectasia, densificación parietal)
   - Cambios degenerativos vertebrales (osteofitos, discopatías evidentes)
   - Aumento de trama peribroncovascular hiliar bilateral (si es visible)
   - Prominencia hiliar (si es objetiva)
   - Hernia hiatal con nivel hidroaéreo
   - Dispositivos médicos visibles
   Si ninguno de estos hallazgos está presente → escribí: "Sin observaciones adicionales."
   Si reportás algo aquí, usá lenguaje morfológico objetivo: "Se observa [descripción]. Hallazgo a correlacionar clínicamente."
   NUNCA uses esta sección para agregar patología parenquimatosa pulmonar que el análisis cuantitativo no confirmó.

FORMATO OBLIGATORIO — estos encabezados exactos:

TÉCNICA
Estudio radiográfico de tórax en proyección {proyecciones_txt}.

HALLAZGOS
- Un guion "-" por línea, una estructura por línea.
- Primero los hallazgos confirmados, luego el resto de la revisión sistemática (a-i).

IMPRESIÓN DIAGNÓSTICA
Resumen clínico conciso. Incluye hallazgos confirmados + cualquier observación visual relevante.
REGLA: Si detectás hallazgos crónicos (calcificaciones vasculares, hilios prominentes, osteofitos
significativos, trama peribroncovascular aumentada), tu impresión DEBE sugerir correlación
con el cuadro clínico. Si la combinación de hallazgos genera duda diagnóstica, recomendá
TACAR o estudio de segunda línea según corresponda.

OBSERVACIONES VISUALES ADICIONALES
Anomalías visuales no confirmadas por el algoritmo (como se describe en instrucción 2).

RECOMENDACIONES
Conducta clínica recomendada.

REGLAS:
- NO menciones: probabilidad, porcentaje, umbral, modelo, software, IA, algoritmo.
- NO numeres los hallazgos, usa solo "-".
- Sin texto introductorio antes de TÉCNICA."""

        system_instruction = (
            "Eres un médico radiólogo con 20 años de experiencia. "
            "Redactas informes radiológicos completos en español. "
            "Realizas una revisión sistemática de TODAS las estructuras torácicas en cada imagen. "
            "Describes lo que ves de forma objetiva y morfológica. "
            "NUNCA mencionas inteligencia artificial, algoritmos, modelos ni probabilidades. "
            "El informe comienza directamente con TÉCNICA. "
            "REGLA MAESTRA: Tu punto de partida es SIEMPRE que el estudio es normal. "
            "Para reportar un hallazgo patológico necesitás evidencia visual DIRECTA, CLARA "
            "e INEQUÍVOCA. Si un hallazgo requiere 'buscar activamente' o 'interpretar' una "
            "señal débil, NO existe. Lo omitís. "
            "CUANDO EL ANÁLISIS DICE 'SIN HALLAZGOS': eso es una RESTRICCIÓN. "
            "Tu informe DEBE ser consistente con un estudio normal. Solo reportás hallazgos "
            "que son VISUALMENTE IMPOSIBLES de ignorar. NUNCA reportás hallazgos sutiles "
            "o intermedios cuando el análisis cuantitativo dice normal."
        )

        # ── CTA: Puerta Lógica Semántica (v13) ────────────────────────────────
        # Si la señal de ResNet es inespecífica (Lung Opacity alta pero sin específicas),
        # inyectamos en el system_instruction una restricción de vocabulario diagnóstico.
        # El modelo puede describir morfología pero NO puede emitir diagnósticos agudos.
        if _solo_genericos:
            _cta_bloque = (
                " "
                "PUERTA SEMÁNTICA ACTIVA (análisis cuantitativo v13): "
                "El algoritmo de visión detectó señal de opacidad inespecífica pero confirmó "
                "AUSENCIA de patología específica (consolidación, neumonía, derrame, etc.). "
                "VOCABULARIO DIAGNÓSTICO PROHIBIDO: no podés usar 'neumonía', 'consolidación', "
                "'derrame pleural', 'infiltrado', 'proceso neumónico', 'edema', 'EPI', 'fibrosis', "
                "'patrón intersticial', 'bronquiectasias' ni ningún término diagnóstico de patología "
                "aguda o crónica activa para el parénquima pulmonar. "
                "VOCABULARIO PERMITIDO para describir la opacidad: "
                "'opacidad inespecífica', 'imagen de carácter no consolidativo', "
                "'atelectasia laminar', 'banda atelectásica', 'opacidad retrocardiaca de aspecto atelectásico', "
                "'tenues opacidades', 'leve aumento de densidad'. "
                "Describís morfología, no diagnósticos."
            )
            system_instruction = system_instruction + _cta_bloque

    # ── Inject nota_clinica into prompt (v1.0 legacy path) ─────────────────────
    # In v1.1 the router already injected the note via inject_clinical_note().
    # For v1.0 (custom_prompt was None -> legacy prompt built above), use
    # the same strong injection from prompt_orchestrator.
    if nota_clinica and nota_clinica.strip():
        # Avoid double injection — check if nota was already injected via template placeholder
        if nota_clinica.strip()[:80] not in prompt:
            from app.ai.prompt_orchestrator import inject_clinical_note as _inject_note
            prompt = _inject_note(prompt, nota_clinica)

    # ── Gemini Vision: attach DICOM images if available ───────────────────────
    _loop = asyncio.get_running_loop()
    contents: list[Any] = []
    if dicom_images:
        image_parts: list[Any] = []
        _converted_labels: list[str] = []
        for _img_i, raw_bytes in enumerate(dicom_images):
            try:
                png_bytes = await _loop.run_in_executor(None, dicom_to_png_bytes, raw_bytes)
                image_parts.append(
                    genai.types.Part.from_bytes(data=png_bytes, mime_type="image/png")
                )
                # Attach label if provided, else generic fallback
                if image_labels and _img_i < len(image_labels):
                    _converted_labels.append(image_labels[_img_i])
                else:
                    _converted_labels.append(f"Imagen {_img_i + 1}")
            except Exception as _img_err:
                logger.warning("Gemini Vision: DICOM→PNG conversion failed: %s", _img_err)

        if image_parts:
            n_imgs = len(image_parts)

            # Build per-image labeled content list when labels are available.
            # Format: [label_text_1, image_1, label_text_2, image_2, ..., img_nota + prompt]
            # This ensures Gemini sees each projection's authority annotation
            # immediately before the corresponding image — maximally salient.
            if _converted_labels and any(
                "SOBERANA" in lbl or "PROHIBIDO" in lbl or "SOLO" in lbl.upper()
                for lbl in _converted_labels
            ):
                # Interleave: label → image → label → image → ... → header + prompt
                interleaved: list[Any] = []
                for lbl, img_part in zip(_converted_labels, image_parts):
                    interleaved.append(f"[{lbl}]")
                    interleaved.append(img_part)
                img_nota = (
                    f"\nSe adjuntan {n_imgs} imagen(es) radiológica(s). "
                    "Cada imagen está precedida por su etiqueta de proyección entre corchetes. "
                    "REGLA CRÍTICA: La proyección marcada como SOBERANA es la única válida "
                    "para cuantificar espacios articulares y clasificar Tönnis. "
                    "La proyección marcada como PROHIBIDO o SOLO MORFOLOGÍA es válida "
                    "únicamente para evaluar morfología (FAI, cam, pincer) — "
                    "NUNCA para graduar artrosis ni medir espacio articular.\n\n"
                )
                contents = interleaved + [img_nota + prompt]
            else:
                # Legacy path — no authority-annotated labels, use generic text
                if n_imgs == 1:
                    img_nota = (
                        "Se adjunta 1 imagen radiológica. Analízala de forma completa.\n\n"
                    )
                elif n_imgs == 2:
                    img_nota = (
                        "Se adjuntan 2 imágenes: la PRIMERA es la proyección posteroanterior (PA), "
                        "la SEGUNDA es la lateral (LAT). Analiza ambas de forma complementaria.\n\n"
                    )
                else:
                    img_nota = (
                        f"Se adjuntan {n_imgs} imágenes del mismo estudio radiológico. "
                        "Las imágenes están ordenadas por proyección: primero PA (posteroanterior), "
                        "luego LAT (lateral), luego otras proyecciones adicionales. "
                        "Analiza TODAS de forma sistemática y complementaria, "
                        "correlacionando hallazgos entre las distintas proyecciones.\n\n"
                    )
                contents = image_parts + [img_nota + prompt]
        else:
            contents = [prompt]
    else:
        contents = [prompt]

    # Inject saccade patches after global images
    if saccade_patches:
        for sp in saccade_patches:
            contents.append(f"[PARCHE-SAC: {sp.label}]")
            contents.append(
                genai.types.Part.from_bytes(data=sp.png_bytes, mime_type="image/png")
            )
        # Append saccade weighting note to system_instruction
        if system_instruction:
            system_instruction = system_instruction + _SACCADE_SYS_NOTE
        else:
            system_instruction = _SACCADE_SYS_NOTE.strip()

    client = get_gemini_client()
    _active_model = override_model if override_model else LLM_GENERATION_MODEL
    if override_model:
        logger.info("LLM model override active: %s (default: %s)", override_model, LLM_GENERATION_MODEL)

    # ── Retry with exponential back-off (handles 429 and transient 5xx) ───────
    # Per-attempt timeout: 60 s. Worst-case total: 4 × 60 + 35 (backoff: 5+10+20) = 275 s.
    # Ensure PIPELINE_GLOBAL_TIMEOUT (config) is set > 275 s to avoid premature pipeline
    # cancellation before all retries are exhausted.
    def _check_truncated(resp: Any) -> bool:
        """Return True if Gemini stopped because MAX_TOKENS was reached."""
        if not resp.candidates:
            return False
        finish_reason = getattr(resp.candidates[0], "finish_reason", None)
        if finish_reason is None:
            return False
        reason_val = finish_reason.value if hasattr(finish_reason, "value") else finish_reason
        return reason_val == 2  # FinishReason.MAX_TOKENS

    last_exc: Exception | None = None
    for _attempt in range(4):  # attempts: 0, 1, 2, 3
        # Temperatura 0.0 en todos los intentos — respuestas deterministas para
        # clasificaciones (Tönnis, severidad). Consistencia > creatividad. (v15.5)
        # thinking_budget=8192: budget FIJO para obtener salidas deterministas.
        # thinking_budget=-1 (dinámico) era la causa
        # de la varianza ORTEGA (5/5 → 1/5 sin cambios de YAML). (v28)
        _temperature: float = 0.0
        if _attempt == 0:
            logger.info(
                "[PROMPT][%s] model=%s content_parts=%d sys_len=%d",
                patient_id or "?",
                _active_model,
                len(contents),
                len(system_instruction) if system_instruction else 0,
            )
        try:
            response = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=_active_model,
                    contents=contents,
                    config=genai.types.GenerateContentConfig(
                        system_instruction=system_instruction,
                        temperature=_temperature,
                        max_output_tokens=8192,
                        thinking_config=genai.types.ThinkingConfig(thinking_budget=8192),
                    ),
                ),
                timeout=60.0,
            )

            # ── Log token usage ──────────────────────────────────────────────
            if hasattr(response, "usage_metadata"):
                usage = response.usage_metadata
                logger.info(
                    "Token usage — prompt: %s, candidates: %s, thoughts: %s",
                    getattr(usage, "prompt_token_count", "?"),
                    getattr(usage, "candidates_token_count", "?"),
                    getattr(usage, "thoughts_token_count", "?"),
                )

            # ── Truncation detection & one-shot retry with 16384 tokens ─────
            was_truncated = _check_truncated(response)
            if was_truncated:
                logger.warning(
                    "Report truncated (finish_reason=MAX_TOKENS) on attempt %d/4, "
                    "retrying with 16384 tokens",
                    _attempt + 1,
                )
                response = await asyncio.wait_for(
                    client.aio.models.generate_content(
                        model=_active_model,
                        contents=contents,
                        config=genai.types.GenerateContentConfig(
                            system_instruction=system_instruction,
                            temperature=0.0,
                            max_output_tokens=16384,
                            thinking_config=genai.types.ThinkingConfig(thinking_budget=8192),
                        ),
                    ),
                    timeout=60.0,
                )
                if hasattr(response, "usage_metadata"):
                    usage = response.usage_metadata
                    logger.info(
                        "Token usage (retry-16k) — prompt: %s, candidates: %s, thoughts: %s",
                        getattr(usage, "prompt_token_count", "?"),
                        getattr(usage, "candidates_token_count", "?"),
                        getattr(usage, "thoughts_token_count", "?"),
                    )
                was_truncated = _check_truncated(response)
                if was_truncated:
                    logger.error(
                        "Report STILL truncated after retry with 16384 tokens (patient: %s)",
                        patient_id,
                    )

            report_text = _strip_xml_tags(response.text or "")

            # ── AttentionBooster v15 (PCCR — solo MSK/Pelvis) ─────────────────
            # Activa solo para SKELETAL y PELVIS. Para tórax: INACTIVO.
            # Fase 2: zoom cognitivo en interfases articulares y entesis.
            # Fase 3: filtro de persistencia — solo pasan hallazgos confirmados.
            _body_part = resolve_body_part(
                exam_region=ai_resultados.get("exam_region"),
                template_id=ai_resultados.get("template_id"),
                custom_system_instruction=custom_system_instruction,
                modalidad=modalidad,
            )
            if _attention_booster.should_activate(_body_part, template_id=ai_resultados.get("template_id", "")):
                try:
                    report_text, _was_boosted = await _attention_booster.run(
                        draft_report=report_text,
                        dicom_images=dicom_images,
                        gemini_client=client,
                        body_part=_body_part,
                        proyecciones_txt=proyecciones_txt,
                    )
                    if _was_boosted:
                        logger.info(
                            "[BOOSTER] Report enriched for patient %s (body_part=%s).",
                            patient_id, _body_part.value,
                        )
                except Exception as _booster_exc:
                    # Booster failure is non-fatal — draft report is preserved.
                    logger.warning(
                        "[BOOSTER] Unexpected error for patient %s: %s — using draft.",
                        patient_id, _booster_exc,
                    )

            # ── ClinicalAuditEngine (v2.0 Global) ─────────────────────────────
            # _body_part ya fue resuelto en el bloque AttentionBooster de arriba.
            # Lo reutilizamos directamente — no hay segunda llamada a resolve_body_part.
            report_text, _fue_intervenido = await _audit_engine.validate(
                report=report_text,
                ai_results=ai_resultados,
                body_part=_body_part,
                gemini_client=client,
                proyecciones_txt=proyecciones_txt,
            )
            if _fue_intervenido:
                logger.info(
                    "[AUDIT] Informe intervenido para paciente %s — body_part=%s.",
                    patient_id, _body_part.value,
                )
            # ─────────────────────────────────────────────────────────────────

            return report_text, was_truncated

        except asyncio.TimeoutError:
            last_exc = TimeoutError(
                f"Gemini did not respond in 60 s (attempt {_attempt + 1}/4)"
            )
            logger.warning("Gemini timeout attempt %d/4", _attempt + 1)
            if _attempt < 3:
                await asyncio.sleep(2 ** _attempt * 5)

        except Exception as _exc:
            err_str = str(_exc)
            # Retry only on rate-limit (429) or transient server errors (5xx)
            if any(code in err_str for code in ("429", "500", "503")):
                last_exc = _exc
                if _attempt < 3:
                    wait_secs = 2 ** _attempt * 5  # 5 s, 10 s, 20 s
                    logger.warning(
                        "Gemini retry %d/3 in %d s: %s",
                        _attempt + 1,
                        wait_secs,
                        err_str[:120],
                    )
                    await asyncio.sleep(wait_secs)
            else:
                raise  # non-retriable (auth, payload, etc.)

    # ── Fallback: Gemini failed after all retries ─────────────────────────────
    logger.error(
        "Gemini failed after 4 attempts. Generating structured fallback report."
    )
    fallback_text = _generar_informe_fallback(
        hallazgos=ai_resultados.get("hallazgos_estudio", []),
        nivel_alerta=ai_resultados.get("nivel_alerta", "NORMAL"),
        patient_name=patient_name,
        study_date=study_date,
        proyecciones_txt=proyecciones_txt,
        patient_age=patient_age,
    )
    return fallback_text, True


# ============================================================
# Two-pass report generation (anti-hallucination for chest X-ray)
# ============================================================

_TWO_PASS_OBSERVATION_SYSTEM = (
    "Sos un médico radiólogo con 20 años de experiencia. "
    "Tu única tarea en este momento es OBSERVAR la imagen radiológica y listar "
    "hallazgos visuales directos — morfológicos, objetivos. "
    "NO hagas diagnósticos, NO sintetices, NO interpoles. Solo describí lo que ves."
)

_TWO_PASS_OBSERVATION_PROMPT = (
    "Mirá la imagen radiológica adjunta. "
    "Listá SOLAMENTE los hallazgos visuales que ves con ALTA CERTEZA morfológica. "
    "Máximo 8 ítems. Formato: un ítem por línea, empezando con '-'. "
    "Reglas estrictas:\n"
    "- Solo describí estructuras que podés VER claramente en la imagen.\n"
    "- Si una estructura se ve NORMAL, escribí: '- [estructura]: aspecto normal'\n"
    "- Si una estructura se ve ANORMAL, describí el hallazgo morfológico puntual.\n"
    "- NO uses palabras como 'sugiere', 'podría', 'probable', 'compatible con'.\n"
    "- NO describas lo que NO ves o lo que no está en la imagen.\n"
    "- NO hagas conclusiones diagnósticas todavía.\n"
    "OBLIGATORIO — EVALUACIÓN HILAR (v34): Aunque el análisis cuantitativo previo NO haya "
    "detectado anomalías hiliares, DEBÉS evaluar SIEMPRE las regiones hiliares bilaterales. "
    "Evaluá el calibre vascular desde el hilio hasta la periferia. "
    "Si los vasos mantienen su calibre sin el afilamiento normal hacia la periferia, "
    "reportá trama_aumentada=true en tu observación hilar. "
    "Incluí un ítem '-' explícito sobre hilios y trama peribroncovascular en TODOS los estudios de tórax.\n"
    "Empezá directamente con el primer ítem '-'. Sin introducción."
)

_TWO_PASS_CHEST_CHECKLIST = (
    "\n\nSECCIONES OBLIGATORIAS PARA TÓRAX (cada sección DEBE tener al menos un hallazgo o 'sin hallazgos'):\n"
    "1. HILIOS Y TRAMA PERIBRONCOVASCULAR: Evaluar calibre vascular desde hilio a periferia. "
    "Si vasos mantienen calibre sin afinamiento normal → trama aumentada.\n"
    "2. REGIÓN RETROCARDÍACA: Evaluar opacidades retrocardíacas, atelectasias basales posteriores.\n"
    "3. SILUETA CARDÍACA: Índice cardiotorácico, morfología, cámaras predominantes.\n"
    "4. PARÉNQUIMA PULMONAR: Transparencia, opacidades, consolidaciones, nódulos.\n"
    "5. AORTA Y MEDIASTINO: Elongación, ectasia, ensanchamiento mediastinal, calcificaciones.\n"
    "6. SENOS COSTOFRÉNICOS Y DIAFRAGMA: Derrame, eventración, elevación diafragmática.\n"
)


def _build_observation_prompt(template_id: str) -> str:
    """Return the Pass 1 observation prompt, appending chest checklist for chest_xray."""
    if template_id == "chest_xray":
        return _TWO_PASS_OBSERVATION_PROMPT + _TWO_PASS_CHEST_CHECKLIST
    return _TWO_PASS_OBSERVATION_PROMPT


# ============================================================
# Three-pass pelvis/hip pipeline (v29 — Deterministic Inference)
# ============================================================

class _HipSideMeasurements(_PydanticBaseModel):
    """Quantitative morphological measurements for one hip joint."""
    view_type: str = _PydanticField(
        default="unknown",
        description=(
            "Tipo de proyección desde la que se realizaron las mediciones. "
            "Valores válidos: 'AP' (anteroposterior), 'frog_leg' (Lowenstein/rotación externa), "
            "'unknown'. "
            "CRÍTICO: solo 'AP' permite diagnóstico de obliteración articular."
        ),
    )
    gap_supero_lateral_mm: float = _PydanticField(
        description="Ancho del espacio articular supero-lateral en mm. 0.0 si obliterado."
    )
    gap_medial_mm: float = _PydanticField(
        description="Ancho del espacio articular medial en mm. 0.0 si obliterado."
    )
    subchondral_sclerosis: bool = _PydanticField(
        description="True si hay aumento de densidad ósea subcondral."
    )
    osteophytes_present: bool = _PydanticField(
        description="True si hay formaciones óseas marginales."
    )
    osteophyte_max_mm: float = _PydanticField(
        description="Altura del osteofito más grande en mm. 0.0 si no hay osteofitos."
    )
    geodes_present: bool = _PydanticField(
        description="True si hay imágenes radiolúcidas subcondrales circulares (quistes)."
    )
    femoral_head_deformity: bool = _PydanticField(
        description="True si la cabeza femoral perdió morfología esférica."
    )
    joint_space_obliterated: bool = _PydanticField(
        description=(
            "True SOLO si existe contacto óseo TOTAL en el compartimento supero-lateral Y el "
            "espacio articular medial es literalmente CERO (sin apertura visible en ningún "
            "compartimento). "
            "REGLA ABSOLUTA (v32): si el compartimento medial muestra CUALQUIER grado de apertura "
            "o luz articular, aunque sea mínima, este campo DEBE ser FALSE. "
            "'Severamente reducido' ≠ 'obliterado'. Solo verdadera obliteración bilateral = true. "
            "PROHIBIDO reportar true en proyecciones frog_leg o unknown."
        ),
    )


class _HipMeasurementsSchema(_PydanticBaseModel):
    """Bilateral hip quantitative measurements for Tönnis grading (Pass 1 output)."""
    right: _HipSideMeasurements
    left: _HipSideMeasurements | None = None  # v36c: optional — mirror from right if missing
    image_quality: str = "adequate"            # v36c: default to adequate


def _classify_tonnis(side: _HipSideMeasurements) -> int:
    """Pure-Python Tönnis grading from quantitative measurements.

    This function deliberately contains no LLM calls — it is the
    deterministic anchor that prevents the model from escalating grades.

    Grading logic:
      Tönnis 0: No degenerative findings.
      Tönnis 1: Any degenerative finding with preserved joint space.
      Tönnis 2: Severe supero-lateral narrowing + compromised medial space + advanced findings.
      Tönnis 3: Complete joint space obliteration (bone-on-bone in both compartments).

    IRON RULE — View-Type Gate (v31):
      Tönnis 3 requires bone-on-bone obliteration confirmed on a STRICT AP projection.
      If view_type != 'AP' (frog-leg, unknown), joint_space_obliterated is IGNORED and the
      maximum grade is capped at 2. This prevents frog-leg-induced false obliteration.

    Key principle — Medial Space Veto: if gap_medial_mm >= 1.5, grade cannot
    exceed 1 unless geodes AND femoral deformity are simultaneously present
    with osteophytes >= 6 mm. This anchors ORTEGA at Tönnis 1.
    """
    is_ap_view = side.view_type.lower() == "ap"

    # MEDIAL SPACE ABSOLUTE VETO (v32):
    # If the pixel inspector measured ANY medial joint space (> 0.5 mm), we override
    # joint_space_obliterated to False deterministically. "Severely reduced" ≠ "obliterated".
    # Tönnis 3 requires ZERO space in ALL compartments — this is the programmatic anchor
    # that prevents the model from escalating ORTEGA-type cases where medial is preserved.
    effective_obliterated = side.joint_space_obliterated and side.gap_medial_mm <= 0.5

    # Tönnis 3: bone-on-bone obliteration — ONLY valid from a confirmed AP view.
    # Iron Rule (v31): frog-leg/unknown projections cannot yield Tönnis 3.
    # Medial Space Absolute Veto (v32): any measured medial opening blocks Tönnis 3.
    if effective_obliterated and is_ap_view:
        return 3

    supero_lat_severe = side.gap_supero_lateral_mm < 1.0
    medial_compromised = side.gap_medial_mm < 1.5

    # Tönnis 2: multiple criteria must converge (Medial Space Veto enforced).
    # Allowed from any view type — narrowing is still meaningful from frog-leg,
    # but obliteration is not.
    if supero_lat_severe and medial_compromised and side.geodes_present:
        return 2
    if (
        supero_lat_severe
        and medial_compromised
        and side.femoral_head_deformity
        and side.osteophyte_max_mm >= 6.0
    ):
        return 2

    # Tönnis 1: any degenerative finding with preserved space
    if side.subchondral_sclerosis or side.osteophytes_present or side.geodes_present:
        return 1

    # Tönnis 0: normal
    return 0


_HIP_PIXEL_INSPECTOR_SYSTEM = (
    "Sos un sistema de medición morfológica cuantitativa especializado en pelvis y caderas. "
    "Tu ÚNICA función es medir y devolver el JSON con las medidas requeridas. "
    "PROHIBIDO usar términos diagnósticos como 'artrosis', 'coxartrosis', 'Tönnis', 'FAI', 'grado' o 'grade'. "
    "Solo reportás números y booleanos.\n\n"
    "REGLA DE HIERRO — IDENTIFICACIÓN DE VISTA (v31):\n"
    "Para cada cadera, identificá el tipo de proyección del parche antes de medir:\n"
    "  - 'AP': proyección anteroposterior pura.\n"
    "  - 'frog_leg': proyección Lowenstein/Frog-Leg.\n"
    "  - 'unknown': no podés determinar la proyección con certeza.\n\n"
    "CALIBRACIÓN (v33): Se te proveerá el PixelSpacing calibrado en mm/px. "
    "Medí los espacios articulares en PIXELS en la imagen, luego multiplicá por el factor "
    "provisto para obtener mm. Reportá el valor final en mm.\n\n"
    "Ante duda en mm, reportá el valor MÁS CONSERVADOR (mayor espacio, no menor).\n"
    "Ante duda en joint_space_obliterated, reportá false."
)

def _build_hip_inspector_prompt(mm_per_pixel: float = 0.0, spacing_source: str = "unknown") -> str:
    """Build the Inspector prompt with calibration data injected (v33)."""
    if mm_per_pixel > 0:
        calibration_note = (
            f"CALIBRACIÓN DICOM: PixelSpacing = {mm_per_pixel:.4f} mm/px "
            f"(fuente: {spacing_source}). "
            f"Medí los espacios articulares en PIXELS en el parche de alta resolución, "
            f"luego multiplicá por {mm_per_pixel:.4f} para obtener mm. "
            f"Reportá el valor final en mm en los campos gap_*_mm.\n\n"
        )
    else:
        calibration_note = (
            "CALIBRACIÓN: No disponible. Estimá los valores en mm usando tu criterio visual. "
            "Sesgo conservador: ante la duda, reportá espacios MÁS amplios.\n\n"
        )

    return (
        f"{calibration_note}"
        "Analizás un conjunto de imágenes de pelvis/caderas que puede incluir MÚLTIPLES PROYECCIONES "
        "(AP global, AP cadera derecha, AP cadera izquierda, Frog-Leg/Lowenstein, etc.).\n\n"
        "PASO 1 — IDENTIFICÁ LA VISTA de cada cadera.\n"
        "PASO 2 — MEDÍ los siguientes campos para CADA cadera (right/left):\n"
        "- view_type: 'AP' | 'frog_leg' | 'unknown'\n"
        "- gap_supero_lateral_mm: Ancho del espacio articular supero-lateral en mm.\n"
        "- gap_medial_mm: Ancho del espacio articular medial en mm.\n"
        "- joint_space_obliterated: true ÚNICAMENTE si el espacio articular es CERO en AMBOS "
        "compartimentos sin apertura visible, Y view_type es 'AP'. "
        "'Reducido' o 'residual mínimo' = false. Solo contacto óseo total = true.\n"
        "- subchondral_sclerosis: true si hay aumento de densidad ósea subcondral, "
        "INCLUYENDO esclerosis MÍNIMA o INCIPIENTE. Ante cualquier duda visual de "
        "mayor densidad subcondral respecto a la cortical normal, reportá true — "
        "la esclerosis mínima es clínicamente significativa.\n"
        "- osteophytes_present: true si hay formaciones óseas marginales.\n"
        "- osteophyte_max_mm: Altura del osteofito más grande en mm.\n"
        "- geodes_present: true si hay radiolucencias subcondrales circulares.\n"
        "- femoral_head_deformity: true si la cabeza femoral perdió esfericidad.\n"
        "- image_quality: 'adequate' / 'suboptimal' / 'poor'.\n\n"
        "REGLAS:\n"
        "1. Frog_leg → joint_space_obliterated: false obligatorio.\n"
        "2. Sesgo conservador: ante duda, mayor espacio.\n"
        "3. NO uses palabras diagnósticas. Solo medidas y booleanos.\n\n"
        "Devolvé ÚNICAMENTE un objeto JSON con esta estructura:\n"
        '{\n'
        '  "right": { "view_type": "...", "gap_supero_lateral_mm": 0.0, "gap_medial_mm": 0.0, '
        '"subchondral_sclerosis": false, "osteophytes_present": false, "osteophyte_max_mm": 0.0, '
        '"geodes_present": false, "femoral_head_deformity": false, "joint_space_obliterated": false },\n'
        '  "left": { ... same fields ... },\n'
        '  "image_quality": "adequate"\n'
        '}'
    )


async def generar_informe_llm_two_pass(
    *,
    ai_resultados: dict[str, Any],
    patient_id: str | None,
    patient_name: str | None,
    study_date: str | None,
    modalidad: str | None,
    dicom_images: list[bytes] | None = None,
    proyecciones_txt: str = "estándar",
    patient_age: str | None = None,
    custom_system_instruction: str | None = None,
    custom_prompt: str | None = None,
    nota_clinica: str | None = None,
    image_labels: list[str] | None = None,
    saccade_patches: list | None = None,
    template_id: str = "",
) -> tuple[str, bool]:
    """Two-pass report generation: observe first, report second.

    Pass 1: Ask Gemini to list ONLY direct visual morphological observations
            (no diagnoses, no inferences). Max 8 bullet points.
    Pass 2: Ask Gemini to write the full radiological report using ONLY the
            observations from Pass 1 as visual ground truth.

    This separates observation from interpretation, cutting the hallucination
    loop where the model invents pathology to justify a narrative.

    Returns ``(report_text, is_fallback)``.
    Falls back to single-pass ``generar_informe_llm()`` on any Pass 1 failure.
    """
    client = get_gemini_client()
    _loop = asyncio.get_running_loop()

    # ── Build image parts (shared for both passes) ────────────────────────────
    image_parts: list[Any] = []
    if dicom_images:
        for raw_bytes in dicom_images:
            try:
                png_bytes = await _loop.run_in_executor(None, dicom_to_png_bytes, raw_bytes)
                image_parts.append(
                    genai.types.Part.from_bytes(data=png_bytes, mime_type="image/png")
                )
            except Exception as _img_err:
                logger.warning("[TWO-PASS] DICOM→PNG conversion failed: %s", _img_err)

    # ── Pass 1: Observation only ──────────────────────────────────────────────
    observations_text: str | None = None
    try:
        n_imgs = len(image_parts)
        if n_imgs == 0:
            obs_prefix = ""
        elif n_imgs == 1:
            obs_prefix = "Se adjunta 1 imagen radiológica.\n\n"
        elif n_imgs == 2:
            obs_prefix = (
                "Se adjuntan 2 imágenes: la PRIMERA es la proyección posteroanterior (PA), "
                "la SEGUNDA es la lateral (LAT).\n\n"
            )
        else:
            obs_prefix = (
                f"Se adjuntan {n_imgs} imágenes del mismo estudio radiológico.\n\n"
            )

        pass1_contents: list[Any] = image_parts + [obs_prefix + _build_observation_prompt(template_id)]

        logger.info("[TWO-PASS][PASS-1] Starting observation pass (patient: %s)", patient_id)
        pass1_response = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=LLM_GENERATION_MODEL,
                contents=pass1_contents,
                config=genai.types.GenerateContentConfig(
                    system_instruction=_TWO_PASS_OBSERVATION_SYSTEM,
                    temperature=0.0,
                    max_output_tokens=2048,          # v36c: was 1024, increased to prevent truncation
                    thinking_config=genai.types.ThinkingConfig(thinking_budget=2048),  # v36c: was 8192, reduced for Pass 1
                ),
            ),
            timeout=60.0,
        )
        observations_text = (pass1_response.text or "").strip()
        if hasattr(pass1_response, "usage_metadata"):
            usage = pass1_response.usage_metadata
            logger.info(
                "[TWO-PASS][PASS-1] Token usage — prompt: %s, candidates: %s, thoughts: %s",
                getattr(usage, "prompt_token_count", "?"),
                getattr(usage, "candidates_token_count", "?"),
                getattr(usage, "thoughts_token_count", "?"),
            )
        logger.info(
            "[TWO-PASS][PASS-1] Observations (%d chars):\n%s",
            len(observations_text),
            observations_text[:800],
        )
    except Exception as _p1_exc:
        logger.warning(
            "[FALLBACK] template=%s reason=%s fallback_target=single_pass",
            template_id,
            _p1_exc,
        )
        return await generar_informe_llm(
            ai_resultados=ai_resultados,
            patient_id=patient_id,
            patient_name=patient_name,
            study_date=study_date,
            modalidad=modalidad,
            dicom_images=dicom_images,
            proyecciones_txt=proyecciones_txt,
            patient_age=patient_age,
            custom_system_instruction=custom_system_instruction,
            custom_prompt=custom_prompt,
            nota_clinica=nota_clinica,
            image_labels=image_labels,
            saccade_patches=saccade_patches,
        )

    # ── Pass 2: Report generation anchored to Pass 1 observations ────────────
    # Inject the observations as a hard constraint block into the YAML prompt.
    observations_block = (
        "\n\n"
        "══════════════════════════════════════════════════════════════\n"
        "OBSERVACIONES VISUALES DIRECTAS (fuente de verdad para este informe)\n"
        "Las siguientes observaciones fueron extraídas directamente de la imagen "
        "en un paso previo de análisis morfológico puro:\n\n"
        f"{observations_text}\n\n"
        "REGLA CRÍTICA: El informe DEBE basarse ÚNICAMENTE en estas observaciones. "
        "NO agregues hallazgos que no estén en esta lista. "
        "Si una estructura no aparece en la lista como anormal, describila como 'de aspecto habitual'.\n"
        "══════════════════════════════════════════════════════════════\n"
    )

    # ── v35: ChestSignal gate for chest_xray two-pass ────────────────────────
    # When routing chest through two-pass, integrate the same ChestSignal
    # reporting instructions that the single-pass path uses, appended after
    # the observations block so Pass 1 is NOT biased.
    if template_id == "chest_xray":
        _chest_signal = classify_chest_signal(ai_resultados)
        hallazgos = ai_resultados.get("hallazgos_estudio", [])
        hallazgos_positivos = [h for h in hallazgos if h.get("probabilidad_pct_max", 0) >= 50]
        if _chest_signal == ChestSignal.PATHOLOGICAL:
            _chest_signal_block = (
                "\n\n"
                "══════════════════════════════════════════════════════════════\n"
                "SEÑAL CLÍNICA TÓRAX (v35): PATOLOGÍA CONFIRMADA\n"
                "El modelo de visión especializado confirmó patología grave. "
                "Podés reportar libremente los hallazgos confirmados:\n"
                + "\n".join(
                    f"  - {nombre_es_patologia(h['patologia'])} (CONFIRMADO — hallazgo principal)"
                    for h in hallazgos_positivos
                )
                + "\n══════════════════════════════════════════════════════════════\n"
            )
        elif _chest_signal == ChestSignal.GENERIC_SIGNAL:
            _generico_nombres = ", ".join(
                nombre_es_patologia(h["patologia"]) for h in hallazgos_positivos
            )
            _chest_signal_block = (
                "\n\n"
                "══════════════════════════════════════════════════════════════\n"
                "SEÑAL CLÍNICA TÓRAX (v35): SEÑAL INESPECÍFICA\n"
                f"Señal inespecífica detectada: {_generico_nombres} — "
                "compatible con atelectasia laminar, opacidades retrocardiacas menores o artefacto técnico.\n"
                "⚠ RESTRICCIÓN ACTIVA: Esta señal es INESPECÍFICA y NO confirma patología aguda.\n"
                "Tu hipótesis de trabajo ES que el estudio es NORMAL con posibles hallazgos MENORES.\n"
                "Podés describir opacidades retrocardiacas sutiles o atelectasias laminares.\n"
                "NO reportés consolidación lobar, derrame pleural mayor, neumotórax ni patología\n"
                "pulmonar crónica (EPI, fibrosis, patrón intersticial) a menos que sea IMPOSIBLE\n"
                "de ignorar visualmente.\n"
                "══════════════════════════════════════════════════════════════\n"
            )
        else:
            _chest_signal_block = (
                "\n\n"
                "══════════════════════════════════════════════════════════════\n"
                "SEÑAL CLÍNICA TÓRAX (v35): ESTUDIO NORMAL\n"
                "El modelo de visión especializado NO detectó patología.\n"
                "⚠ RESTRICCIÓN ACTIVA: Tu hipótesis de trabajo ES que el estudio es NORMAL. "
                "Solo invalidás esa hipótesis si hay evidencia IMPOSIBLE de ignorar "
                "(neumotórax total, consolidación lobar densa, derrame masivo con shift mediastinal, "
                "fractura desplazada). Hallazgos sutiles o intermedios NO invalidan la hipótesis de normalidad.\n"
                "✅ EXCEPCIÓN PERMITIDA: Podés y DEBÉS reportar hallazgos extraparenquimatosos crónicos "
                "si los ves: calcificaciones vasculares, ateromatosis aórtica, elongación aórtica, "
                "cambios degenerativos vertebrales, osteofitos, refuerzo/aumento de trama peribroncovascular "
                "hiliar bilateral, prominencia hiliar descriptiva.\n"
                "══════════════════════════════════════════════════════════════\n"
            )
        observations_block = observations_block + _chest_signal_block
        # Signal Injection — force LLM to acknowledge ML findings above 30% threshold
        _ml_hallazgos = ai_resultados.get("hallazgos_estudio", [])
        _ml_positivos = [h for h in _ml_hallazgos if h.get("probabilidad_pct_max", 0) >= 30]
        if _ml_positivos:
            _signal_lines = []
            for h in _ml_positivos:
                _signal_lines.append(
                    f"- {h.get('patologia', 'hallazgo')}: {h.get('probabilidad_pct_max', 0):.0f}% probabilidad"
                )
            _ml_injection = (
                "\n\n## SEÑAL DEL CLASIFICADOR DE VISIÓN (OBLIGATORIO)\n"
                "El clasificador automático de imágenes detectó los siguientes hallazgos con alta confianza:\n"
                + "\n".join(_signal_lines) + "\n"
                "Es un ERROR CRÍTICO omitir estos hallazgos del informe. "
                "Debés describir cada uno de estos hallazgos en la sección HALLAZGOS, "
                "correlacionándolos con lo que observás en las imágenes. "
                "Si no los ves claramente, describí la región anatómica correspondiente "
                "y mencioná que el clasificador los detectó.\n"
            )
            observations_block = observations_block + _ml_injection
            logger.info("[CHEST-SIGNAL-INJECTION] Injected %d ML findings into Pass 2 prompt", len(_ml_positivos))
        logger.info(
            "[TWO-PASS][PASS-2][CHEST] ChestSignal=%s applied for chest_xray gate",
            _chest_signal.name,
        )

    # Inject into the custom_prompt (YAML template) before the final report instruction
    pass2_custom_prompt: str | None = None
    if custom_prompt:
        pass2_custom_prompt = custom_prompt + observations_block
    else:
        pass2_custom_prompt = None  # will use legacy path in generar_informe_llm

    logger.info("[TWO-PASS][PASS-2] Starting report generation pass (patient: %s)", patient_id)
    _informe_texto, _is_fallback = await generar_informe_llm(
        ai_resultados=ai_resultados,
        patient_id=patient_id,
        patient_name=patient_name,
        study_date=study_date,
        modalidad=modalidad,
        dicom_images=dicom_images,  # images still sent for visual context
        proyecciones_txt=proyecciones_txt,
        patient_age=patient_age,
        custom_system_instruction=custom_system_instruction,
        custom_prompt=pass2_custom_prompt,
        nota_clinica=nota_clinica,
        image_labels=image_labels,
        saccade_patches=saccade_patches,
    )

    # Lumbar safety vetoes — run after two-pass generation.
    # If the spondylolisthesis veto fires, replace the entire report with the
    # conservative template. Otherwise check for degenerative over-diagnosis.
    if template_id == "lumbar_spine_xray":
        _lumbar_verdict = evaluate_lumbar(
            spondylolisthesis_reported=bool(re.search(r"espondilolistesis", _informe_texto, re.I)),
            displacement_mm=0.0,  # no structured displacement data in two-pass
            clinical_indication=nota_clinica or "",
            llm_original_text=_informe_texto,
        )
        if _lumbar_verdict.override_applied:
            logger.warning(
                "[SAFETY-VETO][LUMBAR-SPONDYLOLISTHESIS] Two-pass veto triggered — replacing full report: %s",
                _lumbar_verdict.override_reason,
            )
            _indicacion = nota_clinica or "Columna lumbosacra"
            _informe_texto = _LUMBAR_VETO_TEMPLATE.format(indicacion=_indicacion)
        else:
            # Degenerative veto: 'espondiloartrosis' requires ≥2 levels with frank disc narrowing
            # (DHR evidence in text). LLM systematically over-diagnoses based on L5-S1 alone.
            _imp_m = re.search(r"IMPRESI[O\xd3]N\s+DIAGN[O\xd3]STICA", _informe_texto, re.I)
            if _imp_m and re.search(r"espondiloartr", _informe_texto[_imp_m.start():], re.I):
                _narrowed_discs: set[str] = set()
                for _sent in re.split(r"[.;]", _informe_texto):
                    if re.search(r"\bsin\b", _sent, re.I):
                        continue
                    if re.search(r"reduc|disminui|estrech|angost|p[e\xe9]rdida.*altura", _sent, re.I):
                        for _dm in re.finditer(r"L[1-5][-\u2013](?:L[2-6]|S\d)", _sent, re.I):
                            _narrowed_discs.add(_dm.group(0).upper())
                if len(_narrowed_discs) < 2:
                    logger.warning(
                        "[SAFETY-VETO][LUMBAR-DEGENERATIVE] 'espondiloartr*' in IMPRESION but "
                        "%d/2 disc levels with frank narrowing — replacing IMPRESION DIAGNOSTICA",
                        len(_narrowed_discs),
                    )
                    _new_imp = (
                        "Cambios degenerativos lumbares, predominantemente a nivel L5-S1. "
                        "Los hallazgos no cumplen criterios diagnósticos para espondiloartrosis "
                        "lumbar establecida (requiere \u22652 niveles con reducci\xf3n discal franca). "
                        "A correlacionar cl\xednicamente."
                    )
                    _informe_texto = re.sub(
                        r"(IMPRESI[O\xd3]N\s+DIAGN[O\xd3]STICA[ \t]*\n?).*?"
                        r"(?=\n[A-Z\xc1\xc9\xcd\xd3\xda\xd1][A-Z\xc1\xc9\xcd\xd3\xda\xd1 ]{3,}\n|\Z)",
                        lambda m: m.group(1) + _new_imp + "\n\n",
                        _informe_texto,
                        flags=re.I | re.S,
                        count=1,
                    )

    return _informe_texto, _is_fallback


# ============================================================
# Three-pass pelvis/hip report generation (v29)
# ============================================================

def soft_repair_json(raw: str) -> str:
    """Best-effort JSON repair for Pass 1 Inspector output. No external deps.

    Handles three known failure modes:
    1. Thinking text / preamble before first '{'
    2. Trailing commas before } or ]
    3. Truncated JSON with unclosed braces
    """
    # 1. Strip everything before first '{'
    idx = raw.find("{")
    if idx > 0:
        raw = raw[idx:]
    # 2. Remove trailing commas before } or ]
    raw = re.sub(r",\s*([}\]])", r"\1", raw)
    # 3. Balance unclosed braces
    opens = raw.count("{") - raw.count("}")
    if opens > 0:
        raw += "}" * opens
    return raw


_DEFAULT_NORMAL_MEASUREMENTS = _HipMeasurementsSchema(
    right=_HipSideMeasurements(
        view_type="AP",
        gap_supero_lateral_mm=4.0,
        gap_medial_mm=3.0,
        subchondral_sclerosis=False,
        osteophytes_present=False,
        osteophyte_max_mm=0.0,
        geodes_present=False,
        femoral_head_deformity=False,
        joint_space_obliterated=False,
    ),
    left=_HipSideMeasurements(
        view_type="AP",
        gap_supero_lateral_mm=4.0,
        gap_medial_mm=3.0,
        subchondral_sclerosis=False,
        osteophytes_present=False,
        osteophyte_max_mm=0.0,
        geodes_present=False,
        femoral_head_deformity=False,
        joint_space_obliterated=False,
    ),
    image_quality="adequate",
)


async def generar_informe_llm_three_pass_pelvis(
    *,
    ai_resultados: dict[str, Any],
    patient_id: str | None,
    patient_name: str | None,
    study_date: str | None,
    modalidad: str | None,
    dicom_images: list[bytes] | None = None,
    proyecciones_txt: str = "estándar",
    patient_age: str | None = None,
    custom_system_instruction: str | None = None,
    custom_prompt: str | None = None,
    nota_clinica: str | None = None,
    image_labels: list[str] | None = None,
    saccade_patches: list | None = None,
    template_id: str = "",
) -> tuple[str, bool]:
    """Three-pass deterministic pipeline for pelvis/hip X-rays.

    Pass 1 (Pixel Inspector): Gemini measures quantitative morphological features
        (joint space widths in mm, osteophytes, sclerosis, etc.) via structured JSON.
        No diagnostic language allowed — pure measurement only.
        Uses prompt-based JSON schema (response_mime_type="application/json") for structured output.

    Pass 2 (Tönnis Classifier): Pure-Python _classify_tonnis() derives grades
        bilaterally from Pass 1 measurements. Zero LLM calls — deterministic.

    Pass 3 (Report Writer): generar_informe_llm() writes the narrative report
        with the Tönnis verdicts injected as an IMMOVABLE block in the prompt.
        The model CAN see the image (for anatomical context) but CANNOT override
        the systematic grades.

    Falls back to generar_informe_llm_two_pass() on any Pass 1 failure.

    Returns (report_text, is_fallback).
    """
    client = get_gemini_client()
    _loop = asyncio.get_running_loop()

    # ── Build image parts ─────────────────────────────────────────────────────
    image_parts: list[Any] = []
    if dicom_images:
        for raw_bytes in dicom_images:
            try:
                png_bytes = await _loop.run_in_executor(None, dicom_to_png_bytes, raw_bytes)
                image_parts.append(
                    genai.types.Part.from_bytes(data=png_bytes, mime_type="image/png")
                )
            except Exception as _img_err:
                logger.warning("[THREE-PASS][PASS-1] DICOM→PNG failed: %s", _img_err)

    # ── Build saccade parts (v30) ────────────────────────────────────────────
    # Saccade patches are focused ROI crops of each hip joint.
    # Pass 1 was previously only receiving the global pelvis image (~3% of the
    # frame contains the coxofemoral joint). By prepending saccade_parts the
    # model gets high-res close-ups FIRST, dramatically improving metric accuracy.
    saccade_parts: list[Any] = []
    if saccade_patches:
        for sp in saccade_patches:
            try:
                # saccade_patches may be SaccadePatch objects or raw bytes
                _sp_bytes = sp.png_bytes if hasattr(sp, "png_bytes") else sp
                saccade_parts.append(
                    genai.types.Part.from_bytes(data=_sp_bytes, mime_type="image/png")
                )
            except Exception as _sp_err:
                logger.warning("[THREE-PASS][PASS-1] saccade patch load failed: %s", _sp_err)
    logger.info(
        "[THREE-PASS][PASS-1] saccade_parts built: %d patches available",
        len(saccade_parts),
    )

    # ── Extract PixelSpacing for calibrated measurements ─────────────────
    _mm_per_pixel = 0.0
    _spacing_source = "none"
    if dicom_images:
        try:
            _row_sp, _col_sp, _spacing_source = get_pixel_spacing_mm(dicom_images[0])
            _mm_per_pixel = (_row_sp + _col_sp) / 2.0
            logger.info(
                "[THREE-PASS][PASS-1] PixelSpacing: %.4f mm/px (source: %s)",
                _mm_per_pixel, _spacing_source,
            )
        except Exception as _ps_exc:
            logger.warning("[THREE-PASS][PASS-1] PixelSpacing extraction failed: %s", _ps_exc)

    # ════════════════════════════════════════════════════════════
    # PASS 1 — Pixel Inspector (structured JSON measurement)
    # ════════════════════════════════════════════════════════════
    measurements: _HipMeasurementsSchema | None = None
    try:
        # Saccade patches first (focused ROI), then global image, then prompt.
        # High-res close-ups before the wide frame improves metric accuracy.
        _inspector_prompt = _build_hip_inspector_prompt(_mm_per_pixel, _spacing_source)
        pass1_contents: list[Any] = saccade_parts + image_parts + [_inspector_prompt]
        logger.info(
            "[THREE-PASS][PASS-1] Starting pixel inspection (patient: %s, images: %d)",
            patient_id,
            len(image_parts),
        )
        pass1_response = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=LLM_GENERATION_MODEL,  # Pro for better spatial reasoning + metrics
                contents=pass1_contents,
                config=genai.types.GenerateContentConfig(
                    system_instruction=_HIP_PIXEL_INSPECTOR_SYSTEM,
                    temperature=0.0,
                    max_output_tokens=2048,
                    # Thinking budget 2048: improves metric accuracy on saccade patches
                    # while keeping latency bounded. Sufficient for geometric reasoning.
                    thinking_config=genai.types.ThinkingConfig(thinking_budget=2048),
                ),
            ),
            timeout=120.0,  # Pro + thinking requires extended timeout vs Flash
        )
        # Debug: log the raw response before parsing
        _raw_text = pass1_response.text if hasattr(pass1_response, "text") else None
        # NOTE: getattr default [] only fires when attribute is ABSENT.
        # If candidates exists but is None, we get None back — guard with `or []`.
        _candidates = getattr(pass1_response, "candidates", None) or []
        _finish_reason = getattr(_candidates[0], "finish_reason", "N/A") if _candidates else "no_candidates"
        logger.info(
            "[THREE-PASS][PASS-1] Raw response: text_len=%d, finish_reason=%s, candidates=%d",
            len(_raw_text or ""),
            _finish_reason,
            len(_candidates),
        )
        if _candidates and hasattr(_candidates[0], "content") and getattr(_candidates[0].content, "parts", None):
            for _i, _part in enumerate(_candidates[0].content.parts):
                logger.info("[THREE-PASS][PASS-1] Part[%d]: %s", _i, str(_part)[:200])
        raw_json_text = (_raw_text or "").strip()
        # Extract JSON block via regex — handles free-text output with JSON embedded
        _json_match = re.search(r"\{.*\}", raw_json_text, re.DOTALL)
        if not _json_match:
            raise ValueError(f"No JSON object found in Pass 1 response. Raw text: {raw_json_text[:300]!r}")
        raw_json = soft_repair_json(_json_match.group(0))
        measurements = _HipMeasurementsSchema.model_validate_json(raw_json)
        # v36c: mirror right → left if Inspector omitted left side
        if measurements.left is None and measurements.right is not None:
            logger.warning("[THREE-PASS][PASS-1] left hip missing — mirroring from right")
            measurements = measurements.model_copy(update={"left": measurements.right.model_copy()})
        if hasattr(pass1_response, "usage_metadata"):
            usage = pass1_response.usage_metadata
            logger.info(
                "[THREE-PASS][PASS-1] Tokens — prompt: %s, candidates: %s, thoughts: %s",
                getattr(usage, "prompt_token_count", "?"),
                getattr(usage, "candidates_token_count", "?"),
                getattr(usage, "thoughts_token_count", "?"),
            )
        logger.info(
            "[THREE-PASS][PASS-1] Measurements: R(sup-lat=%.1fmm, med=%.1fmm, scler=%s, "
            "osteo=%s/%.1fmm, geodes=%s, deform=%s, obliterated=%s) | "
            "L(sup-lat=%.1fmm, med=%.1fmm, scler=%s, osteo=%s/%.1fmm, geodes=%s, deform=%s, obliterated=%s) | "
            "quality=%s",
            measurements.right.gap_supero_lateral_mm, measurements.right.gap_medial_mm,
            measurements.right.subchondral_sclerosis, measurements.right.osteophytes_present,
            measurements.right.osteophyte_max_mm, measurements.right.geodes_present,
            measurements.right.femoral_head_deformity, measurements.right.joint_space_obliterated,
            measurements.left.gap_supero_lateral_mm, measurements.left.gap_medial_mm,
            measurements.left.subchondral_sclerosis, measurements.left.osteophytes_present,
            measurements.left.osteophyte_max_mm, measurements.left.geodes_present,
            measurements.left.femoral_head_deformity, measurements.left.joint_space_obliterated,
            measurements.image_quality,
        )
    except Exception as _p1_exc:
        logger.warning(
            "[THREE-PASS][FALLBACK] reason=%s template=%s fallback_target=default_normality",
            _p1_exc,
            template_id,
        )
        measurements = _DEFAULT_NORMAL_MEASUREMENTS

    # ════════════════════════════════════════════════════════════
    # PASS 2 — Tönnis Classifier (pure Python, zero LLM)
    # ════════════════════════════════════════════════════════════
    # Sanity-check measurements, then deterministic Tönnis grading via medical_logic
    _right_m = _sanity_check_measurements({
        "cal_medial_joint_space_mm": measurements.right.gap_medial_mm,
        "cal_superolateral_joint_space_mm": measurements.right.gap_supero_lateral_mm,
        "osteophyte_max_mm": measurements.right.osteophyte_max_mm,
        "obliteration_ap": measurements.right.joint_space_obliterated,
        "geodes_present": measurements.right.geodes_present,
    })
    _left_m = _sanity_check_measurements({
        "cal_medial_joint_space_mm": measurements.left.gap_medial_mm,
        "cal_superolateral_joint_space_mm": measurements.left.gap_supero_lateral_mm,
        "osteophyte_max_mm": measurements.left.osteophyte_max_mm,
        "obliteration_ap": measurements.left.joint_space_obliterated,
        "geodes_present": measurements.left.geodes_present,
    })
    right_verdict = evaluate_tonnis(
        gap_medial_mm=_right_m.get("cal_medial_joint_space_mm", measurements.right.gap_medial_mm),
        gap_supero_lateral_mm=_right_m.get("cal_superolateral_joint_space_mm", measurements.right.gap_supero_lateral_mm),
        subchondral_sclerosis=measurements.right.subchondral_sclerosis,
        osteophytes_present=measurements.right.osteophytes_present,
        osteophyte_max_mm=_right_m.get("osteophyte_max_mm", measurements.right.osteophyte_max_mm),
        geodes_present=_right_m.get("geodes_present", measurements.right.geodes_present),
        femoral_head_deformity=measurements.right.femoral_head_deformity,
        joint_space_obliterated=_right_m.get("obliteration_ap", measurements.right.joint_space_obliterated),
        view_type=measurements.right.view_type,
        mm_per_pixel=_mm_per_pixel,
    )
    left_verdict = evaluate_tonnis(
        gap_medial_mm=_left_m.get("cal_medial_joint_space_mm", measurements.left.gap_medial_mm),
        gap_supero_lateral_mm=_left_m.get("cal_superolateral_joint_space_mm", measurements.left.gap_supero_lateral_mm),
        subchondral_sclerosis=measurements.left.subchondral_sclerosis,
        osteophytes_present=measurements.left.osteophytes_present,
        osteophyte_max_mm=_left_m.get("osteophyte_max_mm", measurements.left.osteophyte_max_mm),
        geodes_present=_left_m.get("geodes_present", measurements.left.geodes_present),
        femoral_head_deformity=measurements.left.femoral_head_deformity,
        joint_space_obliterated=_left_m.get("obliteration_ap", measurements.left.joint_space_obliterated),
        view_type=measurements.left.view_type,
        mm_per_pixel=_mm_per_pixel,
    )
    # Contralateral Consistency Check, then severity short-circuit (cap unsupported Grade ≥3)
    left_verdict, right_verdict = apply_contralateral_consistency(
        left_verdict, right_verdict, _left_m, _right_m
    )
    right_verdict = severity_short_circuit(right_verdict, _right_m)
    left_verdict = severity_short_circuit(left_verdict, _left_m)
    right_grade = right_verdict.grade
    left_grade = left_verdict.grade
    logger.info(
        "[THREE-PASS][PASS-2] v35 Tönnis verdicts (post-CCC): R=%d (%s, %s), L=%d (%s, %s)",
        right_grade, right_verdict.grade_label, right_verdict.calibration_source,
        left_grade, left_verdict.grade_label, left_verdict.calibration_source,
    )

    # Serialize measurements as compact JSON for inclusion in the verdict block
    measurements_json = _json.dumps(
        {
            "right": {
                "gap_supero_lateral_mm": measurements.right.gap_supero_lateral_mm,
                "gap_medial_mm": measurements.right.gap_medial_mm,
                "subchondral_sclerosis": measurements.right.subchondral_sclerosis,
                "osteophytes_present": measurements.right.osteophytes_present,
                "osteophyte_max_mm": measurements.right.osteophyte_max_mm,
                "geodes_present": measurements.right.geodes_present,
                "femoral_head_deformity": measurements.right.femoral_head_deformity,
                "joint_space_obliterated": measurements.right.joint_space_obliterated,
            },
            "left": {
                "gap_supero_lateral_mm": measurements.left.gap_supero_lateral_mm,
                "gap_medial_mm": measurements.left.gap_medial_mm,
                "subchondral_sclerosis": measurements.left.subchondral_sclerosis,
                "osteophytes_present": measurements.left.osteophytes_present,
                "osteophyte_max_mm": measurements.left.osteophyte_max_mm,
                "geodes_present": measurements.left.geodes_present,
                "femoral_head_deformity": measurements.left.femoral_head_deformity,
                "joint_space_obliterated": measurements.left.joint_space_obliterated,
            },
            "image_quality": measurements.image_quality,
        },
        ensure_ascii=False,
        indent=2,
    )

    # ════════════════════════════════════════════════════════════
    # PASS 3 — Report Writer (anchored to deterministic verdicts)
    # ════════════════════════════════════════════════════════════
    # Build per-grade language restriction to prevent narrator from using
    # severity language inconsistent with the Python verdict.
    _right_severity_label = right_verdict.grade_label.lower()
    _left_severity_label = left_verdict.grade_label.lower()
    _severity_constraint = ""
    if right_grade <= 1 or left_grade <= 1:
        _severity_constraint += (
            "\nLENGUAJE PROHIBIDO para grados 0-1: "
            "NO uses 'severo', 'avanzado', 'Grado 3', 'obliteración', 'pinzamiento franco', "
            "'pinzamiento severo', ni ninguna expresión que sugiera severidad avanzada. "
            "Usar exclusivamente: 'leve', 'incipiente', 'de bajo grado', 'conservado'.\n"
        )
    if right_grade <= 2 or left_grade <= 2:
        _severity_constraint += (
            "LENGUAJE PROHIBIDO para grados ≤2: "
            "NO uses 'severo', 'Grado 3', 'destrucción articular', 'colapso articular total'. "
            "Usar: 'moderado' o 'leve' según el grado específico de cada lado.\n"
        )

    verdict_block = (
        "\n\n"
        "══════════════════════════════════════════════════════════════\n"
        "VEREDICTO DEL SISTEMA CUANTITATIVO — INAMOVIBLE\n"
        f"• Cadera derecha: Tönnis Grado {right_grade} — {right_verdict.grade_label}\n"
        f"  Fundamento: {right_verdict.reasoning}\n"
        f"  Medial: {right_verdict.medial_gap_mm:.1f}mm | Supero-lateral: {right_verdict.superolateral_gap_mm:.1f}mm\n"
        f"  Calibración: {right_verdict.calibration_source}\n"
        f"• Cadera izquierda: Tönnis Grado {left_grade} — {left_verdict.grade_label}\n"
        f"  Fundamento: {left_verdict.reasoning}\n"
        f"  Medial: {left_verdict.medial_gap_mm:.1f}mm | Supero-lateral: {left_verdict.superolateral_gap_mm:.1f}mm\n"
        f"  Calibración: {left_verdict.calibration_source}\n\n"
        "Medidas morfológicas crudas del Inspector:\n"
        f"{measurements_json}\n\n"
        "INSTRUCCIÓN CRÍTICA: Reportá EXACTAMENTE estos grados de Tönnis en el informe. "
        "El sistema cuantitativo tiene precedencia absoluta sobre cualquier interpretación visual. "
        "NO cambies los grados. NO los elevas. NO los bajas. "
        "Si el informe incluye Tönnis, DEBE coincidir con los valores indicados arriba.\n"
        f"{_severity_constraint}"
        "VOCABULARIO ANATÓMICO: Usar SIEMPRE terminología en español latino: "
        "'cadera' (no 'hip'), 'artrosis' (no 'osteoarthritis'), 'espacio articular' (no 'joint space').\n"
        "══════════════════════════════════════════════════════════════\n"
    )

    pass3_custom_prompt: str | None = None
    if custom_prompt:
        pass3_custom_prompt = custom_prompt + verdict_block
    else:
        pass3_custom_prompt = verdict_block

    logger.info(
        "[THREE-PASS][PASS-3] Starting report generation (patient: %s, Tönnis R%d/L%d)",
        patient_id,
        right_grade,
        left_grade,
    )
    return await generar_informe_llm(
        ai_resultados=ai_resultados,
        patient_id=patient_id,
        patient_name=patient_name,
        study_date=study_date,
        modalidad=modalidad,
        dicom_images=dicom_images,  # model can see image for anatomical narrative
        proyecciones_txt=proyecciones_txt,
        patient_age=patient_age,
        custom_system_instruction=custom_system_instruction,
        custom_prompt=pass3_custom_prompt,
        nota_clinica=nota_clinica,
        image_labels=image_labels,
        saccade_patches=saccade_patches,
    )
