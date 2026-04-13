"""
app.auth.router — FastAPI router for authentication endpoints.

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

from app.auth.dependencies import get_current_user
from app.auth.models import LoginRequest, UserPayload
from app.auth.service import (
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
    verify_password,
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

    # 1) Rate limit — C-003: PostgreSQL-backed brute-force protection
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

    # 2) Look up user in DB
    # C-003: always run hash-verify to avoid email enumeration via timing
    try:
        user = await get_user_by_email(pool, body.email)
    except Exception:
        return JSONResponse(
            {"detail": "Base de datos no disponible. Arranca PostgreSQL y reinicia la API."},
            status_code=503,
        )

    # 3) Verify credentials — always run verify_password to normalise timing
    # Even if user doesn't exist, verify against a dummy hash for constant time
    _DUMMY_HASH = "b2"
    hash_to_check = user["password_hash"] if user else _DUMMY_HASH
    try:
        password_ok = verify_password(body.password, hash_to_check)
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

    # 4) Check that the account is active
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

    # 5) Issue JWT
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

    # 7) Response with httpOnly cookie
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
    from app.auth.service import (
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
