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

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

import asyncpg
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse

from app.core.config import settings
from app.core.database import create_pool_with_retries
from app.core.middleware import setup_middleware

logger = logging.getLogger("imagiax")


async def _bootstrap_pool_dependent_services(app: FastAPI) -> None:
    """Run startup/bootstrap work that requires an available PostgreSQL pool."""
    pool = getattr(app.state, "pool", None)
    if pool is None:
        logger.warning("IMAGIAX DB: pool not available; skipping pool-dependent bootstrap.")
        return

    # Auth tables (JWT revocation log, login_attempts, etc.)
    try:
        from api.auth.service import create_auth_tables, cleanup_old_login_attempts

        await create_auth_tables(pool)
        await cleanup_old_login_attempts(pool)
        logger.info("IMAGIAX Auth: tables verified/created.")
    except Exception as exc:
        logger.warning("IMAGIAX Auth: could not initialise auth tables.", exc_info=exc)

    # Bootstrap superadmin account from environment
    try:
        from api.auth.service import ensure_admin_exists, upsert_bootstrap_superadmin_from_env

        await ensure_admin_exists(pool)
        await upsert_bootstrap_superadmin_from_env(pool)
        logger.info("IMAGIAX Auth: bootstrap admin verified.")
    except Exception as exc:
        logger.warning("IMAGIAX Auth: bootstrap admin check failed.", exc_info=exc)

    # Ensure patient_age column exists (BUG-008 migration)
    try:
        async with pool.acquire() as conn:
            await conn.execute("ALTER TABLE informes ADD COLUMN IF NOT EXISTS patient_age VARCHAR(10) NULL")
        logger.info("IMAGIAX: patient_age column ensured.")
    except Exception as exc:
        logger.warning("IMAGIAX: Could not ensure patient_age column: %s", exc)

    # Payment tables + trial licence creation
    try:
        from infrastructure.pagos.db import create_tables, get_or_create_trial, get_or_create_installation_id

        await create_tables(pool)
        await get_or_create_trial(pool)
        installation_id = await get_or_create_installation_id(pool)
        logger.info("IMAGIAX Pagos: ready.", extra={"installation_id": installation_id})
    except Exception as exc:
        logger.warning("IMAGIAX Pagos: could not initialise payment tables.", exc_info=exc)


async def _background_reconnect_until_ready(app: FastAPI) -> None:
    """Retry PostgreSQL pool init in background and run deferred bootstrap when recovered."""
    interval = max(3.0, float(settings.POSTGRES_BACKGROUND_RETRY_SEC))
    delay_next = 2.0

    logger.warning(
        "IMAGIAX DB: startup pool unavailable; background reconnect active "
        "(first retry in ~2s, then every %.1fs).",
        interval,
    )

    while True:
        try:
            await asyncio.sleep(delay_next)
            delay_next = interval

            app.state.pool = await create_pool_with_retries(
                settings.POSTGRES_URL,
                retries=1,
                initial_delay_sec=0,
                backoff_multiplier=1.0,
                max_delay_sec=0,
            )
            logger.info("IMAGIAX DB: background reconnect succeeded.")
            await _bootstrap_pool_dependent_services(app)

            async def _cleanup_loop(pool: asyncpg.Pool) -> None:
                while True:
                    try:
                        await asyncio.sleep(3600)
                        from api.auth.service import cleanup_expired_revoked_tokens, cleanup_old_login_attempts

                        deleted = await cleanup_expired_revoked_tokens(pool)
                        await cleanup_old_login_attempts(pool)
                        if deleted:
                            logger.info("IMAGIAX Auth: cleaned up %d expired revoked_tokens rows.", deleted)
                    except asyncio.CancelledError:
                        break
                    except Exception as _exc:
                        logger.warning("IMAGIAX Auth: cleanup loop error — %s", _exc)

            app.state.cleanup_task = asyncio.create_task(_cleanup_loop(app.state.pool))
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("IMAGIAX DB: background reconnect attempt failed: %s", exc)


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
    app.state.pool = None
    _reconnect_task: asyncio.Task | None = None
    try:
        app.state.pool = await create_pool_with_retries(
            settings.POSTGRES_URL,
            retries=max(1, int(settings.POSTGRES_CONNECT_RETRIES)),
            initial_delay_sec=max(0.0, float(settings.POSTGRES_CONNECT_DELAY_SEC)),
            backoff_multiplier=max(1.0, float(settings.POSTGRES_CONNECT_BACKOFF_MULTIPLIER)),
            max_delay_sec=max(0.0, float(settings.POSTGRES_CONNECT_MAX_DELAY_SEC)),
        )
    except Exception as exc:
        logger.warning("IMAGIAX DB: startup pool init exhausted retries.", exc_info=exc)
        if settings.IMAGIAX_REQUIRE_POSTGRES:
            raise RuntimeError(
                "IMAGIAX_REQUIRE_POSTGRES=true and PostgreSQL pool could not be initialized at startup."
            ) from exc

    # 3) Pool-dependent bootstrap (or deferred recovery path)
    if app.state.pool is not None:
        await _bootstrap_pool_dependent_services(app)
    else:
        _reconnect_task = asyncio.create_task(_background_reconnect_until_ready(app))

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

    _cleanup_task: asyncio.Task | None = None
    app.state.cleanup_task = None
    if app.state.pool is not None:
        _cleanup_task = asyncio.create_task(_cleanup_loop(app.state.pool))
        app.state.cleanup_task = _cleanup_task

    yield

    # -- Shutdown -------------------------------------------------------------
    if _reconnect_task is not None:
        _reconnect_task.cancel()
        try:
            await _reconnect_task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    cleanup_task = _cleanup_task or getattr(app.state, "cleanup_task", None)
    if cleanup_task is not None:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except Exception:
            pass
    if getattr(app.state, "orthanc", None):
        await app.state.orthanc.aclose()
    # app.state.pacs is an alias to app.state.orthanc — already closed above
    if getattr(app.state, "pool", None) is not None:
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
        gemini_key = settings.GEMINI_API_KEY.get_secret_value() if settings.GEMINI_API_KEY else ""
        gemini_modelo = settings.GEMINI_MODEL
        gemini_estado = "configurado" if gemini_key else "no_configurado"
        componentes["gemini"] = {"estado": gemini_estado, "modelo": gemini_modelo}

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

        return {
            "estado": estado,
            "version": app.version,
            "servicio": "IMAGIAX",
            "patologias": patologias,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "componentes": componentes,
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

    @app.get("/ping", tags=["health"])
    async def ping() -> dict[str, str]:
        """Public liveness probe used by Docker healthchecks and load balancers."""
        return {"status": "ok"}

    @app.get("/health/db", tags=["health"])
    async def health_db(request: Request) -> dict[str, str | bool]:
        """Readiness check for PostgreSQL connectivity."""
        pool = getattr(request.app.state, "pool", None)
        if pool is None:
            raise HTTPException(
                status_code=503,
                detail=(
                    "Pool de PostgreSQL no inicializado. Revisa logs de arranque (IMAGIAX DB), "
                    "POSTGRES_URL y que el servidor Postgres esté accesible."
                ),
            )

        try:
            async with pool.acquire() as conn:
                n = await conn.fetchval("SELECT 1")
            if n != 1:
                raise HTTPException(status_code=503, detail="PostgreSQL respondió de forma inesperada.")
            return {"ok": True, "database": "up"}
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"PostgreSQL no responde: {exc!s}") from exc

    @app.get("/robots.txt", response_class=PlainTextResponse, include_in_schema=False)
    async def robots_txt() -> str:
        """SEO robots policy aligned with monolith contract."""
        base = settings.IMAGIAX_BASE_URL.rstrip("/")
        return (
            "User-agent: *\n"
            "Disallow: /admin\n"
            "Disallow: /admin/\n"
            "Disallow: /api/\n"
            "Disallow: /auth/\n"
            "Disallow: /pacs/\n"
            "Disallow: /docs\n"
            "Disallow: /redoc\n"
            "Disallow: /openapi.json\n"
            "\n"
            f"Sitemap: {base}/sitemap.xml\n"
        )

    @app.get("/sitemap.xml", include_in_schema=False)
    async def sitemap_xml() -> Response:
        """Minimal XML sitemap aligned with monolith contract."""
        base = settings.IMAGIAX_BASE_URL.rstrip("/")
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            "  <url>\n"
            f"    <loc>{base}/</loc>\n"
            "    <changefreq>monthly</changefreq>\n"
            "    <priority>1.0</priority>\n"
            "  </url>\n"
            "</urlset>\n"
        )
        return Response(content=xml, media_type="application/xml")

    # /favicon.ico — avoid 404 noise in logs
    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=204)

    return app
