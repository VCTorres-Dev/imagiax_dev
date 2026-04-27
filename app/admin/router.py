"""
app.admin.router - Admin panel and API routes.

Routes:
  GET  /admin                         -> Panel HTML (SPA)
  GET  /admin/api/stats               -> Dashboard stats
  GET  /admin/api/usuarios            -> User list
  POST /admin/api/usuarios            -> Create user
  PATCH /admin/api/usuarios/{id}      -> Update user
  POST /admin/api/usuarios/{id}/reset-password -> Reset password
  DELETE /admin/api/usuarios/{id}     -> Delete user
  GET  /admin/api/informes            -> All reports (paginated)
  GET  /admin/api/informes/{id}       -> Report detail
  GET  /admin/api/informes/{id}/pdf   -> Download PDF
  GET  /admin/api/auditoria           -> Audit log
  GET  /admin/api/pagos               -> Payments summary

All /admin/api/* endpoints require role admin.
"""

from __future__ import annotations

import logging
import os as _os

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from api.auth.dependencies import require_role
from api.auth.models import UserPayload
from api.auth import service as auth_service
from app.admin import service as admin_service
from infrastructure.pdf_service import build_pdf_streaming_response
from app.informes import db as informes_db
from app.core.templates import templates

router = APIRouter(tags=["Admin"])

logger = logging.getLogger(__name__)

_IMAGIAX_VIEWER_URL: str = _os.getenv("IMAGIAX_VIEWER_URL", "http://localhost:3000")


def _sanitize_return_to(return_to: str | None) -> str:
    if not return_to:
        return "/"
    rt = return_to.strip()
    if not rt.startswith("/"):
        return "/"
    if rt.startswith("//") or "\\" in rt or "://" in rt:
        return "/"
    return rt


class CreateUserRequest(BaseModel):
    email: str
    password: str = Field(min_length=12)
    nombre: str = Field(min_length=2)
    rol: str = "medico"


class UpdateUserRequest(BaseModel):
    nombre: str | None = None
    rol: str | None = None
    activo: bool | None = None


class ResetPasswordRequest(BaseModel):
    new_password: str = Field(min_length=12)


@router.get("/admin", response_class=HTMLResponse, include_in_schema=False)
async def admin_panel(request: Request) -> Response:
    """Panel HTML del admin (SPA protegida)."""
    token = request.cookies.get("imagiax_session")
    if not token:
        return RedirectResponse(url="/login?returnTo=/admin", status_code=303)
    try:
        from api.auth.service import decode_token, is_jti_revoked, is_user_revoked
        user = decode_token(token)
    except Exception:
        return RedirectResponse(url="/login?returnTo=/admin", status_code=303)
    if user is None:
        return RedirectResponse(url="/login?returnTo=/admin", status_code=303)
    # Check JWT revocation (jti + user-level)
    try:
        pool = request.app.state.pool
        if user.jti and await is_jti_revoked(pool, user.jti):
            return RedirectResponse(url="/login?returnTo=/admin", status_code=303)
        if await is_user_revoked(pool, user.user_id):
            return RedirectResponse(url="/login?returnTo=/admin", status_code=303)
    except Exception as exc:
        # If DB fails, allow through (HTML gate, API endpoints still verify)
        import logging
        logging.getLogger("imagiax.admin").warning(
            "Admin HTML gate: revocation check failed — allowing (API endpoints still verify): %s", exc
        )
    if user.rol not in ("admin", "superadmin"):
        return templates.TemplateResponse(
            "admin/forbidden.html",
            {"request": request},
            status_code=403,
            headers={"Cache-Control": "no-store"},
        )
    return templates.TemplateResponse(
        "admin/admin_spa.html",
        {"request": request},
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


@router.get("/admin/api/stats")
async def admin_stats(
    request: Request,
    user: UserPayload = Depends(require_role("admin")),
) -> JSONResponse:
    """Estadisticas generales para el dashboard."""
    try:
        pool = request.app.state.pool
        stats = await admin_service.get_dashboard_stats(pool)
        return JSONResponse(stats)
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Error al obtener estadísticas del dashboard") from exc


@router.get("/admin/api/usuarios")
async def list_usuarios(
    request: Request,
    user: UserPayload = Depends(require_role("admin")),
) -> JSONResponse:
    """Lista todos los usuarios del sistema."""
    pool = request.app.state.pool
    usuarios = await auth_service.list_users(pool)
    result = [admin_service.serialize_user(u) for u in usuarios]
    return JSONResponse({"usuarios": result, "total": len(result)})


@router.post("/admin/api/usuarios", status_code=201)
async def create_usuario(
    body: CreateUserRequest,
    request: Request,
    admin: UserPayload = Depends(require_role("admin")),
) -> JSONResponse:
    """Crea un nuevo usuario en el sistema."""
    if body.rol == "superadmin" and admin.rol != "superadmin":
        raise HTTPException(403, "Solo el superadmin puede crear cuentas superadmin.")
    pool = request.app.state.pool
    new_user = await auth_service.create_user(
        pool,
        email=body.email,
        password=body.password,
        nombre=body.nombre,
        rol=body.rol,
    )
    await auth_service.log_audit(
        pool, admin.user_id, "admin_crear_usuario",
        recurso="usuario:" + str(new_user["id"]),
        detalle="email=" + body.email + " rol=" + body.rol,
    )
    return JSONResponse({
        "ok": True,
        "usuario": {
            "id": str(new_user["id"]),
            "email": new_user["email"],
            "nombre": new_user["nombre"],
            "rol": new_user["rol"],
        },
    }, status_code=201)


@router.patch("/admin/api/usuarios/{usuario_id}")
async def update_usuario(
    usuario_id: str,
    body: UpdateUserRequest,
    request: Request,
    admin: UserPayload = Depends(require_role("admin")),
) -> JSONResponse:
    """Actualiza nombre, rol o estado activo de un usuario."""
    if usuario_id == admin.user_id and body.rol is not None and body.rol not in ("admin", "superadmin"):
        raise HTTPException(400, "No puedes cambiar tu propio rol.")
    if usuario_id == admin.user_id and body.activo is False:
        raise HTTPException(400, "No puedes desactivar tu propia cuenta.")
    pool = request.app.state.pool
    target = await auth_service.get_user_by_id(pool, usuario_id)
    if target and target["rol"] == "superadmin" and admin.rol != "superadmin":
        raise HTTPException(403, "No tienes permiso para modificar la cuenta superadmin.")
    updated = await auth_service.update_user(pool, usuario_id, nombre=body.nombre, rol=body.rol, activo=body.activo)
    if updated is None:
        raise HTTPException(404, "Usuario no encontrado.")
    await auth_service.log_audit(
        pool, admin.user_id, "admin_actualizar_usuario",
        recurso="usuario:" + usuario_id,
        detalle="nombre=" + str(body.nombre) + " rol=" + str(body.rol) + " activo=" + str(body.activo),
    )
    return JSONResponse({"ok": True, "usuario": admin_service.serialize_user(updated)})


@router.post("/admin/api/usuarios/{usuario_id}/reset-password")
async def reset_password(
    usuario_id: str,
    body: ResetPasswordRequest,
    request: Request,
    admin: UserPayload = Depends(require_role("admin")),
) -> JSONResponse:
    """Fuerza nueva contrasena para un usuario."""
    pool = request.app.state.pool
    target = await auth_service.get_user_by_id(pool, usuario_id)
    if not target:
        raise HTTPException(404, "Usuario no encontrado.")
    if target["rol"] == "superadmin" and admin.rol != "superadmin":
        raise HTTPException(403, "No tienes permiso para cambiar la contrasena del superadmin.")
    await auth_service.update_password(pool, usuario_id, body.new_password)
    await auth_service.log_audit(
        pool, admin.user_id, "admin_reset_password",
        recurso="usuario:" + usuario_id,
        detalle="target_email=" + target["email"],
    )
    return JSONResponse({"ok": True, "mensaje": "Contrasena de " + target["email"] + " actualizada."})


@router.delete("/admin/api/usuarios/{usuario_id}")
async def delete_usuario(
    usuario_id: str,
    request: Request,
    admin: UserPayload = Depends(require_role("admin")),
) -> JSONResponse:
    """Elimina permanentemente un usuario."""
    if usuario_id == admin.user_id:
        raise HTTPException(400, "No puedes eliminar tu propia cuenta.")
    pool = request.app.state.pool
    target_pre = await auth_service.get_user_by_id(pool, usuario_id)
    if target_pre and target_pre["rol"] == "superadmin":
        raise HTTPException(403, "La cuenta superadmin no puede ser eliminada.")
    target = await auth_service.get_user_by_id(pool, usuario_id)
    if not target:
        raise HTTPException(404, "Usuario no encontrado.")
    await auth_service.revoke_all_user_tokens(pool, usuario_id)
    deleted = await auth_service.delete_user(pool, usuario_id)
    if not deleted:
        raise HTTPException(500, "No se pudo eliminar el usuario.")
    await auth_service.log_audit(
        pool, admin.user_id, "admin_eliminar_usuario",
        recurso="usuario:" + usuario_id,
        detalle="email=" + target["email"],
    )
    return JSONResponse({"ok": True, "mensaje": "Usuario " + target["email"] + " eliminado."})


@router.get("/admin/api/informes")
async def admin_informes(
    request: Request,
    estado: str | None = Query(default=None),
    usuario_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: UserPayload = Depends(require_role("admin")),
) -> JSONResponse:
    """Lista todos los informes con filtros opcionales."""
    pool = request.app.state.pool
    result = await informes_db.get_todos_informes(pool, estado=estado, limit=limit, offset=offset, usuario_id=usuario_id)
    return JSONResponse({**result, "filtro_estado": estado, "filtro_usuario": usuario_id})


@router.get("/admin/api/informes/{informe_id}")
async def admin_informe_detail(
    informe_id: str,
    request: Request,
    _: UserPayload = Depends(require_role("admin")),
) -> JSONResponse:
    """Retorna detalle completo de un informe."""
    pool = request.app.state.pool
    informe = await informes_db.get_informe_por_id(pool, informe_id)
    if not informe:
        raise HTTPException(status_code=404, detail="Informe no encontrado.")
    if "created_at" in informe and "timestamp_creacion" not in informe:
        informe["timestamp_creacion"] = informe["created_at"]
    return JSONResponse(informe)


@router.get("/admin/api/informes/{informe_id}/pdf")
async def admin_informe_pdf(
    informe_id: str,
    request: Request,
    _: UserPayload = Depends(require_role("admin")),
):
    """Genera y descarga el informe medico como PDF."""
    pool = request.app.state.pool
    informe = await informes_db.get_informe_por_id(pool, informe_id)
    if not informe:
        raise HTTPException(status_code=404, detail="Informe no encontrado.")
    informe_generado = (informe.get("informe_generado") or "").strip()
    informe_final = (informe.get("informe_final") or "").strip()
    if not informe_generado and not informe_final:
        raise HTTPException(status_code=404, detail="El informe no tiene contenido generado.")
    try:
        return build_pdf_streaming_response(informe)
    except RuntimeError as exc:
        logger.error("Error generando PDF de informe: %s", exc)
        raise HTTPException(status_code=500, detail="Error interno al generar el PDF") from exc


@router.get("/admin/api/auditoria")
async def admin_auditoria(
    request: Request,
    usuario_id: str | None = Query(default=None),
    accion: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    caller: UserPayload = Depends(require_role("admin")),
) -> JSONResponse:
    """Log de auditoria con filtros opcionales."""
    effective_usuario_id = usuario_id
    if caller.rol != "superadmin":
        effective_usuario_id = caller.user_id
    pool = request.app.state.pool
    logs = await auth_service.get_audit_logs(
        pool, limit=limit, offset=offset,
        usuario_id=effective_usuario_id, accion=accion,
    )
    total = await auth_service.count_audit_logs(pool, usuario_id=effective_usuario_id, accion=accion)
    result = [
        {
            "id": str(log["id"]),
            "accion": log["accion"],
            "recurso": log["recurso"],
            "ip": log["ip"],
            "detalle": log["detalle"],
            "created_at": log["created_at"].isoformat() if log.get("created_at") else None,
            "usuario_email": log.get("usuario_email"),
            "usuario_nombre": log.get("usuario_nombre"),
        }
        for log in logs
    ]
    return JSONResponse({"items": result, "total": total, "limit": limit, "offset": offset, "has_more": offset + limit < total})


@router.get("/admin/api/pagos")
async def admin_pagos(
    request: Request,
    _: UserPayload = Depends(require_role("admin")),
) -> JSONResponse:
    """Resumen de pagos e ingresos."""
    try:
        pool = request.app.state.pool
        summary = await admin_service.get_pagos_summary(pool)
        return JSONResponse(summary)
    except Exception as exc:
        raise HTTPException(500, "Error al obtener datos de pagos") from exc
