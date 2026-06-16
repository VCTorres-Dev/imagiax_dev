"""
app.informes.router — Report viewing endpoints.

Extracted from servidor_api_orthanc_v1.py (God Object refactor, Phase 5).

Endpoints:
  GET /informes/{informe_id}/ver   — HTML report viewer (Jinja2 template)
  GET /informes/listado            — Paginated JSON listing of reports

The listado endpoint is also duplicated in app.ai.router (under /ai/listado)
to maintain backwards compatibility with existing frontend clients.  This
module owns the canonical informe-viewer HTML page.

All data access goes through ``app.informes.db`` (pool-injected CRUD).
HTML rendering uses the shared Jinja2 templates instance from ``app.core.templates``.
"""

from __future__ import annotations

import html as _html
import logging

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse

from app.auth.dependencies import get_current_user
from app.auth.models import UserPayload
from app.core.database import get_db_pool
from app.informes import db as informes_db
from app.ai.dicom_service import clean_dicom_tag

logger = logging.getLogger("imagiax.informes.router")

router = APIRouter(prefix="/informes", tags=["informes"])


# ============================================================
# Report viewer
# ============================================================

# Inline HTML is kept here intentionally — the monolith had it inline and
# there is no separate template file yet in app/templates/informes/.
# Future refactor can extract it once a template directory is established.

_NOT_FOUND_HTML = """<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Informe no encontrado — IMAGIAX</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Inter',system-ui,sans-serif;background:#0a0f1e;color:#e2e8f0;
  min-height:100vh;display:flex;align-items:center;justify-content:center}
.card{background:#111827;border:1px solid #1e293b;border-radius:16px;padding:40px;
  max-width:400px;text-align:center}
h1{font-size:20px;margin-bottom:12px;color:#f87171}
p{color:#64748b;font-size:14px}
</style></head>
<body><div class="card">
  <h1>Informe no encontrado</h1>
  <p>El código QR no corresponde a ningún informe registrado en el sistema.</p>
</div></body></html>"""


def _build_viewer_html(informe: dict) -> str:
    """Render the report viewer HTML page from the informe record dict."""
    texto = informe.get("informe_final") or informe.get("informe_generado") or ""
    nivel = informe.get("ai_nivel_alerta", "—")
    nivel_color = {
        "ALTA": "#ef4444",
        "MEDIA": "#f59e0b",
        "BAJA": "#22c55e",
        "NORMAL": "#22c55e",
    }.get(nivel, "#94a3b8")
    estado = informe.get("estado", "pendiente")
    estado_txt = {
        "aceptado": "Aceptado",
        "editado_aceptado": "Aceptado (editado)",
        "rechazado": "Rechazado",
        "pendiente": "Pendiente",
    }.get(estado, estado)
    paciente = clean_dicom_tag(informe.get("patient_name")) or "—"
    fecha_raw = informe.get("created_at") or ""
    fecha = str(fecha_raw)[:10] if fecha_raw else "—"
    modalidad = clean_dicom_tag(informe.get("modalidad")) or "—"
    informe_id = str(informe.get("id", ""))
    texto_html = _html.escape(texto).replace("\n", "<br>")

    return f"""<!DOCTYPE html><html lang="es"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Informe IMAGIAX — Verificación</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
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
</div></body></html>"""


@router.get("/{informe_id}/ver", response_class=HTMLResponse)
async def ver_informe(
    informe_id: str,
    pool: asyncpg.Pool = Depends(get_db_pool),
    user: UserPayload = Depends(get_current_user),
) -> HTMLResponse:
    """Report verification page.

    Requires medico, admin, or superadmin role (SEC-BE-006).
    Tenant scoping: scope=None → 401 (missing tenant context).
    Cross-clinic access denied with opaque 403 (no existence leak).
    Medicos can only view their own reports (IDOR guard, H-005).

    Execution order: role check → tenant check → DB fetch → 404 → clinic scope → IDOR → render.
    The informe is fetched BEFORE clinic scoping so the variable is always assigned.
    """
    if user.rol not in ("medico", "admin", "superadmin"):
        return HTMLResponse("Acceso denegado", status_code=403)

    # Tenant fail-closed: missing tenant context means re-login required
    if user.scope is None:
        raise HTTPException(
            status_code=401,
            detail="Sesión sin contexto de clínica. Inicie sesión nuevamente.",
        )

    try:
        informe = await informes_db.get_informe_por_id(pool, informe_id)
    except Exception:
        informe = None

    if not informe:
        return HTMLResponse(_NOT_FOUND_HTML, status_code=404)

    # Tenant scoping: clinic-scoped users can only access their own clinic's informes
    if user.scope != "global":
        informe_clinic = str(informe.get("clinic_id") or "")
        if informe_clinic != str(user.clinic_id or ""):
            # Opaque 403 — do not reveal whether the informe exists
            return HTMLResponse("Acceso denegado", status_code=403)

    # H-005: IDOR check — medico can only view own reports
    if user.rol == "medico":
        informe_owner = str(informe.get("usuario_id") or "")
        if informe_owner != str(user.user_id):
            return HTMLResponse("Acceso denegado", status_code=403)

    return HTMLResponse(_build_viewer_html(informe))


# ============================================================
# Paginated listing
# ============================================================


@router.get("/listado")
async def listado_informes(
    estado: str | None = Query(
        default=None,
        description="Filtro: aceptado | rechazado | editado_aceptado | pendiente",
    ),
    limit: int = Query(default=50, ge=1, le=50, description="Máximo de resultados por página"),
    offset: int = Query(default=0, ge=0, description="Offset para paginación"),
    pool: asyncpg.Pool = Depends(get_db_pool),
    current_user: UserPayload = Depends(get_current_user),
) -> JSONResponse:
    """Paginated report listing with tenant scoping.

    Tenant scoping: scope=None → 401 (re-login required).
    Clinic-scoped users see only their clinic's reports; global sees all.
    Medicos only see their own reports; admins and superadmins see all (C-004).

    Returns:
        items, total, limit, offset, has_more, filtro_estado
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
