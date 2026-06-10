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

from app.ai.attention_booster import AttentionBooster
from app.ai.clinical_audit_engine import (
    BodyPart,
    ChestSignal,
    ClinicalAuditEngine,
    classify_chest_signal,
    resolve_body_part,
)
from app.ai.constants import NOMBRES_ES, strip_xml_tags
from app.ai.dicom_service import dicom_to_png_bytes
from app.ai.prompt_orchestrator import assemble_prompt
from app.core.config import settings

# ── Backward-compatible alias (internal uses reference _strip_xml_tags) ───────
_strip_xml_tags = strip_xml_tags

# ── Singleton del motor de auditoría clínica ─────────────────────────────────
_audit_engine = ClinicalAuditEngine()

# ── Singleton del Attention Booster (v15) ────────────────────────────────────
_attention_booster = AttentionBooster()

logger = logging.getLogger("imagiax.llm")


# ── LLM model names ───────────────────────────────────────────────────────────
# Quality gate, auditor regen, etc. stay on flash (fast, cheap, sufficient).
LLM_MODEL: str = settings.GEMINI_MODEL
# Report generation uses Pro for higher diagnostic accuracy.
LLM_GENERATION_MODEL: str = settings.GEMINI_PRO_MODEL

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
        response_mime_type: Optional MIME type to force (e.g. "application/json").
        system_instruction: Optional system instruction for the model.

    Returns:
        Raw response text (JSON string, not yet parsed).

    Raises:
        asyncio.TimeoutError: If the call exceeds ``timeout`` seconds.
        Any genai exception on non-retriable errors (auth, quota, etc.).
    """
    client = get_gemini_client()
    config_kwargs: dict[str, Any] = {
        "temperature": temperature,
        "max_output_tokens": max_output_tokens,
    }
    if response_mime_type is not None:
        config_kwargs["response_mime_type"] = response_mime_type
    if system_instruction is not None:
        config_kwargs["system_instruction"] = system_instruction
    response = await asyncio.wait_for(
        client.aio.models.generate_content(
            model=model,
            contents=contents,
            config=genai.types.GenerateContentConfig(**config_kwargs),
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
    override_model: str | None = None,
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
        # Temperatura 0.2 en el primer intento para mayor sensibilidad morfológica.
        # Reintentos (por error técnico) usan temperature=None (default del modelo).
        _temperature: float | None = 0.2 if _attempt == 0 else None
        try:
            response = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=_active_model,
                    contents=contents,
                    config=genai.types.GenerateContentConfig(
                        system_instruction=system_instruction,
                        temperature=_temperature,
                        max_output_tokens=8192,
                        thinking_config=genai.types.ThinkingConfig(thinking_budget=-1),
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
                            temperature=None,
                            max_output_tokens=16384,
                            thinking_config=genai.types.ThinkingConfig(thinking_budget=-1),
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
    "Empezá directamente con el primer ítem '-'. Sin introducción."
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

        pass1_contents: list[Any] = image_parts + [obs_prefix + _TWO_PASS_OBSERVATION_PROMPT]

        logger.info("[TWO-PASS][PASS-1] Starting observation pass (patient: %s)", patient_id)
        pass1_response = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=LLM_GENERATION_MODEL,
                contents=pass1_contents,
                config=genai.types.GenerateContentConfig(
                    system_instruction=_TWO_PASS_OBSERVATION_SYSTEM,
                    temperature=None,
                    max_output_tokens=1024,
                    thinking_config=genai.types.ThinkingConfig(thinking_budget=-1),
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
            "[TWO-PASS][PASS-1] Failed, falling back to single-pass: %s", _p1_exc
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

    # Inject into the custom_prompt (YAML template) before the final report instruction
    pass2_custom_prompt: str | None = None
    if custom_prompt:
        pass2_custom_prompt = custom_prompt + observations_block
    else:
        pass2_custom_prompt = None  # will use legacy path in generar_informe_llm

    logger.info("[TWO-PASS][PASS-2] Starting report generation pass (patient: %s)", patient_id)
    return await generar_informe_llm(
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
    )
