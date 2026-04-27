"""
app.ai.auditor — Phase 2 Selective Auditor service.
Created for ai-pipeline-v1.1 (T-11).

Performs secondary review of a generated radiological report using Gemini Pro.
Triggered automatically for:
  - High-confidence findings (max probability >= 70%)
  - Suboptimal image quality
  - CT or MR studies
  - Explicit physician request (solicitar_auditoria=True)

Always fail-open: on any error returns a skip result so the main report is delivered.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from google import genai
from infrastructure.llm_service import call_gemini_json
from app.ai.pipeline_context import AuditResult, PipelineContext
from app.ai.prompt_orchestrator import assemble_prompt, sanitize_dicom_text
from app.core.config import settings

logger = logging.getLogger("imagiax.ai.auditor")

_AUDITOR_MODEL_DEFAULT = "gemini-2.0-flash"
_CT_MR_MODALITIES = {"CT", "MR", "MRI", "PT", "NM"}
_HIGH_CONFIDENCE_THRESHOLD = 0.70
# v16.1: Templates that always trigger the auditor regardless of finding probabilities.
# Add new template_ids here as new anatomical regions are onboarded.
_ALWAYS_AUDIT_TEMPLATES: frozenset[str] = frozenset({"pelvis_hip_xray"})

# VFY-004: Use "sin_auditoria" verdict to distinguish "audit was skipped/failed"
# from "audit ran and found no issues" (sin_hallazgos).

# Strict system instruction — forces Gemini to return valid JSON without markdown fences.
_AUDITOR_SYSTEM_INSTRUCTION = (
    "You are a senior radiologist performing a peer review of an AI-generated radiology report. "
    "Respond ONLY with a single valid JSON object — no markdown fences, no extra text, no explanation. "
    "The JSON must contain exactly these keys: "
    "verdict (string: 'approved' | 'needs_revision' | 'critical_error'), "
    "observations (array of strings), "
    "severity (string: 'low' | 'medium' | 'high'), "
    "recommendation (string)."
)

_AUDIT_SKIPPED = AuditResult(
    verdict="sin_auditoria",
    observations=[],
    severity="low",
    recommendation="",
    model_used="",
)


def should_audit(
    ctx: PipelineContext,
    modality: str,
    ai_resultados: dict[str, Any],
) -> bool:
    """Determine whether the auditor phase should run for this request.

    Trigger criteria (any one is sufficient):
      1. Explicit physician request: ctx.solicitar_auditoria == True
      2. High-confidence findings: max finding probability >= 70%
      3. Suboptimal image quality: ctx.quality_gate.verdict == "suboptimal"
      4. CT or MR study modality
      5. Template requires always-audit (e.g. pelvis_hip_xray — v16.1)
    """
    if not settings.AUDITOR_ENABLED:
        logger.debug("[AUDITOR] AUDITOR_ENABLED=False — skipping")
        return False

    if ctx.solicitar_auditoria:
        logger.info("[AUDITOR] Trigger: explicit physician request")
        return True

    hallazgos = ai_resultados.get("hallazgos_estudio", [])
    max_prob = max(
        (h.get("probabilidad_pct_max", 0) / 100.0 for h in hallazgos), default=0.0
    )
    if max_prob >= _HIGH_CONFIDENCE_THRESHOLD:
        logger.info("[AUDITOR] Trigger: high-confidence finding (%.0f%%)", max_prob * 100)
        return True

    if ctx.quality_gate and ctx.quality_gate.verdict == "suboptimal":
        logger.info("[AUDITOR] Trigger: suboptimal image quality")
        return True

    modality_upper = (modality or "").strip().upper()
    if modality_upper in _CT_MR_MODALITIES:
        logger.info("[AUDITOR] Trigger: CT/MR modality (%s)", modality_upper)
        return True

    # v16.1: template-based always-audit (uses template_id from ai_resultados)
    template_id = str(ai_resultados.get("template_id", "")).lower().strip()
    if template_id in _ALWAYS_AUDIT_TEMPLATES:
        logger.info("[AUDITOR] Trigger: always-audit template (%s)", template_id)
        return True

    logger.debug("[AUDITOR] No trigger criteria met — skipping audit")
    return False


def _parse_audit_response(raw_text: str, model_used: str) -> AuditResult:
    """Parse raw Gemini JSON output into an AuditResult.

    Maps auditor.yaml verdicts to internal AuditResult verdict literals:
      approved        -> sin_hallazgos
      needs_revision  -> observaciones
      critical_error  -> alerta
    """
    text = raw_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text.strip())

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("[AUDITOR] JSON parse failed: %s | raw: %.200s", exc, raw_text)
        return _AUDIT_SKIPPED

    try:
        verdict_raw = str(parsed.get("verdict", "approved")).lower().strip()
        verdict_map = {
            "approved": "sin_hallazgos",
            "needs_revision": "observaciones",
            "critical_error": "alerta",
        }
        verdict_internal = verdict_map.get(verdict_raw, "sin_hallazgos")

        observations_raw = parsed.get("observations", [])
        if not isinstance(observations_raw, list):
            observations_raw = []
        observations = [
            sanitize_dicom_text(str(obs), max_len=300)
            for obs in observations_raw[:10]
        ]

        severity_raw = str(parsed.get("severity", "low")).lower().strip()
        if severity_raw not in ("low", "medium", "high"):
            severity_raw = "low"

        recommendation = sanitize_dicom_text(
            str(parsed.get("recommendation", "")), max_len=500
        )

        return AuditResult(
            verdict=verdict_internal,  # type: ignore[arg-type]
            observations=observations,
            severity=severity_raw,  # type: ignore[arg-type]
            recommendation=recommendation,
            model_used=model_used,
        )
    except Exception as exc:
        logger.warning("[AUDITOR] Pydantic validation failed: %s", exc)
        return _AUDIT_SKIPPED


def _format_findings_summary(ai_resultados: dict[str, Any]) -> str:
    """Format AI findings into a readable summary string for the auditor prompt."""
    hallazgos = ai_resultados.get("hallazgos_estudio", [])
    nivel = ai_resultados.get("nivel_alerta", "NORMAL")

    if not hallazgos:
        return f"Alert level: {nivel}\nNo significant findings above clinical thresholds."

    lines = [f"Alert level: {nivel}", "Significant findings:"]
    for h in hallazgos:
        pat = sanitize_dicom_text(str(h.get("patologia", "")), max_len=80)
        prob = float(h.get("probabilidad_pct_max", 0))
        lines.append(f"  - {pat}: {prob:.1f}%")
    return "\n".join(lines)


def _format_clinical_note_block(nota_clinica: str | None) -> str:
    """Format the optional clinical note block for the auditor prompt."""
    if not nota_clinica or not nota_clinica.strip():
        return ""
    sanitized = sanitize_dicom_text(nota_clinica.strip(), max_len=500)
    return f"Physician clinical note:\n{sanitized}"


async def run_audit(
    *,
    ctx: PipelineContext,
    report_draft: str,
    modality: str,
    ai_resultados: dict[str, Any],
    patient_age: str | None = None,
    patient_sex: str | None = None,
    study_date: str | None = None,
    image_quality: str = "adequate",
    image_bytes_list: list[bytes] | None = None,
) -> AuditResult:
    """Run the selective auditor on a generated report draft.

    Phase 2 of the v1.1 pipeline. Always fail-open — returns _AUDIT_SKIPPED on any error
    so the main report is still delivered to the physician.

    Args:
        image_bytes_list: Raw DICOM/PNG bytes of study images (VFY-002).
            Passed to Gemini Pro so the auditor can see the actual images.
            Maximum 3 images sent to keep token count reasonable.

    Security: Only age (int), sex, modality, study year are sent to Gemini.
    Patient name and ID are NEVER included. All user text is sanitized.
    """
    model_name = getattr(settings, "AUDITOR_MODEL", getattr(settings, "GEMINI_MODEL", _AUDITOR_MODEL_DEFAULT))

    # PHI-minimized context
    age_years: int = 0
    if patient_age:
        age_match = re.search(r"(\d+)", str(patient_age))
        if age_match:
            age_num = int(age_match.group(1))
            age_years = age_num // 12 if str(patient_age).upper().endswith("M") else age_num

    sex_raw = (patient_sex or "").strip().upper()
    if sex_raw in ("M", "MALE", "MASCULINO"):
        sex_norm = "M"
    elif sex_raw in ("F", "FEMALE", "FEMENINO"):
        sex_norm = "F"
    else:
        sex_norm = "Unknown"

    study_year: int = 0
    if study_date and len(study_date) >= 4:
        try:
            study_year = int(study_date[:4])
        except ValueError:
            pass

    phi_ctx = {
        "modality": sanitize_dicom_text(modality or "Unknown", max_len=10),
        "patient_age_years": age_years,
        "patient_sex": sex_norm,
        "study_year": study_year,
        "image_quality": sanitize_dicom_text(image_quality, max_len=20),
        "ai_findings_summary": _format_findings_summary(ai_resultados),
        "clinical_note_block": _format_clinical_note_block(ctx.nota_clinica),
        "report_draft": sanitize_dicom_text(report_draft, max_len=12000),
    }

    try:
        _sys, prompt_text = assemble_prompt("auditor", phi_ctx)
    except Exception as exc:
        logger.warning("[AUDITOR] Prompt assembly failed: %s — fail-open", exc)
        return _AUDIT_SKIPPED

    # VFY-002: Build contents list including images so auditor can see the study
    contents: list = []
    if image_bytes_list:
        from infrastructure.dicom_service import dicom_to_png_bytes
        import asyncio as _asyncio
        _loop = _asyncio.get_running_loop()
        added_images = 0
        _MAX_AUDIT_IMAGES = 3  # limit to 3 images to keep Pro token usage reasonable
        for _raw in image_bytes_list[:_MAX_AUDIT_IMAGES]:
            try:
                png_bytes = await _loop.run_in_executor(None, dicom_to_png_bytes, _raw)
                contents.append(
                    genai.types.Part.from_bytes(data=png_bytes, mime_type="image/png")
                )
                added_images += 1
            except Exception as _img_exc:
                logger.debug("[AUDITOR] Could not convert image for Pro: %s", _img_exc)
        if added_images > 0:
            logger.debug("[AUDITOR] Attached %d image(s) for Gemini Pro audit", added_images)
    contents.append(prompt_text)

    _call_kwargs = dict(
        model=model_name,
        contents=contents,
        timeout=float(settings.AUDITOR_TIMEOUT),
        temperature=0.0,
        max_output_tokens=2048,
        response_mime_type="application/json",
        system_instruction=_AUDITOR_SYSTEM_INSTRUCTION,
    )
    try:
        raw_text = await call_gemini_json(**_call_kwargs)
        logger.debug("[AUDITOR] Raw response: %.500s", raw_text)
        # Retry once on empty response (Gemini Pro safety filter / transient issue)
        if not raw_text or not raw_text.strip():
            logger.warning(
                "[AUDITOR][PIPELINE:%s] Empty response on first attempt — retrying once",
                ctx.pipeline_id,
            )
            raw_text = await call_gemini_json(**_call_kwargs)
            logger.debug("[AUDITOR] Retry response: %.500s", raw_text)
    except asyncio.TimeoutError:
        logger.warning(
            "[AUDITOR][PIPELINE:%s] Timeout after %ds — fail-open",
            ctx.pipeline_id,
            settings.AUDITOR_TIMEOUT,
        )
        return _AUDIT_SKIPPED
    except Exception as exc:
        logger.warning(
            "[AUDITOR][PIPELINE:%s] Gemini call failed: %s — fail-open",
            ctx.pipeline_id,
            exc,
        )
        return _AUDIT_SKIPPED

    result = _parse_audit_response(raw_text, model_name)
    logger.info(
        "[AUDITOR][PIPELINE:%s] verdict=%s severity=%s observations=%d",
        ctx.pipeline_id,
        result.verdict,
        result.severity,
        len(result.observations),
    )
    return result
