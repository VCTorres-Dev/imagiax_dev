"""
app.pagos.tbk.router — Rutas FastAPI de Transbank Oneclick Mall.

Endpoints:
  POST   /tbk/inscribir/iniciar    — Inicia inscripción Oneclick, redirige a Transbank
  GET    /tbk/inscribir/finalizar  — Callback de Transbank tras la inscripción
  POST   /tbk/cobrar               — Cobro manual (testing/admin)
  DELETE /tbk/tarjeta              — Elimina tarjeta Oneclick inscrita
  GET    /tbk/estado               — Estado de inscripción del usuario
  GET    /tbk/inscribir            — Página HTML de inscripción
"""

from __future__ import annotations

import logging

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from app.auth.dependencies import get_current_user
from app.auth.models import UserPayload
from app.core.config import settings
from app.core.database import get_db_pool
from app.pagos import db as pagos_db
from app.pagos.tbk import service

logger = logging.getLogger("imagiax.tbk")

router = APIRouter(prefix="/tbk", tags=["pagos-tbk"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class InscribirRequest(BaseModel):
    username: str = Field(..., max_length=40, description="ID del usuario (ej: email o RUT)")
    email: str = Field(..., max_length=100, description="Email del usuario")


class CobrarRequest(BaseModel):
    username: str = Field(..., max_length=40, description="Username del usuario inscrito")
    amount: int = Field(default=990, ge=50, description="Monto en CLP (mínimo $50)")
    descripcion: str = Field(default="IMAGIAX — Informe radiológico IA", max_length=255)


class EliminarRequest(BaseModel):
    username: str = Field(..., max_length=40, description="Username del usuario inscrito")


# ── Endpoints de Inscripción ──────────────────────────────────────────────────

@router.post("/inscribir/iniciar")
async def inscribir_iniciar(
    req: InscribirRequest,
    user: UserPayload = Depends(get_current_user),
    pool: asyncpg.Pool = Depends(get_db_pool),
):
    """
    Paso 1: Inicia la inscripción Oneclick.
    Retorna token + url_webpay para redirigir al usuario a Transbank.
    """
    # response_url NO lleva usuario_id — se guarda server-side en tbk_pending
    response_url = f"{settings.IMAGIAX_BASE_URL}/tbk/inscribir/finalizar"

    try:
        inscription = service.get_inscription()
        resp = await service.tbk_call(
            inscription.start,
            username=req.username,
            email=req.email,
            response_url=response_url,
        )
    except Exception as exc:
        logger.error("Error iniciando inscripción Oneclick: %s", exc)
        raise HTTPException(500, "Error al iniciar inscripción Transbank") from exc

    token = resp.get("token") or resp.get("Token")
    url_webpay = resp.get("url_webpay") or resp.get("urlWebpay")

    if not token or not url_webpay:
        logger.error("Respuesta inesperada de Transbank start: %s", resp)
        raise HTTPException(502, "Respuesta inválida de Transbank al iniciar inscripción")

    # Guardar mapeo TBK_TOKEN → usuario_id en DB (TTL 10 min, uso único)
    await pagos_db.tbk_pending_save(pool, token, user.user_id, req.username)

    _safe_username = req.username[:3] + "***" if len(req.username) > 3 else "***"
    logger.info(
        "Inscripción Oneclick iniciada para %s — token: %s...",
        _safe_username,
        token[:16],
    )

    return JSONResponse({
        "token": token,
        "url_webpay": url_webpay,
        "redirect_url": f"{url_webpay}?TBK_TOKEN={token}",
    })


@router.get("/inscribir/finalizar")
async def inscribir_finalizar(
    request: Request,
    current_user: UserPayload = Depends(get_current_user),
    pool: asyncpg.Pool = Depends(get_db_pool),
):
    """
    Paso 2: Callback de Transbank tras la inscripción.
    Transbank redirige aquí con TBK_TOKEN en query params.
    Requiere sesión autenticada (VER-008).
    """
    tbk_token = request.query_params.get("TBK_TOKEN")

    # Si el usuario canceló, Transbank envía TBK_ORDEN_COMPRA sin TBK_TOKEN
    if not tbk_token:
        logger.warning("Inscripción Oneclick cancelada por el usuario")
        return RedirectResponse(
            url=f"{settings.IMAGIAX_BASE_URL}/pagar?tbk_status=cancelled",
            status_code=303,
        )

    try:
        inscription = service.get_inscription()
        resp = await service.tbk_call(inscription.finish, token=tbk_token)
    except Exception as exc:
        logger.error("Error finalizando inscripción Oneclick: %s", exc)
        return RedirectResponse(
            url=f"{settings.IMAGIAX_BASE_URL}/pagar?tbk_status=error&msg=tbk_error",  # SEC-AI-003: opaque error code
            status_code=303,
        )

    response_code = resp.get("response_code")
    tbk_user = resp.get("tbk_user") or resp.get("transbank_user")
    card_type = resp.get("card_type", "")
    card_number = resp.get("card_number", "")
    auth_code = resp.get("authorization_code", "")

    # response_code == 0 significa aprobado
    if response_code != 0:
        logger.warning(
            "Inscripción Oneclick rechazada. response_code=%s, resp=%s",
            response_code,
            resp,
        )
        return RedirectResponse(
            url=f"{settings.IMAGIAX_BASE_URL}/pagar?tbk_status=rejected&code={response_code}",
            status_code=303,
        )

    if not tbk_user:
        logger.error("Inscripción aprobada pero sin tbk_user: %s", resp)
        return RedirectResponse(
            url=f"{settings.IMAGIAX_BASE_URL}/pagar?tbk_status=error&msg=sin_tbk_user",
            status_code=303,
        )

    # Extraer últimos 4 dígitos
    last4 = card_number[-4:] if len(card_number) >= 4 else card_number

    # Recuperar username y usuario_id desde DB server-side (no del query param)
    pending = await pagos_db.tbk_pending_pop(pool, tbk_token)
    if pending:
        username = pending["username"]
        usuario_id = pending["usuario_id"]
    else:
        # H-007: NO username fallback from query params (forgery risk)
        # Si tbk_pending expiró o ya fue consumido, fallar de forma segura
        logger.error(
            "tbk_pending expired or already consumed for token %s", tbk_token[:16]
        )
        return RedirectResponse(
            url=f"{settings.IMAGIAX_BASE_URL}/pagar?tbk_status=error&msg=session_expired",
            status_code=303,
        )

    # Guardar inscripción en DB
    try:
        await pagos_db.guardar_inscripcion_oneclick(
            pool,
            username=username,
            tbk_user=tbk_user,
            card_type=card_type,
            card_number_last4=last4,
            authorization_code=auth_code,
            usuario_id=usuario_id,
        )
        logger.info(
            "Inscripción Oneclick exitosa: user=%s, tbk_user=%s, card=%s ***%s",
            username,
            tbk_user,
            card_type,
            last4,
        )
    except Exception as exc:
        logger.error("Error guardando inscripción en DB: %s", exc)
        return RedirectResponse(
            url=f"{settings.IMAGIAX_BASE_URL}/pagar?tbk_status=error&msg=db_error",
            status_code=303,
        )

    return RedirectResponse(
        url=f"{settings.IMAGIAX_BASE_URL}/pagar?tbk_status=ok&card_type={card_type}&last4={last4}",
        status_code=303,
    )


# ── Cobro Silencioso (1-Click) ────────────────────────────────────────────────

@router.post("/cobrar")
async def cobrar_endpoint(
    req: CobrarRequest,
    user: UserPayload = Depends(get_current_user),
    pool: asyncpg.Pool = Depends(get_db_pool),
):
    """Endpoint manual para cobro silencioso (testing/admin).

    SEGURIDAD (SEC-PAY-003 + SEC-PAY-004, AD-4):
    Los campos username y amount del body son IGNORADOS.
    - username se obtiene de la DB via usuario_id del JWT (previene IDOR)
    - amount se fija al IMAGIAX_PRECIO_SCAN del servidor (previene manipulación de precio)
    """
    # Override: ignorar username/amount del body — usar datos del JWT y constante del servidor
    if req.username and req.username != user.email:
        logger.warning(
            "SECURITY: cobrar_endpoint body.username=%r ignorado, "
            "usando usuario_id=%s del JWT (IDOR prevention)",
            req.username,
            user.user_id,
        )

    # Obtener inscripción desde DB usando el usuario_id del JWT (no del body)
    inscripcion = await pagos_db.get_inscripcion_oneclick_any(pool, usuario_id=user.user_id)
    if not inscripcion:
        raise HTTPException(404, "No hay tarjeta Oneclick inscrita para tu cuenta")

    # Cobrar con username de DB y monto del servidor (no del request body)
    aprobado, buy_order, error_msg = await service.cobrar_scan_oneclick(
        pool=pool,
        username=inscripcion["username"],  # FROM DB — no del body
        tbk_user=inscripcion["tbk_user"],
        amount=settings.IMAGIAX_PRECIO_SCAN,  # SERVER constant — no del body
        descripcion=req.descripcion,
    )

    return JSONResponse({
        "aprobado": aprobado,
        "buy_order": buy_order,
        "monto_clp": settings.IMAGIAX_PRECIO_SCAN,
        "error": error_msg,
    })


# ── Eliminar inscripción ──────────────────────────────────────────────────────

@router.delete("/tarjeta")
async def eliminar_tarjeta(
    user: UserPayload = Depends(get_current_user),
    pool: asyncpg.Pool = Depends(get_db_pool),
):
    """Elimina la inscripción Oneclick del usuario autenticado."""
    inscripcion = await pagos_db.get_inscripcion_oneclick_any(pool, usuario_id=user.user_id)
    if not inscripcion:
        raise HTTPException(404, "No hay tarjeta Oneclick inscrita para este usuario")

    db_username = inscripcion["username"]

    # Eliminar en Transbank (usar username exacto de la DB)
    try:
        inscription = service.get_inscription()
        await service.tbk_call(
            inscription.delete,
            tbk_user=inscripcion["tbk_user"],
            username=db_username,
        )
        logger.info(
            "Inscripción Oneclick eliminada en Transbank: user=%s", db_username
        )
    except Exception as exc:
        logger.warning(
            "Error eliminando inscripción en Transbank (continuando): %s", exc
        )

    # Desactivar en DB
    await pagos_db.borrar_inscripcion_oneclick(pool, db_username)

    return JSONResponse({"ok": True, "mensaje": "Tarjeta desvinculada correctamente"})


# ── Estado de inscripción ─────────────────────────────────────────────────────

@router.get("/estado")
async def estado_oneclick(
    user: UserPayload = Depends(get_current_user),
    pool: asyncpg.Pool = Depends(get_db_pool),
):
    """Retorna el estado de la inscripción Oneclick del usuario autenticado."""
    inscripcion = await pagos_db.get_inscripcion_oneclick_any(pool, usuario_id=user.user_id)

    if not inscripcion:
        return JSONResponse({
            "inscrito": False,
            "card_type": None,
            "card_last4": None,
        })

    return JSONResponse({
        "inscrito": True,
        "username": inscripcion["username"],
        "card_type": inscripcion.get("card_type"),
        "card_last4": inscripcion.get("card_number_last4"),
        "created_at": (
            inscripcion["created_at"].isoformat()
            if inscripcion.get("created_at") and hasattr(inscripcion["created_at"], "isoformat")
            else inscripcion.get("created_at")
        ),
    })


# ── Página HTML de inscripción ────────────────────────────────────────────────

@router.get("/inscribir", response_class=HTMLResponse)
async def inscribir_page():
    """Página para inscribir tarjeta Oneclick."""
    return HTMLResponse(_INSCRIPCION_HTML)


_INSCRIPCION_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>IMAGIAX — Inscribir Tarjeta Oneclick</title>
<style>
  :root { --bg: #0f172a; --card: #1e293b; --accent: #3b82f6; --ok: #22c55e; --err: #ef4444; --text: #f1f5f9; --muted: #94a3b8; }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: 'Segoe UI', system-ui, sans-serif; background: var(--bg); color: var(--text); display: flex; justify-content: center; align-items: center; min-height: 100vh; }
  .card { background: var(--card); border-radius: 16px; padding: 2rem; max-width: 420px; width: 90%; box-shadow: 0 8px 32px rgba(0,0,0,0.4); }
  h2 { font-size: 1.25rem; margin-bottom: 0.5rem; }
  p.desc { color: var(--muted); font-size: 0.875rem; margin-bottom: 1.5rem; line-height: 1.5; }
  label { display: block; font-size: 0.8rem; color: var(--muted); margin-bottom: 0.25rem; }
  input { width: 100%; padding: 0.6rem 0.75rem; border: 1px solid #334155; border-radius: 8px; background: #0f172a; color: var(--text); font-size: 0.9rem; margin-bottom: 1rem; }
  input:focus { outline: none; border-color: var(--accent); }
  button { width: 100%; padding: 0.75rem; border: none; border-radius: 8px; background: var(--accent); color: white; font-size: 1rem; font-weight: 600; cursor: pointer; transition: background 0.2s; }
  button:hover { background: #2563eb; }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  .info { background: #1a2744; border: 1px solid #2d4a7c; border-radius: 8px; padding: 0.75rem; margin-bottom: 1.5rem; font-size: 0.8rem; color: var(--muted); }
  .info strong { color: var(--accent); }
  #status { margin-top: 1rem; padding: 0.75rem; border-radius: 8px; display: none; font-size: 0.85rem; }
  #status.ok { display: block; background: #14532d; color: #86efac; }
  #status.err { display: block; background: #451a1a; color: #fca5a5; }
  .logo { text-align: center; margin-bottom: 1rem; font-size: 1.5rem; font-weight: bold; color: var(--accent); }
</style>
</head>
<body>
<div class="card">
  <div class="logo">IMAGIAX</div>
  <h2>Inscribir Tarjeta — Cobro Automatico</h2>
  <p class="desc">
    Al inscribir tu tarjeta, autorizas cobros automaticos de <strong>$990 CLP</strong>
    por cada informe radiologico generado. Puedes desvincular tu tarjeta en cualquier momento.
  </p>
  <div class="info">
    <strong>Ambiente de pruebas:</strong> Usa la tarjeta VISA <code>4051 8856 0044 6623</code>,
    CVV <code>123</code>, cualquier fecha de expiracion. RUT: <code>11.111.111-1</code>, clave: <code>123</code>.
  </div>
  <form id="form">
    <label>Email</label>
    <input type="email" id="email" placeholder="doctor@clinica.cl" required />
    <label>Identificador de usuario</label>
    <input type="text" id="username" placeholder="doctor@clinica.cl" required />
    <button type="submit" id="btn">Inscribir Tarjeta en Transbank</button>
  </form>
  <div id="status"></div>
</div>
<script>
  const emailEl = document.getElementById('email');
  const usernameEl = document.getElementById('username');
  emailEl.addEventListener('input', () => {
    if (!usernameEl.value || usernameEl.value === emailEl.dataset.lastEmail) {
      usernameEl.value = emailEl.value;
    }
    emailEl.dataset.lastEmail = emailEl.value;
  });

  document.getElementById('form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn = document.getElementById('btn');
    const status = document.getElementById('status');
    btn.disabled = true;
    btn.textContent = 'Conectando con Transbank...';
    status.style.display = 'none';

    try {
      const resp = await fetch('/tbk/inscribir/iniciar', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username: usernameEl.value.trim(),
          email: emailEl.value.trim(),
        }),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || 'Error al iniciar inscripcion');

      // Guardar username en sessionStorage para recuperarlo en el callback
      sessionStorage.setItem('tbk_username', usernameEl.value.trim());

      // Redirigir a Transbank
      window.location.href = data.redirect_url;
    } catch (err) {
      status.className = 'err';
      status.textContent = err.message;
      status.style.display = 'block';
      btn.disabled = false;
      btn.textContent = 'Inscribir Tarjeta en Transbank';
    }
  });

  // Verificar si volvemos del callback
  const params = new URLSearchParams(window.location.search);
  const tbkStatus = params.get('tbk_status');
  if (tbkStatus) {
    const status = document.getElementById('status');
    if (tbkStatus === 'ok') {
      const cardType = params.get('card_type') || '';
      const last4 = params.get('last4') || '****';
      status.className = 'ok';
      status.textContent = 'Tarjeta inscrita exitosamente: ' + cardType + ' ***' + last4 + '. El cobro automatico esta activo.';
      status.style.display = 'block';
    } else if (tbkStatus === 'cancelled') {
      status.className = 'err';
      status.textContent = 'Inscripcion cancelada. Puedes intentarlo nuevamente.';
      status.style.display = 'block';
    } else {
      status.className = 'err';
      status.textContent = 'Error en la inscripcion: ' + (params.get('msg') || 'code=' + params.get('code'));
      status.style.display = 'block';
    }
  }
</script>
</body>
</html>"""
