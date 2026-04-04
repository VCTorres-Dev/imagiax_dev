"""
IMAGIAX — Módulo de pagos Transbank Oneclick Mall.

Flujo 1-Click:
  1. Inscripción: usuario autoriza su tarjeta una sola vez en Transbank
  2. Cobro silencioso: backend cobra automáticamente sin intervención del usuario
  3. Eliminar inscripción: usuario puede desvincular su tarjeta

Dependencias:
  - transbank-sdk >= 6.0.0
  - imagiax_pagos_db (pool asyncpg compartido)
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from typing import Any
from urllib.parse import quote, unquote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from imagiax_auth import get_current_user, UserPayload

from transbank.webpay.oneclick.mall_inscription import MallInscription
from transbank.webpay.oneclick.mall_transaction import MallTransaction
from transbank.webpay.oneclick.request import MallTransactionAuthorizeDetails

import imagiax_pagos_db

logger = logging.getLogger("imagiax.tbk")

# ── Configuración Transbank ───────────────────────────────────────────────────

TBK_MALL_COMMERCE_CODE = os.getenv("TBK_MALL_COMMERCE_CODE", "597055555541")
TBK_CHILD_COMMERCE_CODE = os.getenv("TBK_CHILD_COMMERCE_CODE", "597055555542")
TBK_API_KEY = os.getenv(
    "TBK_API_KEY",
    "579B532A7440BB0C9079DED94D31EA1615BACEB56610332264630D42D0A36B1C",
)
TBK_ENVIRONMENT = os.getenv("TBK_ENVIRONMENT", "integration")  # integration | production

IMAGIAX_BASE_URL = os.getenv("IMAGIAX_BASE_URL", "http://localhost:8010")
PRECIO_SCAN_CLP = int(os.getenv("IMAGIAX_PRECIO_SCAN", "990"))


def _get_inscription() -> MallInscription:
    if TBK_ENVIRONMENT == "production":
        return MallInscription.build_for_production(TBK_MALL_COMMERCE_CODE, TBK_API_KEY)
    return MallInscription.build_for_integration(TBK_MALL_COMMERCE_CODE, TBK_API_KEY)


def _get_transaction() -> MallTransaction:
    if TBK_ENVIRONMENT == "production":
        return MallTransaction.build_for_production(TBK_MALL_COMMERCE_CODE, TBK_API_KEY)
    return MallTransaction.build_for_integration(TBK_MALL_COMMERCE_CODE, TBK_API_KEY)


# ── Router FastAPI ────────────────────────────────────────────────────────────

router = APIRouter(prefix="/tbk", tags=["transbank-oneclick"])


# ── Async wrapper for sync TBK SDK calls (SEC-AI-002, AD-7) ──────────────────

async def _tbk_call(fn, *args, **kwargs):
    """
    Runs synchronous Transbank SDK calls in a thread pool executor.
    Prevents blocking the asyncio event loop during long-running SDK I/O calls.
    (SEC-AI-002, AD-7)
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))



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
async def inscribir_iniciar(req: InscribirRequest, user: UserPayload = Depends(get_current_user)):
    """
    Paso 1: Inicia la inscripción Oneclick.
    Retorna token + url_webpay para redirigir al usuario a Transbank.
    """
    # response_url NO lleva usuario_id — se guarda server-side en tbk_pending
    response_url = f"{IMAGIAX_BASE_URL}/tbk/inscribir/finalizar"

    try:
        inscription = _get_inscription()
        resp = await _tbk_call(
            inscription.start,
            username=req.username,
            email=req.email,
            response_url=response_url,
        )
    except Exception as exc:
        logger.error("Error iniciando inscripción Oneclick: %s", exc)
        raise HTTPException(500, f"Error al iniciar inscripción Transbank: {exc}") from exc

    token = resp.get("token") or resp.get("Token")
    url_webpay = resp.get("url_webpay") or resp.get("urlWebpay")

    if not token or not url_webpay:
        logger.error("Respuesta inesperada de Transbank start: %s", resp)
        raise HTTPException(502, "Respuesta inválida de Transbank al iniciar inscripción")

    # Guardar mapeo TBK_TOKEN → usuario_id en DB (TTL 10 min, uso único)
    await imagiax_pagos_db.tbk_pending_save(token, user.user_id, req.username)

    logger.info("Inscripción Oneclick iniciada para %s — token: %s...", req.username, token[:16])

    return JSONResponse({
        "token": token,
        "url_webpay": url_webpay,
        "redirect_url": f"{url_webpay}?TBK_TOKEN={token}",
    })


@router.get("/inscribir/finalizar")
async def inscribir_finalizar(request: Request):
    """
    Paso 2: Callback de Transbank tras la inscripción.
    Transbank redirige aquí con TBK_TOKEN en query params.
    """
    tbk_token = request.query_params.get("TBK_TOKEN")

    # Si el usuario canceló, Transbank envía TBK_ORDEN_COMPRA sin TBK_TOKEN
    if not tbk_token:
        logger.warning("Inscripción Oneclick cancelada por el usuario")
        return RedirectResponse(
            url=f"{IMAGIAX_BASE_URL}/pagar?tbk_status=cancelled",
            status_code=303,
        )

    try:
        inscription = _get_inscription()
        resp = await _tbk_call(inscription.finish, token=tbk_token)
    except Exception as exc:
        logger.error("Error finalizando inscripción Oneclick: %s", exc)
        return RedirectResponse(
            url=f"{IMAGIAX_BASE_URL}/pagar?tbk_status=error&msg=tbk_error",  # SEC-AI-003: opaque error code
            status_code=303,
        )

    response_code = resp.get("response_code")
    tbk_user = resp.get("tbk_user") or resp.get("transbank_user")
    card_type = resp.get("card_type", "")
    card_number = resp.get("card_number", "")
    auth_code = resp.get("authorization_code", "")

    # response_code == 0 significa aprobado
    if response_code != 0:
        logger.warning("Inscripción Oneclick rechazada. response_code=%s, resp=%s", response_code, resp)
        return RedirectResponse(
            url=f"{IMAGIAX_BASE_URL}/pagar?tbk_status=rejected&code={response_code}",
            status_code=303,
        )

    if not tbk_user:
        logger.error("Inscripción aprobada pero sin tbk_user: %s", resp)
        return RedirectResponse(
            url=f"{IMAGIAX_BASE_URL}/pagar?tbk_status=error&msg=sin_tbk_user",
            status_code=303,
        )

    # Extraer últimos 4 dígitos
    last4 = card_number[-4:] if len(card_number) >= 4 else card_number

    # Recuperar username y usuario_id desde DB server-side (no del query param)
    pending = await imagiax_pagos_db.tbk_pending_pop(tbk_token)
    if pending:
        username = pending["username"]
        usuario_id = pending["usuario_id"]
    else:
        # Fallback: si el registro expiró (>10 min) o ya fue consumido, usar query param como respaldo
        username = unquote(request.query_params.get("username", "default_user"))
        usuario_id = request.query_params.get("usuario_id") or None
        logger.warning("tbk_pending no encontrado para token %s... — usando query params", tbk_token[:16])

    # Guardar inscripción en DB
    try:
        await imagiax_pagos_db.guardar_inscripcion_oneclick(
            username=username,
            tbk_user=tbk_user,
            card_type=card_type,
            card_number_last4=last4,
            authorization_code=auth_code,
            usuario_id=usuario_id,
        )
        logger.info(
            "Inscripción Oneclick exitosa: user=%s, tbk_user=%s, card=%s ***%s",
            username, tbk_user, card_type, last4,
        )
    except Exception as exc:
        logger.error("Error guardando inscripción en DB: %s", exc)
        return RedirectResponse(
            url=f"{IMAGIAX_BASE_URL}/pagar?tbk_status=error&msg=db_error",
            status_code=303,
        )

    return RedirectResponse(
        url=f"{IMAGIAX_BASE_URL}/pagar?tbk_status=ok&card_type={card_type}&last4={last4}",
        status_code=303,
    )


# ── Cobro Silencioso (1-Click) ────────────────────────────────────────────────

async def cobrar_scan_oneclick(
    *,
    username: str,
    tbk_user: str,
    amount: int = PRECIO_SCAN_CLP,
    descripcion: str = "IMAGIAX — Informe radiológico IA",
) -> tuple[bool, str | None, str | None]:
    """
    Cobra silenciosamente usando Oneclick Mall.
    NUNCA lanza excepción — el análisis médico siempre se entrega.

    Retorna (aprobado: bool, buy_order: str | None, error_msg: str | None).
    """
    # Sanitizar: Transbank requiere el username exacto con que se inscribió
    username = unquote(username)
    parent_buy_order = f"IMG-{uuid.uuid4().hex[:16]}"
    child_buy_order = f"IMGc-{uuid.uuid4().hex[:14]}"

    try:
        transaction = _get_transaction()
        details = MallTransactionAuthorizeDetails(
            commerce_code=TBK_CHILD_COMMERCE_CODE,
            buy_order=child_buy_order,
            installments_number=1,
            amount=int(amount),
        )

        resp = await _tbk_call(
            transaction.authorize,
            username=username,
            tbk_user=tbk_user,
            parent_buy_order=parent_buy_order,
            details=details,
        )

        # Analizar respuesta
        resp_details = resp.get("details", [])
        status = resp.get("status", "unknown")

        child_status = None
        child_response_code = None
        child_auth_code = None
        if resp_details and len(resp_details) > 0:
            child = resp_details[0]
            child_status = child.get("status", "unknown")
            child_response_code = child.get("response_code")
            child_auth_code = child.get("authorization_code")

        aprobado = (child_response_code == 0 and child_status == "AUTHORIZED")

        # Registrar en DB
        try:
            await imagiax_pagos_db.registrar_pago_tbk(
                username=username,
                buy_order=parent_buy_order,
                child_buy_order=child_buy_order,
                authorization_code=child_auth_code,
                amount=amount,
                status="approved" if aprobado else "rejected",
                response_code=child_response_code,
                raw_response=resp,
            )
        except Exception as db_exc:
            logger.warning("No se pudo registrar cobro TBK en DB: %s", db_exc)

        if aprobado:
            logger.info(
                "Cobro Oneclick aprobado: $%d CLP, order=%s, auth=%s",
                amount, parent_buy_order, child_auth_code,
            )
            return True, parent_buy_order, None
        else:
            error_msg = f"Cobro rechazado por Transbank (code={child_response_code}, status={child_status})"
            logger.warning("Cobro Oneclick rechazado: %s — resp=%s", error_msg, resp)
            return False, parent_buy_order, error_msg

    except Exception as exc:
        logger.error("Excepción en cobro Oneclick: %s", exc)
        # Registrar intento fallido
        try:
            await imagiax_pagos_db.registrar_pago_tbk(
                username=username,
                buy_order=parent_buy_order,
                child_buy_order=child_buy_order,
                amount=amount,
                status="error",
                raw_response={"error": str(exc)},
            )
        except Exception:
            pass
        return False, None, f"Error al procesar cobro Transbank: {exc}"


@router.post("/cobrar")
async def cobrar_endpoint(req: CobrarRequest, user: UserPayload = Depends(get_current_user)):
    """Endpoint manual para cobro silencioso (testing/admin).

    SEGURIDAD (SEC-PAY-003 + SEC-PAY-004, AD-4):
    Los campos username y amount del body son IGNORADOS.
    - username se obtiene de la DB via usuario_id del JWT (previene IDOR)
    - amount se fija al PRECIO_SCAN_CLP del servidor (previene manipulacion de precio)
    """
    # Override: ignorar username/amount del body — usar datos del JWT y constante del servidor
    if req.username and req.username != user.email:
        logger.warning(
            "SECURITY: cobrar_endpoint body.username=%r ignorado, usando usuario_id=%s del JWT (IDOR prevention)",
            req.username, user.user_id,
        )

    # Obtener inscripcion desde DB usando el usuario_id del JWT (no del body)
    inscripcion = await imagiax_pagos_db.get_inscripcion_oneclick_any(usuario_id=user.user_id)
    if not inscripcion:
        raise HTTPException(404, "No hay tarjeta Oneclick inscrita para tu cuenta")

    # Cobrar con username de DB y monto del servidor (no del request body)
    aprobado, buy_order, error_msg = await cobrar_scan_oneclick(
        username=inscripcion["username"],  # FROM DB — no del body
        tbk_user=inscripcion["tbk_user"],
        amount=PRECIO_SCAN_CLP,  # SERVER constant — no del body
        descripcion=req.descripcion,
    )

    return JSONResponse({
        "aprobado": aprobado,
        "buy_order": buy_order,
        "monto_clp": PRECIO_SCAN_CLP,
        "error": error_msg,
    })


# ── Eliminar inscripción ──────────────────────────────────────────────────────

@router.delete("/tarjeta")
async def eliminar_tarjeta(user: UserPayload = Depends(get_current_user)):
    """Elimina la inscripción Oneclick del usuario autenticado."""
    inscripcion = await imagiax_pagos_db.get_inscripcion_oneclick_any(usuario_id=user.user_id)
    if not inscripcion:
        raise HTTPException(404, "No hay tarjeta Oneclick inscrita para este usuario")

    db_username = inscripcion["username"]

    # Eliminar en Transbank (usar username exacto de la DB)
    try:
        inscription = _get_inscription()
        inscription.delete(
            tbk_user=inscripcion["tbk_user"],
            username=db_username,
        )
        logger.info("Inscripción Oneclick eliminada en Transbank: user=%s", db_username)
    except Exception as exc:
        logger.warning("Error eliminando inscripción en Transbank (continuando): %s", exc)

    # Desactivar en DB
    await imagiax_pagos_db.borrar_inscripcion_oneclick(db_username)

    return JSONResponse({"ok": True, "mensaje": "Tarjeta desvinculada correctamente"})


# ── Estado de inscripción ─────────────────────────────────────────────────────

@router.get("/estado")
async def estado_oneclick(user: UserPayload = Depends(get_current_user)):
    """Retorna el estado de la inscripción Oneclick del usuario autenticado."""
    inscripcion = await imagiax_pagos_db.get_inscripcion_oneclick_any(usuario_id=user.user_id)

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
        "created_at": inscripcion["created_at"].isoformat() if inscripcion.get("created_at") else None,
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
