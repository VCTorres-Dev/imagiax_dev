"""
app.ai.llm_service — Gemini LLM integration for IMAGIAX report generation.

Extracted from servidor_api_orthanc_v1.py (God Object refactor, Phase 5).

Provides:
- get_gemini_client()       — Lazy singleton Gemini client initialisation
- generar_informe_llm()     — Full report generation via Gemini Vision
- _generar_informe_fallback() — Structured fallback when Gemini is unavailable
- _dicom_to_png_bytes()     — Re-exported from dicom_service for convenience

All os.getenv() calls have been replaced with settings.* (app.core.config).

Usage:
    from app.ai.llm_service import get_gemini_client, generar_informe_llm
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from fastapi import HTTPException
from google import genai

from app.ai.dicom_service import dicom_to_png_bytes
from app.ai.prompt_orchestrator import assemble_prompt
from app.core.config import settings

logger = logging.getLogger("imagiax.llm")

# ── LLM model name (constant; can be overridden via settings.GEMINI_MODEL) ────
LLM_MODEL: str = settings.GEMINI_MODEL

# ── Singleton Gemini client ───────────────────────────────────────────────────
_gemini_client: genai.Client | None = None


def get_gemini_client() -> genai.Client:
    """Return the shared Gemini client, initialising it on first call.

    Raises:
        HTTPException(500): If GEMINI_API_KEY is not configured.
    """
    global _gemini_client
    if _gemini_client is None:
        api_key = settings.GEMINI_API_KEY.get_secret_value()
        if not api_key:
            raise HTTPException(
                status_code=500,
                detail="GEMINI_API_KEY is not configured in .env",
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
            ),
        ),
        timeout=timeout,
    )
    return response.text or ""


# ============================================================
# Fallback report (no LLM)
# ============================================================

#: Pathology names in Spanish — used by both the main report and the fallback.
_NOMBRES_ES: dict[str, str] = {
    "Atelectasis": "Atelectasia",
    "Cardiomegaly": "Cardiomegalia",
    "Effusion": "Derrame pleural",
    "Infiltrate": "Infiltrado pulmonar",
    "Infiltration": "Infiltrado pulmonar",
    "Mass": "Masa pulmonar",
    "Nodule": "Nódulo pulmonar",
    "Pneumonia": "Neumonía",
    "Pneumothorax": "Neumotórax",
    "Consolidation": "Consolidación pulmonar",
    "Edema": "Edema pulmonar",
    "Emphysema": "Enfisema pulmonar",
    "Fibrosis": "Fibrosis pulmonar",
    "Pleural_Thickening": "Engrosamiento pleural",
    "Hernia": "Hernia diafragmática",
    "Lung Lesion": "Lesión pulmonar",
    "Fracture": "Fractura",
    "Lung Opacity": "Opacidad pulmonar",
    "Enlarged Cardiomediastinum": "Ensanchamiento mediastínico",
}


def nombre_es_patologia(pat_raw: str) -> str:
    """Return the Spanish display name for a NIH pathology label."""
    return _NOMBRES_ES.get(pat_raw, pat_raw)


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
            age_num = int(patient_age.replace("Y", "").replace("M", "").strip()[:3])
            edad_txt = f", {age_num} años"
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
    proyecciones_txt: str = "estándar",
    patient_age: str | None = None,
    custom_system_instruction: str | None = None,
    custom_prompt: str | None = None,
    nota_clinica: str | None = None,
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

    hallazgos_positivos = [h for h in hallazgos if h.get("probabilidad_pct_max", 0) >= 50]
    hallazgos_leves: list[dict[str, Any]] = []

    if hallazgos_positivos:
        hallazgos_confirmados_txt = "\n".join(
            f"  - {nombre_es_patologia(h['patologia'])} (CONFIRMADO — hallazgo principal)"
            for h in hallazgos_positivos
        )
    else:
        hallazgos_confirmados_txt = (
            "  - Ningún hallazgo patológico significativo confirmado "
            "(estudio dentro de límites normales)"
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
        age_match = re.search(r"(\d+)", str(patient_age))
        if age_match:
            edad_num = int(age_match.group(1))
            base_edad = (
                f"\nEDAD DEL PACIENTE: {edad_num} años. "
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

    # ── Inject nota_clinica into prompt (v1.0 legacy path) ─────────────────────
    # In v1.1 the router already injected the note via inject_clinical_note().
    # For v1.0 (custom_prompt was None -> legacy prompt built above), use
    # the same strong injection from prompt_orchestrator.
    if nota_clinica and nota_clinica.strip():
        from app.ai.prompt_orchestrator import inject_clinical_note as _inject_note
        prompt = _inject_note(prompt, nota_clinica)

    # ── Gemini Vision: attach DICOM images if available ───────────────────────
    _loop = asyncio.get_running_loop()
    contents: list[Any] = []
    if dicom_images:
        image_parts: list[Any] = []
        for raw_bytes in dicom_images:
            try:
                png_bytes = await _loop.run_in_executor(None, dicom_to_png_bytes, raw_bytes)
                image_parts.append(
                    genai.types.Part.from_bytes(data=png_bytes, mime_type="image/png")
                )
            except Exception as _img_err:
                logger.warning("Gemini Vision: DICOM→PNG conversion failed: %s", _img_err)

        if image_parts:
            n_imgs = len(image_parts)
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

    client = get_gemini_client()

    # ── Retry with exponential back-off (handles 429 and transient 5xx) ───────
    last_exc: Exception | None = None
    for _attempt in range(4):  # attempts: 0, 1, 2, 3
        try:
            response = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=LLM_MODEL,
                    contents=contents,
                    config=genai.types.GenerateContentConfig(
                        system_instruction=system_instruction,
                        temperature=0.1,
                        max_output_tokens=4096,
                    ),
                ),
                timeout=90.0,
            )
            return response.text or "", False

        except asyncio.TimeoutError:
            last_exc = TimeoutError(
                f"Gemini did not respond in 90 s (attempt {_attempt + 1}/4)"
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
