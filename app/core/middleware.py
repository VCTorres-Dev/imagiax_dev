"""
app.core.middleware -- CORS and request-lifecycle middleware setup.

Extracted from servidor_api_orthanc_v1.py.

Usage (in create_app):
    from app.core.middleware import setup_middleware
    setup_middleware(app)
"""

from __future__ import annotations

import logging
import time
import uuid as _uuid_mod
from contextvars import ContextVar

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.config import settings
from app.core.security import RateLimiter

logger = logging.getLogger("imagiax")

# Webhook paths that must be exempt from CSRF checks — payment providers
# don't send Origin/Referer headers in their server-to-server calls.
_CSRF_EXEMPT_PREFIXES = ("/pagos/webhook",)  # Only MP IPN / TBK callbacks are GETs (already CSRF-safe)

# HTTP methods that mutate state and therefore require CSRF validation.
_CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class CSRFOriginMiddleware(BaseHTTPMiddleware):
    """Origin/Referer validation middleware (CSRF protection).

    For every state-mutating request (POST, PUT, PATCH, DELETE, …):
      1. Read the Origin header; fall back to Referer if absent.
      2. Parse scheme + host (+ port) from that value.
      3. Accept if it matches any origin in settings.cors_origins_list OR
         settings.IMAGIAX_BASE_URL.
      4. Reject with 403 {"detail": "Origin not allowed"} otherwise.

    Webhook endpoints are exempt because payment providers don't send
    Origin/Referer headers in server-to-server callbacks.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.method in _CSRF_SAFE_METHODS:
            return await call_next(request)

        # Exempt payment-provider webhooks
        path = request.url.path
        if any(path.startswith(p) for p in _CSRF_EXEMPT_PREFIXES):
            return await call_next(request)

        # Build allowed-origin set: CORS list + own base URL
        allowed = set(settings.cors_origins_list)
        if settings.IMAGIAX_BASE_URL:
            allowed.add(settings.IMAGIAX_BASE_URL.rstrip("/"))

        # Normalise an origin string: strip trailing slash, keep scheme+host+port
        def _normalise(raw: str) -> str:
            from urllib.parse import urlparse
            parsed = urlparse(raw.strip())
            origin = parsed.scheme + "://" + parsed.netloc
            return origin.rstrip("/")

        # Prefer Origin header; fall back to Referer
        raw_origin = request.headers.get("origin") or request.headers.get("referer")
        if not raw_origin:
            from starlette.responses import JSONResponse as _JSONResponse
            return _JSONResponse({"detail": "Origin not allowed"}, status_code=403)

        request_origin = _normalise(raw_origin)
        normalised_allowed = {_normalise(o) for o in allowed}

        if request_origin not in normalised_allowed:
            from starlette.responses import JSONResponse as _JSONResponse
            return _JSONResponse({"detail": "Origin not allowed"}, status_code=403)

        return await call_next(request)

# -- Correlation-ID context variable ------------------------------------------
# Shared with logging formatters so every log line carries the request ID.
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

# -- Rate limiter: secondary defense for analysis endpoints (5r/m per worker) --
# NOTE: This is a secondary defense layer.
# Primary rate limiting is done by nginx (see nginx-rate-zones.conf).
# nginx analisis_zone = 10r/m; in-process limit = 5r/m (50% of nginx).
# With 4 workers, worst-case combined = 20r/m (~2× nginx limit).
_analysis_limiter = RateLimiter(max_requests=5, window_seconds=60)

# Prefixes that are subject to rate limiting
_RATE_LIMITED_PREFIXES = ("/analizar/", "/generar/", "/ai/analizar", "/ai/generar")
# Polling endpoint returned by POST /generar/informe (202 Accepted) must not be
# rate-limited with write endpoints; otherwise a single analysis can hit 429
# while checking status every few seconds.
_RATE_LIMIT_EXEMPT_PREFIXES = ("/generar/informe/estado/", "/ai/generar/informe/estado/")


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Applies IP-based rate limiting to AI analysis and report generation endpoints."""

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        if any(path.startswith(p) for p in _RATE_LIMIT_EXEMPT_PREFIXES):
            return await call_next(request)
        if any(path.startswith(p) for p in _RATE_LIMITED_PREFIXES):
            client_ip = request.client.host if request.client else "unknown"
            if not _analysis_limiter.check(client_ip):
                from starlette.responses import JSONResponse

                return JSONResponse(
                    {"detail": "Demasiadas solicitudes. Intenta en 1 minuto."},
                    status_code=429,
                )
        return await call_next(request)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Generates/propagates X-Request-ID, measures duration, emits lifecycle log.

    Runs as the outermost layer so it sees the real status_code and duration.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        # Generate or propagate correlation ID
        req_id = request.headers.get("X-Request-ID") or str(_uuid_mod.uuid4())
        token = request_id_var.set(req_id)

        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            logger.exception(
                "%s %s 500",
                request.method,
                request.url.path,
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": 500,
                    "duration_ms": duration_ms,
                    "request_id": req_id,
                },
            )
            request_id_var.reset(token)
            raise

        duration_ms = round((time.perf_counter() - start) * 1000, 2)

        # Attempt to extract user_id/role from JWT cookie (sync, no DB I/O)
        _user_id = None
        _role = None
        _session_token = request.cookies.get("imagiax_session")
        if _session_token:
            try:
                from api.auth.service import decode_token

                _jwt_payload = decode_token(_session_token)
                _user_id = _jwt_payload.user_id
                _role = _jwt_payload.rol
            except Exception:
                pass  # Invalid or expired token -- continue without user info

        logger.info(
            "%s %s %s",
            request.method,
            request.url.path,
            response.status_code,
            extra={
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
                "request_id": req_id,
                "user_id": _user_id,
                "role": _role,
            },
        )

        response.headers["X-Request-ID"] = req_id
        request_id_var.reset(token)
        return response


def setup_middleware(app: FastAPI) -> None:
    """Register all middleware on the FastAPI application.

    Order matters: Starlette processes middleware LIFO (last added = outermost).
    We want RequestLogging to be outermost (last added here), so it sees the
    real status_code after all inner middleware have run.

    Registration order (innermost -> outermost):
      1. CORS          -- handles preflight before any auth/rate-limit logic
      2. CSRF          -- validates Origin/Referer on mutating requests
      3. RateLimit     -- blocks abusive IPs early
      4. RequestLogging -- outermost: measures total duration + real status_code
    """
    # 1. CORS -- SEC-BE-008: wildcard already rejected by Settings validator
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization", "X-Requested-With"],
    )

    # 2. CSRF -- Origin/Referer validation for state-mutating requests
    app.add_middleware(CSRFOriginMiddleware)

    # 3. Rate limiting for AI endpoints
    app.add_middleware(RateLimitMiddleware)

    # 4. Request logging (outermost -- added last)
    app.add_middleware(RequestLoggingMiddleware)
