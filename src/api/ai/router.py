"""
api.ai.router — AI analysis and DICOMweb proxy endpoints.

Extracted from servidor_api_orthanc_v1.py (God Object refactor, Phase 5).

Endpoints (NO prefix — nginx strips /api/ before proxying to FastAPI):
  POST /analizar/instancia       — Analyse a single DICOM instance
  POST /analizar/serie           — Analyse all instances in a series
  POST /analizar/estudio         — Analyse all instances in a study
  GET  /resultados/estudio/{uid} — Retrieve persisted study result
  POST /generar/informe          — Full pipeline: AI + LLM report + billing
  POST /informes/guardar         — Record physician decision on a report
  GET  /informes/estudio/{uid}   — List reports for a study
  GET  /listado                  — Paginated report listing
  ANY  /pacs/dicom-web/{path}    — Authenticated DICOMweb proxy

Thin handlers: business logic lives in infrastructure.dicom_service and
infrastructure.llm_service.  Payment orchestration uses infrastructure.pagos.mp.service and
infrastructure.pagos.tbk.service directly (mirrors the monolith).
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

import asyncpg
import httpx

# torch is installed separately in the Dockerfile (CPU wheel via --index-url).
# Guard with try/except so the module can be imported in environments without
# torch (e.g. CI or dev containers that skip the heavy ML dependencies).
try:
    import torch
except ImportError:  # pragma: no cover
    torch = None  # type: ignore[assignment]

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from infrastructure.pdf_service import build_pdf_streaming_response
from infrastructure.dicom_service import (
    NIH_PATOLOGIAS,
    UMBRALES,
    analyze_single_instance,
    clean_dicom_tag,
    dicom_to_tensor,
    infer_multilabel,
)
from infrastructure.llm_service import generar_informe_llm, generar_informe_llm_two_pass, generar_informe_llm_three_pass_pelvis, _LUMBAR_VETO_TEMPLATE
from app.ai.orthanc_client import (
    find_instances_by_query,
    get_instance_tags,
    get_instance_dicom_bytes,
    resolve_orthanc_instance_id,
    save_study_result_metadata,
    get_study_result_metadata,
    sanitize_dicomweb_path,
)
from app.ai.prompt_orchestrator import (
    classify_exam,
    extract_patient_context,
    should_run_resnet50,
    assemble_prompt,
    get_template,
    sanitize_dicom_text,
    inject_clinical_note,
    inject_additional_metadata,
    sanitize_nota_clinica,
    sanitize_demographic_context,
    ExamType,
    ExamRegion,
    ExamModality,
    ClassifierSource,
)
# ── v1.1 imports (ai-pipeline-v1.1) ─────────────────────────────────────────
from app.ai.auditor import run_audit, should_audit
from core.constants import NOMBRES_ES
from app.ai.pipeline_context import PipelineContext
from app.ai.quality_gate import run_quality_gate
from api.auth.dependencies import get_current_user
from api.auth.models import UserPayload
from app.core.config import settings
from app.core.database import get_db_pool
from app.informes import db as informes_db
from infrastructure.pagos.db import (
    get_estado_creditos,
    get_inscripcion_oneclick_any,
    get_metodo_pago,
    descontar_credito,
)
from infrastructure.pagos.mp.service import cobrar_scan_automatico
from infrastructure.pagos.tbk.service import cobrar_scan_oneclick

logger = logging.getLogger("imagiax.ai.router")

router = APIRouter(tags=["ai"])

# v59: async informe jobs — return 202 + job_id when generation exceeds gateway seconds
_INFORME_ASYNC_JOBS: dict[str, dict[str, Any]] = {}
IMAGIAX_INFORME_GATEWAY_SEC = 25.0


# ============================================================
# Clean URL redirect endpoint (no auth required)
# ============================================================


@router.get("/estudio/{short_id}", include_in_schema=False)
async def abrir_estudio_viewer(
    short_id: str,
    pool: asyncpg.Pool = Depends(get_db_pool),
) -> RedirectResponse:
    """URL limpia → redirect al viewer OHIF con StudyInstanceUIDs."""
    data = await informes_db.get_study_by_short_id(pool, short_id)
    if not data:
        raise HTTPException(status_code=404, detail="Estudio no encontrado")
    uid = data["study_instance_uid"]
    return RedirectResponse(
        url=f"/?StudyInstanceUIDs={uid}",
        status_code=302,
    )


# ── Modality display names ────────────────────────────────────────────────────
_MODALITY_NAMES: dict[str, str] = {
    "DX": "Radiografía",
    "CR": "Radiografía",
    "CT": "Tomografía Computada",
    "MR": "Resonancia Magnética",
    "US": "Ecografía",
    "MG": "Mamografía",
}

# Templates that use two-pass generation (observe first, report second).
# Single-pass stays for chest (ChestSignal gate + ClinicalAuditEngine already handle it)
# and brain CT / generic fallback (no evidence of regression benefit).
_TEMPLATES_TWO_PASS: frozenset[str] = frozenset({
    "chest_xray",
    "lumbar_spine_xray",
    "msk_xray",
})

# Templates that use three-pass deterministic generation (v29).
# Pelvis/hip gets quantitative Tönnis grading via Pixel Inspector + pure-Python classifier.
_TEMPLATES_THREE_PASS_PELVIS: frozenset[str] = frozenset({
    "pelvis_hip_xray",
})

# Metadata key used to persist analysis results in Orthanc
_IMAGIAX_LAST_RESULT_KEY = "2048"


# ============================================================
# Pydantic request models
# ============================================================


class InstanciaRequest(BaseModel):
    """Analyse a specific DICOM instance."""

    orthanc_instance_id: str | None = Field(default=None, max_length=128)
    sop_instance_uid: str | None = Field(default=None, max_length=128, pattern=r"^\d+(\.\d+)*$")

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_one_id(self) -> "InstanciaRequest":
        if not self.orthanc_instance_id and not self.sop_instance_uid:
            raise ValueError("Debes enviar orthanc_instance_id o sop_instance_uid")
        return self


class SerieRequest(BaseModel):
    """Analyse all (or the first N) instances of a series."""

    series_instance_uid: str = Field(max_length=128, pattern=r"^\d+(\.\d+)*$")
    max_instances: int = Field(default=10, ge=1, le=200)

    model_config = ConfigDict(extra="forbid")


class EstudioRequest(BaseModel):
    """Analyse all (or the first N) instances of a study."""

    study_instance_uid: str = Field(max_length=128, pattern=r"^\d+(\.\d+)*$")
    max_instances: int = Field(default=20, ge=1, le=500)
    persistir_resultado: bool = True

    model_config = ConfigDict(extra="forbid")


class GenerarInformeRequest(BaseModel):
    """Generate a full medical report: AI analysis + LLM writing + DB persistence."""

    study_instance_uid: str = Field(max_length=128, pattern=r"^\d+(\.\d+)*$")
    max_instances: int = Field(default=50, ge=1, le=200)
    # ── v1.1 fields (ai-pipeline-v1.1, T-07) ─────────────────────────────────
    nota_clinica: str | None = Field(default=None, max_length=500)
    solicitar_auditoria: bool = Field(default=False)
    version_pipeline: str | None = Field(default=None, max_length=10)
    # ── v15.1: benchmark / testing flag ──────────────────────────────────────
    # When True, skips the deduplication check and forces a full re-run of the
    # pipeline even if a report already exists for this study.
    # Intended for benchmarking and regression testing only — not for production use.
    force_regenerate: bool = Field(default=False)

    model_config = ConfigDict(extra="forbid")


class GuardarDecisionRequest(BaseModel):
    """Record the physician's decision on a generated report."""

    informe_id: str = Field(max_length=128)
    estado: str  # aceptado | rechazado | editado_aceptado
    informe_final: str = Field(max_length=50_000)
    notas_rechazo: str | None = Field(default=None, max_length=2_000)

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_estado(self) -> "GuardarDecisionRequest":
        allowed = {"aceptado", "rechazado", "editado_aceptado"}
        if self.estado not in allowed:
            raise ValueError(f"estado debe ser uno de: {', '.join(sorted(allowed))}")
        return self


# ============================================================
# Helpers
# ============================================================


def _get_orthanc_client(request: Request) -> httpx.AsyncClient:
    """Return the shared Orthanc HTTP client stored in app.state."""
    client = getattr(request.app.state, "orthanc", None)
    if client is None:
        raise HTTPException(
            status_code=503,
            detail="Cliente Orthanc no inicializado. Verifica lifespan.",
        )
    return client


def _get_pacs_client(request: Request) -> httpx.AsyncClient:
    """Return the shared DICOMweb proxy HTTP client stored in app.state."""
    client = getattr(request.app.state, "pacs", None)
    if client is None:
        raise HTTPException(
            status_code=503,
            detail="Cliente PACS no inicializado. Verifica lifespan.",
        )
    return client


def _get_model(request: Request) -> Any:
    """Return the loaded PyTorch model stored in app.state."""
    model = getattr(request.app.state, "model", None)
    if model is None:
        raise HTTPException(
            status_code=503,
            detail="Modelo IA no inicializado. Verifica lifespan.",
        )
    return model


def _classify_view(inst: dict[str, Any]) -> str:
    """Return 'PA', 'LAT', or 'OTHER' based on DICOM tags."""
    vp = (inst.get("dicom", {}).get("ViewPosition") or "").upper().strip()
    if vp in ("PA", "AP"):
        return "PA"
    if vp in ("LL", "RL", "LAT", "LATERAL"):
        return "LAT"
    sd = (inst.get("dicom", {}).get("SeriesDescription") or "").upper()
    if any(kw in sd for kw in (" PA", "PA ", "PA-", "POSTER", "ANTERO")):
        return "PA"
    if sd.endswith(" PA") or sd == "PA":
        return "PA"
    if any(kw in sd for kw in ("LAT", "LATER", " LL")):
        return "LAT"
    return "OTHER"


# Projection label vocabulary for Gemini image context
_PROJECTION_LABELS: dict[str, str] = {
    # Standard frontal views
    "AP":  "Proyección Anteroposterior (AP) — SOBERANA para espacio articular y clasificación Tönnis",
    "PA":  "Proyección Posteroanterior (PA) — SOBERANA para espacio articular y clasificación Tönnis",
    # Lateral / oblique
    "LL":  "Proyección Lateral (LL) — solo morfología, NO para cuantificar espacio articular",
    "RL":  "Proyección Lateral (RL) — solo morfología, NO para cuantificar espacio articular",
    "LAT": "Proyección Lateral — solo morfología, NO para cuantificar espacio articular",
    # Hip-specific axial / frog-leg views
    "LLAT":       "Proyección Axial/Lowenstein — SOLO morfología FAI (cam/pincer); PROHIBIDO usar para clasificar Tönnis",
    "FROG":       "Proyección Frog-Leg — SOLO morfología FAI (cam/pincer); PROHIBIDO usar para clasificar Tönnis",
    "LAUENSTEIN": "Proyección Lauenstein — SOLO morfología FAI (cam/pincer); PROHIBIDO usar para clasificar Tönnis",
    "AXIAL":      "Proyección Axial — SOLO morfología FAI (cam/pincer); PROHIBIDO usar para clasificar Tönnis",
    "DUNN":       "Proyección Dunn — SOLO morfología FAI (cam/pincer); PROHIBIDO usar para clasificar Tönnis",
}

_SERIES_DESC_AXIAL_KW = ("AXIAL", "LOWENSTEIN", "LAUENSTEIN", "FROG", "DUNN", "LEG", "OBLIQ")


def _get_projection_label(inst: dict[str, Any], index: int) -> str:
    """Return a human-readable, authority-annotated label for a DICOM instance.

    Extracts ViewPosition and SeriesDescription from the instance DICOM dict
    and returns a label string that conveys both the projection name and its
    diagnostic authority (e.g. sovereign for joint-space vs morphology-only).
    Falls back to a generic numbered label when classification is ambiguous.

    Args:
        inst: Analyzed instance dict containing a 'dicom' sub-dict.
        index: 1-based image index (used in the fallback label).

    Returns:
        A Spanish label string ready to be embedded in the Gemini prompt.
    """
    dicom = inst.get("dicom", {})
    vp = (dicom.get("ViewPosition") or "").upper().strip()
    sd = (dicom.get("SeriesDescription") or "").upper().strip()

    # 1. Direct ViewPosition match
    if vp in _PROJECTION_LABELS:
        return _PROJECTION_LABELS[vp]

    # 2. Infer axial from SeriesDescription keywords
    if any(kw in sd for kw in _SERIES_DESC_AXIAL_KW):
        return _PROJECTION_LABELS["AXIAL"]

    # 3. Infer AP/PA from SeriesDescription
    if any(kw in sd for kw in ("ANTERO", " AP", "AP ", "AP-")):
        return _PROJECTION_LABELS["AP"]
    if any(kw in sd for kw in ("POSTER", " PA", "PA ", "PA-")):
        return _PROJECTION_LABELS["PA"]
    if any(kw in sd for kw in ("LAT", "LATER", " LL")):
        return _PROJECTION_LABELS["LAT"]

    # 4. Fallback — generic
    return f"Imagen {index} — proyección no especificada en DICOM"


def _formatear_descripcion_cobro(
    patient_name: str | None, study_date: str | None
) -> str:
    nombre_fmt = "Paciente"
    if patient_name:
        parts = patient_name.split("^")
        apellidos = parts[0].strip().title() if parts else ""
        nombres = parts[1].strip().title() if len(parts) > 1 else ""
        if nombres and apellidos:
            nombre_fmt = f"{nombres.split()[0]} {apellidos}"
        elif apellidos:
            nombre_fmt = apellidos
    return f"Informe IA — {nombre_fmt}"


# ============================================================
# Parallelization helpers (P5)
# ============================================================


async def _download_dicom_data(
    orthanc: httpx.AsyncClient,
    oid: str,
) -> tuple[str, bytes, dict[str, Any]]:
    """Download DICOM bytes and tags concurrently for a single instance.

    Returns ``(orthanc_instance_id, dicom_bytes, tags)`` so callers can
    identify which instance each result belongs to.

    Raises:
        HTTPException: Propagated from ``get_instance_dicom_bytes`` or
            ``get_instance_tags`` (e.g. 404 if the instance is not found).
    """
    dicom_bytes, tags = await asyncio.gather(
        get_instance_dicom_bytes(orthanc, oid),
        get_instance_tags(orthanc, oid),
    )
    return oid, dicom_bytes, tags


async def _analyze_from_bytes(
    oid: str,
    dicom_bytes: bytes,
    tags: dict[str, Any],
    model: Any,
    device: "torch.device",
) -> dict[str, Any]:
    """Run preprocessing + ResNet50 inference on already-downloaded DICOM data.

    Mirrors the inference half of ``analyze_single_instance`` from
    ``dicom_service``, using the pre-fetched ``dicom_bytes`` and ``tags``
    instead of downloading them again.

    PyTorch inference is CPU-bound; it runs inside the default thread-pool
    executor exactly as ``analyze_single_instance`` does — but only ONE call
    is in-flight at a time because callers iterate this function sequentially.

    Args:
        oid: Orthanc internal instance ID (used only to tag the result dict).
        dicom_bytes: Raw DICOM file bytes already fetched from Orthanc.
        tags: DICOM tag dict already fetched from Orthanc.
        model: Loaded NIH ResNet50 model from ``app.state.model``.
        device: PyTorch device for tensor placement.

    Returns:
        Same dict shape as ``analyze_single_instance``.

    Raises:
        HTTPException(422): If DICOM bytes cannot be decoded / preprocessed.
    """
    from fastapi import HTTPException as _HTTPException

    loop = asyncio.get_running_loop()

    # Offload CPU-bound preprocessing (matches analyze_single_instance pattern)
    try:
        tensor = await loop.run_in_executor(
            None,
            lambda: dicom_to_tensor(dicom_bytes, device),
        )
    except Exception as error:
        raise _HTTPException(
            status_code=422,
            detail=f"Could not process DICOM: {error}",
        ) from error

    # Offload CPU-bound inference (sequential — model is NOT thread-safe)
    inference = await loop.run_in_executor(
        None,
        lambda: infer_multilabel(tensor, model),
    )

    from app.ai.prompt_orchestrator import sanitize_dicom_text as _sanitize
    from infrastructure.dicom_service import clean_dicom_tag as _clean_tag

    return {
        "orthanc_instance_id": oid,
        # PERF-C2: Cache raw bytes so generar_informe can pass them to Gemini
        # Vision without re-downloading from Orthanc.  Prefixed with '_' to
        # signal this is an internal pipeline field — not part of the public
        # API response (JSONResponse serialization strips unknown keys).
        "_raw_dicom_bytes": dicom_bytes,
        "dicom": {
            "StudyInstanceUID": _clean_tag(tags.get("StudyInstanceUID")),
            "SeriesInstanceUID": _clean_tag(tags.get("SeriesInstanceUID")),
            "SOPInstanceUID": _clean_tag(tags.get("SOPInstanceUID")),
            "Modality": _clean_tag(tags.get("Modality")),
            "ViewPosition": _clean_tag(tags.get("ViewPosition")),
            "BodyPartExamined": _clean_tag(tags.get("BodyPartExamined")),
            "StudyDate": _clean_tag(tags.get("StudyDate")),
            "PatientID": _clean_tag(tags.get("PatientID")),
            "PatientName": _clean_tag(tags.get("PatientName")),
            "PatientAge": _clean_tag(tags.get("PatientAge")),
            "PatientBirthDate": _clean_tag(tags.get("PatientBirthDate")),
            "AccessionNumber": _clean_tag(tags.get("AccessionNumber")),
            "InstanceNumber": _clean_tag(tags.get("InstanceNumber")),
            "SeriesDescription": _clean_tag(tags.get("SeriesDescription")),
            "PatientSex": _clean_tag(tags.get("PatientSex")) or "No especificado",
            "StudyDescription": _sanitize(_clean_tag(tags.get("StudyDescription")) or ""),
            "RequestedProcedureDescription": _sanitize(
                _clean_tag(tags.get("RequestedProcedureDescription")) or ""
            ),
        },
        **inference,
    }


# ============================================================
# Single-instance / series / study analysis endpoints
# ============================================================


@router.post("/analizar/instancia")
async def analizar_instancia(
    body: InstanciaRequest,
    request: Request,
    current_user: UserPayload = Depends(get_current_user),
) -> JSONResponse:
    orthanc = _get_orthanc_client(request)
    model = _get_model(request)
    device = getattr(request.app.state, "device", torch.device("cpu"))

    if body.orthanc_instance_id:
        instance_id = body.orthanc_instance_id
    else:
        instance_id = await resolve_orthanc_instance_id(
            orthanc, body.sop_instance_uid or ""
        )

    result = await analyze_single_instance(orthanc, instance_id, model, device)
    return JSONResponse(
        {
            "scope": "instancia",
            "resultado": result,
            "advertencia": "Herramienta de apoyo. No reemplaza al médico.",
        }
    )


@router.post("/analizar/serie")
async def analizar_serie(
    body: SerieRequest,
    request: Request,
    current_user: UserPayload = Depends(get_current_user),
) -> JSONResponse:
    orthanc = _get_orthanc_client(request)
    model = _get_model(request)
    device = getattr(request.app.state, "device", torch.device("cpu"))

    instance_ids = await find_instances_by_query(
        orthanc, {"SeriesInstanceUID": body.series_instance_uid}
    )

    analyzed: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    # P5 — Phase 1: parallel download of all DICOM bytes + tags
    _batch = instance_ids[: body.max_instances]
    _download_results = await asyncio.gather(
        *[_download_dicom_data(orthanc, oid) for oid in _batch],
        return_exceptions=True,
    )

    # P5 — Phase 2: sequential ResNet50 inference (model is NOT thread-safe)
    for _idx, _res in enumerate(_download_results):
        if isinstance(_res, Exception):
            _oid = _batch[_idx]
            _detail = _res.detail if isinstance(_res, HTTPException) else str(_res)
            errors.append({"orthanc_instance_id": _oid, "error": _detail})
            continue
        _oid, _dicom_bytes, _tags = _res
        try:
            analyzed.append(await _analyze_from_bytes(_oid, _dicom_bytes, _tags, model, device))
        except HTTPException as err:
            errors.append({"orthanc_instance_id": _oid, "error": str(err.detail)})

    return JSONResponse(
        {
            "scope": "serie",
            "series_instance_uid": body.series_instance_uid,
            "total_encontradas": len(instance_ids),
            "total_analizadas": len(analyzed),
            "max_instances": body.max_instances,
            "resultados": analyzed,
            "errores": errors,
            "advertencia": "Herramienta de apoyo. No reemplaza al médico.",
        }
    )


@router.post("/analizar/estudio")
async def analizar_estudio(
    body: EstudioRequest,
    request: Request,
    current_user: UserPayload = Depends(get_current_user),
) -> JSONResponse:
    orthanc = _get_orthanc_client(request)
    model = _get_model(request)
    device = getattr(request.app.state, "device", torch.device("cpu"))

    instance_ids = await find_instances_by_query(
        orthanc, {"StudyInstanceUID": body.study_instance_uid}
    )

    analyzed: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    # P5 — Phase 1: parallel download of all DICOM bytes + tags
    _batch = instance_ids[: body.max_instances]
    _download_results = await asyncio.gather(
        *[_download_dicom_data(orthanc, oid) for oid in _batch],
        return_exceptions=True,
    )

    # P5 — Phase 2: sequential ResNet50 inference (model is NOT thread-safe)
    for _idx, _res in enumerate(_download_results):
        if isinstance(_res, Exception):
            _oid = _batch[_idx]
            _detail = _res.detail if isinstance(_res, HTTPException) else str(_res)
            errors.append({"orthanc_instance_id": _oid, "error": _detail})
            continue
        _oid, _dicom_bytes, _tags = _res
        try:
            analyzed.append(await _analyze_from_bytes(_oid, _dicom_bytes, _tags, model, device))
        except HTTPException as err:
            errors.append({"orthanc_instance_id": _oid, "error": str(err.detail)})

    # Max probability per pathology across all analysed instances
    max_by_pathology: dict[str, float] = {p: 0.0 for p in NIH_PATOLOGIAS}
    for inst in analyzed:
        labels = inst.get("todas_patologias", {})
        for pat in NIH_PATOLOGIAS:
            val = float(labels.get(pat, {}).get("probabilidad_pct", 0.0))
            if val > max_by_pathology[pat]:
                max_by_pathology[pat] = val

    study_findings = [
        {"patologia": pat, "probabilidad_pct_max": round(prob, 2)}
        for pat, prob in max_by_pathology.items()
        if prob >= (UMBRALES[pat] * 100)
    ]
    study_findings.sort(key=lambda x: x["probabilidad_pct_max"], reverse=True)

    response_payload: dict[str, Any] = {
        "scope": "estudio",
        "study_instance_uid": body.study_instance_uid,
        "total_encontradas": len(instance_ids),
        "total_analizadas": len(analyzed),
        "max_instances": body.max_instances,
        "hallazgos_estudio": study_findings,
        "sin_hallazgos_estudio": len(study_findings) == 0,
        "resultados_instancias": analyzed,
        "errores": errors,
        "advertencia": "Herramienta de apoyo. No reemplaza al médico.",
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }

    if body.persistir_resultado:
        await save_study_result_metadata(orthanc, body.study_instance_uid, response_payload)
        response_payload["persistencia"] = {
            "guardado_en_orthanc": True,
            "metadata_key": _IMAGIAX_LAST_RESULT_KEY,
        }
    else:
        response_payload["persistencia"] = {
            "guardado_en_orthanc": False,
            "metadata_key": _IMAGIAX_LAST_RESULT_KEY,
        }

    return JSONResponse(response_payload)


@router.get("/resultados/estudio/{study_instance_uid}")
async def obtener_resultado_estudio(
    study_instance_uid: str = Path(pattern=r"^\d+(\.\d+)*$"),
    request: Request = None,  # noqa: B008 — FastAPI special injection
    current_user: UserPayload = Depends(get_current_user),
) -> JSONResponse:
    orthanc = _get_orthanc_client(request)
    stored = await get_study_result_metadata(orthanc, study_instance_uid)
    if stored is None:
        raise HTTPException(
            status_code=404,
            detail="No hay resultado IMAGIAX persistido para ese estudio",
        )
    return JSONResponse(
        {
            "scope": "estudio_persistido",
            "study_instance_uid": study_instance_uid,
            "metadata_key": _IMAGIAX_LAST_RESULT_KEY,
            "resultado": stored,
        }
    )


# ============================================================
# Full report generation pipeline — helpers
# ============================================================


async def _check_dedup(
    pool: asyncpg.Pool,
    user_id: str,
    study_uid: str,
    window_secs: int = 120,
    pipeline_version: str | None = None,
) -> dict | None:
    """Check for a recently generated report for the same study and user.

    Returns the existing informe dict if a duplicate is found within
    ``window_secs``, or None if no duplicate exists. Never raises — a DB
    error is logged and treated as no-duplicate so generation can proceed.
    """
    try:
        existing = await informes_db.buscar_informe_reciente(
            pool,
            study_instance_uid=study_uid,
            ventana_segundos=window_secs,
            usuario_id=user_id,
            pipeline_version=pipeline_version,
        )
        return existing or None
    except Exception as exc:
        logger.warning("Error checking deduplication: %s (continuing)", exc)
        return None


async def _verify_payment(
    pool: asyncpg.Pool,
    user: "UserPayload",
    body: "GenerarInformeRequest",
) -> tuple[str | None, dict | None]:
    """Verify the user has a valid payment method available.

    Returns ``(payment_method, payment_detail)`` where ``payment_method`` is
    one of ``"creditos"``, ``"oneclick"``, ``"mp_card"``, or ``None`` (no
    method — will raise 402).

    Raises:
        HTTPException(503): If the credits DB is temporarily unavailable.
        HTTPException(402): If the user has no credits and no payment method.
    """
    try:
        estado_creditos = await get_estado_creditos(pool, usuario_id=user.user_id)
    except Exception as exc:
        logger.error("Payment DB unavailable: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="Sistema de créditos temporalmente no disponible. Intenta en unos segundos.",
        ) from exc

    creditos_disponibles = estado_creditos.get("creditos_disponibles", 0)

    if creditos_disponibles > 0:
        return "creditos", estado_creditos

    # No credits — try payment methods
    oneclick_inscripcion = None
    metodo_pago = None

    try:
        oneclick_inscripcion = await get_inscripcion_oneclick_any(
            pool, usuario_id=user.user_id
        )
    except Exception:
        pass

    if not oneclick_inscripcion:
        try:
            metodo_pago = await get_metodo_pago(pool, usuario_id=user.user_id)
        except Exception:
            pass

    if not oneclick_inscripcion and not metodo_pago:
        raise HTTPException(
            status_code=402,
            detail={
                "error": "Sin créditos disponibles",
                "mensaje": (
                    "Tus creditos se han agotado. "
                    "Adquiere un plan o registra una tarjeta para cobro automatico."
                ),
                "pagar_url": f"{settings.IMAGIAX_BASE_URL}/pagar?trial_agotado=true",
                "opciones_plan": ["pack_individual", "pack_50", "pack_200", "suscripcion_mensual"],
            },
        )

    if oneclick_inscripcion:
        return "oneclick", oneclick_inscripcion
    return "mp_card", metodo_pago


async def _run_ai_analysis(
    request: Request,
    body: "GenerarInformeRequest",
    instance_ids: list[str],
) -> tuple[list[dict], dict, str, dict]:
    """Run the ML/AI analysis pipeline over the study instances.

    Determines whether to run ResNet50 via the ML gate, iterates over
    ``instance_ids`` up to ``body.max_instances``, and aggregates pathology
    scores across all instances.

    Returns:
        ``(analyzed, todas_patologias_resumen, nivel_alerta, first_tags)``

        - ``analyzed``: list of per-instance analysis dicts
        - ``todas_patologias_resumen``: aggregated pathology scores keyed by
          pathology name
        - ``nivel_alerta``: ``"NORMAL"``, ``"BAJA"``, ``"MEDIA"``, or
          ``"ALTA"`` based on the top finding
        - ``first_tags``: DICOM tags dict for the first instance (from the ML
          gate fetch), or ``{}`` if unavailable. Returned so callers can avoid
          a redundant HTTP round-trip to Orthanc.

    Raises:
        HTTPException(422): If no instance could be analysed successfully.
    """
    orthanc = _get_orthanc_client(request)
    model = _get_model(request)
    device = getattr(request.app.state, "device", torch.device("cpu"))

    # ML gate: peek at first instance to decide whether to run ResNet50
    _run_resnet50: bool = True
    _first_tags: dict[str, Any] = {}
    if instance_ids:
        try:
            _raw_first_tags = await get_instance_tags(orthanc, instance_ids[0])
            _first_tags = {k: clean_dicom_tag(v) for k, v in _raw_first_tags.items()}
            _first_ctx = extract_patient_context(_first_tags)
            _bp = (_first_ctx.body_part_examined or "").upper().strip()
            _mod = (_first_ctx.modality_raw or "").upper().strip()
            _is_chest_bp = _bp in (
                "CHEST", "TORAX", "TÓRAX", "THORAX", "PECHO",
                "LUNG", "PULMON", "PULMÓN", "THORACIC",
            )
            _is_xray_mod = _mod in ("DX", "CR")
            if _is_chest_bp and _is_xray_mod:
                _run_resnet50 = True
            elif not _bp and _is_xray_mod:
                _run_resnet50 = True  # ambiguous — safe default: run ResNet50
            elif not _bp and not _mod:
                _run_resnet50 = True  # completely missing metadata — safe default
            else:
                _preliminary = ExamType(
                    region=ExamRegion.GENERIC,
                    modality=ExamModality.GENERIC,
                    confidence=0.5,
                    source=ClassifierSource.RULES,
                    template_id="generic_fallback",
                )
                _run_resnet50 = should_run_resnet50(_preliminary)
        except Exception as _gate_exc:
            logger.warning("ML gate failed, defaulting to run ResNet50: %s", _gate_exc)
            _run_resnet50 = True

    analyzed: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    _batch = instance_ids[: body.max_instances]

    if _run_resnet50:
        # P5 — Path A (ResNet50): parallel download, sequential inference
        # Phase 1: download all DICOM bytes + tags concurrently
        _download_results = await asyncio.gather(
            *[_download_dicom_data(orthanc, oid) for oid in _batch],
            return_exceptions=True,
        )
        # Phase 2: sequential ResNet50 inference (model is NOT thread-safe)
        for _idx, _res in enumerate(_download_results):
            if isinstance(_res, Exception):
                _oid = _batch[_idx]
                _detail = _res.detail if isinstance(_res, HTTPException) else str(_res)
                errors.append({"orthanc_instance_id": _oid, "error": _detail})
                continue
            _oid, _dicom_bytes, _tags = _res
            try:
                analyzed.append(
                    await _analyze_from_bytes(_oid, _dicom_bytes, _tags, model, device)
                )
            except HTTPException as err:
                errors.append({"orthanc_instance_id": _oid, "error": str(err.detail)})
    else:
        # P5 — Path B (tags-only, no inference): fully parallel
        async def _fetch_tags_only(oid: str) -> tuple[str, dict[str, Any]]:
            tags = await get_instance_tags(orthanc, oid)
            return oid, tags

        _tags_results = await asyncio.gather(
            *[_fetch_tags_only(oid) for oid in _batch],
            return_exceptions=True,
        )
        for _idx, _res in enumerate(_tags_results):
            if isinstance(_res, Exception):
                _oid = _batch[_idx]
                _detail = _res.detail if isinstance(_res, HTTPException) else str(_res)
                errors.append({"orthanc_instance_id": _oid, "error": _detail})
                continue
            _oid, _tags = _res
            analyzed.append(
                {
                    "orthanc_instance_id": _oid,
                    "dicom": {
                        "StudyInstanceUID": clean_dicom_tag(_tags.get("StudyInstanceUID")),
                        "SeriesInstanceUID": clean_dicom_tag(_tags.get("SeriesInstanceUID")),
                        "SOPInstanceUID": clean_dicom_tag(_tags.get("SOPInstanceUID")),
                        "Modality": clean_dicom_tag(_tags.get("Modality")),
                        "ViewPosition": clean_dicom_tag(_tags.get("ViewPosition")),
                        "BodyPartExamined": clean_dicom_tag(_tags.get("BodyPartExamined")),
                        "StudyDate": clean_dicom_tag(_tags.get("StudyDate")),
                        "PatientID": clean_dicom_tag(_tags.get("PatientID")),
                        "PatientName": clean_dicom_tag(_tags.get("PatientName")),
                        "PatientAge": clean_dicom_tag(_tags.get("PatientAge")),
                        "PatientBirthDate": clean_dicom_tag(_tags.get("PatientBirthDate")),
                        "AccessionNumber": clean_dicom_tag(_tags.get("AccessionNumber")),
                        "InstanceNumber": clean_dicom_tag(_tags.get("InstanceNumber")),
                        "SeriesDescription": clean_dicom_tag(_tags.get("SeriesDescription")),
                        "PatientSex": clean_dicom_tag(_tags.get("PatientSex")) or "No especificado",
                        "StudyDescription": sanitize_dicom_text(
                            clean_dicom_tag(_tags.get("StudyDescription")) or ""
                        ),
                        "RequestedProcedureDescription": sanitize_dicom_text(
                            clean_dicom_tag(_tags.get("RequestedProcedureDescription")) or ""
                        ),
                    },
                    "nivel_alerta": "NORMAL",
                    "hallazgos": [],
                    "sin_hallazgos": True,
                    "todas_patologias": {},
                }
            )

    if not analyzed:
        raise HTTPException(
            status_code=422,
            detail=(
                "No se pudo analizar ninguna instancia del estudio. "
                "Verifica que el estudio tiene imágenes DX/CR válidas."
            ),
        )

    # Aggregate pathology scores across instances
    max_by_pathology: dict[str, float] = {p: 0.0 for p in NIH_PATOLOGIAS}
    for inst in analyzed:
        for pat in NIH_PATOLOGIAS:
            val = float(
                inst.get("todas_patologias", {}).get(pat, {}).get("probabilidad_pct", 0.0)
            )
            if val > max_by_pathology[pat]:
                max_by_pathology[pat] = val

    study_findings = [
        {"patologia": pat, "probabilidad_pct_max": round(prob, 2)}
        for pat, prob in max_by_pathology.items()
        if prob >= (UMBRALES[pat] * 100)
    ]
    study_findings.sort(key=lambda x: x["probabilidad_pct_max"], reverse=True)

    if not study_findings:
        nivel_alerta = "NORMAL"
    else:
        top = study_findings[0]["probabilidad_pct_max"]
        nivel_alerta = "ALTA" if top >= 80 else ("MEDIA" if top >= 60 else "BAJA")

    todas_patologias_resumen: dict[str, Any] = {
        pat: {
            "probabilidad_pct": round(max_by_pathology[pat], 2),
            "positivo": round(max_by_pathology[pat], 2) >= (UMBRALES[pat] * 100),
        }
        for pat in NIH_PATOLOGIAS
    }

    # Attach study_findings back onto analyzed for callers that need it
    # (stored as a module-level side-effect via return tuple)
    # _first_tags is returned so the caller (generar_informe) can reuse it
    # without issuing a second HTTP request to Orthanc for the same instance.
    return analyzed, todas_patologias_resumen, nivel_alerta, _first_tags


async def _build_template_context(
    classification: "ExamType | None",
    analyzed: list[dict],
    study_findings: list[dict],
    todas_patologias_resumen: dict,
    nivel_alerta: str,
    body: "GenerarInformeRequest",
    first_tags: dict,
    modalidad: str | None,
    patient_age: str | None,
    pipeline_ctx: "PipelineContext | None" = None,
) -> tuple[str, dict]:
    """Select the prompt template and build the context dict for hydration.

    Performs projection detection, age-block construction, and template
    routing based on the exam classification result.

    Returns:
        ``(template_id, template_context)`` ready to pass to
        ``assemble_prompt()``.
    """
    # Build projection text
    pa_insts = [i for i in analyzed if _classify_view(i) == "PA"]
    lat_insts = [i for i in analyzed if _classify_view(i) == "LAT"]
    if pa_insts and lat_insts:
        proyecciones_txt = "posteroanterior y lateral"
    elif pa_insts:
        proyecciones_txt = "posteroanterior"
    elif lat_insts:
        proyecciones_txt = "lateral"
    elif len(analyzed) >= 2:
        proyecciones_txt = "posteroanterior y lateral"
    else:
        proyecciones_txt = "estándar"

    # Build age context block for template routing.
    # DemographicSanitizer: patient_age is precisely calculated from PatientBirthDate+StudyDate
    # (e.g. "10Y11M"), so we override ctx.age with it — otherwise extract_patient_context
    # reads the raw PatientAge tag ("010Y") which is truncated to whole years.
    _validated_age = patient_age or ""
    _age_hint = ""
    if patient_age:
        _age_ctx_for_validation = extract_patient_context(first_tags)
        _age_ctx_for_validation.age = patient_age  # Use our precise calculated age
        _demo_result = sanitize_demographic_context(_age_ctx_for_validation)
        _validated_age = _demo_result.age
        _age_hint = _demo_result.age_hint
        for _w in _demo_result.warnings:
            logger.warning("[DEMOGRAPHIC-SANITIZER][build_template_context] %s", _w)

    _edad_txt_routing = _age_hint  # inject anomaly hint even when age is empty
    if _validated_age:
        # Parse compound age format e.g. "10Y11M", "010Y", "6M", etc.
        _age_compound_match = re.match(r"^(\d+)Y(?:(\d+)M)?$", str(_validated_age).strip().upper())
        if _age_compound_match:
            _edad_num = int(_age_compound_match.group(1))
            _edad_m = int(_age_compound_match.group(2)) if _age_compound_match.group(2) else 0
            # Build human-readable age label in Spanish
            if _edad_num == 0 and _edad_m > 0:
                _edad_label: str | None = f"{_edad_m} meses"
            elif _edad_num == 0:
                _edad_label = None  # 0Y sin meses confirmados — no inyectar edad
            elif _edad_m > 0:
                _edad_label = f"{_edad_num} años {_edad_m} meses"
            else:
                _edad_label = f"{_edad_num} años"
            if _edad_label:
                _edad_txt_routing = (
                    "\nEDAD DEL PACIENTE: " + _edad_label + ". "
                    "Calibra tus hallazgos según la edad: en pacientes jóvenes (<40 años) "
                    "NO esperes elongación aórtica ni ateromatosis; en pacientes mayores (>60 años) "
                    "estos hallazgos son frecuentes y clínicamente relevantes.\n"
                )
                if _edad_num < 18:
                    _edad_txt_routing += (
                        "REGLA PEDIÁTRICA (<18 años): usa lenguaje radiológico pediátrico; evita sesgo "
                        "de adulto (ateromatosis, ectasia aórtica, cambios degenerativos como expectativa "
                        "basal). No sobrediagnostiques cardiomegalia sin evidencia clara y reporta "
                        "conclusiones como 'dentro de límites normales para la edad' cuando corresponda.\n"
                    )

    # Template routing
    _template_id = (
        classification.template_id
        if classification and classification.template_id
        else "generic_fallback"
    )

    _hallazgos_positivos_r = [
        h for h in study_findings if h.get("probabilidad_pct_max", 0) >= 50
    ]

    if _hallazgos_positivos_r:
        _hallazgos_confirmados_r = "\n".join(
            f"  - {NOMBRES_ES.get(h['patologia'], h['patologia'])} (CONFIRMADO — hallazgo principal)"
            for h in _hallazgos_positivos_r
        )
    else:
        _hallazgos_confirmados_r = (
            "  - Ningún hallazgo patológico significativo confirmado "
            "(estudio dentro de límites normales)"
        )

    _is_chest = _template_id == "chest_xray"
    _is_brain_ct = _template_id == "brain_ct"

    if _is_chest:
        template_context: dict[str, Any] = {
            "age_block": _edad_txt_routing,
            "ml_findings": _hallazgos_confirmados_r,
            "projections_text": proyecciones_txt,
            "supplementary_findings_block": "",
            "clinical_indication": sanitize_dicom_text(
                (pipeline_ctx.nota_clinica if pipeline_ctx else None)
                or first_tags.get("StudyDescription")
                or first_tags.get("RequestedProcedureDescription")
                or "No proporcionada"
            ),
        }
    elif _is_brain_ct:
        _contrast_raw = (
            first_tags.get("ContrastBolusAgent")
            or first_tags.get("ContrastBolus")
            or ""
        )
        _contrast_info = f"con contraste ({_contrast_raw})" if _contrast_raw else "sin contraste"
        template_context = {
            "age_block": _edad_txt_routing,
            "projections_text": proyecciones_txt,
            "body_part_specific": first_tags.get("BodyPartExamined") or "No especificado",
            "clinical_indication": sanitize_dicom_text(
                (pipeline_ctx.nota_clinica if pipeline_ctx else None)
                or first_tags.get("StudyDescription")
                or first_tags.get("RequestedProcedureDescription")
                or "No proporcionada"
            ),
            "contrast_info": _contrast_info,
        }
    else:
        _mod_raw = (
            classification.modality.value.upper() if classification else modalidad or ""
        ).upper()
        _modality_text = _MODALITY_NAMES.get(_mod_raw, "Estudio de imagen")
        template_context = {
            "age_block": _edad_txt_routing,
            "modality_text": _modality_text,
            "body_part_text": first_tags.get("BodyPartExamined") or "No especificado",
            "clinical_indication": sanitize_dicom_text(
                (pipeline_ctx.nota_clinica if pipeline_ctx else None)
                or first_tags.get("StudyDescription")
                or first_tags.get("RequestedProcedureDescription")
                or "No proporcionada"
            ),
            "projections_text": proyecciones_txt,
        }
        if _template_id not in ("generic_fallback",):
            # BodyPartExamined DICOM tag may have encoding issues (ISO-8859-1 ñ → '?')
            # Primary lookup: DICOM tag. Secondary: classification.region.value (always clean).
            _bp_raw = (first_tags.get("BodyPartExamined") or "").upper()
            _region_raw = (classification.region.value.upper() if classification else "")
            _BP_ES = {
                "WRIST": "muñeca", "WRISTRIGHT": "muñeca derecha", "WRISTLEFT": "muñeca izquierda",
                "HAND": "mano", "HANDRIGHT": "mano derecha", "HANDLEFT": "mano izquierda",
                "KNEE": "rodilla", "KNEERIGHT": "rodilla derecha", "KNEELEFT": "rodilla izquierda",
                "SHOULDER": "hombro", "SHOULDERRIGHT": "hombro derecho", "SHOULDERLEFT": "hombro izquierdo",
                "ANKLE": "tobillo", "ANKLERIGHT": "tobillo derecho", "ANKLELEFT": "tobillo izquierdo",
                "ELBOW": "codo", "ELBOWRIGHT": "codo derecho", "ELBOWLEFT": "codo izquierdo",
                "FOOT": "pie", "FOOTRIGHT": "pie derecho", "FOOTLEFT": "pie izquierdo",
                "HIP": "cadera", "HIPRIGHT": "cadera derecha", "HIPLEFT": "cadera izquierda",
            }
            template_context["body_part_specific"] = (
                _BP_ES.get(_bp_raw)
                or _BP_ES.get(_region_raw)
                or first_tags.get("BodyPartExamined")
                or "No especificado"
            )

    return _template_id, template_context


async def _handle_billing(
    pool: asyncpg.Pool,
    user: "UserPayload",
    payment_method: str | None,
    payment_detail: dict | None,
    patient_name: str | None,
    study_date: str | None,
    patient_id: str | None = None,
) -> dict:
    """Execute the billing step based on the resolved payment method.

    Handles credit deduction, Oneclick charges, and MercadoPago charges.
    Returns a ``cobro_info`` dict with keys:
    ``estado``, ``metodo``, ``monto_clp``, ``ref_id``, ``error``,
    ``creditos_restantes``, ``pago_automatico``.

    Never raises — billing failures are recorded in the returned dict so the
    caller can still return the generated report to the user.
    """
    precio_scan = settings.IMAGIAX_PRECIO_SCAN
    creditos_restantes: int | None = None
    cobro_estado: str = "pendiente"
    cobro_metodo: str | None = None
    cobro_monto_clp: int | None = None
    cobro_ref_id: str | None = None
    cobro_error: str | None = None
    pago_automatico: dict[str, Any] | None = None

    if payment_method == "creditos":
        try:
            await descontar_credito(pool, usuario_id=user.user_id)
            estado_post = await get_estado_creditos(pool, usuario_id=user.user_id)
            creditos_restantes = estado_post.get("creditos_disponibles")
            cobro_estado = "pagado"
            cobro_metodo = "creditos"
            cobro_monto_clp = 0
        except Exception as exc:
            logger.warning("Error deducting credit: %s", exc)
            cobro_estado = "fallido"
            cobro_metodo = "creditos"
            cobro_error = "Error interno al descontar crédito"

    elif payment_method == "oneclick" and payment_detail:
        desc = _formatear_descripcion_cobro(patient_name, study_date)
        aprobado, buy_order, error_msg = await cobrar_scan_oneclick(
            pool=pool,
            username=payment_detail["username"],
            tbk_user=payment_detail["tbk_user"],
            amount=precio_scan,
            descripcion=desc[:255],
            patient_id=patient_id,
        )
        pago_automatico = {
            "estado": "approved" if aprobado else "rejected",
            "metodo": "oneclick",
            "buy_order": buy_order,
            "monto_clp": precio_scan,
            "error": error_msg,
        }
        cobro_metodo = "oneclick"
        cobro_monto_clp = precio_scan
        cobro_ref_id = buy_order
        if aprobado:
            cobro_estado = "pagado"
        else:
            cobro_estado = "fallido"
            cobro_error = error_msg or "Cobro Oneclick rechazado"

    elif payment_method == "mp_card" and payment_detail:
        desc = _formatear_descripcion_cobro(patient_name, study_date)
        aprobado, payment_id, error_msg = await cobrar_scan_automatico(
            pool=pool,
            mp_customer_id=payment_detail["mp_customer_id"],
            mp_card_id=payment_detail["mp_card_id"],
            mp_card_brand=payment_detail.get("card_brand", ""),
            descripcion=desc[:255],
            patient_id=patient_id,
        )
        pago_automatico = {
            "estado": "approved" if aprobado else "rejected",
            "metodo": "mp_card",
            "payment_id": payment_id,
            "monto_clp": precio_scan,
            "error": error_msg,
        }
        cobro_metodo = "mp_card"
        cobro_monto_clp = precio_scan
        cobro_ref_id = payment_id
        if aprobado:
            cobro_estado = "pagado"
        else:
            cobro_estado = "fallido"
            cobro_error = error_msg or "Cobro MercadoPago rechazado"

    else:
        cobro_estado = "no_aplica"
        cobro_error = "Sin metodo de cobro disponible al momento del analisis"

    return {
        "estado": cobro_estado,
        "metodo": cobro_metodo,
        "monto_clp": cobro_monto_clp,
        "ref_id": cobro_ref_id,
        "error": cobro_error,
        "creditos_restantes": creditos_restantes,
        "pago_automatico": pago_automatico,
    }


# ============================================================
# Full report generation pipeline
# ============================================================


@router.post("/generar/informe")
async def generar_informe(
    body: GenerarInformeRequest,
    request: Request,
    pool: asyncpg.Pool = Depends(get_db_pool),
    current_user: UserPayload = Depends(get_current_user),
) -> JSONResponse:
    """
    Full pipeline:
      0) Deduplication check
      1) Payment/credits verification
      2) AI analysis of DICOM study
      3) LLM report generation via Gemini Vision
      4) Charge / credit deduction
      5) Persist report + billing state to PostgreSQL
      6) Return report (always — even on billing failure)
    """
    orthanc = _get_orthanc_client(request)

    # ── v1.1: Resolve pipeline version (must be before dedup check) ──────────
    # Per-request override takes precedence; falls back to global settings default.
    _effective_version = (body.version_pipeline or settings.PIPELINE_VERSION).strip()

    # UX-003: force_regenerate is for benchmarking/admin only — silently disable for medicos
    _force_regen = body.force_regenerate and current_user.rol in ("admin", "superadmin")
    if body.force_regenerate and not _force_regen:
        logger.warning(
            "force_regenerate ignorado — usuario %s tiene rol %s (no autorizado)",
            current_user.user_id[:8] + "...",
            current_user.rol,
        )

    # ── Step 0: Deduplication ────────────────────────────────────────────────
    existing = None if _force_regen else await _check_dedup(
        pool, current_user.user_id, body.study_instance_uid, pipeline_version=_effective_version
    )
    if existing:
        logger.info(
            "Duplicate report found for study %s (ID: %s). Returning existing.",
            body.study_instance_uid,
            existing["id"],
        )
        return JSONResponse(
            {
                "informe_id": existing["id"],
                "informe_texto": existing["informe_generado"],
                "ai_resultados": {
                    "nivel_alerta": existing["ai_nivel_alerta"],
                    "hallazgos_estudio": existing["ai_hallazgos"],
                    "todas_patologias": existing["ai_todas_patologias"],
                },
                "modelo_llm": existing["modelo_llm"],
                "cobro_info": {
                    "estado": existing["cobro_estado"],
                    "metodo": existing["cobro_metodo"],
                    "error": existing["cobro_error"],
                },
                "pipeline_version": existing.get("pipeline_version", "1.0"),
                "duplicado": True,
                "advertencia": "Este informe ya fue generado anteriormente. Se retorna el existente.",
            }
        )

    # ── Step 1: Verify payment capacity ────────────────────────────────────
    payment_method, payment_detail = await _verify_payment(pool, current_user, body)
    async def _run_informe_pipeline() -> dict[str, Any]:
        """Execute steps 2–6 of informe generation (after payment verified)."""
        _use_v11 = _effective_version == "1.1"

        # ── v1.1: Create PipelineContext ─────────────────────────────────────────
        _pipeline_ctx: PipelineContext | None = None
        if _use_v11:
            _pipeline_ctx = PipelineContext(
                version="1.1",
                solicitar_auditoria=False,
            )
            # Sanitize and attach clinical note (T-07)
            if body.nota_clinica:
                _sanitized_note = sanitize_nota_clinica(body.nota_clinica, pipeline_id=_pipeline_ctx.pipeline_id)
                _pipeline_ctx.nota_clinica = _sanitized_note
            logger.info(
                "[PIPELINE:%s][PHASE:init] v1.1 pipeline started with nota_clinica=%s solicitar_auditoria=%s",
                _pipeline_ctx.pipeline_id,
                bool(_pipeline_ctx.nota_clinica),
                _pipeline_ctx.solicitar_auditoria,
            )
    
        # ── Step 2: AI analysis ──────────────────────────────────────────────────
        instance_ids = await find_instances_by_query(
            orthanc, {"StudyInstanceUID": body.study_instance_uid}
        )
    
        analyzed, todas_patologias_resumen, nivel_alerta, _first_tags = await _run_ai_analysis(
            request, body, instance_ids
        )
    
        # Derive study-level findings from aggregated pathology data
        study_findings = [
            {"patologia": pat, "probabilidad_pct_max": data["probabilidad_pct"]}
            for pat, data in todas_patologias_resumen.items()
            if data["positivo"]
        ]
        study_findings.sort(key=lambda x: x["probabilidad_pct_max"], reverse=True)
    
        # Extract DICOM metadata from first analysed instance
        dicom_meta = analyzed[0].get("dicom", {})
        patient_id = dicom_meta.get("PatientID")
        patient_name = dicom_meta.get("PatientName")
        study_date = dicom_meta.get("StudyDate")
        modalidad = dicom_meta.get("Modality")
        # FIX: Always prefer precise age from PatientBirthDate+StudyDate when both available.
        # PatientAge DICOM tag (VR=AS) is stored rounded to whole years by most modalities
        # (e.g., 10y11m → "010Y"), which inverts bone age discrepancy in pediatric cases.
        _birth_date_str = (dicom_meta.get("PatientBirthDate") or "").strip()
        _study_date_str = ((study_date or "") if isinstance(study_date, str) else str(study_date or ""))[:8].strip()
        patient_age: str | None = None
        if len(_birth_date_str) == 8 and len(_study_date_str) == 8:
            try:
                from datetime import datetime as _dt
                _bd = _dt.strptime(_birth_date_str, "%Y%m%d")
                _sd = _dt.strptime(_study_date_str, "%Y%m%d")
                _delta_days = (_sd - _bd).days
                _total_months = int(_delta_days / 30.4375)
                _age_y = _total_months // 12
                _age_m = _total_months % 12
                if _age_m > 0:
                    patient_age = f"{_age_y}Y{_age_m}M"  # e.g. "10Y11M"
                else:
                    patient_age = f"{_age_y:03d}Y"        # e.g. "011Y"
            except ValueError:
                pass
        # Fallback to PatientAge tag only if dates unavailable
        if not patient_age:
            patient_age = dicom_meta.get("PatientAge") or None
    
        # Sanitize zero-year ages (DICOM tag bug: "000Y" for adult patients).
        # Storing 0 years would display as invalid age in PDF — set to None instead.
        if patient_age:
            _age_zero_m = re.match(r"^0+Y(?:0+M)?$", str(patient_age).strip().upper())
            if _age_zero_m:
                logger.warning(
                    "[AGE-SANITIZE] patient_age='%s' resolves to 0 years — cleared to None",
                    patient_age,
                )
                patient_age = None
    
        ai_resultados: dict[str, Any] = {
            "scope": "estudio",
            "study_instance_uid": body.study_instance_uid,
            "total_encontradas": len(instance_ids),
            "total_analizadas": len(analyzed),
            "hallazgos_estudio": study_findings,
            "sin_hallazgos_estudio": len(study_findings) == 0,
            "nivel_alerta": nivel_alerta,
            "todas_patologias": todas_patologias_resumen,
            "errores": [],
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        }
    
        # ── Classification pipeline ──────────────────────────────────────────────
        from infrastructure.llm_service import get_gemini_client as _get_gemini_client
    
        # Re-use _first_tags returned by _run_ai_analysis — no second HTTP fetch.
        # (The ML gate already retrieved these tags; see _run_ai_analysis return value.)
        # Safe nota_clinica for classify_exam — works for both v1.0 and v1.1
        _nota_for_classify: str = ""
        if _pipeline_ctx is not None and _pipeline_ctx.nota_clinica:
            _nota_for_classify = _pipeline_ctx.nota_clinica          # already sanitized by pipeline
        elif body.nota_clinica:
            _nota_for_classify = sanitize_nota_clinica(body.nota_clinica)
    
        classification: ExamType | None = None
        try:
            if instance_ids:
                _shadow_ctx = extract_patient_context(_first_tags)
                _gemini_for_classify = (
                    _get_gemini_client() if settings.GEMINI_API_KEY.get_secret_value() else None
                )
                classification = await classify_exam(
                    _shadow_ctx,
                    gemini_client=_gemini_for_classify,
                    nota_clinica=_nota_for_classify,
                )
                logger.info(
                    "[CLASSIFY] region=%s, modality=%s, confidence=%.2f, template_id=%s, source=%s",
                    getattr(getattr(classification, "region", None), "value", None),
                    getattr(getattr(classification, "modality", None), "value", None),
                    getattr(classification, "confidence", 0),
                    getattr(classification, "template_id", None),
                    getattr(getattr(classification, "source", None), "value", None),
                )
        except Exception as _cls_exc:
            logger.warning("[CLASSIFY] Classification failed: %s", _cls_exc)
            classification = None
    
        # Collect DICOM images for Gemini Vision
        pa_insts = [i for i in analyzed if _classify_view(i) == "PA"]
        lat_insts = [i for i in analyzed if _classify_view(i) == "LAT"]
        other_insts = [i for i in analyzed if _classify_view(i) == "OTHER"]
        MAX_GEMINI_IMAGES = 10  # Gemini 2.5 Flash supports ~1M tokens; 10 CXR PNGs is safe
        all_ordered = pa_insts + lat_insts + other_insts
        ordered_for_gemini = all_ordered[:MAX_GEMINI_IMAGES]
        if len(all_ordered) > MAX_GEMINI_IMAGES:
            logger.warning(
                "[GEMINI] Sending %d/%d images (cap=%d)",
                len(ordered_for_gemini), len(all_ordered), MAX_GEMINI_IMAGES,
            )
        else:
            logger.info("[GEMINI] Sending all %d analyzed images to Gemini", len(ordered_for_gemini))
        dicom_images: list[bytes] = []
        image_labels: list[str] = []  # per-image projection labels for Gemini
        for _img_idx, inst in enumerate(ordered_for_gemini, start=1):
            # PERF-C2: Reuse raw bytes cached by _analyze_from_bytes (ResNet50 path).
            # If _raw_dicom_bytes is absent (tags-only path B, or future callers that
            # don't go through _analyze_from_bytes), fall back to a fresh download.
            cached = inst.get("_raw_dicom_bytes")
            if cached is not None:
                dicom_images.append(cached)
                image_labels.append(_get_projection_label(inst, _img_idx))
            else:
                try:
                    dicom_images.append(
                        await get_instance_dicom_bytes(orthanc, inst["orthanc_instance_id"])
                    )
                    image_labels.append(_get_projection_label(inst, _img_idx))
                except Exception:
                    pass
    
        # ── VFY-001: v1.1 Global {timeout}s Timeout ──────────────────────────────
        # Wrap entire v1.1 pipeline (quality gate + generation + auditor) in global
        # timeout. On asyncio.TimeoutError, fall back to v1.0 bare LLM generation.
        _requiere_revision_manual: bool = False
        _informe_es_fallback: bool = False
        # Note: informe_texto will be assigned inside _v11_phases() or in the v1.0 fallback
        informe_texto: str = ""
        # VFY-001 fix: declare fallback-accessible vars in outer scope so timeout
        # handler can safely use them even if _v11_phases() was interrupted early.
        _proyecciones_txt: str = "estándar"
        _custom_sys: str | None = None
        _custom_prompt: str | None = None
        _saccade_patches = []  # v16
    
        async def _v11_phases() -> None:
            nonlocal informe_texto, _informe_es_fallback, _requiere_revision_manual, _use_v11
            nonlocal _proyecciones_txt, _custom_sys, _custom_prompt, _saccade_patches
            # ── v1.1: Quality Gate (Phase 0.5) ───────────────────────────────────────
            # Run BEFORE billing and LLM. On not_analyzable → abort (no charge).
            # On any failure → fail-open (continue as v1).
            _quality_verdict = "adequate"  # type: str — default for v1 path; nonlocal can't have annotated assignment
            if _use_v11 and _pipeline_ctx is not None and dicom_images:
                from infrastructure.llm_service import get_gemini_client as _get_gemini_client_v11
                _pipeline_ctx.start_phase("quality_gate", model=settings.GEMINI_MODEL)
                try:
                    _qg_result = await run_quality_gate(
                        gemini_client=_get_gemini_client_v11(),
                        image_bytes=dicom_images[0],
                        modality=modalidad or "",
                        patient_age=patient_age,
                        patient_sex=dicom_meta.get("PatientSex"),
                        study_date=study_date,
                    )
                    _pipeline_ctx.quality_gate = _qg_result
                    _quality_verdict = _qg_result.verdict
                    _pipeline_ctx.end_phase(
                        "quality_gate",
                        status="ok",
                        payload={"verdict": _qg_result.verdict, "confidence": _qg_result.confidence},
                    )
                    logger.info(
                        "[PIPELINE:%s][PHASE:quality_gate] verdict=%s confidence=%.2f",
                        _pipeline_ctx.pipeline_id,
                        _qg_result.verdict,
                        _qg_result.confidence,
                    )
    
                    # FR-03: not_analyzable → abort pipeline, no charge
                    if _qg_result.verdict == "not_analyzable":
                        logger.warning(
                            "[PIPELINE:%s][PHASE:quality_gate] NOT ANALYZABLE — aborting pipeline. reason=%s",
                            _pipeline_ctx.pipeline_id,
                            _qg_result.reason,
                        )
                        raise HTTPException(
                            status_code=422,
                            detail={
                                "error": "imagen_no_analizable",
                                "mensaje": (
                                    "La imagen no es de calidad suficiente para generar un informe diagnóstico. "
                                    "Por favor, verifica la calidad del estudio DICOM y vuelve a intentarlo."
                                ),
                                "motivo": _qg_result.reason,
                                "pipeline_id": _pipeline_ctx.pipeline_id,
                            },
                        )
    
                    # FR-04: suboptimal → proceed but note the limitation
                    if _qg_result.verdict == "suboptimal":
                        logger.info(
                            "[PIPELINE:%s][PHASE:quality_gate] SUBOPTIMAL — proceeding with limitation note.",
                            _pipeline_ctx.pipeline_id,
                        )
    
                except HTTPException:
                    raise  # Re-raise not_analyzable abort
                except Exception as _qg_exc:
                    logger.warning(
                        "[PIPELINE:%s][PHASE:quality_gate] Failed — fail-open: %s",
                        _pipeline_ctx.pipeline_id if _pipeline_ctx else "N/A",
                        _qg_exc,
                    )
                    _pipeline_ctx.end_phase("quality_gate", status="failed", error=str(_qg_exc))
                    _quality_verdict = "adequate"  # fail-open
    
            # Build template context
            _template_id, _template_context = await _build_template_context(
                classification=classification,
                analyzed=analyzed,
                study_findings=study_findings,
                todas_patologias_resumen=todas_patologias_resumen,
                nivel_alerta=nivel_alerta,
                body=body,
                first_tags=_first_tags,
                modalidad=modalidad,
                patient_age=patient_age,
                pipeline_ctx=_pipeline_ctx,
            )
    
            # Assemble prompt from template
            _custom_sys = None
            _custom_prompt = None
    
            # ── Enrich ai_resultados with routing metadata for ClinicalAuditEngine ──
            # resolve_body_part() uses exam_region and template_id as the primary
            # resolution layers (before DICOM keyword fallback).
            ai_resultados["template_id"] = _template_id
            if classification and classification.region:
                ai_resultados["exam_region"] = classification.region.value
            # ─────────────────────────────────────────────────────────────────────
    
            try:
                _custom_sys, _custom_prompt = assemble_prompt(_template_id, _template_context)
                logger.info("[T4.2] Template assembled: %s", _template_id)
            except (ValueError, KeyError) as _tpl_exc:
                logger.warning(
                    "[T4.2] Template '%s' failed, falling back to generic_fallback: %s",
                    _template_id,
                    _tpl_exc,
                )
                try:
                    _mod_fb = (
                        classification.modality.value.upper() if classification else modalidad or ""
                    ).upper()
                    _custom_sys, _custom_prompt = assemble_prompt(
                        "generic_fallback",
                        {
                            "age_block": _template_context.get("age_block", ""),
                            "modality_text": _MODALITY_NAMES.get(_mod_fb, "Estudio de imagen"),
                            "body_part_text": _first_tags.get("BodyPartExamined") or "No especificado",
                            "clinical_indication": sanitize_dicom_text(
                                (_pipeline_ctx.nota_clinica if _pipeline_ctx else None)
                                or _first_tags.get("StudyDescription")
                                or _first_tags.get("RequestedProcedureDescription")
                                or "No proporcionada"
                            ),
                            "projections_text": _template_context.get("projections_text", "estándar"),
                        },
                    )
                except Exception as _fb_exc:
                    logger.error("[T4.2] Generic fallback also failed: %s", _fb_exc)
                    _custom_sys = None
                    _custom_prompt = None
            except Exception as _asm_exc:
                logger.error("[T4.2] Unexpected template assembly error: %s", _asm_exc)
                _custom_sys = None
                _custom_prompt = None
    
            # ── FIX-2: Inject additional DICOM metadata block ─────────────────────────
            # Runs AFTER assemble_prompt and BEFORE inject_clinical_note, so that
            # the clinical note always appears closest to the prompt body.
            if _custom_prompt is not None:
                _dicom_ctx_for_metadata = extract_patient_context(_first_tags)
                _custom_prompt = inject_additional_metadata(_custom_prompt, _dicom_ctx_for_metadata)
    
            # ── v1.1: Inject clinical note into prompt (T-08) ─────────────────────────
            # If pipeline v1.1 and a sanitized nota_clinica exists, inject it now.
            # If no note (or v1 path), the prompt is unchanged (Scenario 9).
            if _use_v11 and _pipeline_ctx is not None and _pipeline_ctx.nota_clinica:
                if _custom_prompt is not None:
                    _custom_prompt = inject_clinical_note(_custom_prompt, _pipeline_ctx.nota_clinica)
                    logger.info(
                        "[PIPELINE:%s][PHASE:generation] nota_clinica injected (%d chars)",
                        _pipeline_ctx.pipeline_id,
                        len(_pipeline_ctx.nota_clinica),
                    )
    
            # ── v20: Inject classifier signal — Evidence Arbitrator ──────────────────
            # When Lung Opacity >70% or Atelectasis >50%, inject an arbitration block
            # that forces the LLM to actively search those zones before reporting normal.
            # This bypasses the "positivo" flag threshold — raw probabilities are used directly.
            # Only applies to chest_xray template; MSK templates are not affected.
            if _template_id == "chest_xray" and _custom_prompt is not None:
                _CLASSIFIER_THRESHOLDS_V20 = {"Lung Opacity": 70.0, "Atelectasis": 50.0}
                _triggered_v20 = [
                    f"    \u00b7 {pat}: {todas_patologias_resumen[pat]['probabilidad_pct']:.1f}% (umbral: >{thr:.0f}%)"
                    for pat, thr in _CLASSIFIER_THRESHOLDS_V20.items()
                    if pat in todas_patologias_resumen
                    and todas_patologias_resumen[pat].get("probabilidad_pct", 0.0) >= thr
                ]
                if _triggered_v20:
                    _signal_lines_v20 = "\n".join(_triggered_v20)
                    _arbitration_block_v20 = (
                        "\n\n\u2550\u2550\u2550 ALERTA VISI\u00d3N COMPUTACIONAL \u2014 PROTOCOLO DE ARBITRAJE (v21) \u2550\u2550\u2550\n"
                        "El clasificador especializado (>100.000 radiograf\u00edas) detect\u00f3 se\u00f1ales con alta confianza:\n"
                        + _signal_lines_v20
                        + "\n\n\u26a0\ufe0f ZONA OBJETIVO DE ESTA ALERTA: OPACIDADES PARENQUIMATOSAS Y RETROCARD\u00cdACAS.\n"
                        "Esta alerta NO refiere a los hilios pulmonares.\n"
                        "Los hilios se eval\u00faan por separado con el FILTRO DE ARBORIZACI\u00d3N HILIAR habitual\n"
                        "(siempre activo, independiente de esta alerta). Si encontr\u00e1s cambios hiliares,\n"
                        "report\u00e1los con los criterios propios del filtro hiliar \u2014 NO los atribuyas a esta alerta.\n\n"
                        "PROTOCOLO DE B\u00daSQUEDA ACTIVA \u2014 ejecutar ANTES de redactar el informe:\n"
                        "1. Zona retrocard\u00edaca inferior izquierda (proyecci\u00f3n PA y lateral):\n"
                        "   \u00b7 \u00bfHay densificaci\u00f3n tenue que borre el contorno del hemidiafragma izquierdo?\n"
                        "   \u00b7 \u00bfHay bandas horizontales de atelectasia laminar (<5mm de grosor)?\n"
                        "   \u00b7 Examin\u00e1 esta zona CON ATENCI\u00d3N ESPEC\u00cdFICA \u2014 puede estar oculta por la sombra\n"
                        "     card\u00edaca. Una opacidad tenue que persiste al examinar dos veces \u2192 existe.\n"
                        "2. Bases pulmonares bilaterales (zonas parenquimatosas subdiafragm\u00e1ticas):\n"
                        "   \u00b7 \u00bfHay opacidad focal, velamiento o p\u00e9rdida de transparencia en las bases?\n"
                        "3. Campo inferior derecho y regi\u00f3n perihiliar como zona de b\u00fasqueda SECUNDARIA\n"
                        "   (si los hallazgos 1 y 2 son negativos):\n"
                        "   \u00b7 \u00bfHay consolidaci\u00f3n o velamiento en base derecha?\n\n"
                        "REGLA DE ARBITRAJE:\n"
                        "\u00b7 Si encontr\u00e1s evidencia visual parenquimatosa concordante \u2192 report\u00e1la (opacidad,\n"
                        "  atelectasia, consolidaci\u00f3n retrocard\u00edaca o en bases).\n"
                        "\u00b7 Si encontr\u00e1s cambios hiliares \u2192 evalualos con el FILTRO HILIAR (criterios propios),\n"
                        "  NO como consecuencia de esta alerta.\n"
                        "\u00b7 Si tras b\u00fasqueda activa NO encontr\u00e1s evidencia visual parenquimatosa \u2192 reportar\n"
                        "  campos sin opacidades significativas.\n"
                        "\u00b7 Tu evaluaci\u00f3n visual es SOBERANA. Este bloque es una alerta, no una orden.\n"
                        "  Pod\u00e9s rechazar el hallazgo si la imagen no lo muestra.\n"
                        "\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\n"
                    )
                    _custom_prompt = _custom_prompt + _arbitration_block_v20
                    logger.info(
                        "[v21][ARBITRATOR] Classifier signal injected for chest_xray: %s",
                        [s.strip() for s in _triggered_v20],
                    )
    
            # ── Step 3: LLM report generation ────────────────────────────────────────
            _proyecciones_txt = _template_context.get("projections_text", "estándar")
    
            # Universal Saccades — extract high-res ROI patches if template defines them
            from app.ai.saccade_service import SaccadeROI, extract_roi_patches as _extract_roi_patches
            _saccade_patches = []
            _template_raw = get_template(_template_id) if _template_id else None
            _saccade_rois_cfg = _template_raw.get("saccade_rois") if _template_raw else None
            if _saccade_rois_cfg and dicom_images:
                try:
                    _saccade_rois = [SaccadeROI(**roi) for roi in _saccade_rois_cfg]
                    # Pass pixel spacing to saccade service for calibrated mm_per_pixel
                    from infrastructure.dicom_service import get_pixel_spacing_mm as _get_ps
                    try:
                        _ps_row, _ps_col, _ps_src = _get_ps(dicom_images[0])
                        _pixel_spacing_mm = (_ps_row, _ps_col)
                        logger.info("[SACCADE] PixelSpacing: %.4f x %.4f mm (source: %s)", _ps_row, _ps_col, _ps_src)
                    except Exception:
                        _pixel_spacing_mm = None
                    _saccade_patches = _extract_roi_patches(dicom_images[0], _saccade_rois, pixel_spacing_mm=_pixel_spacing_mm)
                    logger.info("[SACCADE] extracted %d patches for template %s", len(_saccade_patches), _template_id)
                    # Extract mm_per_pixel from first patch for three-pass calibration
                    _saccade_mm_per_pixel = 0.0
                    if _saccade_patches:
                        _saccade_mm_per_pixel = _saccade_patches[0].effective_mm_per_pixel
                except Exception as _sac_exc:
                    logger.warning("[SACCADE] extraction failed: %s — falling back to global-image-only mode", _sac_exc)
                    _saccade_patches = []
            _informe_es_fallback = False  # type annotation removed — nonlocal can't have annotated assignment
            if _use_v11 and _pipeline_ctx is not None:
                _pipeline_ctx.start_phase("generation", model=settings.GEMINI_PRO_MODEL)
            # Route MSK/Pelvis/Lumbar templates through two-pass generation.
            # Two-pass separates observation (Pass 1) from interpretation (Pass 2),
            # cutting the hallucination loop that causes Tönnis over-escalation and
            # spurious degenerative findings. Falls back to single-pass on Pass 1 failure.
            _llm_fn = (
                generar_informe_llm_three_pass_pelvis
                if _template_id in _TEMPLATES_THREE_PASS_PELVIS
                else generar_informe_llm_two_pass
                if _template_id in _TEMPLATES_TWO_PASS
                else generar_informe_llm
            )
            logger.info("[v17] LLM generator: %s for template_id=%s", _llm_fn.__name__, _template_id)
            try:
                informe_texto, _informe_es_fallback = await _llm_fn(
                    ai_resultados=ai_resultados,
                    patient_id=patient_id,
                    patient_name=patient_name,
                    study_date=study_date,
                    modalidad=modalidad,
                    dicom_images=dicom_images or None,
                    proyecciones_txt=_proyecciones_txt,
                    patient_age=patient_age,
                    custom_system_instruction=_custom_sys,
                    custom_prompt=_custom_prompt,
                    image_labels=image_labels or None,
                    saccade_patches=_saccade_patches or None,
                    template_id=_template_id,
                )
                if _use_v11 and _pipeline_ctx is not None:
                    _pipeline_ctx.end_phase(
                        "generation",
                        status="ok",
                        payload={"is_fallback": _informe_es_fallback},
                    )
                    logger.info(
                        "[PIPELINE:%s][PHASE:generation] LLM report generated is_fallback=%s",
                        _pipeline_ctx.pipeline_id,
                        _informe_es_fallback,
                    )
            except HTTPException:
                if _use_v11 and _pipeline_ctx is not None:
                    _pipeline_ctx.end_phase("generation", status="failed", error="HTTPException")
                raise
            except Exception as exc:
                if _use_v11 and _pipeline_ctx is not None:
                    _pipeline_ctx.end_phase("generation", status="failed", error=str(exc)[:200])
                logger.exception(
                    "LLM report generation error for study_uid=%s", body.study_instance_uid
                )
                raise HTTPException(
                    status_code=502, detail="Error al generar informe. Intente nuevamente."
                ) from exc
    
            # VFY-008: FR-04 suboptimal quality → prepend limitation note to generated report
            if _use_v11 and _quality_verdict == "suboptimal" and informe_texto:
                _limitation_header = (
                    "⚠️ NOTA DE LIMITACIÓN: Este informe fue generado a partir de imágenes "
                    "de calidad subóptima. Los hallazgos deben interpretarse con cautela y "
                    "se recomienda correlación clínica adicional.\n\n"
                )
                informe_texto = _limitation_header + informe_texto
                logger.info(
                    "[PIPELINE:%s][PHASE:generation] Suboptimal quality limitation note prepended.",
                    _pipeline_ctx.pipeline_id if _pipeline_ctx else "N/A",
                )
    
            # ── Step 3.5 (v1.1): Selective Auditor (Phase 2) ─────────────────────────
            # Runs after LLM generation and before billing. Always fail-open.
            # FR-09: If audit verdict is blocking ("alerta"), regenerate Phase 1 ONCE
            # using auditor feedback. If second audit still blocks → flag manual review.
            _requiere_revision_manual = False  # type: bool — nonlocal can't have annotated assignment
            if _use_v11 and _pipeline_ctx is not None:
                if should_audit(_pipeline_ctx, modalidad, ai_resultados):
                    _pipeline_ctx.start_phase(
                        "auditor",
                        model=getattr(settings, "GEMINI_PRO_MODEL", "gemini-2.5-pro"),
                    )
                    _image_quality = (
                        _pipeline_ctx.quality_gate.verdict
                        if _pipeline_ctx.quality_gate
                        else "adequate"
                    )
                    _audit_result = None
                    _audit_attempt = 0
                    _MAX_AUDIT_ATTEMPTS = 2
    
                    while _audit_attempt < _MAX_AUDIT_ATTEMPTS:
                        try:
                            # VFY-002: Pass study images to auditor so Gemini Pro can see them
                            _audit_result = await run_audit(
                                ctx=_pipeline_ctx,
                                report_draft=informe_texto,
                                modality=modalidad,
                                ai_resultados=ai_resultados,
                                patient_age=patient_age,
                                patient_sex=None,
                                study_date=study_date,
                                image_quality=_image_quality,
                                image_bytes_list=dicom_images or None,
                            )
                            logger.info(
                                "[PIPELINE:%s][PHASE:auditor] attempt=%d verdict=%s severity=%s",
                                _pipeline_ctx.pipeline_id,
                                _audit_attempt + 1,
                                _audit_result.verdict,
                                _audit_result.severity,
                            )
    
                            # VFY-003: If verdict is blocking, regenerate report with feedback
                            if _audit_result.verdict == "alerta" and _audit_attempt == 0:
                                logger.warning(
                                    "[PIPELINE:%s][PHASE:auditor] BLOCKING verdict on attempt 1 — "
                                    "regenerating report with auditor feedback",
                                    _pipeline_ctx.pipeline_id,
                                )
                                # "Rewrite, don't Append" (Silent Refiner)
                                # Feedback is injected as a SYSTEM-level instruction, never as
                                # visible text in the prompt body. The LLM rewrites the full
                                # report silently. The physician never sees internal audit notes.
                                _silent_refinement = (
                                    "\n\n## REFINAMIENTO SILENCIOSO (instrucción interna)\n"
                                    "El borrador anterior contiene errores clínicos. "
                                    "REESCRIBÍ el informe completo desde cero, corrigiendo o eliminando "
                                    "los hallazgos cuestionados. "
                                    "NUNCA menciones al Auditor ni incluyas sus comentarios en el informe final. "
                                    "NUNCA uses frases como 'NOTA DE AUDITORIA', 'auditor', 'revisión interna' "
                                    "ni similares. El resultado debe ser un informe médico limpio y definitivo.\n"
                                    "Correcciones requeridas:\n"
                                    + "\n".join(f"- {obs}" for obs in _audit_result.observations)
                                    + (
                                        f"\nGuía clínica: {_audit_result.recommendation}"
                                        if _audit_result.recommendation else ""
                                    )
                                )
                                # Inject only into system instruction — prompt stays clean
                                _regen_sys = (_custom_sys or "") + _silent_refinement
                                _regen_prompt = _custom_prompt  # prompt unchanged
                                try:
                                    _pipeline_ctx.start_phase("auditor_regen", model=settings.GEMINI_MODEL)
                                    informe_texto, _ = await generar_informe_llm(
                                        ai_resultados=ai_resultados,
                                        patient_id=patient_id,
                                        patient_name=patient_name,
                                        study_date=study_date,
                                        modalidad=modalidad,
                                        dicom_images=dicom_images or None,
                                        proyecciones_txt=_proyecciones_txt,
                                        patient_age=patient_age,
                                        custom_system_instruction=_regen_sys,
                                        custom_prompt=_regen_prompt,
                                        image_labels=image_labels or None,
                                        saccade_patches=_saccade_patches or None,
                                    )
                                    _pipeline_ctx.end_phase("auditor_regen", status="ok")
                                    logger.info(
                                        "[PIPELINE:%s][PHASE:auditor] Report regenerated with auditor feedback",
                                        _pipeline_ctx.pipeline_id,
                                    )
                                except Exception as _regen_exc:
                                    _pipeline_ctx.end_phase(
                                        "auditor_regen", status="failed", error=str(_regen_exc)[:200]
                                    )
                                    logger.warning(
                                        "[PIPELINE:%s][PHASE:auditor] Regeneration failed: %s — using original",
                                        _pipeline_ctx.pipeline_id,
                                        _regen_exc,
                                    )
                                _audit_attempt += 1
                                continue  # re-audit regenerated report
    
                            # Non-blocking or second attempt: accept result
                            _pipeline_ctx.audit_result = _audit_result
                            if _audit_result.verdict == "alerta" and _audit_attempt >= 1:
                                # Second audit still blocking → flag for manual review
                                _requiere_revision_manual = True
                                logger.error(
                                    "[PIPELINE:%s][PHASE:auditor] Second audit still BLOCKING — "
                                    "flagging requiere_revision_manual=True",
                                    _pipeline_ctx.pipeline_id,
                                )
                            _pipeline_ctx.end_phase(
                                "auditor",
                                status="ok",
                                payload={
                                    "verdict": _audit_result.verdict,
                                    "severity": _audit_result.severity,
                                    "observations_count": len(_audit_result.observations),
                                    "attempt": _audit_attempt + 1,
                                    "requiere_revision_manual": _requiere_revision_manual,
                                },
                            )
                            break  # done
    
                        except Exception as _audit_exc:
                            logger.warning(
                                "[PIPELINE:%s][PHASE:auditor] Attempt %d exception: %s",
                                _pipeline_ctx.pipeline_id,
                                _audit_attempt + 1,
                                _audit_exc,
                            )
                            if _audit_attempt >= _MAX_AUDIT_ATTEMPTS - 1:
                                logger.error(
                                    "[PIPELINE:%s][PHASE:auditor] All attempts exhausted — fail-open",
                                    _pipeline_ctx.pipeline_id,
                                )
                                _pipeline_ctx.end_phase(
                                    "auditor",
                                    status="failed",
                                    error=str(_audit_exc)[:200],
                                )
                            _audit_attempt += 1
    
                else:
                    logger.debug(
                        "[PIPELINE:%s][PHASE:auditor] skipped (no trigger criteria met)",
                        _pipeline_ctx.pipeline_id,
                    )
                    _pipeline_ctx.end_phase("auditor", status="skipped")
    
        # Sanitize nota_clinica for v1.0 path (v1.1 uses _pipeline_ctx.nota_clinica)
        _v10_nota_clinica: str | None = None
        if not _use_v11 and body.nota_clinica:
            _v10_nota_clinica = sanitize_nota_clinica(body.nota_clinica)
    
        if _use_v11 and _pipeline_ctx is not None:
            try:
                await asyncio.wait_for(
                    _v11_phases(),
                    timeout=float(settings.PIPELINE_GLOBAL_TIMEOUT),
                )
            except asyncio.TimeoutError:
                logger.error(
                    "[PIPELINE:%s] GLOBAL TIMEOUT after %ds — falling back to v1.0 pipeline",
                    _pipeline_ctx.pipeline_id,
                    settings.PIPELINE_GLOBAL_TIMEOUT,
                )
                _use_v11 = False
                _pipeline_ctx.fallback_to_v1 = True
                try:
                    informe_texto, _informe_es_fallback = await generar_informe_llm(
                        ai_resultados=ai_resultados,
                        patient_id=patient_id,
                        patient_name=patient_name,
                        study_date=study_date,
                        modalidad=modalidad,
                        dicom_images=dicom_images or None,
                        proyecciones_txt=_proyecciones_txt,
                        patient_age=patient_age,
                        custom_system_instruction=_custom_sys,
                        custom_prompt=_custom_prompt,
                        nota_clinica=_v10_nota_clinica,
                        image_labels=image_labels or None,
                        saccade_patches=_saccade_patches or None,
                    )
                    logger.info("[PIPELINE:fallback] v1.0 fallback report generated after timeout")
                except HTTPException:
                    raise
                except Exception as _fb_exc:
                    logger.exception("[PIPELINE:fallback] v1.0 fallback failed: %s", _fb_exc)
                    raise HTTPException(
                        status_code=502,
                        detail="Error al generar informe. Intente nuevamente.",
                    ) from _fb_exc
        else:
            # Fallback path: run generation directly (no quality gate, no auditor)
            try:
                informe_texto, _informe_es_fallback = await generar_informe_llm(
                    ai_resultados=ai_resultados,
                    patient_id=patient_id,
                    patient_name=patient_name,
                    study_date=study_date,
                    modalidad=modalidad,
                    dicom_images=dicom_images or None,
                    proyecciones_txt=_proyecciones_txt,
                    patient_age=patient_age,
                    custom_system_instruction=_custom_sys,
                    custom_prompt=_custom_prompt,
                    nota_clinica=_v10_nota_clinica,
                    image_labels=image_labels or None,
                    saccade_patches=_saccade_patches or None,
                )
            except HTTPException:
                raise
            except Exception as exc:
                logger.exception(
                    "LLM report generation error for study_uid=%s", body.study_instance_uid
                )
                raise HTTPException(
                    status_code=502, detail="Error al generar informe. Intente nuevamente."
                ) from exc
    
        # ── Step 4: Billing ───────────────────────────────────────────────────────
        cobro_info = await _handle_billing(
            pool=pool,
            user=current_user,
            payment_method=payment_method,
            payment_detail=payment_detail,
            patient_name=patient_name,
            study_date=study_date,
            patient_id=patient_id,
        )
    
        # S3.5: Billing failure visibility — warn loudly so ops can detect missed charges.
        # The report is ALWAYS delivered (fail-open is correct for medical reports),
        # but the failure must never be silent.
        if cobro_info["estado"] == "fallido":
            logger.warning(
                "[BILLING_FAILURE] study_uid=%s user_id=%s method=%s error=%r "
                "report will be delivered WITHOUT confirmed payment.",
                body.study_instance_uid,
                current_user.user_id,
                cobro_info.get("metodo"),
                cobro_info.get("error"),
            )
        elif cobro_info["estado"] == "no_aplica":
            logger.warning(
                "[BILLING_SKIPPED] study_uid=%s user_id=%s — no payment method resolved. "
                "Report delivered without charge.",
                body.study_instance_uid,
                current_user.user_id,
            )
    
        # ── Step 4.9: Post-auditor safety vetoes ─────────────────────────────────
        # If the auditor regenerated the report after llm_service applied a veto,
        # it may have reintroduced hallucinated findings. Re-apply vetoes here as
        # the final safety net — these run after ALL LLM regenerations.
        if ai_resultados.get("template_id") == "lumbar_spine_xray" and informe_texto:
            _spondy_synonyms = re.search(
                r"(?:espondilolistesis|espondilólisis|desplazamiento\s+(?:anterior|vertebral)\s+.*(?:alto\s+grado|grado\s+(?:III|IV|V)|>?\s*(?:50|75)\s*%)|meyerding\s+(?:III|IV|V)|sombrero\s+de\s+napole[oó]n)",
                informe_texto, re.I
            )
            if _spondy_synonyms:
                logger.warning(
                    "[SAFETY-VETO][LUMBAR-SPONDYLOLISTHESIS] Post-auditor: high-grade synonym '%s' "
                    "detected — applying conservative template",
                    _spondy_synonyms.group(0),
                )
                _indicacion = ai_resultados.get("nota_clinica") or "Columna lumbosacra"
                informe_texto = _LUMBAR_VETO_TEMPLATE.format(indicacion=_indicacion)
    
        # Degenerative lumbar veto: blocks 'espondiloartrosis' (requires ≥2 frank levels)
        # and 'discopatía' (requires ≥1 frank level with DHR evidence). Runs post-auditor
        # to catch hallucinations reintroduced during audit regeneration.
        if ai_resultados.get("template_id") == "lumbar_spine_xray" and informe_texto:
            _imp_m_g = re.search(r"IMPRESI[O\xd3]N\s+DIAGN[O\xd3]STICA", informe_texto, re.I)
            if _imp_m_g:
                _imp_suffix_g = informe_texto[_imp_m_g.start():]
                _triggered_spondy = bool(re.search(r"espondiloartr", _imp_suffix_g, re.I))
                _triggered_disco = bool(re.search(r"discopat", _imp_suffix_g, re.I))
                if _triggered_spondy or _triggered_disco:
                    _narrowed_g: set[str] = set()
                    for _gs in re.split(r"[.;]", informe_texto):
                        if re.search(r"\bsin\b", _gs, re.I):
                            continue
                        if re.search(r"reduc|disminui|estrech|angost|p[e\xe9]rdida.*altura", _gs, re.I):
                            for _gd in re.finditer(r"L[1-5][-\u2013](?:L[2-6]|S\d)", _gs, re.I):
                                _narrowed_g.add(_gd.group(0).upper())
                    # espondiloartrosis requires ≥2 levels; discopatía requires ≥1 frank level
                    _threshold_g = 2 if _triggered_spondy else 1
                    if len(_narrowed_g) < _threshold_g:
                        _trigger_label = "espondiloartr*" if _triggered_spondy else "discopat*"
                        logger.warning(
                            "[SAFETY-VETO][LUMBAR-DEGENERATIVE] Post-auditor: '%s' in IMPRESION but "
                            "%d/%d disc levels with frank narrowing — replacing IMPRESION DIAGNOSTICA",
                            _trigger_label, len(_narrowed_g), _threshold_g,
                        )
                        if _triggered_spondy:
                            _new_g_imp = (
                                "Cambios degenerativos lumbares, predominantemente a nivel L5-S1. "
                                "Los hallazgos no cumplen criterios diagn\xf3sticos para espondiloartrosis "
                                "lumbar establecida (requiere \u22652 niveles con reducci\xf3n discal franca). "
                                "A correlacionar cl\xednicamente."
                            )
                        else:
                            _new_g_imp = (
                                "Cambios degenerativos lumbares incipientes. "
                                "No se identifican criterios radiológicos para discopatía con reducción "
                                "de altura discal franca. A correlacionar clínicamente."
                            )
                        informe_texto = re.sub(
                            r"(IMPRESI[O\xd3]N\s+DIAGN[O\xd3]STICA[ \t]*\n?).*?"
                            r"(?=\n[A-Z\xc1\xc9\xcd\xd3\xda\xd1][A-Z\xc1\xc9\xcd\xd3\xda\xd1 ]{3,}\n|\Z)",
                            lambda m: m.group(1) + _new_g_imp + "\n\n",
                            informe_texto,
                            flags=re.I | re.S,
                            count=1,
                        )
    
        # Pelvis severity language veto: if Tönnis grade in text is ≤ 2, any "severo"/"Grado 3"
        # descriptor in the narrative is a language escalation error. Fires post-auditor so it
        # catches hallucinated severity that survived audit regeneration.
        if ai_resultados.get("template_id") == "pelvis_hip_xray" and informe_texto:
            _tonnis_found = [
                int(m.group(1))
                for m in re.finditer(r"T[oö]nnis\s+Grado\s+(\d)", informe_texto, re.I)
            ]
            _max_tonnis = max(_tonnis_found, default=99)
            _has_sev_lang = bool(
                re.search(
                    r"(?:coxartrosis|pinzamiento|artrosis|estrechamiento)"
                    r"(?:\s+\w+){0,3}\s+sever[ao]"
                    r"|pinzamiento\s+sever[ao]"
                    r"|Grado\s+3\b",
                    informe_texto, re.I,
                )
            )
            if _max_tonnis <= 2 and _has_sev_lang:
                _grade_term_pelv = "moderada" if _max_tonnis == 2 else "leve"
                logger.warning(
                    "[SAFETY-VETO][PELVIS-SEVERITY] Max Tönnis=%d but severity escalation language "
                    "found — replacing with '%s'",
                    _max_tonnis, _grade_term_pelv,
                )
                informe_texto = re.sub(
                    r"((?:coxartrosis|pinzamiento|artrosis|estrechamiento)"
                    r"(?:\s+\w+){0,3}\s+)sever[ao]",
                    lambda m: m.group(1) + _grade_term_pelv,
                    informe_texto, flags=re.I,
                )
                informe_texto = re.sub(
                    r"T[oö]nnis\s+Grado\s+3",
                    f"Tönnis Grado {_max_tonnis}",
                    informe_texto, flags=re.I,
                )
    
        # MSK Sanity Veto: vision classifier says NORMAL but LLM hallucinates fractures/luxations.
        # exam_region is an English enum value (e.g. "wrist") — translate to Spanish for the report.
        _msk_templates = ("msk_xray",)
        if (
            ai_resultados.get("template_id") in _msk_templates
            and ai_resultados.get("sin_hallazgos_estudio", False)
            and ai_resultados.get("nivel_alerta") == "NORMAL"
            and informe_texto
        ):
            _hallucinated_pathology = re.search(
                r"(?:fractura|luxaci[oó]n|subluxaci[oó]n|soluci[oó]n\s+de\s+continuidad|avulsi[oó]n|"
                r"disociaci[oó]n\s+(?:escafo)?lunar|DISI|VISI|inestabilidad\s+(?:carpal|ligament))",
                informe_texto, re.I
            )
            if _hallucinated_pathology:
                _region_raw = ai_resultados.get("exam_region", "extremidad")
                _REGION_ES = {
                    "wrist": "muñeca", "knee": "rodilla", "shoulder": "hombro",
                    "ankle": "tobillo", "elbow": "codo", "foot": "pie",
                    "hand": "mano", "pelvis": "cadera", "chest": "tórax",
                    "lumbar_spine": "columna lumbosacra", "cervical_spine": "columna cervical",
                }
                _region = _REGION_ES.get(_region_raw, _region_raw)
                logger.warning(
                    "[SAFETY-VETO][MSK-HALLUCINATION] Vision=NORMAL but LLM hallucinated '%s' "
                    "(region=%s) — replacing with normal template",
                    _hallucinated_pathology.group(0),
                    _region,
                )
                _indicacion = ai_resultados.get("nota_clinica") or "Estudio radiográfico"
                _body_part = "muñeca" if "muñeca" in (_indicacion or "").lower() or "wrist" in (_indicacion or "").lower() else _region
                informe_texto = f"""TÉCNICA
    Estudio radiográfico de {_body_part} en proyecciones posteroanterior y lateral.
    
    INDICACIÓN CLÍNICA
    {_indicacion}
    
    HALLAZGOS
    - Estructuras óseas de morfología y densidad conservadas, sin soluciones de continuidad cortical que sugieran fracturas agudas.
    - Relaciones articulares conservadas, sin signos de luxación ni subluxación.
    - Arcos carpales de Gilula íntegros (si aplica a muñeca).
    - Espacios articulares de amplitud normal.
    - Partes blandas sin aumento de volumen significativo ni calcificaciones anómalas.
    
    IMPRESIÓN DIAGNÓSTICA
    Estudio radiográfico de {_body_part} dentro de límites normales.
    Sin evidencia de fracturas, luxaciones ni otras alteraciones óseas agudas.
    
    OBSERVACIONES VISUALES ADICIONALES
    Sin observaciones adicionales.
    
    RECOMENDACIONES
    Correlacionar con cuadro clínico. En caso de persistencia de sintomatología, considerar estudio complementario.
    
COMPARACIÓN
Sin estudios previos disponibles para comparación."""
    
        # Cardiomegaly NIH cross-signal veto: if the vision classifier is confident
        # that Cardiomegaly is absent, any LLM mention of cardiomegalia or elevated ICT
        # is a hallucination driven by ruler estimates. Remove it from the narrative.
        if ai_resultados.get("template_id") == "chest_xray" and informe_texto:
            _cardio_data = todas_patologias_resumen.get("Cardiomegaly", {})
            _cardio_positivo = _cardio_data.get("positivo", True)
            _cardio_pct = float(_cardio_data.get("probabilidad_pct", 100.0))
            if not _cardio_positivo and _cardio_pct < 40.0:
                _has_cardio_claim = bool(re.search(r"cardiomegali", informe_texto, re.I))
                _has_ict_claim = bool(re.search(
                    r"ICT\s*(?:aumentado|elevado|>|mayor)\s*(?:de\s+)?0[.,]\d"
                    r"|relaci[oó]n\s+cardio(?:tor[aá]cica)?\s*(?:aumentada|elevada|>|mayor)",
                    informe_texto, re.I,
                ))
                if _has_cardio_claim or _has_ict_claim:
                    logger.warning(
                        "[SAFETY-VETO][CARDIO-NIH] NIH Cardiomegaly=False (%.1f%%) but LLM "
                        "reported cardiomegalia/ICT claim — removing from narrative",
                        _cardio_pct,
                    )
                    # Replace cardiomegalia bullet lines in HALLAZGOS with normal statement
                    informe_texto = re.sub(
                        r"^[-\u2022*]\s*[^\n]*cardiomegali[^\n]*\n?",
                        "- Silueta cardiaca de tama\xf1o y contornos dentro de la normalidad.\n",
                        informe_texto, flags=re.I | re.M, count=1,
                    )
                    # Remove any remaining cardiomegalia lines (duplicates after substitution)
                    informe_texto = re.sub(
                        r"^[-\u2022*]\s*[^\n]*cardiomegali[^\n]*\n?",
                        "",
                        informe_texto, flags=re.I | re.M,
                    )
                    # Remove ICT > 0.xx mentions
                    informe_texto = re.sub(
                        r"ICT\s*(?:aumentado|elevado|>|mayor)\s*(?:de\s+)?0[.,]\d\d[^\n]*\n?",
                        "",
                        informe_texto, flags=re.I,
                    )
                    informe_texto = re.sub(
                        r"relaci[oó]n\s+cardio(?:tor[aá]cica)?\s*(?:aumentada|elevada|>|mayor)[^\n]*\n?",
                        "",
                        informe_texto, flags=re.I,
                    )
                    # Remove cardiomegalia numbered items from IMPRESION
                    informe_texto = re.sub(
                        r"^\d+\.\s*[^\n]*cardiomegali[^\n]*\n?",
                        "",
                        informe_texto, flags=re.I | re.M,
                    )
    
        # ── Step 5: Persist report ────────────────────────────────────────────────
        try:
            informe_id, informe_short_id = await informes_db.guardar_informe(
                pool,
                study_instance_uid=body.study_instance_uid,
                patient_id=patient_id,
                patient_name=patient_name,
                study_date=study_date,
                modalidad=modalidad,
                ai_nivel_alerta=nivel_alerta,
                ai_hallazgos=study_findings,
                ai_todas_patologias=todas_patologias_resumen,
                informe_generado=informe_texto,
                modelo_llm=settings.GEMINI_PRO_MODEL,
                usuario_id=current_user.user_id,
                usuario_email=current_user.email,
                cobro_estado=cobro_info["estado"],
                cobro_metodo=cobro_info["metodo"],
                cobro_monto_clp=cobro_info["monto_clp"],
                cobro_ref_id=cobro_info["ref_id"],
                cobro_error=cobro_info["error"],
                exam_region=classification.region.value if classification else None,
                exam_modality=classification.modality.value if classification else None,
                exam_confidence=classification.confidence if classification else None,
                classification_source=classification.source if classification else None,
                # ── v1.1 fields (T-08) ────────────────────────────────────────────
                nota_clinica=_pipeline_ctx.nota_clinica if _pipeline_ctx else None,
                quality_gate_result=_pipeline_ctx.quality_gate.model_dump() if (
                    _pipeline_ctx and _pipeline_ctx.quality_gate
                ) else None,
                audit_result=_pipeline_ctx.audit_result.model_dump() if (
                    _pipeline_ctx and _pipeline_ctx.audit_result
                ) else None,
                pipeline_version="1.1" if _use_v11 else "1.0",
                pipeline_trace=_pipeline_ctx.to_trace_dict() if _pipeline_ctx else None,
                patient_age=patient_age,
            )
        except Exception as exc:
            logger.exception(
                "Error persisting report for study_uid=%s", body.study_instance_uid
            )
            raise HTTPException(
                status_code=500, detail="Error interno del servidor. Intente nuevamente."
            ) from exc
    
        # ── Step 6: Response ──────────────────────────────────────────────────────
        response_data: dict[str, Any] = {
            "informe_id": informe_id,
            "informe_short_id": informe_short_id,
            "estudio_url": f"/estudio/{informe_short_id}",
            "informe_texto": informe_texto,
            "ai_resultados": ai_resultados,
            "modelo_llm": settings.GEMINI_PRO_MODEL,
            "creditos_restantes": cobro_info["creditos_restantes"],
            "cobro_info": {
                "estado": cobro_info["estado"],
                "metodo": cobro_info["metodo"],
                "monto_clp": cobro_info["monto_clp"],
                "ref_id": cobro_info["ref_id"],
                "error": cobro_info["error"],
            },
            "pago_automatico": cobro_info["pago_automatico"],
            "metodo_cobro": cobro_info["metodo"],
            "advertencia": "Informe generado por IA. Requiere validación del médico.",
            "es_fallback": _informe_es_fallback,
            # ── v1.1 enriched response (T-08) ────────────────────────────────────
            "pipeline_version": "1.1" if _use_v11 else "1.0",
            "pipeline_id": _pipeline_ctx.pipeline_id if _pipeline_ctx else None,
            "calidad_imagen": (
                _pipeline_ctx.quality_gate.verdict
                if (_pipeline_ctx and _pipeline_ctx.quality_gate)
                else None
            ),
            "auditoria_estado": (
                _pipeline_ctx.audit_result.verdict
                if (_pipeline_ctx and _pipeline_ctx.audit_result)
                else None
            ),
            "requiere_revision_manual": _requiere_revision_manual,
        }
    
        if cobro_info["estado"] == "fallido":
            response_data["cobro_pendiente"] = True
            response_data["cobro_mensaje"] = (
                f"El informe se generó correctamente, pero el cobro falló: {cobro_info['error']}. "
                f"Puedes reintentar desde tu panel de pagos o contactar soporte."
            )
            response_data["pagar_url"] = f"{settings.IMAGIAX_BASE_URL}/pagar"
    
        return response_data

    _job_id = uuid.uuid4().hex
    _task = asyncio.create_task(_run_informe_pipeline())
    _INFORME_ASYNC_JOBS[_job_id] = {"task": _task, "user_id": current_user.user_id}
    try:
        # Keep the background job alive when the gateway timeout elapses.
        # Without shield(), wait_for() cancels the task, and polling receives
        # a cancelled job instead of a final result.
        _response_data = await asyncio.wait_for(
            asyncio.shield(_task),
            IMAGIAX_INFORME_GATEWAY_SEC,
        )
    except asyncio.TimeoutError:
        return JSONResponse({"job_id": _job_id, "status": "processing"}, status_code=202)
    _INFORME_ASYNC_JOBS.pop(_job_id, None)
    return JSONResponse(_response_data)


@router.get("/generar/informe/estado/{job_id}")
async def generar_informe_estado(
    job_id: str,
    current_user: UserPayload = Depends(get_current_user),
) -> JSONResponse:
    """Poll async informe job started when POST /generar/informe returned 202 (v59)."""
    rec = _INFORME_ASYNC_JOBS.get(job_id)
    if not rec or rec.get("user_id") != current_user.user_id:
        raise HTTPException(status_code=404, detail="Trabajo no encontrado o expirado.")
    task: asyncio.Task = rec["task"]
    if not task.done():
        return JSONResponse({"job_id": job_id, "status": "processing"}, status_code=202)
    _INFORME_ASYNC_JOBS.pop(job_id, None)

    # NOTE: task.exception()/task.result() can raise CancelledError when a
    # background task is cancelled between polls. If unhandled, Starlette may
    # bubble this into "No response returned." through middleware layers.
    if task.cancelled():
        logger.warning("Informe async job was cancelled job_id=%s", job_id)
        raise HTTPException(
            status_code=503,
            detail="El trabajo de informe fue cancelado. Intente nuevamente.",
        )

    try:
        exc = task.exception()
    except asyncio.CancelledError as cancel_err:
        logger.warning("Informe async job cancelled during exception read job_id=%s", job_id)
        raise HTTPException(
            status_code=503,
            detail="El trabajo de informe fue cancelado. Intente nuevamente.",
        ) from cancel_err

    if exc is not None:
        if isinstance(exc, HTTPException):
            raise exc
        logger.exception("Informe async job failed job_id=%s", job_id)
        raise HTTPException(
            status_code=500,
            detail="Error al generar informe. Intente nuevamente.",
        ) from exc
    try:
        return JSONResponse(task.result())
    except asyncio.CancelledError as cancel_err:
        logger.warning("Informe async job cancelled during result read job_id=%s", job_id)
        raise HTTPException(
            status_code=503,
            detail="El trabajo de informe fue cancelado. Intente nuevamente.",
        ) from cancel_err


# ============================================================
# Physician decision endpoints
# ============================================================


@router.post("/informes/guardar")
async def guardar_decision(
    body: GuardarDecisionRequest,
    pool: asyncpg.Pool = Depends(get_db_pool),
    current_user: UserPayload = Depends(get_current_user),
) -> JSONResponse:
    """Record the physician's final decision on a report."""
    # SEC-IDOR: non-admin users can only update reports they own
    _requiere_propiedad: str | None = None
    if current_user.rol not in ("admin", "superadmin"):
        _requiere_propiedad = current_user.user_id
    try:
        updated = await informes_db.actualizar_decision(
            pool,
            informe_id=body.informe_id,
            estado=body.estado,
            informe_final=body.informe_final,
            notas_rechazo=body.notas_rechazo,
            decision_usuario_id=current_user.user_id,
            decision_usuario_email=current_user.email,
            requiere_propiedad=_requiere_propiedad,
        )
    except Exception as exc:
        logger.exception("Error updating decision for informe_id=%s", body.informe_id)
        raise HTTPException(
            status_code=500, detail="Error interno del servidor. Intente nuevamente."
        ) from exc

    if updated is None:
        raise HTTPException(
            status_code=404,
            detail=f"No se encontró informe con ID: {body.informe_id}",
        )

    return JSONResponse(
        {
            "ok": True,
            "informe_id": updated["id"],
            "estado": updated["estado"],
            "fue_editado": updated["fue_editado"],
            "timestamp_decision": updated["timestamp_decision"],
            "mensaje": f"Informe {updated['estado']} correctamente y registrado con trazabilidad.",
        }
    )


@router.get("/informes/estudio/{study_instance_uid}")
async def informes_por_estudio(
    study_instance_uid: str = Path(pattern=r"^\d+(\.\d+)*$"),
    pool: asyncpg.Pool = Depends(get_db_pool),
    current_user: UserPayload = Depends(get_current_user),
) -> JSONResponse:
    """Return all reports for a study, newest first."""
    usuario_id_filtro = (
        None
        if current_user.rol in ("admin", "superadmin")
        else current_user.user_id
    )
    try:
        informes = await informes_db.get_informes_por_estudio(
            pool, study_instance_uid, usuario_id=usuario_id_filtro
        )
    except Exception as exc:
        logger.exception("Error fetching reports for study_uid=%s", study_instance_uid)
        raise HTTPException(
            status_code=500, detail="Error interno del servidor. Intente nuevamente."
        ) from exc

    return JSONResponse(
        {
            "study_instance_uid": study_instance_uid,
            "total": len(informes),
            "informes": informes,
        }
    )


@router.get("/listado")
async def listado_informes(
    estado: str | None = Query(
        default=None,
        description="Filtro: aceptado | rechazado | editado_aceptado | pendiente",
    ),
    limit: int = Query(default=50, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
    cursor_id: str | None = Query(default=None, pattern=r"^[0-9a-f-]{36}$"),
    pool: asyncpg.Pool = Depends(get_db_pool),
    current_user: UserPayload = Depends(get_current_user),
) -> JSONResponse:
    """Paginated report listing scoped by role."""
    try:
        _scope_uid = (
            None
            if current_user.rol in ("admin", "superadmin")
            else current_user.user_id
        )
        result = await informes_db.get_todos_informes(
            pool,
            estado=estado,
            usuario_id=_scope_uid,
            limit=limit,
            offset=offset,
            cursor_id=cursor_id,
        )
    except Exception as exc:
        logger.exception("Error fetching report listing")
        raise HTTPException(
            status_code=500, detail="Error interno del servidor. Intente nuevamente."
        ) from exc

    return JSONResponse({**result, "filtro_estado": estado})


@router.get("/informes/{informe_id}/pdf")
async def descargar_informe_pdf(
    informe_id: str = Path(..., pattern=r"^[0-9a-f-]{36}$"),
    mode: str = Query(default="download", pattern=r"^(inline|download|preview)$"),
    pool: asyncpg.Pool = Depends(get_db_pool),
    current_user: UserPayload = Depends(get_current_user),
) -> StreamingResponse:
    """Download a medical report as PDF. Scoped: non-admins can only access their own reports."""
    informe = await informes_db.get_informe_por_id(pool, informe_id)
    if not informe:
        raise HTTPException(status_code=404, detail="Informe no encontrado.")
    if current_user.rol not in ("admin", "superadmin"):
        if str(informe.get("usuario_id") or "") != str(current_user.user_id):
            raise HTTPException(status_code=403, detail="No tiene permiso para acceder a este informe.")
    if not (informe.get("informe_generado") or informe.get("informe_final")):
        raise HTTPException(status_code=404, detail="El informe no tiene contenido generado.")
    try:
        return build_pdf_streaming_response(informe, mode=mode)
    except RuntimeError as exc:
        logger.exception("Error generando PDF informe_id=%s", informe_id)
        raise HTTPException(status_code=500, detail="Error interno al generar el PDF.") from exc


# ============================================================
# DICOMweb proxy
# ============================================================


@router.get("/informes/{informe_id}/trace")
async def get_informe_pipeline_trace(
    informe_id: str,
    current_user: UserPayload = Depends(get_current_user),
    pool: asyncpg.Pool = Depends(get_db_pool),
) -> dict:
    """[v1.1] Return the pipeline trace for a report (debug endpoint).

    Returns quality gate result, audit result, and phase timings.
    Only accessible to the report's owner. No PHI is included in the trace.

    Security: auth via get_current_user() + user ownership check in DB query.
    """
    trace = await informes_db.get_pipeline_trace(
        pool,
        informe_id=informe_id,
        usuario_id=current_user.user_id,
    )
    if trace is None:
        raise HTTPException(status_code=404, detail="Informe no encontrado")

    return {
        "ok": True,
        **trace,
    }


@router.api_route(
    "/pacs/dicom-web/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE"],
    include_in_schema=True,
)
async def pacs_dicom_proxy(
    path: str,
    request: Request,
    response: Response,
) -> Response:
    """Authenticated reverse proxy: /ai/pacs/dicom-web/* → Orthanc /dicom-web/*.

    Authentication is done via session cookie (imagiax_session) — not via
    get_current_user() dependency — so that OHIF streaming works correctly
    with the existing cookie-based auth flow.

    Security:
      - decode_token() + is_user_revoked() (SEC-BE-002)
      - sanitize_dicomweb_path() against path traversal (SEC-BE-003)
    """
    from api.auth.service import decode_token, is_user_revoked

    token = request.cookies.get("imagiax_session")
    if not token:
        return JSONResponse(status_code=401, content={"detail": "No autenticado"})

    try:
        proxy_user = decode_token(token)
    except HTTPException:
        return JSONResponse(status_code=401, content={"detail": "Sesión inválida"})

    pool = getattr(request.app.state, "pool", None)
    if pool and await is_user_revoked(pool, proxy_user.user_id):
        return JSONResponse(status_code=401, content={"detail": "Cuenta revocada"})

    try:
        path = sanitize_dicomweb_path(path)
    except HTTPException:
        return JSONResponse(status_code=400, content={"detail": "Invalid DICOMweb path"})

    pacs = getattr(request.app.state, "pacs", None)
    if pacs is None:
        return JSONResponse(status_code=503, content={"detail": "Cliente PACS no inicializado"})

    qs = request.url.query
    upstream_url = f"/dicom-web/{path}"
    if qs:
        upstream_url += f"?{qs}"

    _SKIP_HEADERS = {"host", "authorization", "content-length", "transfer-encoding"}
    fwd_headers = {
        k: v for k, v in request.headers.items() if k.lower() not in _SKIP_HEADERS
    }
    body = await request.body()

    try:
        upstream_req = pacs.build_request(
            method=request.method,
            url=upstream_url,
            headers=fwd_headers,
            content=body if body else None,
        )
        upstream_response = await pacs.send(upstream_req, stream=True)

        async def _stream_and_close():
            try:
                async for chunk in upstream_response.aiter_bytes():
                    yield chunk
            finally:
                await upstream_response.aclose()

        resp_headers = {
            k: v
            for k, v in upstream_response.headers.items()
            if k.lower()
            not in {"transfer-encoding", "content-encoding", "content-length"}
        }
        return StreamingResponse(
            _stream_and_close(),
            status_code=upstream_response.status_code,
            headers=resp_headers,
            media_type=upstream_response.headers.get(
                "content-type", "application/octet-stream"
            ),
        )
    except httpx.TimeoutException:
        return JSONResponse(
            status_code=504, content={"detail": "Timeout conectando a PACS"}
        )
    except httpx.RequestError as exc:
        return JSONResponse(
            status_code=502,
            content={"detail": "Error de conexión a PACS"},
        )
