"""
app.auth.dependencies — FastAPI dependency functions for authentication.

Provides get_current_user and require_role for use with Depends() in route handlers.

Sliding-window JWT renewal (W2):
    When a valid token has passed 50% of its lifetime, a new JWT is transparently issued
    and set as the response cookie. The user never notices; inactive sessions still expire.
"""

from __future__ import annotations

import logging

from fastapi import Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.auth.models import UserPayload
from app.auth.service import (
    create_access_token,
    decode_token_with_renewal_check,
    generate_session_fingerprint,
    is_jti_revoked,
    is_user_revoked,
)
from app.core.config import settings

logger = logging.getLogger("imagiax.auth")

# Bearer token extraction scheme (auto_error=False so cookie fallback works)
_bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    response: Response,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    request: Request = None,
) -> UserPayload:
    """
    FastAPI dependency: extract the authenticated user from a Bearer token or httpOnly cookie.
    Bearer takes precedence (for API clients). Cookie is used for the admin panel.

    Sliding-window renewal (W2):
        If the token has passed 50% of its lifetime, a new token is created and set
        as the response cookie transparently. Only applies to cookie-based sessions —
        Bearer tokens are renewed by the API client itself.

    Also checks if the user has been revoked (deleted or globally logged out).
    """
    token: str | None = None
    from_cookie = False

    if credentials:
        token = credentials.credentials
    elif request:
        token = request.cookies.get("imagiax_session")
        from_cookie = token is not None

    if not token:
        raise HTTPException(status_code=401, detail="Autenticación requerida")

    user, needs_renewal = decode_token_with_renewal_check(token)

    # Verify session fingerprint (if the token has one)
    if user.fp and request:
        current_fp = generate_session_fingerprint(request)
        if user.fp != current_fp:
            logger.warning(
                "Session fingerprint mismatch — posible robo de sesión",
                extra={
                    "email": user.email,
                    "expected_fp": user.fp[:8] + "...",
                    "actual_fp": current_fp[:8] + "...",
                },
            )
            raise HTTPException(
                status_code=401,
                detail="Sesión inválida — dispositivo no reconocido",
            )

    pool = request.app.state.pool

    if user.jti and await is_jti_revoked(pool, user.jti):
        raise HTTPException(status_code=401, detail="Sesión revocada")

    if await is_user_revoked(pool, user.user_id):
        raise HTTPException(status_code=401, detail="Cuenta desactivada o eliminada")

    # Sliding-window renewal — only for cookie sessions (not Bearer API tokens)
    if needs_renewal and from_cookie:
        try:
            fp = generate_session_fingerprint(request) if request else None
            # SEC-H03: Preserve the original session type from the `remember` claim.
            # Old JWTs without the claim default to remember=False (normal session duration).
            # This prevents normal sessions from being silently extended to remember-me lifetime.
            if user.remember:
                renewal_hours = float(settings.JWT_REMEMBER_HOURS)
                renewal_max_age = int(settings.JWT_REMEMBER_HOURS * 3600)
            else:
                renewal_hours = float(settings.JWT_EXPIRE_HOURS)
                renewal_max_age = int(settings.JWT_EXPIRE_HOURS * 3600)

            new_token = create_access_token(
                user_id=user.user_id,
                email=user.email,
                nombre=user.nombre,
                rol=user.rol,
                expire_hours=renewal_hours,
                fingerprint=fp,
                remember=user.remember,
                clinic_id=user.clinic_id,
                scope=user.scope,
            )
            response.set_cookie(
                key="imagiax_session",
                value=new_token,
                httponly=True,
                secure=settings.is_production,
                samesite="strict",
                max_age=renewal_max_age,
                path="/",
            )
            logger.debug(
                "JWT sliding-window renewal issued",
                extra={"user_id": user.user_id[:8] + "...", "remember": user.remember},
            )
        except Exception as exc:
            # Renewal failure must never block the authenticated request
            logger.warning("JWT sliding-window renewal failed — skipping: %s", exc)

    return user


def require_role(*allowed_roles: str):
    """
    Dependency factory to restrict access by role.
    superadmin always passes — it has privileges above all other roles.
    Usage: user = Depends(require_role("admin", "medico"))
    """
    async def _check(user: UserPayload = Depends(get_current_user)) -> UserPayload:
        if user.rol != "superadmin" and user.rol not in allowed_roles:
            raise HTTPException(
                status_code=403,
                detail=f"Permiso denegado. Rol requerido: {', '.join(allowed_roles)}",
            )
        return user

    return _check
