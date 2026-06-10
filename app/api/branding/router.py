"""
app.api.branding.router — Self-service branding endpoints.

Routes (prefix="/me"):
  GET    /me/branding   JSON  current user's branding config
  PUT    /me/branding   JSON  upsert branding fields (partial update)
  DELETE /me/branding   JSON  soft-delete (activo=FALSE) for audit trail

All endpoints require authentication via Depends(get_current_user).
Client-supplied user_id is always ignored — identity comes from the session.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from app.auth.dependencies import get_current_user
from app.auth.models import UserPayload
from app.informes.branding_validation import BrandingRequest
from app.informes import db as informes_db

logger = logging.getLogger("imagiax.branding")

router = APIRouter(tags=["branding"])


@router.get("/me/branding")
async def get_branding(
    request: Request,
    user: UserPayload = Depends(get_current_user),
) -> dict:
    """Return the authenticated user's active branding config.

    Returns branding fields or an empty dict if no config exists.
    """
    pool = request.app.state.pool
    result = await informes_db.get_branding_for_user(pool, usuario_id=user.user_id)
    if result is None:
        return {}
    return result


@router.put("/me/branding")
async def put_branding(
    body: BrandingRequest,
    request: Request,
    user: UserPayload = Depends(get_current_user),
) -> dict:
    """Upsert branding config for the authenticated user.

    All fields are optional — None means "keep existing value".
    The user_id field is always forced to None (ignored).
    """
    pool = request.app.state.pool
    result = await informes_db.save_branding_config(
        pool,
        usuario_id=user.user_id,
        brand_name=body.brand_name,
        brand_subtitle=body.brand_subtitle,
        doctor_name=body.doctor_name,
        doctor_specialty=body.doctor_specialty,
        doctor_license=body.doctor_license,
        logo_b64=body.logo_b64,
        signature_b64=body.signature_b64,
    )
    return result


@router.delete("/me/branding")
async def delete_branding(
    request: Request,
    user: UserPayload = Depends(get_current_user),
) -> dict:
    """Soft-delete the authenticated user's branding config (activo=FALSE).

    The row is retained for audit; deactivated configs are excluded from GET.
    """
    pool = request.app.state.pool
    result = await informes_db.set_branding_active(
        pool, usuario_id=user.user_id, activo=False
    )
    if result is None:
        raise HTTPException(status_code=404, detail="No existe configuración de branding para eliminar")
    return {"ok": True, "mensaje": "Configuración de branding desactivada"}