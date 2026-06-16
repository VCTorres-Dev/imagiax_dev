"""
app.ai.router — AI analysis and DICOMweb proxy endpoints.

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

Thin handlers: business logic lives in app.ai.dicom_service and
app.ai.llm_service.  Payment orchestration uses app.pagos.mp.service and
app.pagos.tbk.service directly (mirrors the monolith).
"""

from __future__ import annotations

import asyncio
import logging
import re
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

from app.ai.dicom_service import (
    NIH_PATOLOGIAS,
    UMBRALES,
    analyze_single_instance,
    clean_dicom_tag,
    dicom_to_tensor,
    infer_multilabel,
)
from app.ai.llm_service import generar_informe_llm
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
    sanitize_dicom_text,
    inject_clinical_note,
    inject_additional_metadata,
    sanitize_nota_clinica,
    validate_patient_age,
    sanitize_demographic_context,
    ExamType,
    ExamRegion,
    ExamModality,
    ClassifierSource,
)
# ── v1.1 imports (ai-pipeline-v1.1) ─────────────────────────────────────────
from app.ai.auditor import run_audit, should_audit
from app.ai.constants import NOMBRES_ES
from app.ai.pipeline_context import PipelineContext, QualityGateResult
from app.ai.quality_gate import run_quality_gate
from app.auth.dependencies import get_current_user
from app.auth.models import UserPayload
from app.core.config import settings
from app.core.database import get_db_pool
from app.informes import db as informes_db
from app.pagos.db import (
    get_estado_creditos,
    get_inscripcion_oneclick_any,
    get_metodo_pago,
    descontar_credito,
)
from app.pagos.mp.service import cobrar_scan_automatico
from app.pagos.tbk.service import cobrar_scan_oneclick

logger = logging.getLogger("imagiax.ai.router")

router = APIRouter(tags=["ai"])


# ============================================================
# Clean URL redirect endpoint (no auth required)
# ============================================================


@router.get("/estudio/{short_id}", include_in_schema=False)
async def abrir_estudio_viewer(
    short_id: str,
    pool: asyncpg.Pool = Depends(get_db_pool),
    current_user: UserPayload = Depends(get_current_user),
) -> RedirectResponse:
    """URL limpia → redirect al viewer OHIF con StudyInstanceUIDs.

    Requires authentication and valid tenant context. Cross-clinic short_ids
    return 404 (no existence leak).
    """
    if current_user.scope is None:
        raise HTTPException(status_code=401, detail="Sesión sin contexto de clínica. Inicie sesión nuevamente.")
    data = await informes_db.get_study_by_short_id(
        pool, short_id,
        clinic_id=current_user.clinic_id,
        scope=current_user.scope,
    )
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
    from app.ai.dicom_service import clean_dicom_tag as _clean_tag

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

    # Build age context block for template routing
    # v15: DemographicSanitizer — replaces raw validate_patient_age call.
    # NOTE: patient_age is already precisely calculated from PatientBirthDate+StudyDate
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
            if _edad_m > 0:
                _edad_label = f"{_edad_num} años {_edad_m} meses"
            else:
                _edad_label = f"{_edad_num} años"
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
            template_context["body_part_specific"] = (
                first_tags.get("BodyPartExamined") or "No especificado"
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
      0) Tenant fail-closed check
      1) Deduplication check
      2) Payment/credits verification
      3) AI analysis of DICOM study
      4) LLM report generation via Gemini Vision
      5) Charge / credit deduction
      6) Persist report + billing state to PostgreSQL
      7) Return report (always — even on billing failure)
    """
    orthanc = _get_orthanc_client(request)

    # ── Tenant fail-closed: reject before any persistence ──────────────────────
    # Old JWTs without clinic/scope claims MUST NOT create PHI rows with clinic_id=NULL.
    # Clinic-scoped users without clinic_id are also rejected (session data incomplete).
    if current_user.scope is None:
        raise HTTPException(
            status_code=401,
            detail="Sesión sin contexto de clínica. Inicie sesión nuevamente.",
        )
    if current_user.scope != "global" and not current_user.clinic_id:
        raise HTTPException(
            status_code=401,
            detail="Sesión sin contexto de clínica. Inicie sesión nuevamente.",
        )

    # ── v1.1: Resolve pipeline version (must be before dedup check) ──────────
    # Per-request override takes precedence; falls back to global settings default.
    _effective_version = (body.version_pipeline or settings.PIPELINE_VERSION).strip()
    _use_v11 = _effective_version == "1.1"

    # ── Step 0: Deduplication ────────────────────────────────────────────────
    existing = None if body.force_regenerate else await _check_dedup(
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

    # ── v1.1: Create PipelineContext ─────────────────────────────────────────
    _pipeline_ctx: PipelineContext | None = None
    if _use_v11:
        _pipeline_ctx = PipelineContext(
            version="1.1",
            solicitar_auditoria=body.solicitar_auditoria,
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
    from app.ai.llm_service import get_gemini_client as _get_gemini_client

    # Re-use _first_tags returned by _run_ai_analysis — no second HTTP fetch.
    # (The ML gate already retrieved these tags; see _run_ai_analysis return value.)
    # Safe nota_clinica for classify_exam — works for both v1.0 and v1.1
    _nota_for_classify: str = ""
    if _pipeline_ctx is not None and _pipeline_ctx.nota_clinica:
        _nota_for_classify = _pipeline_ctx.nota_clinica          # v1.1: already sanitized
    elif body.nota_clinica:
        _nota_for_classify = sanitize_nota_clinica(body.nota_clinica)  # v1.0: sanitize here

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
    for inst in ordered_for_gemini:
        # PERF-C2: Reuse raw bytes cached by _analyze_from_bytes (ResNet50 path).
        # If _raw_dicom_bytes is absent (tags-only path B, or future callers that
        # don't go through _analyze_from_bytes), fall back to a fresh download.
        cached = inst.get("_raw_dicom_bytes")
        if cached is not None:
            dicom_images.append(cached)
        else:
            try:
                dicom_images.append(
                    await get_instance_dicom_bytes(orthanc, inst["orthanc_instance_id"])
                )
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

    async def _v11_phases() -> None:
        nonlocal informe_texto, _informe_es_fallback, _requiere_revision_manual, _use_v11
        nonlocal _proyecciones_txt, _custom_sys, _custom_prompt
        # ── v1.1: Quality Gate (Phase 0.5) ───────────────────────────────────────
        # Run BEFORE billing and LLM. On not_analyzable → abort (no charge).
        # On any failure → fail-open (continue as v1).
        _quality_verdict = "adequate"  # type: str — default for v1 path; nonlocal can't have annotated assignment
        if _use_v11 and _pipeline_ctx is not None and dicom_images:
            from app.ai.llm_service import get_gemini_client as _get_gemini_client_v11
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

        # ── Step 3: LLM report generation ────────────────────────────────────────
        _proyecciones_txt = _template_context.get("projections_text", "estándar")
        _informe_es_fallback = False  # type annotation removed — nonlocal can't have annotated assignment
        if _use_v11 and _pipeline_ctx is not None:
            _pipeline_ctx.start_phase("generation", model=settings.GEMINI_PRO_MODEL)
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
                            # Build auditor feedback note for regeneration
                            _feedback_note = (
                                "NOTA DE AUDITORIA - Por favor corrige los siguientes problemas "  # noqa: RUF001
                                "antes de finalizar el informe:\n"
                                + "\n".join(f"- {obs}" for obs in _audit_result.observations)
                                + (
                                    f"\nRecomendacion: {_audit_result.recommendation}"
                                    if _audit_result.recommendation else ""
                                )
                            )
                            # Regenerate with feedback injected into prompt
                            _regen_prompt = _custom_prompt
                            if _regen_prompt:
                                _regen_prompt = inject_clinical_note(_regen_prompt, _feedback_note)
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
                                    custom_system_instruction=_custom_sys,
                                    custom_prompt=_regen_prompt,
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
        # v1.0 path: run generation directly (no quality gate, no auditor)
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
            # ── Tenant: persist clinic_id for PHI isolation ────────────────────
            clinic_id=current_user.clinic_id,
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

    return JSONResponse(response_data)


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
    """Return all reports for a study, newest first. Tenant-scoped."""
    # Tenant fail-closed: missing tenant context means re-login required
    if current_user.scope is None:
        raise HTTPException(
            status_code=401,
            detail="Sesión sin contexto de clínica. Inicie sesión nuevamente.",
        )
    if current_user.scope != "global" and not current_user.clinic_id:
        raise HTTPException(
            status_code=401,
            detail="Sesión sin contexto de clínica. Inicie sesión nuevamente.",
        )
    # Tenant filter: clinic-scoped users only see their own clinic's reports
    _clinic_filter = current_user.clinic_id if current_user.scope != "global" else None
    usuario_id_filtro = (
        None
        if current_user.rol in ("admin", "superadmin")
        else current_user.user_id
    )
    try:
        informes = await informes_db.get_informes_por_estudio(
            pool, study_instance_uid, usuario_id=usuario_id_filtro,
            clinic_id=_clinic_filter, scope=current_user.scope,
        )
    except HTTPException:
        raise  # BUG FIX: re-raise before generic catch
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
    pool: asyncpg.Pool = Depends(get_db_pool),
    current_user: UserPayload = Depends(get_current_user),
) -> JSONResponse:
    """Paginated report listing scoped by role and tenant.

    Tenant scoping: scope=None → 401 (re-login required).
    Clinic-scoped users see only their clinic's reports; global sees all.
    """
    # Tenant fail-closed: missing tenant context means re-login required
    if current_user.scope is None:
        raise HTTPException(
            status_code=401,
            detail="Sesión sin contexto de clínica. Inicie sesión nuevamente.",
        )
    if current_user.scope != "global" and not current_user.clinic_id:
        raise HTTPException(
            status_code=401,
            detail="Sesión sin contexto de clínica. Inicie sesión nuevamente.",
        )

    _scope_uid = (
        None
        if current_user.rol in ("admin", "superadmin")
        else current_user.user_id
    )
    _clinic_id = current_user.clinic_id if current_user.scope != "global" else None

    try:
        result = await informes_db.get_todos_informes(
            pool,
            estado=estado,
            usuario_id=_scope_uid,
            limit=limit,
            offset=offset,
            clinic_id=_clinic_id,
            scope=current_user.scope,
        )
    except HTTPException:
        raise  # BUG FIX: re-raise HTTPException before generic catch prevents 401→500 wrap
    except Exception as exc:
        logger.exception("Error fetching report listing")
        raise HTTPException(
            status_code=500, detail="Error interno del servidor. Intente nuevamente."
        ) from exc

    return JSONResponse({**result, "filtro_estado": estado})


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
    from app.auth.service import decode_token, is_user_revoked

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
