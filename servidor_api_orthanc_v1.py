"""
IMAGIAX — Capa 1 (Orthanc + IA)

Servicio FastAPI que conecta OHIF/Orthanc con el modelo NIH multilabel existente.
Objetivo: analizar estudios/series/instancias DICOM directamente desde PACS,
sin exportar JPG/PNG manualmente.

Ejecutar:
  uvicorn servidor_api_orthanc_v1:app --host 0.0.0.0 --port 8010

Endpoints principales:
  GET  /salud
  POST /analizar/instancia
  POST /analizar/serie
  POST /analizar/estudio
"""

from __future__ import annotations

import asyncio
import collections
import io
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import cv2
import httpx
import numpy as np
import pydicom
import skimage.transform
import torch
from dotenv import load_dotenv
from fastapi.middleware.cors import CORSMiddleware
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, HTMLResponse, StreamingResponse
from google import genai
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydicom.pixel_data_handlers.util import apply_modality_lut

import imagiax_db
import imagiax_pagos_db
import imagiax_auth
from imagiax_auth import get_current_user, UserPayload
from pagos_mp import cobrar_scan_automatico, PRECIO_SCAN_CLP, router as pagos_router
from pagos_tbk import cobrar_scan_oneclick, router as tbk_router
from servidor_api_v6 import DEVICE, NIH_PATOLOGIAS, UMBRALES, get_model

logger = logging.getLogger("imagiax")
logger.setLevel(logging.INFO)


# ── Rate limiter simple (sin dependencias externas) ───────────────────────────
class _RateLimiter:
    """Limita requests por IP con ventana deslizante."""
    def __init__(self, max_requests: int = 10, window_seconds: int = 60):
        self._max = max_requests
        self._window = window_seconds
        self._hits: dict[str, collections.deque] = {}
        self._last_cleanup: float = 0.0

    def check(self, key: str) -> bool:
        """Retorna True si la request está dentro del límite."""
        now = time.monotonic()
        # Limpiar entradas completamente vacías cada 5 minutos
        # Evita que bots acumulen miles de keys en el diccionario
        if now - self._last_cleanup > 300:
            self._hits = {k: v for k, v in self._hits.items() if v}
            self._last_cleanup = now
        q = self._hits.setdefault(key, collections.deque())
        while q and q[0] < now - self._window:
            q.popleft()
        if len(q) >= self._max:
            return False
        q.append(now)
        return True

# 10 análisis por minuto por IP (protege Gemini API)
_analysis_limiter = _RateLimiter(max_requests=10, window_seconds=60)
# 30 requests/min para endpoints generales
_general_limiter = _RateLimiter(max_requests=30, window_seconds=60)

# Cargar variables de entorno desde .env
load_dotenv()

# -------------------- Configuración --------------------
ORTHANC_BASE_URL = os.getenv("ORTHANC_BASE_URL", "http://localhost:8042")
HTTP_TIMEOUT = float(os.getenv("IMAGIAX_HTTP_TIMEOUT", "30"))
IMG_SIZE = 512

# Google Gemini
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
LLM_MODEL = "gemini-2.5-flash"
_gemini_client: genai.Client | None = None

def get_gemini_client() -> genai.Client:
    global _gemini_client
    if _gemini_client is None:
        if not GEMINI_API_KEY:
            raise HTTPException(status_code=500, detail="GEMINI_API_KEY no configurada en .env")
        _gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    return _gemini_client

# PostgreSQL — NUNCA hardcodear credenciales; requiere .env correctamente configurado
POSTGRES_URL = os.getenv("POSTGRES_URL")
if not POSTGRES_URL:
    raise RuntimeError(
        "POSTGRES_URL no configurada. Agrega POSTGRES_URL=postgresql://user:pass@host:port/db en tu .env"
    )

IMAGIAX_BASE_URL = os.getenv("IMAGIAX_BASE_URL", "http://localhost:8010")


class InstanciaRequest(BaseModel):
    """Analiza una instancia DICOM específica."""

    orthanc_instance_id: str | None = Field(default=None, max_length=128)  # SEC-AI-004
    sop_instance_uid: str | None = Field(default=None, max_length=128)  # SEC-AI-004

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_one_id(self) -> "InstanciaRequest":
        if not self.orthanc_instance_id and not self.sop_instance_uid:
            raise ValueError("Debes enviar orthanc_instance_id o sop_instance_uid")
        return self


class SerieRequest(BaseModel):
    """Analiza todas (o las primeras N) instancias de una serie."""

    series_instance_uid: str = Field(max_length=128)  # SEC-AI-004
    max_instances: int = Field(default=10, ge=1, le=200)

    model_config = ConfigDict(extra="forbid")


class EstudioRequest(BaseModel):
    """Analiza todas (o las primeras N) instancias de un estudio."""

    study_instance_uid: str = Field(max_length=128)  # SEC-AI-004
    max_instances: int = Field(default=20, ge=1, le=500)
    persistir_resultado: bool = True

    model_config = ConfigDict(extra="forbid")


class GenerarInformeRequest(BaseModel):
    """Genera informe médico completo: análisis IA + redacción LLM + persistencia en DB."""

    study_instance_uid: str = Field(max_length=128)  # SEC-AI-004
    max_instances: int = Field(default=20, ge=1, le=50)  # SEC-AI-001: capped at 50

    model_config = ConfigDict(extra="forbid")


class GuardarDecisionRequest(BaseModel):
    """Registra la decisión del médico sobre un informe generado."""

    informe_id: str = Field(max_length=128)  # SEC-AI-004
    estado: str  # aceptado | rechazado | editado_aceptado
    informe_final: str = Field(max_length=50_000)  # SEC-AI-004: ~50k chars = informe médico real
    notas_rechazo: str | None = Field(default=None, max_length=2_000)  # SEC-AI-004

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_estado(self) -> "GuardarDecisionRequest":
        allowed = {"aceptado", "rechazado", "editado_aceptado"}
        if self.estado not in allowed:
            raise ValueError(f"estado debe ser uno de: {', '.join(sorted(allowed))}")
        return self


# -------------------- Cliente HTTP compartido para Orthanc --------------------
# Evita crear 40+ conexiones TCP por estudio (una por cada llamada)
IMAGIAX_LAST_RESULT_KEY = "2048"

# Cliente HTTP compartido — se inicializa en lifespan y se reutiliza
_orthanc_client: httpx.AsyncClient | None = None


def _get_orthanc_client() -> httpx.AsyncClient:
    """Retorna el cliente HTTP compartido para Orthanc."""
    if _orthanc_client is None:
        raise RuntimeError("Cliente HTTP de Orthanc no inicializado. Verifica lifespan.")
    return _orthanc_client


async def _init_orthanc_client() -> None:
    """Inicializa el cliente HTTP compartido. Llamado desde lifespan."""
    global _orthanc_client
    _orthanc_client = httpx.AsyncClient(
        base_url=ORTHANC_BASE_URL,
        timeout=HTTP_TIMEOUT,
        limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
    )


async def _close_orthanc_client() -> None:
    """Cierra el cliente HTTP compartido. Llamado desde lifespan."""
    global _orthanc_client
    if _orthanc_client is not None:
        await _orthanc_client.aclose()
        _orthanc_client = None


# Cliente HTTP para proxy DICOMweb (SEC-BE-002, SEC-BE-003) — autenticado con Basic Auth
_pacs_client: httpx.AsyncClient | None = None


async def orthanc_get(path: str) -> httpx.Response:
    """GET request a Orthanc usando cliente compartido."""
    client = _get_orthanc_client()
    return await client.get(path)


async def orthanc_post(path: str, json_body: dict[str, Any]) -> httpx.Response:
    """POST request a Orthanc usando cliente compartido."""
    client = _get_orthanc_client()
    return await client.post(path, json=json_body)


async def orthanc_put(path: str, text_body: str) -> httpx.Response:
    """PUT request a Orthanc usando cliente compartido."""
    client = _get_orthanc_client()
    return await client.put(
        path,
        content=text_body.encode("utf-8"),
        headers={"Content-Type": "text/plain"},
    )


async def resolve_orthanc_instance_id(sop_instance_uid: str) -> str:
    payload = {
        "Level": "Instance",
        "Query": {
            "SOPInstanceUID": sop_instance_uid,
        },
        "Expand": False,
    }
    response = await orthanc_post("/tools/find", payload)
    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Orthanc /tools/find falló ({response.status_code}): {response.text}",
        )

    ids = response.json()
    if not ids:
        raise HTTPException(status_code=404, detail="No se encontró instancia para el SOPInstanceUID")

    return ids[0]


async def find_instances_by_query(query: dict[str, str]) -> list[str]:
    payload = {
        "Level": "Instance",
        "Query": query,
        "Expand": False,
    }
    response = await orthanc_post("/tools/find", payload)
    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Orthanc /tools/find falló ({response.status_code}): {response.text}",
        )

    ids = response.json()
    if not ids:
        raise HTTPException(status_code=404, detail="No se encontraron instancias para el criterio solicitado")

    return ids


async def find_study_id_by_uid(study_instance_uid: str) -> str:
    payload = {
        "Level": "Study",
        "Query": {
            "StudyInstanceUID": study_instance_uid,
        },
        "Expand": False,
    }
    response = await orthanc_post("/tools/find", payload)
    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Orthanc /tools/find (study) falló ({response.status_code}): {response.text}",
        )

    ids = response.json()
    if not ids:
        raise HTTPException(status_code=404, detail="No se encontró estudio para el StudyInstanceUID")
    return ids[0]


async def save_study_result_metadata(study_instance_uid: str, payload: dict[str, Any]) -> None:
    study_id = await find_study_id_by_uid(study_instance_uid)
    response = await orthanc_put(
        f"/studies/{study_id}/metadata/{IMAGIAX_LAST_RESULT_KEY}",
        json.dumps(payload, ensure_ascii=False),
    )
    if response.status_code not in (200, 204):
        raise HTTPException(
            status_code=502,
            detail=f"No se pudo persistir metadata en Orthanc ({response.status_code}): {response.text}",
        )


async def get_study_result_metadata(study_instance_uid: str) -> dict[str, Any] | None:
    study_id = await find_study_id_by_uid(study_instance_uid)
    response = await orthanc_get(f"/studies/{study_id}/metadata/{IMAGIAX_LAST_RESULT_KEY}")
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"No se pudo leer metadata de Orthanc ({response.status_code}): {response.text}",
        )

    try:
        return json.loads(response.text)
    except json.JSONDecodeError as error:
        raise HTTPException(status_code=500, detail=f"Metadata IMAGIAX corrupta en Orthanc: {error}") from error


async def get_instance_dicom_bytes(orthanc_instance_id: str) -> bytes:
    response = await orthanc_get(f"/instances/{orthanc_instance_id}/file")
    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="Instancia no encontrada en Orthanc")
    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"No se pudo descargar DICOM ({response.status_code}): {response.text}",
        )
    return response.content


async def get_instance_tags(orthanc_instance_id: str) -> dict[str, Any]:
    response = await orthanc_get(f"/instances/{orthanc_instance_id}/simplified-tags")
    if response.status_code != 200:
        return {}
    return response.json()


# -------------------- IA sobre DICOM --------------------
def dicom_to_tensor(dicom_bytes: bytes) -> torch.Tensor:
    """
    Convierte un DICOM a tensor [1,1,512,512] para el modelo NIH.

    Pipeline:
      1) Lee pixel_array con pydicom.
      2) Aplica Modality LUT (RescaleSlope/Intercept) si existe.
      3) Corrige MONOCHROME1 invirtiendo intensidades.
      4) Escala robustamente a 8 bits para CLAHE.
      5) Reusa normalización esperada por el modelo TorchXRayVision.
    """
    dataset = pydicom.dcmread(io.BytesIO(dicom_bytes), force=True)

    if "PixelData" not in dataset:
        raise ValueError("El DICOM no contiene PixelData")

    pixel_data = dataset.pixel_array.astype(np.float32)
    pixel_data = apply_modality_lut(pixel_data, dataset).astype(np.float32)

    photometric = str(getattr(dataset, "PhotometricInterpretation", ""))
    if photometric.upper() == "MONOCHROME1":
        pixel_data = np.max(pixel_data) - pixel_data

    # Robust min/max (evita outliers extremos)
    p01 = np.percentile(pixel_data, 1)
    p99 = np.percentile(pixel_data, 99)
    if p99 <= p01:
        p01 = float(np.min(pixel_data))
        p99 = float(np.max(pixel_data)) + 1e-6

    img_01 = np.clip((pixel_data - p01) / (p99 - p01), 0.0, 1.0)
    img_8u = (img_01 * 255.0).astype(np.uint8)

    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    img_8u = clahe.apply(img_8u)

    resized = skimage.transform.resize(img_8u, (IMG_SIZE, IMG_SIZE), anti_aliasing=True)
    normalized = resized * 2048.0 - 1024.0

    tensor = torch.FloatTensor(normalized).unsqueeze(0).unsqueeze(0).to(DEVICE)
    return tensor


def infer_multilabel(tensor: torch.Tensor) -> dict[str, Any]:
    model = get_model()
    with torch.no_grad():
        probs = model(tensor)[0].cpu().numpy()  # ResNet50-512 aplica sigmoid+op_norm internamente

    all_labels: dict[str, Any] = {}
    findings: list[dict[str, Any]] = []

    for index, pathology in enumerate(NIH_PATOLOGIAS):
        probability = float(probs[index])
        threshold = float(UMBRALES[pathology])
        positive = probability >= threshold

        all_labels[pathology] = {
            "probabilidad_pct": round(probability * 100, 2),
            "positivo": positive,
            "umbral_pct": round(threshold * 100, 0),
        }

        if positive:
            findings.append(
                {
                    "patologia": pathology,
                    "probabilidad_pct": round(probability * 100, 2),
                }
            )

    findings.sort(key=lambda item: item["probabilidad_pct"], reverse=True)

    if not findings:
        alert_level = "NORMAL"
    else:
        top_probability = findings[0]["probabilidad_pct"]
        alert_level = "ALTA" if top_probability >= 80 else ("MEDIA" if top_probability >= 60 else "BAJA")

    return {
        "nivel_alerta": alert_level,
        "hallazgos": findings,
        "sin_hallazgos": len(findings) == 0,
        "todas_patologias": all_labels,
    }


async def analyze_single_instance(orthanc_instance_id: str) -> dict[str, Any]:
    dicom_bytes = await get_instance_dicom_bytes(orthanc_instance_id)
    tags = await get_instance_tags(orthanc_instance_id)

    try:
        tensor = dicom_to_tensor(dicom_bytes)
    except Exception as error:
        raise HTTPException(status_code=422, detail=f"No se pudo procesar DICOM: {error}") from error

    inference = infer_multilabel(tensor)
    return {
        "orthanc_instance_id": orthanc_instance_id,
        "dicom": {
            "StudyInstanceUID": tags.get("StudyInstanceUID"),
            "SeriesInstanceUID": tags.get("SeriesInstanceUID"),
            "SOPInstanceUID": tags.get("SOPInstanceUID"),
            "Modality": tags.get("Modality"),
            "ViewPosition": tags.get("ViewPosition"),
            "BodyPartExamined": tags.get("BodyPartExamined"),
            "StudyDate": tags.get("StudyDate"),
            "PatientID": tags.get("PatientID"),
            "PatientName": tags.get("PatientName"),
            "PatientAge": tags.get("PatientAge"),
            "PatientBirthDate": tags.get("PatientBirthDate"),
            "AccessionNumber": tags.get("AccessionNumber"),
            "InstanceNumber": tags.get("InstanceNumber"),
            "SeriesDescription": tags.get("SeriesDescription"),
        },
        **inference,
    }


# -------------------- FastAPI app --------------------
@asynccontextmanager
async def lifespan(_: FastAPI):
    # 1) Warmup del modelo PyTorch
    get_model()

    # 2) Inicializar cliente HTTP compartido para Orthanc
    await _init_orthanc_client()

    # 2b) Inicializar cliente HTTP DICOMweb para proxy autenticado
    global _pacs_client
    _orthanc_auth = os.getenv("ORTHANC_AUTH_HEADER", "")
    _pacs_headers = {"Authorization": f"Basic {_orthanc_auth}"} if _orthanc_auth else {}
    _pacs_client = httpx.AsyncClient(
        base_url=ORTHANC_BASE_URL,
        timeout=httpx.Timeout(60.0),
        headers=_pacs_headers,
        limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
    )

    # SEC-BE-008: CORS wildcard check — credentials + wildcard is forbidden
    if "*" in _cors_origins:
        raise RuntimeError(
            "CORS_ORIGINS contains wildcard '*' — this is forbidden when allow_credentials=True. "
            "Specify explicit origins in CORS_ORIGINS env var."
        )

    # 3) Verificar conectividad Orthanc
    try:
        response = await orthanc_get("/system")
        if response.status_code == 200:
            name = response.json().get("Name", "Orthanc")
            print(f"IMAGIAX Orthanc API lista en {DEVICE}. PACS: {name}")
        else:
            print(f"IMAGIAX: Orthanc respondió con {response.status_code}.")
    except Exception:
        print(f"IMAGIAX: Orthanc no disponible al inicio (URL: {ORTHANC_BASE_URL}).")

    # 4) Inicializar pool de base de datos PostgreSQL
    try:
        await imagiax_db.init_pool(POSTGRES_URL)
    except Exception as exc:
        # No bloqueamos el arranque si la DB no está lista aún
        print(f"IMAGIAX DB: advertencia al inicializar PostgreSQL: {exc}")

    # 5) Crear tablas de pagos/licencias y el trial automático inicial
    try:
        await imagiax_pagos_db.create_tables()
        await imagiax_pagos_db.get_or_create_trial()
        installation_id = await imagiax_pagos_db.get_or_create_installation_id()
        print(f"IMAGIAX Pagos: installation_id={installation_id}")
    except Exception as exc:
        print(f"IMAGIAX Pagos: advertencia al inicializar tablas de pagos: {exc}")

    yield

    # Cierre limpio
    if _pacs_client is not None:
        await _pacs_client.aclose()
    await _close_orthanc_client()
    await imagiax_db.close_pool()


app = FastAPI(title="IMAGIAX Orthanc API", version="1.0.0", lifespan=lifespan)

# Orígenes CORS: leídos del env para soportar local Y cloud sin cambiar código.
# Local (.env):        CORS_ORIGINS=http://localhost:3000,http://127.0.0.1:3000
# Cloud (.env.prod):   CORS_ORIGINS=http://IP_ORACLE
_cors_origins = os.getenv(
    "CORS_ORIGINS",
    "http://localhost:3000,http://127.0.0.1:3000"
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,   # seguro: origins son siempre explícitos, nunca ["*"]
    allow_methods=["*"],
    allow_headers=["*"],
)

# Montar el router de pagos MercadoPago
app.include_router(pagos_router)

# Montar el router de pagos Transbank Oneclick
app.include_router(tbk_router)

# Rate limiting middleware para endpoints costosos (Gemini API)
_RATE_LIMITED_PREFIXES = ("/analizar/", "/generar/")

@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    path = request.url.path
    if any(path.startswith(p) for p in _RATE_LIMITED_PREFIXES):
        client_ip = request.client.host if request.client else "unknown"
        if not _analysis_limiter.check(client_ip):
            return JSONResponse(
                {"detail": "Demasiadas solicitudes. Intenta en 1 minuto."},
                status_code=429,
            )
    response = await call_next(request)
    # SEC-BE-007: Content Security Policy header
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'"
    )
    return response


@app.get("/salud")
async def salud() -> dict[str, Any]:
    """
    Health check completo del sistema IMAGIAX.
    
    Verifica todos los componentes críticos:
    - Modelo PyTorch (ResNet50-512)
    - Orthanc PACS
    - PostgreSQL (base de datos)
    - Gemini API (LLM)
    
    El estado general es "ok" solo si TODOS los componentes están operativos.
    """
    componentes = {}
    errores = []

    # 1. Modelo PyTorch
    try:
        model = get_model()
        componentes["modelo"] = {
            "estado": "ok" if model is not None else "no_cargado",
            "tipo": "ResNet50-512-all",
            "dispositivo": str(DEVICE),
        }
    except Exception as e:
        componentes["modelo"] = {"estado": "error", "error": str(e)}
        errores.append(f"Modelo: {e}")

    # 2. Orthanc PACS
    try:
        response = await orthanc_get("/system")
        if response.status_code == 200:
            orthanc_data = response.json()
            componentes["orthanc"] = {
                "estado": "ok",
                "url": ORTHANC_BASE_URL,
                "nombre": orthanc_data.get("Name"),
                "version": orthanc_data.get("Version"),
            }
        else:
            componentes["orthanc"] = {
                "estado": "error",
                "url": ORTHANC_BASE_URL,
                "error": f"HTTP {response.status_code}",
            }
            errores.append(f"Orthanc: HTTP {response.status_code}")
    except Exception as e:
        componentes["orthanc"] = {
            "estado": "no_disponible",
            "url": ORTHANC_BASE_URL,
            "error": str(e),
        }
        errores.append(f"Orthanc: {e}")

    # 3. PostgreSQL
    try:
        pool = imagiax_db.get_pool()
        async with pool.acquire() as conn:
            # Simple query para verificar conectividad
            result = await conn.fetchval("SELECT 1")
            if result == 1:
                componentes["postgresql"] = {
                    "estado": "ok",
                    "pool_size": pool.get_size(),
                    "pool_free": pool.get_idle_size(),
                }
            else:
                componentes["postgresql"] = {"estado": "error", "error": "Query de prueba falló"}
                errores.append("PostgreSQL: Query de prueba falló")
    except RuntimeError as e:
        # Pool no inicializado
        componentes["postgresql"] = {"estado": "no_inicializado", "error": str(e)}
        errores.append(f"PostgreSQL: {e}")
    except Exception as e:
        componentes["postgresql"] = {"estado": "error", "error": str(e)}
        errores.append(f"PostgreSQL: {e}")

    # 4. Gemini API (verificación ligera - solo validar que hay API key configurada)
    # No hacemos una llamada real para no gastar tokens en cada health check
    try:
        if GEMINI_API_KEY:
            # Verificamos que el cliente se puede crear (no hace llamada de red)
            client = get_gemini_client()
            componentes["gemini"] = {
                "estado": "configurado",
                "modelo": LLM_MODEL,
                "nota": "API key presente. No se verifica conectividad para ahorrar tokens.",
            }
        else:
            componentes["gemini"] = {
                "estado": "no_configurado",
                "error": "GEMINI_API_KEY no presente en .env",
            }
            errores.append("Gemini: API key no configurada")
    except Exception as e:
        componentes["gemini"] = {"estado": "error", "error": str(e)}
        errores.append(f"Gemini: {e}")

    # Estado general: ok solo si no hay errores
    estado_general = "ok" if len(errores) == 0 else "degradado"
    
    # Si falta algún componente crítico, marcar como "error"
    criticos = ["modelo", "postgresql"]
    for comp in criticos:
        if componentes.get(comp, {}).get("estado") not in ("ok", "configurado"):
            estado_general = "error"
            break

    return {
        "estado": estado_general,
        "servicio": "IMAGIAX-Orthanc-Bridge",
        "version": "1.1.0",
        "patologias": len(NIH_PATOLOGIAS),
        "componentes": componentes,
        "errores": errores if errores else None,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }



# ── Endpoint de salud de la base de datos (SEC-BE-001, AD-1) ──────────────────
@app.get("/health/db")
async def health_db() -> JSONResponse:
    """
    Health check de la base de datos PostgreSQL.
    NO expone detalles de excepción para evitar info leak (SEC-BE-001).
    """
    try:
        pool = imagiax_db.get_pool()
    except RuntimeError:
        return JSONResponse(status_code=503, content={"ok": False, "database": "unavailable"})
    try:
        async with pool.acquire() as conn:
            n = await conn.fetchval("SELECT 1")
        if n != 1:
            return JSONResponse(status_code=503, content={"ok": False, "database": "unhealthy"})
        return JSONResponse(content={"ok": True, "database": "up"})
    except Exception:
        # SEC-BE-001: No exponer detalles de la excepción
        return JSONResponse(status_code=503, content={"ok": False, "database": "unavailable"})

# ── Favicon (evita 404 en navegadores) ────────────────────────────────────────
_FAVICON_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    b'<rect width="32" height="32" rx="6" fill="#1e40af"/>'
    b'<text x="16" y="23" text-anchor="middle" font-size="20" '
    b'font-family="sans-serif" font-weight="bold" fill="white">I</text></svg>'
)

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(content=_FAVICON_SVG, media_type="image/svg+xml")
_FRONTEND_CONFIG = {
    "doctor_name":       os.getenv("IMAGIAX_DOCTOR_NAME", ""),
    "doctor_specialty":  os.getenv("IMAGIAX_DOCTOR_SPECIALTY", ""),
    "doctor_license":    os.getenv("IMAGIAX_DOCTOR_LICENSE", ""),
    "brand_name":        os.getenv("IMAGIAX_BRAND_NAME", ""),
    "brand_subtitle":    os.getenv("IMAGIAX_BRAND_SUBTITLE", ""),
    "backend_url":       IMAGIAX_BASE_URL,
    "precio_scan_clp":   PRECIO_SCAN_CLP,
}

@app.get("/config/frontend")
async def config_frontend() -> dict[str, Any]:
    """Configuración pública para el frontend (doctor, clínica, precios)."""
    return {k: v for k, v in _FRONTEND_CONFIG.items() if v}


@app.post("/analizar/instancia")
async def analizar_instancia(request: InstanciaRequest) -> JSONResponse:
    if request.orthanc_instance_id:
        orthanc_instance_id = request.orthanc_instance_id
    else:
        orthanc_instance_id = await resolve_orthanc_instance_id(request.sop_instance_uid or "")

    result = await analyze_single_instance(orthanc_instance_id)
    return JSONResponse(
        {
            "scope": "instancia",
            "resultado": result,
            "advertencia": "Herramienta de apoyo. No reemplaza al médico.",
        }
    )


@app.post("/analizar/serie")
async def analizar_serie(request: SerieRequest) -> JSONResponse:
    instance_ids = await find_instances_by_query({"SeriesInstanceUID": request.series_instance_uid})

    analyzed: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    for orthanc_id in instance_ids[: request.max_instances]:
        try:
            analyzed.append(await analyze_single_instance(orthanc_id))
        except HTTPException as error:
            errors.append({"orthanc_instance_id": orthanc_id, "error": str(error.detail)})

    return JSONResponse(
        {
            "scope": "serie",
            "series_instance_uid": request.series_instance_uid,
            "total_encontradas": len(instance_ids),
            "total_analizadas": len(analyzed),
            "max_instances": request.max_instances,
            "resultados": analyzed,
            "errores": errors,
            "advertencia": "Herramienta de apoyo. No reemplaza al médico.",
        }
    )


@app.post("/analizar/estudio")
async def analizar_estudio(request: EstudioRequest) -> JSONResponse:
    instance_ids = await find_instances_by_query({"StudyInstanceUID": request.study_instance_uid})

    analyzed: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    for orthanc_id in instance_ids[: request.max_instances]:
        try:
            analyzed.append(await analyze_single_instance(orthanc_id))
        except HTTPException as error:
            errors.append({"orthanc_instance_id": orthanc_id, "error": str(error.detail)})

    # Resumen del estudio: hallazgos máximos por patología entre instancias
    max_by_pathology: dict[str, float] = {pathology: 0.0 for pathology in NIH_PATOLOGIAS}
    for instance in analyzed:
        labels = instance.get("todas_patologias", {})
        for pathology in NIH_PATOLOGIAS:
            value = float(labels.get(pathology, {}).get("probabilidad_pct", 0.0))
            if value > max_by_pathology[pathology]:
                max_by_pathology[pathology] = value

    study_findings = [
        {"patologia": pathology, "probabilidad_pct_max": round(probability, 2)}
        for pathology, probability in max_by_pathology.items()
        if probability >= (UMBRALES[pathology] * 100)
    ]
    study_findings.sort(key=lambda item: item["probabilidad_pct_max"], reverse=True)

    response_payload = {
        "scope": "estudio",
        "study_instance_uid": request.study_instance_uid,
        "total_encontradas": len(instance_ids),
        "total_analizadas": len(analyzed),
        "max_instances": request.max_instances,
        "hallazgos_estudio": study_findings,
        "sin_hallazgos_estudio": len(study_findings) == 0,
        "resultados_instancias": analyzed,
        "errores": errors,
        "advertencia": "Herramienta de apoyo. No reemplaza al médico.",
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }

    if request.persistir_resultado:
        await save_study_result_metadata(request.study_instance_uid, response_payload)
        response_payload["persistencia"] = {
            "guardado_en_orthanc": True,
            "metadata_key": IMAGIAX_LAST_RESULT_KEY,
        }
    else:
        response_payload["persistencia"] = {
            "guardado_en_orthanc": False,
            "metadata_key": IMAGIAX_LAST_RESULT_KEY,
        }

    return JSONResponse(response_payload)


@app.get("/resultados/estudio/{study_instance_uid}")
async def obtener_resultado_estudio(study_instance_uid: str) -> JSONResponse:
    stored = await get_study_result_metadata(study_instance_uid)
    if stored is None:
        raise HTTPException(
            status_code=404,
            detail="No hay resultado IMAGIAX persistido para ese estudio",
        )

    return JSONResponse(
        {
            "scope": "estudio_persistido",
            "study_instance_uid": study_instance_uid,
            "metadata_key": IMAGIAX_LAST_RESULT_KEY,
            "resultado": stored,
        }
    )


# ============================================================
# Generación de informe médico con LLM
# ============================================================

def _dicom_to_png_bytes(raw_bytes: bytes) -> bytes:
    """Convierte bytes DICOM a PNG normalizado listo para Gemini Vision."""
    import io as _io
    ds = pydicom.dcmread(_io.BytesIO(raw_bytes), force=True)
    pixel_data = ds.pixel_array.astype(np.float32)
    pixel_data = apply_modality_lut(pixel_data, ds).astype(np.float32)
    photometric = str(getattr(ds, "PhotometricInterpretation", ""))
    if photometric.upper() == "MONOCHROME1":
        pixel_data = np.max(pixel_data) - pixel_data
    p1 = np.percentile(pixel_data, 1)
    p99 = np.percentile(pixel_data, 99)
    if p99 <= p1:
        p1, p99 = float(np.min(pixel_data)), float(np.max(pixel_data)) + 1e-6
    img_8u = np.clip((pixel_data - p1) / (p99 - p1) * 255, 0, 255).astype(np.uint8)
    h, w = img_8u.shape[:2]
    if max(h, w) > 1536:
        scale = 1536 / max(h, w)
        img_8u = cv2.resize(img_8u, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_LANCZOS4)
    _, png_buf = cv2.imencode(".png", img_8u)
    return png_buf.tobytes()


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
) -> str:
    """
    Genera un informe radiológico estructurado en español usando Google Gemini Vision.
    Si se proveen dicom_bytes, convierte a PNG y los envía a Gemini junto con los scores
    de ResNet50-512 como contexto cuantitativo. Gemini ve la imagen + los números.
    """
    hallazgos = ai_resultados.get("hallazgos_estudio", [])
    nivel_alerta = ai_resultados.get("nivel_alerta", ai_resultados.get("ai_nivel_alerta", "NORMAL"))
    total_analizadas = ai_resultados.get("total_analizadas", 1)

    # Construir texto de hallazgos CONFIRMADOS vs hallazgos secundarios a describir
    # hallazgos ya viene filtrado por UMBRALES (>=50%), así que todo hallazgo aquí es real
    hallazgos_positivos = [h for h in hallazgos if h.get("probabilidad_pct_max", 0) >= 50]
    hallazgos_leves = []  # todos los que pasan umbral van como confirmados

    def nombre_es_patologia(pat_raw: str) -> str:
        NOMBRES_ES = {
            'Atelectasis': 'Atelectasia', 'Cardiomegaly': 'Cardiomegalia',
            'Effusion': 'Derrame pleural',
            'Infiltrate': 'Infiltrado pulmonar', 'Infiltration': 'Infiltrado pulmonar',
            'Mass': 'Masa pulmonar', 'Nodule': 'Nódulo pulmonar',
            'Pneumonia': 'Neumonía', 'Pneumothorax': 'Neumotórax',
            'Consolidation': 'Consolidación pulmonar', 'Edema': 'Edema pulmonar',
            'Emphysema': 'Enfisema pulmonar', 'Fibrosis': 'Fibrosis pulmonar',
            'Pleural_Thickening': 'Engrosamiento pleural', 'Hernia': 'Hernia diafragmática',
            'Lung Lesion': 'Lesión pulmonar', 'Fracture': 'Fractura',
            'Lung Opacity': 'Opacidad pulmonar', 'Enlarged Cardiomediastinum': 'Ensanchamiento mediastínico',
        }
        return NOMBRES_ES.get(pat_raw, pat_raw)

    if hallazgos_positivos:
        hallazgos_confirmados_txt = "\n".join(
            f"  - {nombre_es_patologia(h['patologia'])} (CONFIRMADO — hallazgo principal)"
            for h in hallazgos_positivos
        )
    else:
        hallazgos_confirmados_txt = "  - Ningún hallazgo patológico significativo confirmado (estudio dentro de límites normales)"

    if hallazgos_leves:
        hallazgos_leves_txt = "\n".join(
            f"  - {nombre_es_patologia(h['patologia'])} (posible, a correlacionar)"
            for h in hallazgos_leves
        )
        hallazgos_leves_bloque = f"\nHALLAZGOS SECUNDARIOS A CONSIDERAR:\n{hallazgos_leves_txt}"
    else:
        hallazgos_leves_bloque = ""

    # Contexto etario para calibrar hallazgos esperados (incluye regla pediátrica)
    edad_txt = ""
    if patient_age:
        # PatientAge puede venir como "081Y", "013Y", etc. o como fecha de nacimiento
        import re as _re
        age_match = _re.search(r'(\d+)', str(patient_age))
        if age_match:
            edad_num = int(age_match.group(1))
            base_edad = (
                f"\nEDAD DEL PACIENTE: {edad_num} años. "
                "Calibra tus hallazgos según la edad: en pacientes jóvenes (<40 años) "
                "NO esperes elongación aórtica ni ateromatosis; en pacientes mayores (>60 años) "
                "estos hallazgos son frecuentes y clínicamente relevantes.\n"
            )
            regla_pediatrica = (
                "REGLA PEDIÁTRICA (<18 años): usa lenguaje radiológico pediátrico; evita sesgo de adulto "
                "(ateromatosis, ectasia aórtica, cambios degenerativos como expectativa basal). "
                "No sobrediagnostiques cardiomegalia sin evidencia clara y reporta conclusiones "
                "como 'dentro de límites normales para la edad' cuando corresponda.\n"
                if edad_num < 18 else ""
            )
            edad_txt = base_edad + regla_pediatrica

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

    client = get_gemini_client()
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

    # ── Gemini Vision: enviar imagen(es) si están disponibles ──────────────
    contents: list = []
    if dicom_images:
        image_parts: list = []
        for raw_bytes in dicom_images:
            try:
                png_bytes = _dicom_to_png_bytes(raw_bytes)
                image_parts.append(genai.types.Part.from_bytes(data=png_bytes, mime_type="image/png"))
            except Exception as _img_err:
                logging.getLogger("imagiax").warning("Gemini Vision: fallo conversión DICOM→PNG: %s", _img_err)
        if image_parts:
            if len(image_parts) >= 2:
                img_nota = (
                    "Se adjuntan 2 imágenes: la PRIMERA es la proyección posteroanterior (PA), "
                    "la SEGUNDA es la lateral (LAT). Analiza ambas de forma complementaria.\n\n"
                )
            else:
                img_nota = ""
            contents = image_parts + [img_nota + prompt]
        else:
            contents = [prompt]
    else:
        contents = [prompt]

    # Retry con backoff exponencial para manejar 429 (rate limit) y errores transitorios 5xx
    last_exc: Exception | None = None
    for _attempt in range(4):  # intentos: 0, 1, 2, 3
        try:
            response = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=LLM_MODEL,
                    contents=contents,
                    config=genai.types.GenerateContentConfig(
                        system_instruction=system_instruction,
                        temperature=0.3,
                        max_output_tokens=4096,
                    ),
                ),
                timeout=90.0,  # 90s máximo por intento (evita cuelgues)
            )
            return response.text or ""
        except asyncio.TimeoutError:
            last_exc = TimeoutError(f"Gemini no respondió en 90s (intento {_attempt + 1}/4)")
            logging.getLogger("imagiax").warning("Gemini timeout intento %d/4", _attempt + 1)
            if _attempt < 3:
                await asyncio.sleep(2 ** _attempt * 5)
        except Exception as _exc:
            err_str = str(_exc)
            # Solo reintentar en 429 (rate limit) o errores de servidor 5xx
            if "429" in err_str or "500" in err_str or "503" in err_str:
                last_exc = _exc
                if _attempt < 3:
                    wait_secs = 2 ** _attempt * 5  # 5s, 10s, 20s
                    logging.getLogger("imagiax").warning(
                        "Gemini — reintento %d/3 en %ds: %s",
                        _attempt + 1, wait_secs, err_str[:120]
                    )
                    await asyncio.sleep(wait_secs)
            else:
                raise  # Error no retriable (autenticación, payload, etc.)
    
    # ── FALLBACK: Si Gemini falla, generar informe con template estructurado ──
    # Esto garantiza que el médico siempre recibe un informe, aunque sea básico
    logging.getLogger("imagiax").error(
        "Gemini falló después de 4 intentos. Generando informe con fallback estructurado."
    )
    return _generar_informe_fallback(
        hallazgos=ai_resultados.get("hallazgos_estudio", []),
        nivel_alerta=ai_resultados.get("nivel_alerta", "NORMAL"),
        patient_name=patient_name,
        study_date=study_date,
        proyecciones_txt=proyecciones_txt,
        patient_age=patient_age,
    )


def _generar_informe_fallback(
    *,
    hallazgos: list,
    nivel_alerta: str,
    patient_name: str | None,
    study_date: str | None,
    proyecciones_txt: str,
    patient_age: str | None,
) -> str:
    """
    Genera un informe estructurado básico cuando Gemini no está disponible.
    Usa los resultados del modelo ResNet50-512 para crear un informe mínimo.
    """
    # Formatear fecha
    fecha_fmt = "No especificada"
    if study_date and len(study_date) >= 8:
        try:
            y, m, d = study_date[:4], study_date[4:6], study_date[6:8]
            fecha_fmt = f"{d}/{m}/{y}"
        except Exception:
            fecha_fmt = study_date
    
    # Formatear edad
    edad_txt = ""
    if patient_age:
        try:
            age_num = int(patient_age.replace("Y", "").replace("M", "").strip()[:3])
            edad_txt = f", {age_num} años"
        except Exception:
            pass
    
    # Nombres de patologías en español
    NOMBRES_ES = {
        'Atelectasis': 'Atelectasia', 'Cardiomegaly': 'Cardiomegalia',
        'Effusion': 'Derrame pleural', 'Infiltrate': 'Infiltrado pulmonar',
        'Infiltration': 'Infiltrado pulmonar', 'Mass': 'Masa pulmonar',
        'Nodule': 'Nódulo pulmonar', 'Pneumonia': 'Neumonía',
        'Pneumothorax': 'Neumotórax', 'Consolidation': 'Consolidación pulmonar',
        'Edema': 'Edema pulmonar', 'Emphysema': 'Enfisema pulmonar',
        'Fibrosis': 'Fibrosis pulmonar', 'Pleural_Thickening': 'Engrosamiento pleural',
        'Hernia': 'Hernia diafragmática', 'Lung Lesion': 'Lesión pulmonar',
        'Fracture': 'Fractura', 'Lung Opacity': 'Opacidad pulmonar',
        'Enlarged Cardiomediastinum': 'Ensanchamiento mediastínico',
    }
    
    # Construir lista de hallazgos
    if hallazgos:
        hallazgos_txt = "\n".join([
            f"- {NOMBRES_ES.get(h.get('patologia', ''), h.get('patologia', 'Hallazgo'))}"
            for h in hallazgos
        ])
    else:
        hallazgos_txt = "- Sin hallazgos patológicos detectados."
    
    # Impresión según nivel de alerta
    if nivel_alerta == "NORMAL":
        impresion = "Estudio radiológico de tórax sin hallazgos patológicos significativos."
    elif nivel_alerta == "BAJA":
        impresion = "Hallazgos de baja probabilidad que ameritan correlación clínica."
    elif nivel_alerta == "MEDIA":
        impresion = "Hallazgos de moderada probabilidad. Se recomienda evaluación clínica."
    else:  # ALTA
        impresion = "Hallazgos significativos que requieren atención médica prioritaria."
    
    # Generar informe estructurado
    informe = f"""TÉCNICA
Radiografía de tórax en proyección {proyecciones_txt}. Fecha: {fecha_fmt}.

HALLAZGOS
{hallazgos_txt}

IMPRESIÓN DIAGNÓSTICA
{impresion}

OBSERVACIONES VISUALES ADICIONALES
Evaluación basada en los hallazgos detectados. Se recomienda correlación clínica directa y revisión detallada de las imágenes por médico radiólogo.

RECOMENDACIONES
Correlacionar con clínica y exámenes previos. Considerar evaluación por médico radiólogo si persisten dudas diagnósticas."""

    return informe


def _formatear_descripcion_cobro(patient_name: str | None, study_date: str | None) -> str:
    """Formatea nombre DICOM en una descripción profesional para el cobro."""
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
# Nuevos endpoints: Flujo completo IA → Informe → Decisión
# ============================================================

@app.post("/generar/informe")
async def generar_informe(request: GenerarInformeRequest) -> JSONResponse:
    """
    Pipeline completo:
      1) Deduplicación: verificar si ya existe informe reciente para este estudio
      2) Verificación de créditos/método de pago disponible
      3) Análisis IA del estudio DICOM
      4) Generación de informe médico con Gemini 2.5 Flash Vision
      5) Cobro/descuento según prioridad: Créditos > Oneclick > Tarjeta MP
      6) Persistencia en PostgreSQL con estado de cobro integrado
    
    IMPORTANTE: Siempre retorna el informe, incluso si el cobro falla.
    El estado de cobro se indica en 'cobro_info' del response.
    """
    # ── Paso 0: Deduplicación — evitar análisis duplicados ────────────────────
    try:
        informe_existente = await imagiax_db.buscar_informe_reciente(
            study_instance_uid=request.study_instance_uid,
            ventana_segundos=120  # 2 minutos
        )
        if informe_existente:
            logger.info(
                "Informe reciente encontrado para estudio %s (ID: %s, creado: %s). Retornando existente.",
                request.study_instance_uid,
                informe_existente["id"],
                informe_existente["created_at"],
            )
            return JSONResponse({
                "informe_id": informe_existente["id"],
                "informe_texto": informe_existente["informe_generado"],
                "ai_resultados": {
                    "nivel_alerta": informe_existente["ai_nivel_alerta"],
                    "hallazgos_estudio": informe_existente["ai_hallazgos"],
                    "todas_patologias": informe_existente["ai_todas_patologias"],
                },
                "modelo_llm": informe_existente["modelo_llm"],
                "cobro_info": {
                    "estado": informe_existente["cobro_estado"],
                    "metodo": informe_existente["cobro_metodo"],
                    "error": informe_existente["cobro_error"],
                },
                "duplicado": True,
                "advertencia": "Este informe ya fue generado anteriormente. Se retorna el existente.",
            })
    except Exception as exc:
        logger.warning("Error verificando deduplicación: %s (continuando)", exc)

    # ── Paso 1: Verificar capacidad de pago antes de procesar ─────────────────
    # NUEVA LÓGICA: Créditos prepagados tienen PRIORIDAD sobre cobro automático
    # Esto evita doble cobro cuando usuario tiene pack/suscripción + tarjeta
    estado_creditos = None
    oneclick_inscripcion = None
    metodo_pago = None

    try:
        estado_creditos = await imagiax_pagos_db.get_estado_creditos()
    except Exception as exc:
        logger.error("DB de pagos no disponible: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="Sistema de créditos temporalmente no disponible. Intenta en unos segundos.",
        ) from exc

    creditos_disponibles = estado_creditos.get("creditos_disponibles", 0)

    # Solo buscar métodos de pago si NO hay créditos disponibles
    if creditos_disponibles <= 0:
        try:
            oneclick_inscripcion = await imagiax_pagos_db.get_inscripcion_oneclick_any()
        except Exception:
            pass
        if not oneclick_inscripcion:
            try:
                metodo_pago = await imagiax_pagos_db.get_metodo_pago()
            except Exception:
                pass

        # Sin créditos Y sin tarjeta → bloquear
        if not oneclick_inscripcion and not metodo_pago:
            raise HTTPException(
                status_code=402,
                detail={
                    "error": "Sin créditos disponibles",
                    "mensaje": (
                        "Tus creditos se han agotado. "
                        "Adquiere un plan o registra una tarjeta para cobro automatico."
                    ),
                    "pagar_url": f"{IMAGIAX_BASE_URL}/pagar?trial_agotado=true",
                    "opciones_plan": ["pack_individual", "pack_50", "pack_200", "suscripcion_mensual"],
                },
            )

    # Paso 1: análisis IA (reutiliza lógica existente)
    instance_ids = await find_instances_by_query({"StudyInstanceUID": request.study_instance_uid})

    analyzed: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for orthanc_id in instance_ids[: request.max_instances]:
        try:
            analyzed.append(await analyze_single_instance(orthanc_id))
        except HTTPException as err:
            errors.append({"orthanc_instance_id": orthanc_id, "error": str(err.detail)})

    if not analyzed:
        raise HTTPException(
            status_code=422,
            detail="No se pudo analizar ninguna instancia del estudio. Verifica que el estudio tiene imágenes DX/CR válidas.",
        )

    # Resumen por patología (máximo entre instancias)
    max_by_pathology: dict[str, float] = {p: 0.0 for p in NIH_PATOLOGIAS}
    for inst in analyzed:
        for pat in NIH_PATOLOGIAS:
            val = float(inst.get("todas_patologias", {}).get(pat, {}).get("probabilidad_pct", 0.0))
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

    # Metadatos DICOM del primer estudio analizado
    dicom_meta = analyzed[0].get("dicom", {})
    patient_id = dicom_meta.get("PatientID")
    patient_name = dicom_meta.get("PatientName")
    study_date = dicom_meta.get("StudyDate")
    modalidad = dicom_meta.get("Modality")
    patient_age = dicom_meta.get("PatientAge") or None
    if not patient_age:
        # Fallback: calcular edad desde PatientBirthDate
        birth_date_str = dicom_meta.get("PatientBirthDate") or ""
        study_date_str = study_date or ""
        if len(birth_date_str) == 8 and len(study_date_str) >= 8:
            try:
                from datetime import datetime as _dt
                bd = _dt.strptime(birth_date_str, "%Y%m%d")
                sd = _dt.strptime(study_date_str[:8], "%Y%m%d")
                age_years = (sd - bd).days // 365
                patient_age = f"{age_years:03d}Y"
            except ValueError:
                pass

    ai_resultados: dict[str, Any] = {
        "scope": "estudio",
        "study_instance_uid": request.study_instance_uid,
        "total_encontradas": len(instance_ids),
        "total_analizadas": len(analyzed),
        "hallazgos_estudio": study_findings,
        "sin_hallazgos_estudio": len(study_findings) == 0,
        "nivel_alerta": nivel_alerta,
        "errores": errors,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }

    # Todas las patologías (para el prompt del LLM)
    todas_patologias_resumen: dict[str, Any] = {}
    for pat in NIH_PATOLOGIAS:
        prob_pct = round(max_by_pathology[pat], 2)
        todas_patologias_resumen[pat] = {
            "probabilidad_pct": prob_pct,
            "positivo": prob_pct >= (UMBRALES[pat] * 100),
        }

    ai_resultados["todas_patologias"] = todas_patologias_resumen

    # Paso 2: Clasificar instancias por proyección (ViewPosition o SeriesDescription)
    def _classify_view(inst: dict) -> str:
        """Retorna 'PA', 'LAT' u 'OTHER' según tags DICOM disponibles."""
        vp = (inst.get("dicom", {}).get("ViewPosition") or "").upper().strip()
        if vp in ("PA", "AP"):
            return "PA"
        if vp in ("LL", "RL", "LAT", "LATERAL"):
            return "LAT"
        # Fallback: buscar en SeriesDescription (ej: "Torax PA", "Torax LAT")
        sd = (inst.get("dicom", {}).get("SeriesDescription") or "").upper()
        if any(kw in sd for kw in (" PA", "PA ", "PA-", "POSTER", "ANTERO")):
            return "PA"
        if sd.endswith(" PA") or sd == "PA":
            return "PA"
        if any(kw in sd for kw in ("LAT", "LATER", " LL")):
            return "LAT"
        return "OTHER"

    pa_insts = [i for i in analyzed if _classify_view(i) == "PA"]
    lat_insts = [i for i in analyzed if _classify_view(i) == "LAT"]
    other_insts = [i for i in analyzed if _classify_view(i) == "OTHER"]

    # Texto de proyecciones para el informe
    if pa_insts and lat_insts:
        proyecciones_txt = "posteroanterior y lateral"
    elif pa_insts:
        proyecciones_txt = "posteroanterior"
    elif lat_insts:
        proyecciones_txt = "lateral"
    elif len(analyzed) >= 2:
        proyecciones_txt = "posteroanterior y lateral"  # 2 imágenes sin tag → asumir PA+LAT
    else:
        proyecciones_txt = "estándar"

    # Recolectar imágenes para Gemini: PA primero, LAT segundo (máx 2)
    ordered_for_gemini = (pa_insts + lat_insts + other_insts)[:2]
    dicom_images: list[bytes] = []
    for inst in ordered_for_gemini:
        try:
            dicom_images.append(await get_instance_dicom_bytes(inst["orthanc_instance_id"]))
        except Exception:
            pass

    # Paso 3: Generar informe con LLM (Gemini Vision si hay imagen disponible)
    try:
        informe_texto = await generar_informe_llm(
            ai_resultados=ai_resultados,
            patient_id=patient_id,
            patient_name=patient_name,
            study_date=study_date,
            modalidad=modalidad,
            dicom_images=dicom_images or None,
            proyecciones_txt=proyecciones_txt,
            patient_age=patient_age,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Error al generar informe con LLM: {exc}") from exc

    # ── Paso 5: Cobro / descuento ANTES de persistir ─────────────────────────
    # PRIORIDAD: Créditos prepagados > Oneclick (Transbank) > Tarjeta MP
    # El resultado del cobro se guarda junto con el informe (cobro como estado)
    creditos_restantes: int | None = None
    cobro_estado: str = "pendiente"
    cobro_metodo: str | None = None
    cobro_monto_clp: int | None = None
    cobro_ref_id: str | None = None
    cobro_error: str | None = None
    pago_automatico: dict[str, Any] | None = None

    if creditos_disponibles > 0:
        # PRIORIDAD 1: Descontar crédito de pack/suscripción/trial
        try:
            await imagiax_pagos_db.descontar_credito()
            estado_post = await imagiax_pagos_db.get_estado_creditos()
            creditos_restantes = estado_post.get("creditos_disponibles")
            cobro_estado = "pagado"
            cobro_metodo = "creditos"
            cobro_monto_clp = 0  # No hay cobro monetario, se usó crédito prepagado
        except Exception as exc:
            logger.warning("Error al descontar credito: %s", exc)
            cobro_estado = "fallido"
            cobro_metodo = "creditos"
            cobro_error = str(exc)

    elif oneclick_inscripcion:
        # PRIORIDAD 2: Transbank Oneclick (solo si sin créditos)
        desc = _formatear_descripcion_cobro(patient_name, study_date)
        aprobado, buy_order, error_msg = await cobrar_scan_oneclick(
            username=oneclick_inscripcion["username"],
            tbk_user=oneclick_inscripcion["tbk_user"],
            amount=PRECIO_SCAN_CLP,
            descripcion=desc[:255],
        )
        pago_automatico = {
            "estado": "approved" if aprobado else "rejected",
            "metodo": "oneclick",
            "buy_order": buy_order,
            "monto_clp": PRECIO_SCAN_CLP,
            "error": error_msg,
        }
        cobro_metodo = "oneclick"
        cobro_monto_clp = PRECIO_SCAN_CLP
        cobro_ref_id = buy_order
        if aprobado:
            cobro_estado = "pagado"
        else:
            cobro_estado = "fallido"
            cobro_error = error_msg or "Cobro Oneclick rechazado"
            logger.warning(
                "Cobro Oneclick rechazado para estudio %s. Error: %s",
                request.study_instance_uid, error_msg,
            )

    elif metodo_pago:
        # PRIORIDAD 3: MercadoPago tarjeta (solo si sin créditos y sin Oneclick)
        desc = _formatear_descripcion_cobro(patient_name, study_date)
        aprobado, payment_id, error_msg = await cobrar_scan_automatico(
            mp_customer_id=metodo_pago["mp_customer_id"],
            mp_card_id=metodo_pago["mp_card_id"],
            mp_card_brand=metodo_pago.get("card_brand", ""),
            descripcion=desc[:255],
        )
        pago_automatico = {
            "estado": "approved" if aprobado else "rejected",
            "metodo": "mp_card",
            "payment_id": payment_id,
            "monto_clp": PRECIO_SCAN_CLP,
            "error": error_msg,
        }
        cobro_metodo = "mp_card"
        cobro_monto_clp = PRECIO_SCAN_CLP
        cobro_ref_id = payment_id
        if aprobado:
            cobro_estado = "pagado"
        else:
            cobro_estado = "fallido"
            cobro_error = error_msg or "Cobro MercadoPago rechazado"
            logger.warning(
                "Cobro automatico MP rechazado para estudio %s. Error: %s",
                request.study_instance_uid, error_msg,
            )
    else:
        # Defensa en profundidad: si cambia validación previa, registrar estado explícito.
        cobro_estado = "no_aplica"
        cobro_error = "Sin metodo de cobro disponible al momento del analisis"

    # ── Paso 6: Persistir informe CON estado de cobro ──────────────────────────
    try:
        informe_id = await imagiax_db.guardar_informe(
            study_instance_uid=request.study_instance_uid,
            patient_id=patient_id,
            patient_name=patient_name,
            study_date=study_date,
            modalidad=modalidad,
            ai_nivel_alerta=nivel_alerta,
            ai_hallazgos=study_findings,
            ai_todas_patologias=todas_patologias_resumen,
            informe_generado=informe_texto,
            modelo_llm=LLM_MODEL,
            # Campos de cobro integrados
            cobro_estado=cobro_estado,
            cobro_metodo=cobro_metodo,
            cobro_monto_clp=cobro_monto_clp,
            cobro_ref_id=cobro_ref_id,
            cobro_error=cobro_error,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error al guardar informe en base de datos: {exc}") from exc

    # ── Paso 7: Respuesta (SIEMPRE retorna informe, nunca 402) ─────────────────
    response_data = {
        "informe_id": informe_id,
        "informe_texto": informe_texto,
        "ai_resultados": ai_resultados,
        "modelo_llm": LLM_MODEL,
        "creditos_restantes": creditos_restantes,
        "cobro_info": {
            "estado": cobro_estado,
            "metodo": cobro_metodo,
            "monto_clp": cobro_monto_clp,
            "ref_id": cobro_ref_id,
            "error": cobro_error,
        },
        "pago_automatico": pago_automatico,  # Backwards compatibility
        "metodo_cobro": cobro_metodo,  # Backwards compatibility
        "advertencia": "Informe generado por IA. Requiere validación del médico.",
    }

    # Agregar aviso si el cobro falló
    if cobro_estado == "fallido":
        response_data["cobro_pendiente"] = True
        response_data["cobro_mensaje"] = (
            f"El informe se generó correctamente, pero el cobro falló: {cobro_error}. "
            f"Puedes reintentar desde tu panel de pagos o contactar soporte."
        )
        response_data["pagar_url"] = f"{IMAGIAX_BASE_URL}/pagar"

    return JSONResponse(response_data)


@app.post("/informes/guardar")
async def guardar_decision(request: GuardarDecisionRequest) -> JSONResponse:
    """
    Registra la decisión final del médico sobre un informe:
      - aceptado: informe aprobado tal como fue generado
      - rechazado: informe rechazado (se guarda nota opcional)
      - editado_aceptado: médico modificó el informe y lo aceptó
    Incluye timestamp de decisión para trazabilidad legal.
    """
    try:
        updated = await imagiax_db.actualizar_decision(
            informe_id=request.informe_id,
            estado=request.estado,
            informe_final=request.informe_final,
            notas_rechazo=request.notas_rechazo,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error al actualizar decisión en base de datos: {exc}") from exc

    if updated is None:
        raise HTTPException(
            status_code=404,
            detail=f"No se encontró informe con ID: {request.informe_id}",
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


@app.get("/informes/estudio/{study_instance_uid}")
async def informes_por_estudio(study_instance_uid: str) -> JSONResponse:
    """Retorna todos los informes generados para un estudio, del más reciente al más antiguo."""
    try:
        informes = await imagiax_db.get_informes_por_estudio(study_instance_uid)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error al consultar informes: {exc}") from exc

    return JSONResponse(
        {
            "study_instance_uid": study_instance_uid,
            "total": len(informes),
            "informes": informes,
        }
    )


@app.get("/informes/{informe_id}/ver", response_class=HTMLResponse)
async def ver_informe(
    informe_id: str,
    user: UserPayload = Depends(get_current_user),
) -> HTMLResponse:
    """Página de verificación de informe. Requiere rol médico, admin o superadmin. (SEC-BE-006)"""
    # SEC-BE-006: Role-based access — only doctors/admins can view reports
    if user.rol not in ("medico", "admin", "superadmin"):
        return HTMLResponse("Acceso denegado", status_code=403)
    try:
        informe = await imagiax_db.get_informe_por_id(informe_id)
    except Exception:
        informe = None

    if not informe:
        return HTMLResponse(
            """<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Informe no encontrado — IMAGIAX</title>
<style>*{box-sizing:border-box;margin:0;padding:0}body{font-family:'Inter',system-ui,sans-serif;
background:#0a0f1e;color:#e2e8f0;min-height:100vh;display:flex;align-items:center;justify-content:center}
.card{background:#111827;border:1px solid #1e293b;border-radius:16px;padding:40px;max-width:400px;text-align:center}
h1{font-size:20px;margin-bottom:12px;color:#f87171}p{color:#64748b;font-size:14px}</style></head>
<body><div class="card"><h1>Informe no encontrado</h1>
<p>El código QR no corresponde a ningún informe registrado en el sistema.</p></div></body></html>""",
            status_code=404,
        )

    # Formatear datos para mostrar
    texto = informe.get("informe_final") or informe.get("informe_generado") or ""
    nivel = informe.get("ai_nivel_alerta", "—")
    nivel_color = {"ALTA": "#ef4444", "MEDIA": "#f59e0b", "BAJA": "#22c55e", "NORMAL": "#22c55e"}.get(nivel, "#94a3b8")
    estado = informe.get("estado", "pendiente")
    estado_txt = {"aceptado": "Aceptado", "editado_aceptado": "Aceptado (editado)",
                  "rechazado": "Rechazado", "pendiente": "Pendiente"}.get(estado, estado)
    paciente = informe.get("patient_name") or "—"
    fecha_raw = informe.get("created_at") or ""
    fecha = str(fecha_raw)[:10] if fecha_raw else "—"
    modalidad = informe.get("modalidad") or "—"
    # Escapar el texto para HTML (evitar XSS)
    import html as _html
    texto_html = _html.escape(texto).replace("\n", "<br>")

    return HTMLResponse(f"""<!DOCTYPE html><html lang="es"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Informe IMAGIAX — Verificación</title>
<style>*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Inter',system-ui,sans-serif;background:#0a0f1e;color:#e2e8f0;padding:24px}}
.container{{max-width:760px;margin:0 auto}}
.header{{display:flex;align-items:center;justify-content:space-between;margin-bottom:24px;
  padding-bottom:16px;border-bottom:1px solid #1e293b}}
.brand{{font-size:20px;font-weight:800;color:#3b82f6;letter-spacing:-0.5px}}
.verified{{display:flex;align-items:center;gap:6px;color:#22c55e;font-size:12px;font-weight:600}}
.card{{background:#111827;border:1px solid #1e293b;border-radius:12px;padding:24px;margin-bottom:16px}}
.meta{{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:12px;margin-bottom:20px}}
.meta-item label{{display:block;font-size:10px;font-weight:600;color:#475569;
  text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px}}
.meta-item span{{font-size:14px;color:#e2e8f0;font-weight:500}}
.nivel{{display:inline-block;padding:4px 12px;border-radius:20px;font-size:12px;font-weight:700;
  color:{nivel_color};border:1px solid {nivel_color};background:{nivel_color}22}}
.informe-body{{font-size:13px;line-height:1.8;color:#cbd5e1;white-space:pre-wrap;word-break:break-word}}
.footer{{text-align:center;margin-top:24px;color:#334155;font-size:11px}}
</style></head><body>
<div class="container">
  <div class="header">
    <div class="brand">IMAGIAX</div>
    <div class="verified">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
        <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/>
      </svg>
      Informe verificado
    </div>
  </div>
  <div class="card">
    <div class="meta">
      <div class="meta-item"><label>Paciente</label><span>{_html.escape(paciente)}</span></div>
      <div class="meta-item"><label>Fecha</label><span>{fecha}</span></div>
      <div class="meta-item"><label>Modalidad</label><span>{modalidad}</span></div>
      <div class="meta-item"><label>Estado</label><span>{estado_txt}</span></div>
      <div class="meta-item"><label>Nivel de alerta</label><span class="nivel">{nivel}</span></div>
    </div>
    <div class="informe-body">{texto_html}</div>
  </div>
  <div class="footer">Generado por IMAGIAX AI &middot; ID: {informe_id[:8]}...</div>
</div></body></html>""")


@app.get("/informes/listado")
async def listado_informes(
    estado: str | None = Query(default=None, description="Filtro: aceptado | rechazado | editado_aceptado | pendiente"),
    limit: int = Query(default=50, ge=1, le=50, description="Máximo de resultados por página"),  # SEC-BE-009
    offset: int = Query(default=0, ge=0, description="Offset para paginación"),
) -> JSONResponse:
    """
    Listado de informes con paginación.
    
    Returns:
        - items: Lista de informes
        - total: Total de informes (con filtro aplicado)
        - limit: Límite actual
        - offset: Offset actual
        - has_more: True si hay más páginas
        - filtro_estado: Filtro aplicado (si hay)
    """
    try:
        result = await imagiax_db.get_todos_informes(
            estado=estado,
            limit=limit,
            offset=offset,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error al consultar listado: {exc}") from exc

    return JSONResponse({
        **result,
        "filtro_estado": estado,
    })



# ── Path traversal protection (SEC-BE-003, AD-3) ──────────────────────────────
_DICOMWEB_PATH_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9._/:-]*$')


def _sanitize_dicomweb_path(path: str) -> str:
    """
    Validates DICOMweb proxy path against a strict whitelist regex.
    Raises HTTPException(400) on any violation (path traversal, absolute paths, etc.).
    Valid DICOMweb paths: studies/X/series/Y/instances/Z/...
    """
    if '..' in path or path.startswith('/') or not _DICOMWEB_PATH_RE.match(path):
        raise HTTPException(status_code=400, detail="Invalid DICOMweb path")
    return path


# ── Proxy DICOMweb autenticado — redirige /pacs/dicom-web/* a Orthanc ─────────
@app.api_route("/pacs/dicom-web/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def pacs_dicom_proxy(
    path: str,
    request: Request,
    response: Response,
):
    """
    Proxy inverso DICOMweb protegido por cookie de sesión imagiax_session.

    OHIF apunta a /pacs/dicom-web/* en lugar de hablar directo a Orthanc :8042,
    eliminando la necesidad de exponer Orthanc al browser y evitando el popup
    de Basic Auth nativo.

    Seguridad:
    - SEC-BE-002 (AD-2): decode_token() + is_user_revoked() en lugar de verify_session_cookie_token()
    - SEC-BE-003 (AD-3): sanitize_dicomweb_path() antes de reenviar al PACS
    """
    # 1. Leer cookie de sesión
    token = request.cookies.get("imagiax_session")
    if not token:
        return JSONResponse(status_code=401, content={"detail": "No autenticado"})

    # 2. Validar token con decode_token (SEC-BE-002, AD-2) + revocation check
    try:
        proxy_user = imagiax_auth.decode_token(token)
    except HTTPException:
        return JSONResponse(status_code=401, content={"detail": "Sesión inválida"})

    if await imagiax_auth.is_user_revoked(proxy_user.user_id):
        return JSONResponse(status_code=401, content={"detail": "Cuenta revocada"})

    # 3. Sanitizar path contra traversal (SEC-BE-003, AD-3)
    try:
        path = _sanitize_dicomweb_path(path)
    except HTTPException:
        return JSONResponse(status_code=400, content={"detail": "Invalid DICOMweb path"})

    # 4. Construir URL upstream
    qs = request.url.query
    upstream_url = f"/dicom-web/{path}"
    if qs:
        upstream_url += f"?{qs}"

    # 5. Reenviar headers seguros (no Host, no Authorization — la inyectamos nosotros)
    SKIP_HEADERS = {"host", "authorization", "content-length", "transfer-encoding"}
    fwd_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in SKIP_HEADERS
    }

    # 6. Leer body del request (para POST/PUT)
    body = await request.body()

    # 7. Stream de la respuesta desde Orthanc
    if _pacs_client is None:
        return JSONResponse(status_code=503, content={"detail": "Cliente PACS no inicializado"})

    try:
        upstream_req = _pacs_client.build_request(
            method=request.method,
            url=upstream_url,
            headers=fwd_headers,
            content=body if body else None,
        )
        upstream_response = await _pacs_client.send(upstream_req, stream=True)

        # Generador async que itera los bytes Y cierra el stream al terminar
        async def _stream_and_close():
            try:
                async for chunk in upstream_response.aiter_bytes():
                    yield chunk
            finally:
                await upstream_response.aclose()

        resp_headers = {
            k: v for k, v in upstream_response.headers.items()
            if k.lower() not in {"transfer-encoding", "content-encoding", "content-length"}
        }
        return StreamingResponse(
            _stream_and_close(),
            status_code=upstream_response.status_code,
            headers=resp_headers,
            media_type=upstream_response.headers.get("content-type", "application/octet-stream"),
        )
    except httpx.TimeoutException:
        return JSONResponse(status_code=504, content={"detail": "Timeout conectando a PACS"})
    except httpx.RequestError as e:
        return JSONResponse(status_code=502, content={"detail": f"Error de conexión a PACS: {str(e)}"})

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8010, log_level="info")
