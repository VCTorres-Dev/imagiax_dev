"""
app.ai.quality_gate — Phase 0.5 Quality Gate service.

Created for ai-pipeline-v1.1 (T-05).

Assesses whether a DICOM image is analyzable before running the full pipeline.
Uses Gemini Flash with a single image and a 10-second timeout.
Always fail-open: on any error returns an "adequate" verdict so the pipeline continues.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from google import genai

from app.ai.dicom_service import dicom_to_png_bytes
from app.ai.llm_service import call_gemini_json
from app.ai.pipeline_context import QualityGateResult
from app.ai.prompt_orchestrator import assemble_prompt, sanitize_dicom_text
from app.core.config import settings

logger = logging.getLogger("imagiax.ai.quality_gate")

# Model used for quality assessment — uses the project-wide Gemini model from settings
# (was hardcoded "gemini-2.0-flash" — now respects .env GEMINI_MODEL)
_QUALITY_GATE_MODEL: str = settings.GEMINI_MODEL

# Strict system instruction — prevents JSON truncation on low max_output_tokens budgets.
_QUALITY_GATE_SYSTEM_INSTRUCTION = (
    "You are an expert radiologist evaluating image quality. "
    "Respond ONLY with a single valid JSON object — no markdown fences, no extra text. "
    "The JSON must contain exactly these keys: "
    "verdict (string: 'adequate' | 'suboptimal' | 'not_analyzable'), "
    "reason (string, max 200 chars), "
    "confidence (float 0.0–1.0). "
    "Do NOT truncate the JSON. Output the complete object."
)

# Fail-open result returned whenever the gate cannot complete
_FAIL_OPEN_RESULT = QualityGateResult(
    verdict="adequate",
    reason="Quality gate skipped (fail-open)",
    confidence=0.0,
)


def _extract_phi_minimized_context(
    modality: str,
    patient_age: str | None,
    patient_sex: str | None,
    study_date: str | None,
) -> dict[str, Any]:
    """Build a PHI-minimized context dict for the quality gate prompt.

    NFR-04: Only age (int), sex (str), modality (str), and study year (int)
    are passed to LLM calls. NO patient name or ID.
    """
    # Age: extract integer from DICOM PatientAge (e.g. "045Y", "12M")
    age_years: int = 0
    if patient_age:
        age_match = re.search(r"(\d+)", str(patient_age))
        if age_match:
            age_num = int(age_match.group(1))
            # If format is months (ends with M), convert roughly
            if str(patient_age).upper().endswith("M"):
                age_years = max(0, age_num // 12)
            else:
                age_years = age_num

    # Sex: normalize to M / F / Unknown
    sex_raw = (patient_sex or "").strip().upper()
    if sex_raw in ("M", "MALE", "MASCULINO"):
        sex_norm = "M"
    elif sex_raw in ("F", "FEMALE", "FEMENINO"):
        sex_norm = "F"
    else:
        sex_norm = "Unknown"

    # Study year: extract from YYYYMMDD
    study_year: int = 0
    if study_date and len(study_date) >= 4:
        try:
            study_year = int(study_date[:4])
        except ValueError:
            pass

    return {
        "modality": sanitize_dicom_text(modality or "Unknown", max_len=10),
        "patient_age_years": age_years,
        "patient_sex": sex_norm,
        "study_year": study_year,
    }


def _parse_quality_gate_response(raw_text: str) -> QualityGateResult:
    """Parse raw Gemini JSON output into a QualityGateResult.

    Strips optional markdown fences, then parses JSON via Pydantic.
    Returns fail-open result on any parse error.
    """
    text = raw_text.strip()
    # Strip optional ```json ... ``` fencing
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text.strip())

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("[QUALITY_GATE] JSON parse failed: %s | raw: %.200s", exc, raw_text)
        return _FAIL_OPEN_RESULT

    # Validate and coerce via Pydantic — rejects malformed/untrusted LLM output
    try:
        verdict_raw = str(parsed.get("verdict", "adequate")).lower().strip()
        # Map any unexpected verdict to "adequate" (fail-open)
        if verdict_raw not in ("adequate", "suboptimal", "not_analyzable"):
            logger.warning(
                "[QUALITY_GATE] Unexpected verdict %r — defaulting to adequate", verdict_raw
            )
            verdict_raw = "adequate"

        reason_raw = sanitize_dicom_text(
            str(parsed.get("reason", "")), max_len=500
        )
        confidence_raw = float(parsed.get("confidence", 0.0))
        confidence_raw = max(0.0, min(1.0, confidence_raw))

        return QualityGateResult(
            verdict=verdict_raw,  # type: ignore[arg-type]
            reason=reason_raw,
            confidence=confidence_raw,
        )
    except Exception as exc:
        logger.warning("[QUALITY_GATE] Pydantic validation failed: %s", exc)
        return _FAIL_OPEN_RESULT


async def run_quality_gate(
    *,
    gemini_client: genai.Client,  # noqa: ARG001 — kept for API symmetry; uses singleton via call_gemini_json
    image_bytes: bytes,
    modality: str,
    patient_age: str | None = None,
    patient_sex: str | None = None,
    study_date: str | None = None,
) -> QualityGateResult:
    """Assess image quality using Gemini Flash.

    Phase 0.5 of the v1.1 pipeline. Runs in < 10 seconds (timeout enforced).

    Args:
        gemini_client: Initialized genai.Client (passed for API consistency; the
            actual call goes through llm_service.call_gemini_json which uses the
            singleton client).
        image_bytes: Raw DICOM bytes of the primary image (first instance).
        modality: DICOM Modality tag value (e.g. "DX", "CT").
        patient_age: DICOM PatientAge (e.g. "045Y"). PHI-minimized to int age.
        patient_sex: DICOM PatientSex. PHI-minimized to "M"/"F"/"Unknown".
        study_date: DICOM StudyDate (YYYYMMDD). PHI-minimized to study year.

    Returns:
        QualityGateResult — always returns, never raises.
        On any error (timeout, API, parse) → fail-open "adequate" result.

    Security:
        Only age (int), sex (str), modality (str), study year (int) are sent to Gemini.
        Patient name and ID are NEVER included.
    """
    if not settings.QUALITY_GATE_ENABLED:
        logger.info("[QUALITY_GATE] Disabled by QUALITY_GATE_ENABLED=False — skipping")
        return _FAIL_OPEN_RESULT

    # ── Convert DICOM → PNG ───────────────────────────────────────────────────
    try:
        loop = asyncio.get_running_loop()
        png_bytes = await loop.run_in_executor(None, dicom_to_png_bytes, image_bytes)
    except Exception as exc:
        logger.warning("[QUALITY_GATE] DICOM→PNG conversion failed: %s — fail-open", exc)
        return _FAIL_OPEN_RESULT

    # ── Assemble prompt (PHI-minimized context) ───────────────────────────────
    phi_ctx = _extract_phi_minimized_context(modality, patient_age, patient_sex, study_date)
    try:
        _sys, prompt_text = assemble_prompt("quality_gate", phi_ctx)
    except Exception as exc:
        logger.warning("[QUALITY_GATE] Prompt assembly failed: %s — fail-open", exc)
        return _FAIL_OPEN_RESULT

    # ── Build contents: image + prompt ───────────────────────────────────────
    image_part = genai.types.Part.from_bytes(data=png_bytes, mime_type="image/png")
    contents = [image_part, prompt_text]

    # ── Call Gemini Flash with timeout ────────────────────────────────────────
    try:
        raw_text = await call_gemini_json(
            model=_QUALITY_GATE_MODEL,
            contents=contents,
            timeout=float(settings.QUALITY_GATE_TIMEOUT),
            temperature=0.0,
            max_output_tokens=512,
            response_mime_type="application/json",
            system_instruction=_QUALITY_GATE_SYSTEM_INSTRUCTION,
        )
        logger.debug("[QUALITY_GATE] Raw response: %.300s", raw_text)
    except asyncio.TimeoutError:
        logger.warning(
            "[QUALITY_GATE] Timeout after %ds — fail-open", settings.QUALITY_GATE_TIMEOUT
        )
        return _FAIL_OPEN_RESULT
    except Exception as exc:
        logger.warning("[QUALITY_GATE] Gemini call failed: %s — fail-open", exc)
        return _FAIL_OPEN_RESULT

    # ── Parse response via Pydantic (SEC: never trust raw LLM strings) ────────
    result = _parse_quality_gate_response(raw_text)
    logger.info(
        "[QUALITY_GATE] verdict=%s confidence=%.2f reason=%.100s",
        result.verdict,
        result.confidence,
        result.reason,
    )
    return result
