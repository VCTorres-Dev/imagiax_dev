"""
api.auth.router — FastAPI router for authentication endpoints.

Endpoints:
    POST /auth/login   — validate credentials, return JWT + httpOnly cookie
    GET  /auth/me      — return current authenticated user
    POST /auth/logout  — revoke token and clear session cookie
    GET  /login        — public login page (HTML)
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from api.auth.dependencies import get_current_user
from api.auth.models import LoginRequest, UserPayload
from api.auth.service import (
    check_login_rate_pg,
    clear_login_attempts_pg,
    create_access_token,
    decode_token,
    extract_raw_token,
    generate_session_fingerprint,
    get_token_expires_at_utc,
    get_user_by_email,
    log_audit,
    record_login_attempt_pg,
    revoke_token,
    verify_password_async,
)
from app.core.config import settings
from app.core.templates import templates

logger = logging.getLogger("imagiax.auth")

router = APIRouter(tags=["auth"])


def _sanitize_return_to(return_to: str | None) -> str:
    """Sanitize the returnTo query parameter to prevent open-redirect attacks."""
    if not return_to:
        return "/"
    rt = return_to.strip()
    if not rt.startswith("/"):
        return "/"
    if rt.startswith("//") or "\\" in rt or "://" in rt:
        return "/"
    return rt


# ── Login ──────────────────────────────────────────────────────────────────────

@router.post("/auth/login")
async def auth_login(body: LoginRequest, request: Request) -> JSONResponse:
    """
    Authenticate a user and issue a JWT.
    Responds with { access_token, token_type, user: {email, nombre, rol} }
    and sets the httpOnly 'imagiax_session' cookie.
    """
    pool = request.app.state.pool
    client_ip = request.client.host if request.client else "unknown"

    # La verificación de rate limit es fail-closed: si no podemos consultar el
    # estado de seguridad, preferimos frenar el login antes que degradarlo.
    try:
        if not await check_login_rate_pg(pool, body.email):
            return JSONResponse(
                {"detail": "Demasiados intentos fallidos. Espera unos minutos antes de reintentar."},
                status_code=429,
            )
    except Exception as exc:
        logger.error("Login rate-limit DB check failed — denying login (fail-closed): %s", exc)
        return JSONResponse(
            {"detail": "Sistema de seguridad temporalmente no disponible. Reintente en unos minutos."},
            status_code=503,
        )

    # Buscamos el usuario, pero la decisión final no depende solo de existir o
    # no: más abajo igual normalizamos el tiempo de verificación del password.
    try:
        user = await get_user_by_email(pool, body.email)
    except Exception:
        return JSONResponse(
            {"detail": "Base de datos no disponible. Arranca PostgreSQL y reinicia la API."},
            status_code=503,
        )

    # Verificamos siempre contra un hash válido para no filtrar por timing si el
    # email existe o no existe en la base.
    _DUMMY_HASH = "b2"
    hash_to_check = user["password_hash"] if user else _DUMMY_HASH
    try:
        password_ok = await verify_password_async(body.password, hash_to_check)
    except Exception:
        password_ok = False

    if not user or not password_ok:
        try:
            await record_login_attempt_pg(pool, body.email)
        except Exception:
            pass  # Don't let PG failure block the flow
        await log_audit(
            pool,
            usuario_id=str(user["id"]) if user else None,
            accion="login_fallido",
            ip=client_ip,
            detalle=f"email={body.email}",
            recurso=f"usuario:{body.email}",
        )
        raise HTTPException(status_code=401, detail="Credenciales incorrectas.")

    # Una cuenta inactiva nunca debe obtener token aunque la contraseña sea válida.
    if not user.get("activo", True):
        await log_audit(
            pool,
            usuario_id=str(user["id"]),
            accion="login_fallido",
            ip=client_ip,
            detalle=f"email={body.email} motivo=cuenta_inactiva",
            recurso=f"usuario:{body.email}",
        )
        raise HTTPException(status_code=403, detail="Cuenta desactivada. Contacta al administrador.")

    # El fingerprint ata la sesión al contexto del request y reduce el valor de
    # un token robado fuera del dispositivo/navegador esperado.
    expire_hours = float(settings.JWT_REMEMBER_HOURS if body.remember else settings.JWT_EXPIRE_HOURS)
    # Session fingerprinting: bind JWT to the device's User-Agent
    session_fp = generate_session_fingerprint(request)
    token = create_access_token(
        user_id=str(user["id"]),
        email=user["email"],
        nombre=user["nombre"],
        rol=user["rol"],
        expire_hours=expire_hours,
        fingerprint=session_fp,
        # SEC-H03: Store the session type so sliding-window renewal preserves it.
        remember=body.remember,
    )

    # 5b) Clear failed attempts on successful login — C-003
    try:
        await clear_login_attempts_pg(pool, body.email)
    except Exception:
        pass  # Don't block login if cleanup fails

    # 6) Audit successful login
    await log_audit(
        pool,
        usuario_id=str(user["id"]),
        accion="login_exitoso",
        ip=client_ip,
        detalle=f"email={user['email']} remember={body.remember}",
        recurso=f"usuario:{user['email']}",
    )

    # Devolvemos Bearer + cookie para cubrir API y panel web con la misma emisión.
    response = JSONResponse({
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "email": user["email"],
            "nombre": user["nombre"],
            "rol": user["rol"],
        },
    })
    response.set_cookie(
        key="imagiax_session",
        value=token,
        httponly=True,
        secure=settings.is_production,
        samesite="strict",
        max_age=int(expire_hours * 3600),
        path="/",
    )
    return response


# ── Me ─────────────────────────────────────────────────────────────────────────

@router.get("/auth/me")
async def auth_me(
    current_user: UserPayload = Depends(get_current_user),
) -> JSONResponse:
    """Return the currently authenticated user (Bearer token or cookie)."""
    return JSONResponse({
        "user": {
            "email": current_user.email,
            "nombre": current_user.nombre,
            "rol": current_user.rol,
        }
    })


# ── Logout ─────────────────────────────────────────────────────────────────────

@router.post("/auth/logout")
async def auth_logout(request: Request) -> JSONResponse:
    """Revoke the active token and delete the session cookie."""
    pool = request.app.state.pool
    raw_token = extract_raw_token(request)

    if raw_token:
        try:
            payload = decode_token(raw_token)
            if payload.jti:
                expires_at = get_token_expires_at_utc(raw_token)
                await revoke_token(
                    pool,
                    jti=payload.jti,
                    user_id=payload.user_id,
                    expires_at=expires_at,
                )
            await log_audit(
                pool,
                usuario_id=payload.user_id,
                accion="logout",
                ip=request.client.host if request.client else None,
                recurso=f"usuario:{getattr(payload, 'email', payload.user_id)}",
            )
        except Exception:
            # If the token is invalid/expired, still clear the cookie
            pass

    response = JSONResponse({"ok": True})
    response.delete_cookie(key="imagiax_session", path="/")
    return response


# ── Login page (HTML) ──────────────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_panel(request: Request) -> HTMLResponse:
    """Public login page (GSAP), separate from the admin dashboard."""
    return_to = _sanitize_return_to(request.query_params.get("returnTo"))
    return templates.TemplateResponse(
        "admin/login.html",
        {
            "request": request,
            "return_to": return_to,
            "viewer_url": settings.IMAGIAX_VIEWER_URL,
        },
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        },
    )


# ── /auth/verify — nginx auth_request subrequest ─────────────────────────────
# nginx sends a subrequest here to decide if the user is authenticated.
# Returns 200 (allow) or 401 (reject, nginx redirects to /login).

@router.get("/auth/verify", include_in_schema=False)
async def auth_verify(request: Request) -> Response:
    """
    Token verification endpoint for nginx auth_request.

    Two-phase check:
    1. Fast crypto check (sync, no DB) — verifies signature / expiry.
    2. Revocation check (async, DB) — JTI + user-level revocation.

    Graceful degradation: if DB fails, allow through to avoid OHIF mass lockout.
    """
    from api.auth.service import (
        verify_session_cookie_token,
        decode_token,
        is_jti_revoked,
        is_user_revoked,
    )

    token = request.cookies.get("imagiax_session")

    # Phase 1: crypto check (no DB)
    if not verify_session_cookie_token(token):
        return Response(status_code=401)

    # Phase 2: revocation check (DB) — fail-open on errors
    try:
        pool = request.app.state.pool
        payload = decode_token(token)  # type: ignore[arg-type]
        if payload.jti and await is_jti_revoked(pool, payload.jti):
            return Response(status_code=401)
        if await is_user_revoked(pool, payload.user_id):
            return Response(status_code=401)
    except Exception as exc:
        logger.warning(
            "auth_verify: revocation DB check failed — falling back to signature-only: %s",
            exc,
        )

    return Response(status_code=200)
