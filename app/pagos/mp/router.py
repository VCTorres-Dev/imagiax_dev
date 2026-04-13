"""
app.pagos.mp.router -- MercadoPago payment endpoints.

Routes (NO prefix -- main.py includes without prefix):
  GET  /pagos/estado                 JSON  credit status
  POST /pagos/registrar-tarjeta      JSON  register card for auto-charge
  GET  /pagos/metodo-pago            JSON  current payment method
  DELETE /pagos/metodo-pago          JSON  delete saved card
  POST /pagos/cancelar-suscripcion   JSON  cancel active subscription
  POST /pagos/crear-sesion           JSON  create checkout session
  GET  /pagos/exito                  HTML  success callback
  GET  /pagos/suscripcion/exito      HTML  subscription success callback
  GET  /pagos/pendiente              HTML  pending callback
  GET  /pagos/fallo                  HTML  failure callback
  POST /pagos/webhook                JSON  IPN webhook (public)
  GET  /pagos/admin/resumen          JSON  admin summary
  GET  /pagos/verificar/{payment_id} JSON  manual verify
  GET  /pagos/historial-cobros       JSON  billing history
  GET  /pagos/exportar-excel         file  Excel export
  GET  /historial                    HTML  billing history page
  GET  /pagar                        HTML  pricing page
"""
from __future__ import annotations

import io
import json
import logging
import uuid as _uuid_mod
from datetime import datetime
from html import escape as html_escape

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from app.auth.dependencies import get_current_user, require_role
from app.auth.models import UserPayload
from app.auth.service import decode_token
from app.core.config import settings
from app.core.templates import templates
from app.pagos import db as pagos_db
from app.pagos.mp import service
from app.pagos.mp.client import mp_get, mp_post

logger = logging.getLogger("imagiax.pagos.mp.router")

router = APIRouter()


async def _optional_current_user(request: Request) -> "UserPayload | None":
    """Optional auth for payment callback pages.

    Returns the authenticated user if the session cookie is still valid,
    or None if the cookie expired during external checkout. Payment
    callbacks must always display results — the payment has already
    been processed regardless of auth state.
    """
    try:
        from app.auth.dependencies import get_current_user as _get_user
        return await _get_user(credentials=None, request=request)
    except Exception:
        return None


# ── Pydantic request models ───────────────────────────────────────────────────

class CrearSesionRequest(BaseModel):
    plan_id: str
    email: str
    centro_medico: str
    nombre_contacto: str


class RegistrarTarjetaRequest(BaseModel):
    card_token_id: str
    email: str
    centro_medico: str = ""
    acepta_cobros_automaticos: bool = False


# ── Helpers ────────────────────────────────────────────────────────────────────

PRECIO_SCAN_CLP: int = 990


def _plan_card_html(plan_id: str, plan: dict, tiene_suscripcion: bool = False) -> str:
    """Build HTML card for a single plan (used by /pagar)."""
    badge_text = plan.get("badge", "")
    if badge_text:
        badge_cls = "badge verde" if "valor" in badge_text.lower() else "badge"
        badge = f'<div class="{badge_cls}">{badge_text}</div>'
    else:
        badge = ""

    precio_fmt = f"${plan['precio_clp']:,}".replace(",", ".")
    frecuencia = "/mes" if plan["recurrente"] else ""
    creditos_txt = f"{plan['creditos']}/mes" if plan["recurrente"] else str(plan["creditos"])

    precio_unit = round(plan["precio_clp"] / plan["creditos"])
    precio_unit_fmt = f"${precio_unit:,}".replace(",", ".")
    unit_label = f'<div class="plan-unit-price">{precio_unit_fmt} CLP por informe</div>'

    destacado = plan.get("badge") == "Más popular"
    card_class = "plan-card destacado" if destacado else "plan-card"

    icons = {
        "pack_individual": '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><path d="M12 8v4l3 3"/></svg>',
        "pack_50": '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/></svg>',
        "pack_200": '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg>',
        "suscripcion_mensual": '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>',
    }
    icon_html = icons.get(plan_id, "")
    icon_color = "#3b82f6" if not destacado else "#60a5fa"

    btn_disabled = ""
    btn_text = "Suscribirse" if plan["recurrente"] else "Comprar ahora"
    if plan["recurrente"] and tiene_suscripcion:
        btn_disabled = 'disabled style="opacity:0.5;cursor:not-allowed"'
        btn_text = "Ya suscrito"

    return f"""
    <div class="{card_class}" data-plan="{plan_id}">
      {badge}
      <div class="plan-icon" style="color:{icon_color}">{icon_html}</div>
      <div class="plan-titulo">{plan['titulo']}</div>
      <div class="plan-precio">
        <span class="precio-monto">{precio_fmt}</span>
        <span class="precio-clp">CLP{frecuencia}</span>
      </div>
      {unit_label}
      <div class="plan-desc">{plan['descripcion']}</div>
      <div class="plan-creditos">
        <span class="credito-num">{creditos_txt}</span>
        <span class="credito-lbl"> informes</span>
      </div>
      <button class="btn-comprar" onclick="seleccionarPlan('{plan_id}')" {btn_disabled}>
        {btn_text}
      </button>
    </div>"""


def _test_badge_html() -> str:
    if service._is_test():
        return (
            '<div style="position:fixed;top:0;left:50%;transform:translateX(-50%);'
            "background:#f59e0b;color:#000;font-size:11px;font-weight:700;"
            "padding:3px 16px;border-radius:0 0 8px 8px;z-index:9999;"
            'letter-spacing:.5px">MODO TEST — Pagos simulados</div>'
        )
    return ""


# ═══════════════════════════════════════════════════════════════════════════════
# JSON API ROUTES (authenticated)
# ═══════════════════════════════════════════════════════════════════════════════


@router.get("/pagos/estado")
async def estado_creditos(
    request: Request,
    user: UserPayload = Depends(get_current_user),
) -> JSONResponse:
    pool = request.app.state.pool
    estado = await pagos_db.get_estado_creditos(pool, usuario_id=user.user_id)
    installation_id = await pagos_db.get_or_create_installation_id(pool)
    return JSONResponse({
        "installation_id": installation_id,
        "creditos_disponibles": estado["creditos_disponibles"],
        "creditos_total": estado["creditos_total"],
        "es_trial": estado["es_trial"],
        "tiene_suscripcion": estado["tiene_suscripcion"],
        "tiene_tarjeta": estado["tiene_tarjeta"],
        "card_last4": estado["card_last4"],
        "activa": estado["activa"],
        "pagar_url": f"{settings.IMAGIAX_BASE_URL}/pagar",
        "mensaje": (
            f"Período de prueba — {estado['creditos_disponibles']} informes gratuitos restantes"
            if estado["es_trial"]
            else f"Suscripción activa — {estado['creditos_disponibles']} créditos disponibles"
            if estado["tiene_suscripcion"]
            else f"{estado['creditos_disponibles']} créditos disponibles"
        ),
    })


@router.post("/pagos/registrar-tarjeta")
async def registrar_tarjeta(
    body: RegistrarTarjetaRequest,
    request: Request,
    user: UserPayload = Depends(get_current_user),
) -> JSONResponse:
    if not body.acepta_cobros_automaticos:
        raise HTTPException(400, "Debe aceptar la autorización de cobros automáticos para continuar.")

    pool = request.app.state.pool
    from urllib.parse import quote as _url_quote

    # 1. Search or create MP Customer
    try:
        search = await mp_get(f"/v1/customers/search?email={_url_quote(body.email)}")
        results = search.get("results", [])
        if results:
            mp_customer_id = str(results[0]["id"])
        else:
            customer = await mp_post("/v1/customers", {"email": body.email})
            mp_customer_id = str(customer["id"])
    except HTTPException:
        raise

    # 2. Associate card to customer
    try:
        card_data = await mp_post(
            f"/v1/customers/{mp_customer_id}/cards",
            {"token": body.card_token_id},
            idempotency_key=str(_uuid_mod.uuid4()),
        )
    except HTTPException as exc:
        raise HTTPException(502, f"No se pudo guardar la tarjeta en MercadoPago: {exc.detail}") from exc

    mp_card_id = str(card_data["id"])
    card_last4 = str(card_data.get("last_four_digits", "****"))
    card_brand = (card_data.get("payment_method") or {}).get("id", "") or card_data.get("payment_method_id", "")

    # 3. Save locally
    await pagos_db.guardar_metodo_pago(
        pool,
        mp_customer_id=mp_customer_id,
        mp_card_id=mp_card_id,
        card_last4=card_last4,
        card_brand=card_brand,
        email=body.email,
        usuario_id=user.user_id,
    )
    await pagos_db.crear_o_buscar_cliente(pool, email=body.email, centro_medico=body.centro_medico)

    return JSONResponse({
        "ok": True,
        "mp_customer_id": mp_customer_id,
        "card_last4": card_last4,
        "card_brand": card_brand,
        "mensaje": (
            f"Tarjeta ****{card_last4} registrada correctamente. "
            f"Se cobrará ${PRECIO_SCAN_CLP:,} CLP de forma automática por cada informe generado."
        ),
    })


@router.get("/pagos/metodo-pago")
async def get_metodo_pago_endpoint(
    request: Request,
    user: UserPayload = Depends(get_current_user),
) -> JSONResponse:
    pool = request.app.state.pool
    oneclick = await pagos_db.get_inscripcion_oneclick_any(pool, usuario_id=user.user_id)
    if oneclick:
        return JSONResponse({
            "tiene_tarjeta": True,
            "metodo": "oneclick",
            "card_last4": oneclick.get("card_number_last4", "****"),
            "card_brand": oneclick.get("card_type", ""),
            "email": oneclick.get("email", ""),
        })

    metodo = await pagos_db.get_metodo_pago(pool, usuario_id=user.user_id)
    if not metodo:
        return JSONResponse({"tiene_tarjeta": False})
    return JSONResponse({
        "tiene_tarjeta": True,
        "metodo": "mp_card",
        "card_last4": metodo["card_last4"],
        "card_brand": metodo["card_brand"],
        "email": metodo["email"],
    })


@router.delete("/pagos/metodo-pago")
async def borrar_metodo_pago_endpoint(
    request: Request,
    user: UserPayload = Depends(get_current_user),
) -> JSONResponse:
    pool = request.app.state.pool
    metodo = await pagos_db.get_metodo_pago(pool, usuario_id=user.user_id)
    if metodo and metodo.get("mp_customer_id") and metodo.get("mp_card_id"):
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                token = settings.MP_ACCESS_TOKEN.get_secret_value()
                resp = await client.delete(
                    f"https://api.mercadopago.com/v1/customers/{metodo['mp_customer_id']}/cards/{metodo['mp_card_id']}",
                    headers={"Authorization": f"Bearer {token}"},
                )
                if resp.status_code < 300:
                    logger.info("Tarjeta %s eliminada de MercadoPago.", metodo["mp_card_id"])
                else:
                    logger.warning("No se pudo borrar tarjeta de MP (status %d).", resp.status_code)
        except Exception as exc:
            logger.warning("Error al borrar tarjeta de MP (continuando): %s", exc)

    await pagos_db.borrar_metodo_pago(pool, usuario_id=user.user_id)
    return JSONResponse({"ok": True, "mensaje": "Método de pago eliminado correctamente."})


@router.post("/pagos/cancelar-suscripcion")
async def cancelar_suscripcion_endpoint(
    request: Request,
    user: UserPayload = Depends(get_current_user),
) -> JSONResponse:
    pool = request.app.state.pool
    suscripcion = await pagos_db.get_suscripcion_activa(pool, usuario_id=user.user_id)
    if not suscripcion:
        raise HTTPException(400, "No hay suscripción activa para cancelar.")

    preapproval_id = suscripcion.get("mp_preapproval_id")

    if preapproval_id:
        mp_exito, mp_mensaje = await service.cancelar_preapproval_mp(preapproval_id)
        if not mp_exito:
            raise HTTPException(502, f"No se pudo cancelar en MercadoPago: {mp_mensaje}. La suscripción sigue activa.")
        mensaje_mp = f" {mp_mensaje}"
    else:
        mensaje_mp = " No existe preapproval_id asociado; se desactivará localmente."

    licencia_id = str(suscripcion.get("id", ""))
    exito_db, mensaje_db = await pagos_db.desactivar_licencia(pool, licencia_id)
    if not exito_db:
        raise HTTPException(500, f"Cancelada en MP pero error local: {mensaje_db}")

    return JSONResponse({
        "ok": True,
        "mensaje": f"Suscripción cancelada correctamente.{mensaje_mp} {mensaje_db}",
        "cancelado_en_mp": bool(preapproval_id),
    })


@router.post("/pagos/crear-sesion")
async def crear_sesion_pago_endpoint(
    body: CrearSesionRequest,
    request: Request,
    user: UserPayload = Depends(get_current_user),
) -> JSONResponse:
    pool = request.app.state.pool
    plan = service.PLANES.get(body.plan_id)
    if not plan:
        raise HTTPException(400, f"Plan '{body.plan_id}' no existe. Opciones: {list(service.PLANES)}")

    if plan["recurrente"]:
        estado = await pagos_db.get_estado_creditos(pool, usuario_id=user.user_id)
        if estado.get("tiene_suscripcion"):
            raise HTTPException(
                400,
                "Ya tienes una suscripcion activa. Cancela la actual antes de adquirir otra, "
                "o espera a que expire. Puedes comprar packs adicionales si necesitas mas creditos.",
            )

    result = await service.crear_sesion_pago(
        pool,
        plan_id=body.plan_id,
        email=body.email,
        centro_medico=body.centro_medico,
        nombre_contacto=body.nombre_contacto,
        usuario_id=user.user_id,
    )
    return JSONResponse(result)


# ═══════════════════════════════════════════════════════════════════════════════
# HTML CALLBACK ROUTES (authenticated)
# ═══════════════════════════════════════════════════════════════════════════════


@router.get("/pagos/exito", response_class=HTMLResponse)
async def pago_exitoso(
    request: Request,
    payment_id: str = Query(None),
    collection_id: str = Query(None),
    collection_status: str = Query(None),
    preference_id: str = Query(None),
    status: str = Query(None),
    merchant_order_id: str = Query(None),
    current_user: UserPayload | None = Depends(_optional_current_user),
) -> HTMLResponse:
    pool = request.app.state.pool
    payment_id = payment_id or collection_id
    if not payment_id:
        return templates.TemplateResponse("pagos/resultado.html", {
            "request": request, "status": "error",
            "mensaje": "No se recibió un ID de pago válido.",
            "viewer_url": settings.IMAGIAX_VIEWER_URL,
            "pagar_url": f"{settings.IMAGIAX_BASE_URL}/pagar",
        })

    try:
        payment_data = await mp_get(f"/v1/payments/{payment_id}")
    except HTTPException:
        return templates.TemplateResponse("pagos/resultado.html", {
            "request": request, "status": "error",
            "mensaje": "No se pudo verificar el pago con MercadoPago.",
            "viewer_url": settings.IMAGIAX_VIEWER_URL,
            "pagar_url": f"{settings.IMAGIAX_BASE_URL}/pagar",
        })

    if payment_data.get("status") != "approved":
        estado_mp = payment_data.get("status", "desconocido")
        if estado_mp == "pending":
            return templates.TemplateResponse("pagos/resultado.html", {
                "request": request, "status": "pendiente",
                "payment_id": payment_id,
                "viewer_url": settings.IMAGIAX_VIEWER_URL,
                "pagar_url": f"{settings.IMAGIAX_BASE_URL}/pagar",
            })
        return templates.TemplateResponse("pagos/resultado.html", {
            "request": request, "status": "error",
            "mensaje": f"El pago está en estado '{estado_mp}'. Si fue un error, contáctenos.",
            "viewer_url": settings.IMAGIAX_VIEWER_URL,
            "pagar_url": f"{settings.IMAGIAX_BASE_URL}/pagar",
        })

    ext_ref_raw = payment_data.get("external_reference") or preference_id or ""
    ext_ref = service.parse_external_reference(ext_ref_raw)

    plan_id = ext_ref.get("plan_id", "pack_individual")
    cliente_id = ext_ref.get("cliente_id")
    ext_uid = ext_ref.get("usuario_id") or None
    email = ext_ref.get("email", payment_data.get("payer", {}).get("email", ""))
    plan = service.PLANES.get(plan_id, service.PLANES["pack_individual"])

    if cliente_id:
        try:
            licencia = await pagos_db.acreditar_pago(
                pool,
                cliente_id=cliente_id,
                tipo=plan_id,
                creditos=plan["creditos"],
                mp_payment_id=str(payment_id),
                mp_preference_id=preference_id,
                duracion_dias=plan.get("duracion_dias"),
                usuario_id=ext_uid,
            )
            await pagos_db.registrar_pago(
                pool,
                cliente_id=cliente_id,
                licencia_id=licencia["id"] if licencia else None,
                mp_payment_id=str(payment_id),
                mp_preference_id=preference_id,
                mp_preapproval_id=None,
                tipo="pago_unico",
                estado="approved",
                monto_clp=int(payment_data.get("transaction_amount", plan["precio_clp"])),
                descripcion=plan["titulo"],
                raw_webhook=payment_data,
            )
        except Exception as exc:
            logger.error("Error acreditando pago %s: %s", payment_id, exc)

    estado_actual = await pagos_db.get_estado_creditos(pool, usuario_id=ext_uid)
    return templates.TemplateResponse("pagos/resultado.html", {
        "request": request,
        "status": "exito",
        "plan_title": plan["titulo"],
        "creditos_nuevos": plan["creditos"],
        "creditos_totales": estado_actual["creditos_disponibles"],
        "email": email,
        "es_suscripcion": False,
        "viewer_url": settings.IMAGIAX_VIEWER_URL,
        "pagar_url": f"{settings.IMAGIAX_BASE_URL}/pagar",
    })


@router.get("/pagos/suscripcion/exito", response_class=HTMLResponse)
async def suscripcion_exitosa(
    request: Request,
    preapproval_id: str = Query(None),
    current_user: UserPayload | None = Depends(_optional_current_user),
) -> HTMLResponse:
    pool = request.app.state.pool
    if not preapproval_id:
        return templates.TemplateResponse("pagos/resultado.html", {
            "request": request, "status": "error",
            "mensaje": "No se recibió el ID de suscripción.",
            "viewer_url": settings.IMAGIAX_VIEWER_URL,
            "pagar_url": f"{settings.IMAGIAX_BASE_URL}/pagar",
        })

    try:
        preapproval_data = await mp_get(f"/preapproval/{preapproval_id}")
    except HTTPException:
        return templates.TemplateResponse("pagos/resultado.html", {
            "request": request, "status": "error",
            "mensaje": "No se pudo verificar la suscripción.",
            "viewer_url": settings.IMAGIAX_VIEWER_URL,
            "pagar_url": f"{settings.IMAGIAX_BASE_URL}/pagar",
        })

    suscripcion_status = preapproval_data.get("status", "")
    if suscripcion_status not in ("authorized", "pending"):
        return templates.TemplateResponse("pagos/resultado.html", {
            "request": request, "status": "error",
            "mensaje": f"Estado de suscripción: '{suscripcion_status}'.",
            "viewer_url": settings.IMAGIAX_VIEWER_URL,
            "pagar_url": f"{settings.IMAGIAX_BASE_URL}/pagar",
        })

    ext_ref = service.parse_external_reference(preapproval_data.get("external_reference", ""))
    plan_id = ext_ref.get("plan_id", "suscripcion_mensual")
    cliente_id = ext_ref.get("cliente_id")
    ext_uid = ext_ref.get("usuario_id") or None
    email = ext_ref.get("email", preapproval_data.get("payer_email", ""))
    plan = service.PLANES.get(plan_id, service.PLANES["suscripcion_mensual"])

    if cliente_id and suscripcion_status == "authorized":
        try:
            licencia = await pagos_db.acreditar_pago(
                pool,
                cliente_id=cliente_id,
                tipo=plan_id,
                creditos=plan["creditos"],
                mp_preapproval_id=preapproval_id,
                duracion_dias=plan.get("duracion_dias", 31),
                usuario_id=ext_uid,
            )
            await pagos_db.registrar_pago(
                pool,
                cliente_id=cliente_id,
                licencia_id=licencia["id"],
                mp_payment_id=None,
                mp_preference_id=None,
                mp_preapproval_id=preapproval_id,
                tipo="suscripcion",
                estado="authorized",
                monto_clp=plan["precio_clp"],
                descripcion=f"Suscripción: {plan['titulo']}",
                raw_webhook=preapproval_data,
            )
        except Exception as exc:
            logger.error("Error acreditando suscripción %s: %s", preapproval_id, exc)

    estado_actual = await pagos_db.get_estado_creditos(pool, usuario_id=ext_uid)
    return templates.TemplateResponse("pagos/resultado.html", {
        "request": request,
        "status": "exito",
        "plan_title": plan["titulo"],
        "creditos_nuevos": plan["creditos"],
        "creditos_totales": estado_actual["creditos_disponibles"],
        "email": email,
        "es_suscripcion": True,
        "viewer_url": settings.IMAGIAX_VIEWER_URL,
        "pagar_url": f"{settings.IMAGIAX_BASE_URL}/pagar",
    })


@router.get("/pagos/pendiente", response_class=HTMLResponse)
async def pago_pendiente(
    request: Request,
    payment_id: str = Query(None),
    current_user: UserPayload | None = Depends(_optional_current_user),
) -> HTMLResponse:
    return templates.TemplateResponse("pagos/resultado.html", {
        "request": request,
        "status": "pendiente",
        "payment_id": payment_id or "",
        "viewer_url": settings.IMAGIAX_VIEWER_URL,
        "pagar_url": f"{settings.IMAGIAX_BASE_URL}/pagar",
    })


@router.get("/pagos/fallo", response_class=HTMLResponse)
async def pago_fallido(
    request: Request,
    current_user: UserPayload | None = Depends(_optional_current_user),
) -> HTMLResponse:
    return templates.TemplateResponse("pagos/resultado.html", {
        "request": request,
        "status": "error",
        "mensaje": (
            "El pago no pudo ser procesado. Por favor intente nuevamente o "
            "contacte a su banco si el problema persiste."
        ),
        "viewer_url": settings.IMAGIAX_VIEWER_URL,
        "pagar_url": f"{settings.IMAGIAX_BASE_URL}/pagar",
    })


# ═══════════════════════════════════════════════════════════════════════════════
# WEBHOOK (public -- no auth, validates X-Signature)
# ═══════════════════════════════════════════════════════════════════════════════


@router.post("/pagos/webhook")
async def webhook_mp(request: Request) -> JSONResponse:
    pool = request.app.state.pool
    body_bytes = await request.body()
    try:
        body = json.loads(body_bytes) if body_bytes else {}
    except Exception:
        body = {}

    if not service.validar_webhook_signature(request, body_bytes):
        logger.warning("Webhook rechazado — firma inválida.")
        return JSONResponse({"ok": False, "detalle": "firma inválida"}, status_code=401)

    notification_type = body.get("type") or request.query_params.get("type", "")
    data_id = (body.get("data") or {}).get("id") or request.query_params.get("data.id", "")

    logger.info("Webhook MP recibido: type=%s, id=%s", notification_type, data_id)

    if not data_id:
        return JSONResponse({"ok": True, "detalle": "sin data.id"})

    try:
        if notification_type == "payment":
            await service.procesar_webhook_payment(str(data_id), body, pool)
        elif notification_type in ("subscription_preapproval", "preapproval"):
            await service.procesar_webhook_preapproval(str(data_id), body, pool)
        elif notification_type == "subscription_authorized_payment":
            await service.procesar_webhook_renovacion(str(data_id), body, pool)
        else:
            logger.debug("Webhook tipo '%s' ignorado.", notification_type)
    except Exception as exc:
        logger.error("Error procesando webhook %s/%s: %s", notification_type, data_id, exc)

    return JSONResponse({"ok": True})


# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN ROUTES (require_role("admin"))
# ═══════════════════════════════════════════════════════════════════════════════


@router.get("/pagos/admin/resumen")
async def admin_resumen(
    request: Request,
    user: UserPayload = Depends(require_role("admin")),
) -> JSONResponse:
    pool = request.app.state.pool
    resumen = await pagos_db.get_admin_resumen(pool)
    return JSONResponse(resumen)


@router.get("/pagos/verificar/{payment_id}")
async def verificar_pago_manual(
    payment_id: str,
    request: Request,
    user: UserPayload = Depends(require_role("admin")),
) -> JSONResponse:
    # SEC: MP payment IDs are always numeric — block path traversal
    if not payment_id.isdigit():
        return JSONResponse({"ok": False, "error": "payment_id inválido"}, status_code=400)

    pool = request.app.state.pool
    try:
        payment_data = await mp_get(f"/v1/payments/{payment_id}")
    except HTTPException as exc:
        return JSONResponse({"ok": False, "error": f"No se pudo verificar: {exc.detail}"}, status_code=400)

    status = payment_data.get("status", "")
    if status != "approved":
        return JSONResponse({
            "ok": False,
            "status": status,
            "mensaje": f"El pago tiene estado '{status}', no se acreditaron creditos.",
        })

    ext_ref = service.parse_external_reference(payment_data.get("external_reference", ""))
    plan_id = ext_ref.get("plan_id", "pack_individual")
    cliente_id = ext_ref.get("cliente_id")
    ext_uid = ext_ref.get("usuario_id") or None
    plan = service.PLANES.get(plan_id, service.PLANES["pack_individual"])

    if not cliente_id:
        return JSONResponse({
            "ok": False,
            "error": "No se encontro cliente_id en el pago. Verifique la referencia externa.",
        }, status_code=400)

    pago_existente = await pagos_db.buscar_pago_por_mp_id(pool, str(payment_id))
    if pago_existente and pago_existente.get("estado") == "approved":
        estado_actual = await pagos_db.get_estado_creditos(pool, usuario_id=ext_uid)
        return JSONResponse({
            "ok": True,
            "mensaje": "Este pago ya fue procesado anteriormente.",
            "creditos_actuales": estado_actual["creditos_disponibles"],
        })

    try:
        licencia = await pagos_db.acreditar_pago(
            pool,
            cliente_id=cliente_id,
            tipo=plan_id,
            creditos=plan["creditos"],
            mp_payment_id=str(payment_id),
            mp_preference_id=payment_data.get("preference_id"),
            duracion_dias=plan.get("duracion_dias"),
            usuario_id=ext_uid,
        )
        await pagos_db.registrar_pago(
            pool,
            cliente_id=cliente_id,
            licencia_id=licencia["id"] if licencia else None,
            mp_payment_id=str(payment_id),
            mp_preference_id=payment_data.get("preference_id"),
            mp_preapproval_id=None,
            tipo=plan_id,
            estado="approved",
            monto_clp=int(payment_data.get("transaction_amount", plan["precio_clp"])),
            descripcion=plan["titulo"],
            raw_webhook=payment_data,
        )
    except Exception as exc:
        logger.error("Error acreditando pago manual %s: %s", payment_id, exc)
        return JSONResponse({"ok": False, "error": "Error interno al verificar pago"}, status_code=500)

    estado_actual = await pagos_db.get_estado_creditos(pool, usuario_id=ext_uid)
    return JSONResponse({
        "ok": True,
        "mensaje": "Pago verificado y acreditado correctamente.",
        "plan": plan["titulo"],
        "creditos_acreditados": plan["creditos"],
        "creditos_totales": estado_actual["creditos_disponibles"],
    })


@router.get("/pagos/historial-cobros")
async def historial_cobros(
    request: Request,
    periodo: str = Query("total"),
    user: UserPayload = Depends(require_role("admin")),
) -> JSONResponse:
    """
    Vista administrativa de TODAS las transacciones de la plataforma.

    NOTA: Este endpoint muestra transacciones de TODOS los médicos/usuarios,
    no solo del usuario logueado. Esto es por diseño — es una herramienta de
    gestión financiera para admins/superadmins. Los médicos individuales
    consultan su historial a través de /informes/listado.

    No agregar filtro por usuario_id aquí — eso rompería la vista administrativa.
    Ver BUG-013.
    """
    pool = request.app.state.pool
    historial = await pagos_db.get_historial_cobros(pool, periodo)
    return JSONResponse(historial)


@router.get("/pagos/exportar-excel")
async def exportar_excel(
    request: Request,
    periodo: str = Query("total"),
    user: UserPayload = Depends(require_role("admin")),
):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.chart import BarChart, PieChart, Reference
    from openpyxl.utils import get_column_letter

    pool = request.app.state.pool
    historial = await pagos_db.get_historial_cobros(pool, periodo)
    cobros = historial.get("cobros", [])
    resumen = historial.get("resumen", {})

    wb = Workbook()

    # ── Palette ──────────────────────────────────────────────────────────────
    BLUE        = "1E40AF"
    BLUE_LIGHT  = "DBEAFE"
    DARK        = "0F172A"
    GREEN       = "166534"
    GREEN_LIGHT = "DCFCE7"
    RED_LIGHT   = "FEE2E2"
    GRAY        = "64748B"
    WHITE       = "FFFFFF"
    BORDER_COLOR = "CBD5E1"
    CHART_BLUE  = "2563EB"
    ZEBRA_BLUE  = "F8FAFC"

    header_font  = Font(name="Calibri", bold=True, color=WHITE, size=11)
    header_fill  = PatternFill(start_color=BLUE, end_color=BLUE, fill_type="solid")
    money_fmt    = '#,##0" CLP"'
    thin_border  = Border(bottom=Side(style="thin", color=BORDER_COLOR))

    # ══════════════════════════════════════════════════════════════════════════
    # Sheet 1: Resumen — professional dashboard layout
    # ══════════════════════════════════════════════════════════════════════════
    ws = wb.active
    ws.title = "Resumen"
    ws.sheet_properties.tabColor = BLUE

    # ── Section 1: Header (rows 1-3) ─────────────────────────────────────────
    ws.merge_cells("A1:J1")
    ws["A1"] = "IMAGIAX \u2014 Reporte de Facturaci\u00f3n"
    ws["A1"].font = Font(name="Calibri", bold=True, color=DARK, size=18)
    ws["A1"].alignment = Alignment(vertical="center")
    ws.row_dimensions[1].height = 42

    # Row 2: empty spacing
    ws.row_dimensions[2].height = 8

    periodos_label = {
        "dia":       "Hoy",
        "semana":    "\u00daltimos 7 d\u00edas",
        "mes":       "\u00daltimos 30 d\u00edas",
        "trimestre": "\u00daltimos 90 d\u00edas",
        "anio":      "\u00daltimos 12 meses",
        "total":     "Hist\u00f3rico completo",
    }
    ws.merge_cells("A3:J3")
    ws["A3"] = (
        f"Per\u00edodo: {periodos_label.get(periodo, periodo)}"
        f" \u2502 Generado: {datetime.now().strftime('%d/%m/%Y %H:%M')}"
    )
    ws["A3"].font = Font(name="Calibri", color=GRAY, size=10)
    ws["A3"].alignment = Alignment(vertical="center")
    ws.row_dimensions[3].height = 20

    # ── Section 2: KPI Cards (rows 5-7) ──────────────────────────────────────
    # 4 cards: cols A-B, D-E, G-H, J-K  (1-col gap between)
    kpi_data = [
        ("Total Cobrado",   f"${resumen.get('total_gastado', 0):,.0f} CLP", GREEN,    1),
        ("Transacciones",   str(resumen.get("total_transacciones", 0)),      CHART_BLUE, 4),
        ("Aprobadas",       str(resumen.get("aprobados", 0)),                GREEN,    7),
        ("Rechazadas",      str(resumen.get("rechazados", 0)),               "DC2626", 10),
    ]
    for label, value, color, col in kpi_data:
        # Label row 5
        lbl = ws.cell(row=5, column=col, value=label)
        lbl.font = Font(name="Calibri", color=GRAY, size=9)
        lbl.alignment = Alignment(horizontal="center")
        ws.merge_cells(start_row=5, start_column=col, end_row=5, end_column=col + 1)
        # Value row 6
        val = ws.cell(row=6, column=col, value=value)
        val.font = Font(name="Calibri", bold=True, color=color, size=14)
        val.alignment = Alignment(horizontal="center")
        ws.merge_cells(start_row=6, start_column=col, end_row=6, end_column=col + 1)
        # Thin bottom border row 7
        for c in range(col, col + 2):
            ws.cell(row=7, column=c).border = Border(bottom=Side(style="thin", color=BORDER_COLOR))

    ws.row_dimensions[5].height = 18
    ws.row_dimensions[6].height = 28
    ws.row_dimensions[7].height = 6

    # ── Section 3: Status table + PieChart (rows 9-20) ───────────────────────
    aprobados  = resumen.get("aprobados", 0)
    rechazados = resumen.get("rechazados", 0)
    pendientes = resumen.get("total_transacciones", 0) - aprobados - rechazados

    pie_end_row = 20   # conservative lower bound for PieChart area

    if aprobados or rechazados or pendientes:
        # Table headers row 9
        hdr_estado = ws.cell(row=9, column=1, value="Estado")
        hdr_estado.font = header_font
        hdr_estado.fill = header_fill
        hdr_estado.alignment = Alignment(horizontal="center")

        hdr_cant = ws.cell(row=9, column=2, value="Cantidad")
        hdr_cant.font = header_font
        hdr_cant.fill = header_fill
        hdr_cant.alignment = Alignment(horizontal="center")

        tbl_rows = [
            ("Aprobados",  aprobados),
            ("Rechazados", rechazados),
        ]
        if pendientes > 0:
            tbl_rows.append(("Pendientes", pendientes))

        data_start = 10
        for i, (lbl, cnt) in enumerate(tbl_rows):
            ws.cell(row=data_start + i, column=1, value=lbl)
            ws.cell(row=data_start + i, column=2, value=cnt)

        tbl_end = data_start + len(tbl_rows) - 1  # last data row

        # PieChart anchored at E9, fixed — no collision risk
        pie = PieChart()
        pie.title = "Distribuci\u00f3n por Estado"
        pie.style = 26
        pie.width  = 12
        pie.height = 10

        data_ref = Reference(ws, min_col=2, min_row=9, max_row=tbl_end)
        cats_ref = Reference(ws, min_col=1, min_row=data_start, max_row=tbl_end)
        pie.add_data(data_ref, titles_from_data=True)
        pie.set_categories(cats_ref)

        # Professional slice colors: blue/red/amber
        try:
            from openpyxl.chart.series import DataPoint
            slice_colors = [CHART_BLUE, "DC2626", "F59E0B"]
            if pie.series:
                for idx, hex_color in enumerate(slice_colors[:len(tbl_rows)]):
                    pt = DataPoint(idx=idx)
                    pt.graphicalProperties.solidFill = hex_color
                    pie.series[0].dPt.append(pt)
        except Exception:
            pass

        # Data labels: percentage + category
        try:
            from openpyxl.chart.label import DataLabelList
            dl = DataLabelList()
            dl.showPercent  = True
            dl.showVal      = False
            dl.showCatName  = True
            dl.showLegendKey = False
            dl.showSerName  = False
            pie.dataLabels  = dl
        except Exception:
            pass

        # Legend at bottom
        try:
            from openpyxl.chart.legend import Legend
            leg = Legend()
            leg.position = "b"
            pie.legend = leg
        except Exception:
            pie.legend = None

        ws.add_chart(pie, "E9")

    # ── Section 4: Daily cobros table + BarChart (rows 23+) ──────────────────
    # ALWAYS placed below the PieChart to prevent overlap.
    # The bar table starts at row 23 minimum, BarChart anchored with max() guard.

    if cobros:
        daily: dict[str, int] = {}
        for c in cobros:
            if c.get("estado") == "approved":
                d = c.get("created_at", "")[:10]
                daily[d] = daily.get(d, 0) + c.get("monto_clp", 0)

        if daily:
            sorted_days = sorted(daily.items())
            days_slice  = sorted_days[-30:]

            # Table starts at row 23 (Section separator at 22 is empty)
            tbl_header_row = 23
            tbl_data_start = tbl_header_row + 1
            data_end = tbl_data_start + len(days_slice) - 1

            # Header
            h_fecha = ws.cell(row=tbl_header_row, column=1, value="Fecha")
            h_fecha.font  = header_font
            h_fecha.fill  = header_fill
            h_fecha.alignment = Alignment(horizontal="center")

            h_cobros = ws.cell(row=tbl_header_row, column=2, value="Cobros CLP")
            h_cobros.font  = header_font
            h_cobros.fill  = header_fill
            h_cobros.alignment = Alignment(horizontal="center")

            for idx, (day, amount) in enumerate(days_slice):
                ws.cell(row=tbl_data_start + idx, column=1, value=day)
                ws.cell(row=tbl_data_start + idx, column=2, value=amount)

            # BarChart — FIXED anchor using max() to never overlap PieChart
            bar_anchor_row = max(data_end + 2, 36)

            bar = BarChart()
            bar.type   = "col"
            bar.title  = "Cobros Diarios (CLP)"
            bar.style  = 10
            bar.width  = 22
            bar.height = 12
            bar.y_axis.title = "CLP"
            bar.x_axis.title = "Fecha"
            bar.legend = None  # single series — legend is redundant

            bar_data = Reference(ws, min_col=2, min_row=tbl_header_row, max_row=data_end)
            bar_cats = Reference(ws, min_col=1, min_row=tbl_data_start, max_row=data_end)
            bar.add_data(bar_data, titles_from_data=True)
            bar.set_categories(bar_cats)

            # Single professional blue for all bars
            if bar.series:
                bar.series[0].graphicalProperties.solidFill = CHART_BLUE
                bar.series[0].graphicalProperties.line.solidFill = CHART_BLUE
                bar.series[0].title = None

            ws.add_chart(bar, f"A{bar_anchor_row}")

    # Column widths for Resumen sheet
    for col_idx in range(1, 12):
        ws.column_dimensions[get_column_letter(col_idx)].width = 14

    # ══════════════════════════════════════════════════════════════════════════
    # Sheet 2: Detalle — keep structure, add zebra striping
    # ══════════════════════════════════════════════════════════════════════════
    ws2 = wb.create_sheet("Detalle")
    ws2.sheet_properties.tabColor = "334155"

    tipos_map = {
        "scan_automatico": "Cobro por informe",
        "pago_unico":      "Pago \u00fanico",
        "suscripcion":     "Suscripci\u00f3n",
        "pack":            "Pack",
        "trial":           "Trial",
    }
    estados_map = {
        "approved": "Aprobado",
        "rejected": "Rechazado",
        "pending":  "Pendiente",
        "error":    "Error",
    }
    origenes_map = {"mercadopago": "MercadoPago", "transbank": "Transbank"}

    headers = [
        "Fecha", "Hora", "Descripci\u00f3n", "Tipo", "Estado",
        "Monto CLP", "Rut Paciente", "V\u00eda", "ID Transacci\u00f3n",
    ]
    widths = [14, 10, 36, 20, 14, 16, 16, 14, 38]

    for col_idx, (h, w) in enumerate(zip(headers, widths), 1):
        cell = ws2.cell(row=1, column=col_idx, value=h)
        cell.font      = header_font
        cell.fill      = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
        ws2.column_dimensions[get_column_letter(col_idx)].width = w
    ws2.row_dimensions[1].height = 28

    approved_fill = PatternFill(start_color=GREEN_LIGHT, end_color=GREEN_LIGHT, fill_type="solid")
    rejected_fill = PatternFill(start_color=RED_LIGHT,   end_color=RED_LIGHT,   fill_type="solid")
    zebra_fill    = PatternFill(start_color=ZEBRA_BLUE,  end_color=ZEBRA_BLUE,  fill_type="solid")

    for row_idx, c in enumerate(cobros, 2):
        created = c.get("created_at", "")
        fecha   = created[:10]  if len(created) >= 10 else created
        hora    = created[11:16] if len(created) >= 16 else ""

        is_zebra = (row_idx % 2 == 1)  # alternate rows starting at row 3
        row_bg   = zebra_fill if is_zebra else None

        def _wcell(col, value):
            cell = ws2.cell(row=row_idx, column=col, value=value)
            cell.border = thin_border
            if row_bg:
                cell.fill = row_bg
            return cell

        _wcell(1, fecha)
        _wcell(2, hora)
        _wcell(3, c.get("descripcion", ""))
        _wcell(4, tipos_map.get(c.get("tipo", ""), c.get("tipo", "")))

        estado_cell = _wcell(5, estados_map.get(c.get("estado", ""), c.get("estado", "")))
        estado_cell.alignment = Alignment(horizontal="center")
        if c.get("estado") == "approved":
            estado_cell.fill = approved_fill
            estado_cell.font = Font(name="Calibri", bold=True, color=GREEN)
        elif c.get("estado") == "rejected":
            estado_cell.fill = rejected_fill
            estado_cell.font = Font(name="Calibri", bold=True, color="DC2626")

        monto_cell = _wcell(6, c.get("monto_clp", 0))
        monto_cell.number_format = money_fmt
        monto_cell.font          = Font(name="Calibri", bold=True)
        monto_cell.alignment     = Alignment(horizontal="right")

        rut_cell = _wcell(7, c.get("patient_id") or "")
        rut_cell.font      = Font(name="Calibri", color=GRAY, size=10)
        rut_cell.alignment = Alignment(horizontal="center")

        origen_cell = _wcell(8, origenes_map.get(c.get("origen", ""), c.get("origen", "MP")))
        origen_cell.alignment = Alignment(horizontal="center")

        ref_cell = _wcell(9, c.get("ref_id") or c.get("id", ""))
        ref_cell.font = Font(name="Calibri", color=GRAY, size=9)

    if cobros:
        total_row = len(cobros) + 2
        ws2.cell(row=total_row, column=5, value="TOTAL").font = Font(name="Calibri", bold=True, size=11)
        ws2.cell(row=total_row, column=5).alignment = Alignment(horizontal="right")
        total_cell = ws2.cell(row=total_row, column=6, value=resumen.get("total_gastado", 0))
        total_cell.number_format = money_fmt
        total_cell.font          = Font(name="Calibri", bold=True, color=BLUE, size=12)
        total_cell.alignment     = Alignment(horizontal="right")
        ws2.auto_filter.ref = f"A1:I{len(cobros) + 1}"

    ws2.freeze_panes = "A2"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    filename = f"IMAGIAX_Reporte_{periodo}_{datetime.now().strftime('%Y%m%d')}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ═══════════════════════════════════════════════════════════════════════════════
# HTML PAGES
# ═══════════════════════════════════════════════════════════════════════════════


@router.get("/historial", response_class=HTMLResponse)
async def pagina_historial(request: Request) -> HTMLResponse:
    test_badge = _test_badge_html()
    return templates.TemplateResponse("pagos/historial.html", {
        "request": request,
        "test_badge": test_badge,
        "api_base": settings.IMAGIAX_BASE_URL.rstrip("/"),
        "pagar_url": f"{settings.IMAGIAX_BASE_URL.rstrip('/')}/pagar",
    })


@router.get("/pagar", response_class=HTMLResponse)
async def pagina_pago(
    request: Request,
    sin_creditos: bool = Query(False),
    trial_agotado: bool = Query(False),
    tbk_status: str = Query(""),
) -> HTMLResponse:
    pool = request.app.state.pool

    # Optional auth from cookie (public page)
    _pagar_uid: str | None = None
    _session_token = request.cookies.get("imagiax_session")
    if _session_token:
        try:
            _pagar_user = decode_token(_session_token)
            _pagar_uid = _pagar_user.user_id
        except Exception:
            pass

    estado = await pagos_db.get_estado_creditos(pool, usuario_id=_pagar_uid)
    metodo_actual = await pagos_db.get_metodo_pago(pool, usuario_id=_pagar_uid)
    oneclick_inscripcion = await pagos_db.get_inscripcion_oneclick_any(pool, usuario_id=_pagar_uid)

    tiene_tarjeta = estado.get("tiene_tarjeta", False)
    tiene_oneclick = oneclick_inscripcion is not None
    tiene_mp_card = metodo_actual is not None
    creditos_disp = estado.get("creditos_disponibles", 0)
    tiene_suscripcion = estado.get("tiene_suscripcion", False)
    es_trial = estado.get("es_trial", False)

    # Account status badge
    if creditos_disp > 0 or tiene_tarjeta:
        estado_badge = '<span class="estado-panel-badge activo">Activo</span>'
    elif es_trial and creditos_disp > 0:
        estado_badge = '<span class="estado-panel-badge trial">Trial</span>'
    else:
        estado_badge = '<span class="estado-panel-badge sin-creditos">Sin creditos</span>'

    creditos_color = "verde" if creditos_disp > 10 else ("amarillo" if creditos_disp > 0 else "rojo")
    creditos_tipo = ""
    if es_trial:
        creditos_tipo = f"Trial ({pagos_db.CREDITOS_TRIAL} iniciales)"
    elif tiene_suscripcion:
        creditos_tipo = "Suscripcion mensual"
    elif creditos_disp > 0:
        creditos_tipo = "Pack de creditos"

    # Payment method HTML
    metodo_pago_html = ""
    if tiene_oneclick:
        ct = html_escape((oneclick_inscripcion.get("card_type") or "TARJETA").upper())
        cl4 = html_escape(oneclick_inscripcion.get("card_number_last4") or "****")
        metodo_pago_html = f"""
        <div class="estado-item accent-blue">
          <div class="estado-item-label">Pago automatico</div>
          <div class="estado-item-value azul">{ct} ****{cl4}</div>
          <div class="estado-item-detail">Transbank Oneclick</div>
        </div>"""
    elif tiene_mp_card:
        brand = html_escape((metodo_actual.get("card_brand") or "TARJETA").upper())
        last4 = html_escape(metodo_actual.get("card_last4") or "****")
        metodo_pago_html = f"""
        <div class="estado-item accent-blue">
          <div class="estado-item-label">Pago automatico</div>
          <div class="estado-item-value azul">{brand} ****{last4}</div>
          <div class="estado-item-detail">MercadoPago</div>
        </div>"""
    else:
        metodo_pago_html = """
        <div class="estado-item">
          <div class="estado-item-label">Pago automatico</div>
          <div class="estado-item-value" style="font-size:14px;color:var(--muted)">No configurado</div>
          <div class="estado-item-detail">Registra una tarjeta abajo</div>
        </div>"""

    suscripcion_html = ""
    if tiene_suscripcion:
        suscripcion_html = """
        <div class="estado-item accent-green">
          <div class="estado-item-label">Suscripcion</div>
          <div class="estado-item-value verde">Activa</div>
          <div class="estado-item-detail">300 creditos/mes</div>
          <button class="btn-cancelar-sub" onclick="cancelarSuscripcion()">Cancelar</button>
        </div>"""

    alerta_creditos = ""
    if 0 < creditos_disp <= 5 and not tiene_tarjeta:
        alerta_creditos = f"""
        <div class="estado-alerta">
          <strong>Creditos bajos:</strong> Solo te quedan <strong>{creditos_disp}</strong> informes.
          Recarga pronto para evitar interrupciones.
        </div>"""
    elif creditos_disp == 0 and not tiene_tarjeta:
        alerta_creditos = """
        <div class="estado-alerta critico">
          <strong>Sin creditos:</strong> No puedes generar informes.
          Compra un pack o registra una tarjeta para continuar.
        </div>"""

    prioridad_msg = ""
    if creditos_disp > 0 and tiene_tarjeta:
        prioridad_msg = f"""
        <div class="estado-prioridad">
          <strong>Prioridad de cobro:</strong> Primero se usaran tus <strong>{creditos_disp} creditos</strong> disponibles.
          Cuando se agoten, se cobrara automaticamente <strong>$990 CLP</strong> por informe a tu tarjeta registrada.
        </div>"""
    elif creditos_disp > 0:
        prioridad_msg = f"""
        <div class="estado-prioridad">
          <strong>Informacion:</strong> Tienes <strong>{creditos_disp} creditos</strong> disponibles.
          Cuando se agoten, necesitaras comprar mas creditos o registrar una tarjeta para cobro automatico.
        </div>"""
    elif tiene_tarjeta:
        prioridad_msg = """
        <div class="estado-prioridad">
          <strong>Cobro automatico:</strong> Se cobrara <strong>$990 CLP</strong> por cada informe generado.
          Puedes comprar un pack o suscripcion para obtener mejor precio por informe.
        </div>"""

    estado_panel = f"""
    <div class="estado-panel">
      <div class="estado-panel-header">
        <div class="estado-panel-title">Resumen de cuenta</div>
        {estado_badge}
      </div>
      {alerta_creditos}
      <div class="estado-grid">
        <div class="estado-item">
          <div class="estado-item-label">Creditos disponibles</div>
          <div class="estado-item-value {creditos_color}">{creditos_disp}</div>
          <div class="estado-item-detail">{creditos_tipo}</div>
        </div>
        {suscripcion_html}
        {metodo_pago_html}
      </div>
      {prioridad_msg}
    </div>"""

    banner = ""
    if not tiene_tarjeta and es_trial and creditos_disp == 0:
        banner = """
        <div class="banner-trial">
          <div>
            <strong>Periodo de prueba agotado</strong>
            <div style="font-size:13px;margin-top:2px;opacity:.85;">
              Has utilizado tus 5 informes gratuitos. Elige un plan o registra una tarjeta para continuar.
            </div>
          </div>
        </div>"""

    planes_html = "\n".join(
        _plan_card_html(pid, p, tiene_suscripcion) for pid, p in service.PLANES.items()
    )

    tarjeta_html = ""
    if metodo_actual:
        brand = html_escape((metodo_actual["card_brand"] or "TARJETA").upper())
        t_last4 = html_escape(metodo_actual["card_last4"] or "****")
        t_email = html_escape(metodo_actual["email"] or "")
        tarjeta_html = f"""
        <div class="card-registered-badge">
          <div>
            <div class="card-badge-brand">{brand} ****{t_last4}</div>
            <div class="card-badge-detail">{t_email} · Cobro automático activo (MercadoPago)</div>
          </div>
          <button class="btn-borrar-tarjeta" onclick="borrarTarjeta()" title="Eliminar tarjeta">Eliminar</button>
        </div>"""

    oneclick_html = ""
    if oneclick_inscripcion:
        ct = html_escape((oneclick_inscripcion.get("card_type") or "TARJETA").upper())
        cl4 = html_escape(oneclick_inscripcion.get("card_number_last4") or "****")
        oneclick_html = f"""
        <div class="card-registered-badge oneclick-badge">
          <div>
            <div class="card-badge-brand">{ct} ****{cl4}</div>
            <div class="card-badge-detail">Cobro automático 1-Click activo (Transbank Oneclick)</div>
          </div>
          <button class="btn-borrar-tarjeta" onclick="borrarOneclick()" title="Desvincular tarjeta">Desvincular</button>
        </div>"""

    test_badge = _test_badge_html()

    return templates.TemplateResponse("pagos/pagar.html", {
        "request": request,
        "test_badge": test_badge,
        "viewer_url": settings.IMAGIAX_VIEWER_URL,
        "banner": banner,
        "estado_panel": estado_panel,
        "planes": planes_html,
        "tarjeta_registrada": tarjeta_html,
        "oneclick_registrada": oneclick_html,
        "oneclick_form_display": "display:none" if oneclick_inscripcion else "",
        "api_base": settings.IMAGIAX_BASE_URL.rstrip("/"),
        "mp_public_key": settings.MP_PUBLIC_KEY,
    })
