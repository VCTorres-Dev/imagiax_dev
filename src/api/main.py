"""
api.main -- IMAGIAX application factory.

Entry point:
    uvicorn api.main:create_app --factory --host 0.0.0.0 --port 8010

The create_app() factory pattern allows:
- Testing: each test suite gets a fresh app instance
- Multiple environments without module-level side effects
- Clean lifespan management per instance
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

import asyncpg
from fastapi import Depends, FastAPI, Request, Response

from app.core.config import settings
from app.core.database import create_pool
from app.core.middleware import setup_middleware

logger = logging.getLogger("imagiax")


def _config_source(*, env_keys: list[str], current_value: object, default_value: object | None = None) -> str:
    """Return configuration source as runtime|env|default (non-secret aware)."""
    for key in env_keys:
        val = os.getenv(key)
        if val is not None and str(val).strip() != "":
            return "env"
    if default_value is not None and current_value == default_value:
        return "default"
    return "runtime"


def _gemini_health_payload() -> dict[str, object]:
    """Build Gemini health payload without exposing secrets."""
    api_key_configured = bool(settings.GEMINI_API_KEY and settings.GEMINI_API_KEY.get_secret_value())
    vertex_project_configured = bool((settings.VERTEX_AI_PROJECT or "").strip())
    service_account_configured = bool((settings.GOOGLE_APPLICATION_CREDENTIALS or "").strip())

    mode_raw = settings.gemini_auth_mode
    if mode_raw == "api_key":
        auth_mode = "api_key"
        estado = "ok"
        semaforo = "green"
        reason_code = "ok_api_key"
        reason_detail = "Gemini configurado por API key."
    elif mode_raw == "vertex_ai":
        auth_mode = "vertex_service_account"
        if vertex_project_configured and service_account_configured:
            estado = "ok"
            semaforo = "green"
            reason_code = "ok_vertex_ai"
            reason_detail = "Gemini configurado por Vertex AI con service account."
        else:
            estado = "degradado"
            semaforo = "yellow"
            reason_code = "missing_vertex_project_or_service_account"
            reason_detail = "Falta VERTEX_AI_PROJECT o GOOGLE_APPLICATION_CREDENTIALS para Vertex AI."
    else:
        auth_mode = "unconfigured"
        estado = "error"
        semaforo = "red"
        reason_code = "missing_api_key_and_vertex_config"
        reason_detail = "Gemini no configurado: faltan API key y credenciales Vertex."

    return {
        "estado": estado,
        "semaforo": semaforo,
        "auth_mode": auth_mode,
        "modelo": settings.GEMINI_MODEL,
        "reason": reason_detail,
        "reason_code": reason_code,
        "reason_detail": reason_detail,
        "checks": {
            "api_key_configured": api_key_configured,
            "vertex_project_configured": vertex_project_configured,
            "service_account_configured": service_account_configured,
        },
    }


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage startup and shutdown tasks for the IMAGIAX application."""

    # -- Startup --------------------------------------------------------------

    # 1) PyTorch model warm-up -> stored in app.state.model / app.state.device
    try:
        from app.ai.model_loader import get_model, DEVICE

        app.state.model = get_model()
        app.state.device = DEVICE
        logger.info("IMAGIAX AI: model warm-up complete (device=%s).", DEVICE)
    except Exception as exc:
        app.state.model = None
        app.state.device = None
        logger.warning("IMAGIAX AI: model warm-up skipped.", exc_info=exc)

    # 2) PostgreSQL connection pool -> stored in app.state.pool
    app.state.pool = await create_pool(settings.POSTGRES_URL)

    readonly_canary = settings.IMAGIAX_CANARY_READONLY

    # 3) Auth tables (JWT revocation log, login_attempts, etc.)
    if readonly_canary:
        logger.warning("IMAGIAX Canary: skipping auth table migrations and login cleanup.")
    else:
        try:
            from api.auth.service import create_auth_tables, cleanup_old_login_attempts

            await create_auth_tables(app.state.pool)
            await cleanup_old_login_attempts(app.state.pool)
            logger.info("IMAGIAX Auth: tables verified/created.")
        except Exception as exc:
            logger.warning("IMAGIAX Auth: could not initialise auth tables.", exc_info=exc)

    # 3b) Bootstrap superadmin account from environment
    if readonly_canary:
        logger.warning("IMAGIAX Canary: skipping bootstrap admin checks.")
    else:
        try:
            from api.auth.service import ensure_admin_exists, upsert_bootstrap_superadmin_from_env
            await ensure_admin_exists(app.state.pool)
            await upsert_bootstrap_superadmin_from_env(app.state.pool)
            logger.info("IMAGIAX Auth: bootstrap admin verified.")
        except Exception as exc:
            logger.warning("IMAGIAX Auth: bootstrap admin check failed.", exc_info=exc)

    # 3c) Ensure patient_age column exists (BUG-008 migration)
    if readonly_canary:
        logger.warning("IMAGIAX Canary: skipping patient_age schema migration.")
    else:
        try:
            async with app.state.pool.acquire() as conn:
                await conn.execute(
                    "ALTER TABLE informes ADD COLUMN IF NOT EXISTS patient_age VARCHAR(10) NULL"
                )
            logger.info("IMAGIAX: patient_age column ensured.")
        except Exception as exc:
            logger.warning("IMAGIAX: Could not ensure patient_age column: %s", exc)

    # 4) Payment tables + trial licence creation
    if readonly_canary:
        logger.warning("IMAGIAX Canary: skipping payment table/trial initialisation.")
    else:
        try:
            from infrastructure.pagos.db import create_tables, get_or_create_trial, get_or_create_installation_id

            await create_tables(app.state.pool)
            await get_or_create_trial(app.state.pool)
            installation_id = await get_or_create_installation_id(app.state.pool)
            logger.info("IMAGIAX Pagos: ready.", extra={"installation_id": installation_id})
        except Exception as exc:
            logger.warning("IMAGIAX Pagos: could not initialise payment tables.", exc_info=exc)

    # 5) Pre-load Jinja2 prompt templates from YAML files
    try:
        from app.ai.prompt_orchestrator import load_templates

        _prompts_dir = os.path.join(os.path.dirname(__file__), "..", "..", "prompts")
        templates = load_templates(os.path.normpath(_prompts_dir))
        logger.info("IMAGIAX Prompts: %d templates loaded.", len(templates))
    except Exception as exc:
        logger.warning("IMAGIAX Prompts: could not pre-load prompt templates.", exc_info=exc)

    # 6) Orthanc + PACS shared HTTP clients
    import httpx as _httpx
    _tbk_timeout = _httpx.Timeout(settings.IMAGIAX_HTTP_TIMEOUT)
    _orthanc_headers = {}
    if settings.ORTHANC_AUTH_HEADER:
        _auth = settings.ORTHANC_AUTH_HEADER
        # Ensure "Basic " prefix — env var may contain raw base64 only
        if not _auth.lower().startswith(("basic ", "bearer ")):
            _auth = "Basic " + _auth
        _orthanc_headers["Authorization"] = _auth
    elif settings.ORTHANC_USER:
        import base64 as _b64
        _creds = f"{settings.ORTHANC_USER}:{settings.ORTHANC_PASSWORD.get_secret_value()}"
        _orthanc_headers["Authorization"] = "Basic " + _b64.b64encode(_creds.encode()).decode()
    # One shared client — pacs is an alias for backward compat
    app.state.orthanc = _httpx.AsyncClient(
        base_url=settings.ORTHANC_BASE_URL,
        headers=_orthanc_headers,
        timeout=_tbk_timeout,
    )
    app.state.pacs = app.state.orthanc  # alias — same client, same connection pool
    logger.info("IMAGIAX AI: Orthanc client ready (pacs aliased to orthanc).")

    # 7) Periodic cleanup — revoked_tokens grows as users log out and tokens are renewed.
    #    Every hour we delete rows that are already past their expiry (PERF-003).
    async def _cleanup_loop(pool: asyncpg.Pool) -> None:
        import asyncio as _asyncio
        while True:
            try:
                await _asyncio.sleep(3600)  # 1 hour
                from api.auth.service import cleanup_expired_revoked_tokens, cleanup_old_login_attempts
                deleted = await cleanup_expired_revoked_tokens(pool)
                await cleanup_old_login_attempts(pool)
                if deleted:
                    logger.info("IMAGIAX Auth: cleaned up %d expired revoked_tokens rows.", deleted)
            except _asyncio.CancelledError:
                break
            except Exception as _exc:
                logger.warning("IMAGIAX Auth: cleanup loop error — %s", _exc)

    import asyncio as _asyncio_main
    _cleanup_task = None
    if readonly_canary:
        logger.warning("IMAGIAX Canary: skipping periodic DB cleanup loop.")
    else:
        _cleanup_task = _asyncio_main.create_task(_cleanup_loop(app.state.pool))

    yield

    # -- Shutdown -------------------------------------------------------------
    if _cleanup_task:
        _cleanup_task.cancel()
        try:
            await _cleanup_task
        except Exception:
            pass
    if getattr(app.state, "orthanc", None):
        await app.state.orthanc.aclose()
    # app.state.pacs is an alias to app.state.orthanc — already closed above
    await app.state.pool.close()
    logger.info("IMAGIAX: PostgreSQL pool closed.")


def create_app() -> FastAPI:
    """Create and return a fully configured FastAPI application instance."""

    _is_prod = settings.is_production

    app = FastAPI(
        title="IMAGIAX API",
        version="1.0.0",
        lifespan=_lifespan,
        # SEC-INFRA: hide interactive docs in production
        docs_url=None if _is_prod else "/docs",
        redoc_url=None if _is_prod else "/redoc",
        openapi_url=None if _is_prod else "/openapi.json",
    )

    # -- Middleware ------------------------------------------------------------
    setup_middleware(app)

    # -- Routers --------------------------------------------------------------

    # Auth -- login, logout, /auth/me
    from api.auth.router import router as auth_router

    app.include_router(auth_router, tags=["auth"])

    # Admin panel
    from app.admin.router import router as admin_router

    app.include_router(admin_router, tags=["admin"])

    # Payments -- Transbank Oneclick (router already has prefix="/tbk")
    from infrastructure.pagos.tbk.router import router as tbk_router

    app.include_router(tbk_router, tags=["pagos-tbk"])

    # Payments -- MercadoPago (NO prefix -- routes already have /pagos/*)
    from infrastructure.pagos.mp.router import router as mp_router

    app.include_router(mp_router, tags=["pagos-mp"])

    # AI analysis & report generation (NO prefix -- nginx strips /api/ before proxy)
    from api.ai.router import router as ai_router

    app.include_router(ai_router, tags=["ai"])

    # Medical reports (router already has prefix="/informes")
    from app.informes.router import router as informes_router

    app.include_router(informes_router, tags=["informes"])

    # ── Missing production routes ─────────────────────────────────────────────
    from api.auth.dependencies import get_current_user as get_current_user_dep  # noqa: F401
    from api.auth.dependencies import require_role
    from api.auth.models import UserPayload


    # /salud — lightweight health check (Docker HEALTHCHECK + load-balancer probes)
    # Returns ONLY {"status": "ok"} — no internal topology exposed publicly.
    @app.get("/salud", tags=["health"])
    async def salud() -> dict:
        """Lightweight health check for Docker HEALTHCHECK and load-balancer probes."""
        return {"status": "ok"}

    # /salud/detalle — detailed diagnostic for admins (DB, model, Orthanc, Gemini)
    # Response shape matches admin_spa.html loadSistema() expectations.
    @app.get("/salud/detalle", tags=["health"])
    async def salud_detalle(
        request: Request,
        _user: UserPayload = Depends(require_role("admin")),
    ) -> dict:
        """Detailed health diagnostics — requires admin role."""
        from datetime import datetime, timezone

        now_iso = datetime.now(timezone.utc).isoformat()
        estado = "ok"
        componentes: dict = {}

        # PostgreSQL check
        pg_estado = "error"
        try:
            async with request.app.state.pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            pg_estado = "ok"
        except Exception:
            estado = "degradado"
        componentes["postgresql"] = {"estado": pg_estado}

        # Orthanc PACS check
        orthanc_estado = "error"
        orthanc_url = settings.ORTHANC_BASE_URL
        orthanc_client = getattr(request.app.state, "orthanc", None)
        if orthanc_client:
            try:
                resp = await orthanc_client.get("/system")
                if resp.status_code < 400:
                    orthanc_estado = "ok"
            except Exception:
                pass
        componentes["orthanc"] = {"estado": orthanc_estado, "url": orthanc_url}

        # Gemini IA check
        gemini_payload = _gemini_health_payload()
        if gemini_payload["estado"] != "ok" and estado == "ok":
            estado = "degradado"
        componentes["gemini"] = gemini_payload

        # AI model (ResNet/TorchXRayVision) check
        model_loaded = getattr(request.app.state, "model", None) is not None
        if not model_loaded:
            estado = "degradado"

        # Pathologies count
        try:
            from infrastructure.dicom_service import NIH_PATOLOGIAS
            patologias = f"{len(NIH_PATOLOGIAS)} patologías (ChestX-ray14)"
        except Exception:
            patologias = "—"

        configuracion = {
            "version": {
                "valor": app.version,
                "fuente": "runtime",
                "refrescado_en": now_iso,
            },
            "servicio": {
                "valor": "IMAGIAX",
                "fuente": "runtime",
                "refrescado_en": now_iso,
            },
            "gemini_model": {
                "valor": settings.GEMINI_MODEL,
                "fuente": _config_source(env_keys=["GEMINI_MODEL"], current_value=settings.GEMINI_MODEL, default_value="gemini-2.5-flash"),
                "refrescado_en": now_iso,
            },
            "gemini_auth_mode": {
                "valor": gemini_payload.get("auth_mode", "unconfigured"),
                "fuente": "runtime",
                "refrescado_en": now_iso,
            },
            "gemini_credentials": {
                "valor": {
                    "api_key_configured": gemini_payload.get("checks", {}).get("api_key_configured", False),
                    "vertex_project_configured": gemini_payload.get("checks", {}).get("vertex_project_configured", False),
                    "service_account_configured": gemini_payload.get("checks", {}).get("service_account_configured", False),
                },
                "fuente": "runtime",
                "refrescado_en": now_iso,
            },
            "vertex_project": {
                "valor": settings.VERTEX_AI_PROJECT or "—",
                "fuente": _config_source(env_keys=["VERTEX_AI_PROJECT"], current_value=settings.VERTEX_AI_PROJECT),
                "refrescado_en": now_iso,
            },
            "service_account_path": {
                "valor": settings.GOOGLE_APPLICATION_CREDENTIALS or "—",
                "fuente": _config_source(env_keys=["GOOGLE_APPLICATION_CREDENTIALS"], current_value=settings.GOOGLE_APPLICATION_CREDENTIALS),
                "refrescado_en": now_iso,
            },
            "pathologias": {
                "valor": patologias,
                "fuente": "runtime",
                "refrescado_en": now_iso,
            },
        }

        return {
            "estado": estado,
            "version": app.version,
            "servicio": "IMAGIAX",
            "patologias": patologias,
            "timestamp_utc": now_iso,
            "componentes": componentes,
            "configuracion": configuracion,
        }

    # /config/frontend — public config for OHIF + frontend JS
    @app.get("/config/frontend", tags=["config"])
    async def config_frontend(current_user=Depends(get_current_user_dep)) -> dict:
        """Frontend configuration: doctor info, brand, pricing."""
        cfg = {
            "doctor_name": settings.IMAGIAX_DOCTOR_NAME,
            "doctor_specialty": settings.IMAGIAX_DOCTOR_SPECIALTY,
            "doctor_license": settings.IMAGIAX_DOCTOR_LICENSE,
            "brand_name": settings.IMAGIAX_BRAND_NAME,
            "brand_subtitle": settings.IMAGIAX_BRAND_SUBTITLE,
            "backend_url": settings.IMAGIAX_BASE_URL,
            "viewer_url": settings.IMAGIAX_VIEWER_URL,
            "precio_scan_clp": settings.IMAGIAX_PRECIO_SCAN,
        }
        return {k: v for k, v in cfg.items() if v}

    # /favicon.ico — avoid 404 noise in logs
    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=204)

    return app
