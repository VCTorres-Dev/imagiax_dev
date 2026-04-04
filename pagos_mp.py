"""
IMAGIAX — Router de pagos con MercadoPago

Endpoints:
  GET  /pagar                          → Página de planes y precios + registro de tarjeta (HTML)
  POST /pagos/registrar-tarjeta        → Registra tarjeta para cobros automáticos por scan
  GET  /pagos/metodo-pago              → Consulta tarjeta registrada (last4, brand)
  DELETE /pagos/metodo-pago            → Elimina tarjeta guardada
  POST /pagos/crear-sesion             → Crea sesión de pago en MP (preference o preapproval)
  GET  /pagos/exito                    → Callback éxito: verifica pago y acredita créditos
  GET  /pagos/pendiente                → Callback pago pendiente
  GET  /pagos/fallo                    → Callback pago fallido
  POST /pagos/webhook                  → Notificaciones IPN de MercadoPago (producción)
  GET  /pagos/estado                   → Estado de créditos de esta instalación
  GET  /pagos/admin/resumen            → Resumen administrativo (sin auth en testing)

Modelos de monetización:
  1. Trial gratuito: 5 informes incluidos al instalar
  2. Packs de créditos: pago único por lote de informes (Checkout Pro)
  3. Suscripción mensual: cuota fija + renovación automática (Preapproval)
  4. Pago por scan: registra tarjeta UNA VEZ → cobro automático de $990 CLP por informe
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse, quote as _url_quote

import uuid as _uuid_mod

import httpx
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

import imagiax_pagos_db
from imagiax_auth import get_current_user, require_role, UserPayload

load_dotenv()

logger = logging.getLogger("imagiax.pagos")

# ── Configuración ──────────────────────────────────────────────────────────────
MP_ACCESS_TOKEN: str = os.getenv("MP_ACCESS_TOKEN", "")
MP_PUBLIC_KEY:   str = os.getenv("MP_PUBLIC_KEY", "")
IMAGIAX_BASE_URL: str = os.getenv("IMAGIAX_BASE_URL", "http://localhost:8010")
# URL del visor OHIF. Local: http://localhost:3000 | Cloud: / (relativo, nginx enruta a OHIF)
_VIEWER_URL: str = os.getenv("IMAGIAX_VIEWER_URL", "http://localhost:3000")

# SEC-PAY-006: Use explicit IMAGIAX_PAYMENT_MODE env var instead of token prefix (AD-6)
IMAGIAX_PAYMENT_MODE: str = os.getenv("IMAGIAX_PAYMENT_MODE", "testing")
_IS_TEST = (IMAGIAX_PAYMENT_MODE != "production")

# Startup cross-validation: production APP_ENV must use production payment mode
if os.getenv("APP_ENV") == "production" and IMAGIAX_PAYMENT_MODE != "production":
    raise RuntimeError(
        "IMAGIAX_PAYMENT_MODE must be 'production' when APP_ENV=production. "
        "Set IMAGIAX_PAYMENT_MODE=production in .env.production."
    )

# Secret para validar webhooks de MercadoPago (X-Signature header).
# Obtener en: Panel MP → Webhooks → tu webhook → Secret key
MP_WEBHOOK_SECRET: str = os.getenv("MP_WEBHOOK_SECRET", "")

# URL pública para callbacks de MercadoPago.
# En producción = IMAGIAX_BASE_URL (tu dominio).
# En testing local = tunelizar con ngrok y setear MP_CALLBACK_BASE_URL,
#   o dejar el fallback público (no recibe webhook, pero el checkout sí crea).
# Ej: export MP_CALLBACK_BASE_URL=https://xxxx.ngrok-free.app
_CALLBACK_BASE_URL: str = os.getenv(
    "MP_CALLBACK_BASE_URL",
    IMAGIAX_BASE_URL if not IMAGIAX_BASE_URL.startswith("http://localhost")
    else "https://imagiax.cl"   # placeholder para testing local (no recibe webhooks)
).rstrip("/")

# Precio por scan en cobro automático (tarjeta guardada)
PRECIO_SCAN_CLP: int = int(os.getenv("IMAGIAX_PRECIO_SCAN", "990"))

# ── Planes disponibles ─────────────────────────────────────────────────────────
PLANES: dict[str, dict[str, Any]] = {
    "pack_individual": {
        "titulo": "Escaneo Individual",
        "descripcion": "1 informe radiológico generado por IA.",
        "precio_clp": 990,
        "creditos": 1,
        "recurrente": False,
        "badge": None,
        "duracion_dias": None,
    },
    "pack_50": {
        "titulo": "Pack 50 Informes",
        "descripcion": "50 informes sin vencimiento. Ideal para clínicas pequeñas.",
        "precio_clp": 24990,
        "creditos": 50,
        "recurrente": False,
        "badge": None,
        "duracion_dias": None,
    },
    "pack_200": {
        "titulo": "Pack 200 Informes",
        "descripcion": "200 informes sin vencimiento. Ahorro del 38% vs individual.",
        "precio_clp": 69990,
        "creditos": 200,
        "recurrente": False,
        "badge": "Más popular",
        "duracion_dias": None,
    },
    "suscripcion_mensual": {
        "titulo": "Suscripción Mensual",
        "descripcion": "300 informes/mes. Créditos se renuevan automáticamente cada 30 días.",
        "precio_clp": 79990,
        "creditos": 300,
        "recurrente": True,
        "badge": "Mejor valor",
        "duracion_dias": 31,
    },
}

router = APIRouter(tags=["Pagos MercadoPago"])


# ── Helpers comunes ────────────────────────────────────────────────────────────

def _parse_external_reference(raw_value: str) -> dict[str, Any]:
    """
    Parsea external_reference de MP (JSON string) de forma segura.
    SEC-PAY-010: Validates that required fields (usuario_id, paquete) are present.
    Logs and returns empty dict on malformed input.
    """
    if not raw_value:
        return {}
    try:
        data = json.loads(raw_value) if raw_value.startswith("{") else {}
    except (json.JSONDecodeError, AttributeError):
        logger.warning("external_reference malformado (no es JSON válido): %r", raw_value[:200])
        return {}
    # SEC-PAY-010: Strict validation — must contain usuario_id
    if not isinstance(data, dict):
        logger.warning("external_reference no es un objeto JSON: %r", raw_value[:200])
        return {}
    if not data.get("usuario_id"):
        logger.warning(
            "external_reference sin usuario_id — posible manipulación: %r", raw_value[:200]
        )
    return data


def _validar_webhook_signature(request: Request, body_bytes: bytes) -> bool:
    """
    Valida la firma X-Signature de los webhooks de MercadoPago.
    Retorna True si es válida, False si no.
    En modo TEST sin secret configurado, se acepta siempre (con warning).
    Docs: https://www.mercadopago.com.ar/developers/es/docs/your-integrations/notifications/webhooks
    """
    if not MP_WEBHOOK_SECRET:
        # SEC-PAY-001: Siempre rechazar si no hay secret — sin excepcion para _IS_TEST
        # MP sandbox soporta secrets reales; no hay razon para bypassear en test
        logger.error("MP_WEBHOOK_SECRET no configurado — webhook rechazado por seguridad.")
        return False

    x_signature = request.headers.get("x-signature", "")
    x_request_id = request.headers.get("x-request-id", "")

    if not x_signature:
        logger.warning("Webhook sin header X-Signature — rechazado.")
        return False

    # Parsear ts y v1 del header: "ts=xxxx,v1=yyyy"
    parts = {k.strip(): v.strip() for k, v in
             (p.split("=", 1) for p in x_signature.split(",") if "=" in p)}
    ts = parts.get("ts", "")
    v1_received = parts.get("v1", "")

    if not ts or not v1_received:
        logger.warning("Webhook X-Signature mal formateado: %s", x_signature)
        return False

    # data.id viene del query param o del body
    data_id = request.query_params.get("data.id", "")

    # Construir manifest según docs de MP
    manifest = f"id:{data_id};request-id:{x_request_id};ts:{ts};"
    expected = hmac.new(
        MP_WEBHOOK_SECRET.encode(),
        manifest.encode(),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, v1_received):
        logger.warning("Webhook firma inválida. Esperada: %s..., Recibida: %s...",
                       expected[:12], v1_received[:12])
        return False

    return True


# ── Helpers MercadoPago ────────────────────────────────────────────────────────

def _mp_headers() -> dict[str, str]:
    if not MP_ACCESS_TOKEN:
        raise HTTPException(500, "MP_ACCESS_TOKEN no configurado en .env")
    return {
        "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
        "X-Idempotency-Key": "",  # Se sobrescribe por llamada
    }


async def _mp_get(path: str) -> dict[str, Any] | list:
    """GET a la API de MercadoPago."""
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            f"https://api.mercadopago.com{path}",
            headers={
                "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
                "Content-Type": "application/json",
            },
        )
    if resp.status_code not in (200, 201):
        logger.error("MP GET %s → %d: %s", path, resp.status_code, resp.text[:300])
        raise HTTPException(502, f"MercadoPago error {resp.status_code}")
    return resp.json()


async def _mp_post(path: str, body: dict, idempotency_key: str = "") -> dict[str, Any]:
    """POST a la API de MercadoPago."""
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            f"https://api.mercadopago.com{path}",
            json=body,
            headers={
                "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
                "Content-Type": "application/json",
                "X-Idempotency-Key": idempotency_key,
            },
        )
    if resp.status_code not in (200, 201):
        logger.error("MP POST %s → %d: %s", path, resp.status_code, resp.text[:300])
        raise HTTPException(502, f"MercadoPago error {resp.status_code}: {resp.text[:200]}")
    return resp.json()


async def _mp_put(path: str, body: dict) -> dict[str, Any]:
    """PUT a la API de MercadoPago (usado para actualizar suscripciones)."""
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.put(
            f"https://api.mercadopago.com{path}",
            json=body,
            headers={
                "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
                "Content-Type": "application/json",
            },
        )
    if resp.status_code not in (200, 201):
        logger.error("MP PUT %s → %d: %s", path, resp.status_code, resp.text[:300])
        raise HTTPException(502, f"MercadoPago error {resp.status_code}: {resp.text[:200]}")
    return resp.json()


async def cancelar_preapproval_mp(preapproval_id: str) -> tuple[bool, str]:
    """
    Cancela una suscripción (preapproval) en MercadoPago.
    
    Según la API de MP: PUT /preapproval/{id} con status: "cancelled"
    
    Retorna (exito: bool, mensaje: str).
    Esta función NO lanza excepciones — siempre retorna un resultado.
    """
    if not preapproval_id:
        return False, "No hay preapproval_id para cancelar."
    
    # En modo TEST, simular cancelación
    if _IS_TEST:
        logger.info("MODO TEST: simulando cancelación de preapproval %s", preapproval_id)
        return True, f"Suscripción {preapproval_id} cancelada (modo test)."
    
    try:
        result = await _mp_put(f"/preapproval/{preapproval_id}", {"status": "cancelled"})
        new_status = result.get("status", "")
        if new_status == "cancelled":
            logger.info("Preapproval %s cancelado exitosamente en MercadoPago.", preapproval_id)
            return True, f"Suscripción cancelada en MercadoPago (status: {new_status})."
        else:
            logger.warning("Preapproval %s — status inesperado: %s", preapproval_id, new_status)
            return True, f"MercadoPago respondió con status: {new_status}"
    except HTTPException as he:
        detail = str(he.detail)
        detail_lower = detail.lower()
        # Idempotencia funcional: si ya estaba cancelada, tratar como éxito.
        if he.status_code == 502 and ("already cancelled" in detail_lower or "cancelled" in detail_lower):
            logger.info("Preapproval %s ya estaba cancelado en MercadoPago.", preapproval_id)
            return True, "Suscripción ya estaba cancelada en MercadoPago."
        logger.error("Error HTTP al cancelar preapproval %s: %s", preapproval_id, he.detail)
        return False, f"Error al cancelar en MercadoPago: {he.detail}"
    except Exception as exc:
        logger.error("Error inesperado al cancelar preapproval %s: %s", preapproval_id, exc)
        return False, f"Error inesperado: {exc}"


# ── Schemas de request ─────────────────────────────────────────────────────────

class CrearSesionRequest(BaseModel):
    plan_id: str = Field(..., description="ID del plan: pack_individual | pack_10 | pack_50 | pack_200 | suscripcion_mensual")
    email: str = Field(..., description="Email del comprador")
    centro_medico: str = Field(default="Centro Médico", description="Nombre del centro médico")
    nombre_contacto: str | None = Field(default=None, description="Nombre del contacto")


class RegistrarTarjetaRequest(BaseModel):
    card_token_id: str = Field(..., description="Token temporal de tarjeta obtenido con MP SDK")
    email: str = Field(..., description="Email del médico/centro")
    centro_medico: str = Field(default="Centro Médico")
    acepta_cobros_automaticos: bool = Field(
        ...,
        description="El usuario autoriza explícitamente el cobro automático de $990 CLP por informe"
    )


# ── Función pública: cobro automático por scan ─────────────────────────────────

async def cobrar_scan_automatico(
    *,
    mp_customer_id: str,
    mp_card_id: str,
    mp_card_brand: str,
    descripcion: str = "IMAGIAX — Informe radiológico IA",
) -> tuple[bool, str | None, str | None]:
    """
    Cobra PRECIO_SCAN_CLP al customer usando su tarjeta guardada.
    Llamado desde el endpoint /generar/informe tras un análisis exitoso.

    Retorna (aprobado: bool, payment_id: str | None, error_msg: str | None).
    NUNCA lanza excepción — el análisis médico siempre se entrega al doctor
    independientemente del resultado del cobro.
    """
    try:
        # En entorno TEST de MercadoPago, las tarjetas guardadas no soportan
        # cobros sin CVV. Simulamos el pago aprobado para no bloquear el desarrollo.
        if _IS_TEST:
            logger.info("MODO TEST: simulando cobro aprobado de $%d CLP (card_id=%s)",
                        PRECIO_SCAN_CLP, mp_card_id)
            simulated_id = f"test-{_uuid_mod.uuid4()}"
            try:
                await imagiax_pagos_db.registrar_pago(
                    cliente_id=None,
                    licencia_id=None,
                    mp_payment_id=simulated_id,
                    mp_preference_id=None,
                    mp_preapproval_id=None,
                    tipo="scan_automatico",
                    estado="approved",
                    monto_clp=PRECIO_SCAN_CLP,
                    descripcion=descripcion,
                    raw_webhook={"test_mode": True, "simulated": True},
                )
            except Exception as log_exc:
                logger.warning("No se pudo registrar cobro test en DB: %s", log_exc)
            return True, simulated_id, None

        # ── Flujo PRODUCCION: cobro real con card_token ──────────────────────
        # Verificar que la tarjeta aun existe en MercadoPago
        try:
            cards_list = await _mp_get(f"/v1/customers/{mp_customer_id}/cards")
            card_ids_in_mp = [str(c.get("id", "")) for c in cards_list] if isinstance(cards_list, list) else []
            if mp_card_id not in card_ids_in_mp:
                logger.error("card_id %s NO existe en customer %s de MP", mp_card_id, mp_customer_id)
                await imagiax_pagos_db.borrar_metodo_pago()
                return False, None, (
                    "La tarjeta registrada ya no es valida en MercadoPago. "
                    "Fue eliminada automaticamente. Por favor, registra una nueva tarjeta."
                )
        except Exception as verify_exc:
            logger.warning("No se pudo verificar tarjeta en MP: %s", verify_exc)

        # Crear card_token temporal desde la tarjeta guardada
        token_result = await _mp_post(
            "/v1/card_tokens",
            {"card_id": mp_card_id, "customer_id": mp_customer_id},
            idempotency_key=str(_uuid_mod.uuid4()),
        )
        card_token = str(token_result["id"])

        # Crear pago con el token temporal
        body: dict[str, Any] = {
            "transaction_amount": PRECIO_SCAN_CLP,
            "token": card_token,
            "description": descripcion,
            "installments": 1,
            "payer": {
                "type": "customer",
                "id": mp_customer_id,
            },
        }
        # payment_method_id es requerido; usar brand si está disponible
        if mp_card_brand:
            body["payment_method_id"] = mp_card_brand

        result = await _mp_post(
            "/v1/payments",
            body,
            idempotency_key=str(_uuid_mod.uuid4()),
        )
        status: str = result.get("status", "unknown")
        payment_id: str = str(result.get("id", ""))

        # Registrar en log de pagos (no bloquea)
        try:
            await imagiax_pagos_db.registrar_pago(
                cliente_id=None,
                licencia_id=None,
                mp_payment_id=payment_id,
                mp_preference_id=None,
                mp_preapproval_id=None,
                tipo="scan_automatico",
                estado=status,
                monto_clp=PRECIO_SCAN_CLP,
                descripcion=descripcion,
                raw_webhook=result,
            )
        except Exception as log_exc:
            logger.warning("No se pudo registrar cobro scan en DB: %s", log_exc)

        aprobado = status == "approved"
        error_msg = None if aprobado else f"MP status: {status} — {result.get('status_detail', '')}"
        if not aprobado:
            logger.warning("Cobro automático rechazado: payment_id=%s, status=%s, detail=%s",
                           payment_id, status, result.get("status_detail"))
        return aprobado, payment_id, error_msg

    except Exception as exc:
        logger.error("Error en cobro automatico de scan: %s", exc)
        # Registrar intento fallido en DB para que aparezca en historial
        try:
            await imagiax_pagos_db.registrar_pago(
                cliente_id=None,
                licencia_id=None,
                mp_payment_id=f"error-{_uuid_mod.uuid4()}",
                mp_preference_id=None,
                mp_preapproval_id=None,
                tipo="scan_automatico",
                estado="rejected",
                monto_clp=PRECIO_SCAN_CLP,
                descripcion=descripcion,
                raw_webhook={"error": str(exc)},
            )
        except Exception as log_exc:
            logger.warning("No se pudo registrar cobro fallido en DB: %s", log_exc)
        return False, None, str(exc)


# ── Endpoint: Estado de créditos ───────────────────────────────────────────────

@router.get("/pagos/estado")
async def estado_creditos(user: UserPayload = Depends(get_current_user)) -> JSONResponse:
    """
    Retorna el estado de créditos de esta instalación IMAGIAX.
    Usado por el panel OHIF para mostrar el contador y activar el CTA de pago.
    """
    estado = await imagiax_pagos_db.get_estado_creditos(usuario_id=user.user_id)
    installation_id = await imagiax_pagos_db.get_or_create_installation_id()
    return JSONResponse({
        "installation_id": installation_id,
        "creditos_disponibles": estado["creditos_disponibles"],
        "creditos_total": estado["creditos_total"],
        "es_trial": estado["es_trial"],
        "tiene_suscripcion": estado["tiene_suscripcion"],
        "tiene_tarjeta": estado["tiene_tarjeta"],
        "card_last4": estado["card_last4"],
        "activa": estado["activa"],
        "pagar_url": f"{IMAGIAX_BASE_URL}/pagar",
        "mensaje": (
            f"Período de prueba — {estado['creditos_disponibles']} informes gratuitos restantes"
            if estado["es_trial"]
            else f"Suscripción activa — {estado['creditos_disponibles']} créditos disponibles"
            if estado["tiene_suscripcion"]
            else f"{estado['creditos_disponibles']} créditos disponibles"
        ),
    })


# ── Endpoint: Registrar tarjeta para cobros automáticos ───────────────────────

@router.post("/pagos/registrar-tarjeta")
async def registrar_tarjeta(body: RegistrarTarjetaRequest, user: UserPayload = Depends(get_current_user)) -> JSONResponse:
    """
    Registra una tarjeta de crédito/débito para cobros automáticos de $990 CLP por scan.

    Flujo:
      1. Recibe card_token_id temporal (generado por MP SDK en el frontend)
      2. Busca o crea un Customer en MP con el email del médico
      3. Asocia el token al customer → obtiene card_id permanente
      4. Persiste customer_id + card_id en tabla configuracion
    """
    if not body.acepta_cobros_automaticos:
        raise HTTPException(400, "Debe aceptar la autorización de cobros automáticos para continuar.")

    # 1. Buscar o crear Customer en MercadoPago
    try:
        search = await _mp_get(f"/v1/customers/search?email={_url_quote(body.email)}")
        results = search.get("results", [])
        if results:
            mp_customer_id = str(results[0]["id"])
            logger.info("Customer MP existente: %s", mp_customer_id)
        else:
            customer = await _mp_post("/v1/customers", {"email": body.email})
            mp_customer_id = str(customer["id"])
            logger.info("Customer MP creado: %s", mp_customer_id)
    except HTTPException:
        raise

    # 2. Asociar tarjeta al customer
    try:
        card_data = await _mp_post(
            f"/v1/customers/{mp_customer_id}/cards",
            {"token": body.card_token_id},
            idempotency_key=str(_uuid_mod.uuid4()),
        )
    except HTTPException as exc:
        raise HTTPException(502, f"No se pudo guardar la tarjeta en MercadoPago: {exc.detail}") from exc

    mp_card_id  = str(card_data["id"])
    card_last4  = str(card_data.get("last_four_digits", "****"))
    card_brand  = (card_data.get("payment_method") or {}).get("id", "") or card_data.get("payment_method_id", "")

    # 3. Guardar en configuracion local
    await imagiax_pagos_db.guardar_metodo_pago(
        mp_customer_id=mp_customer_id,
        mp_card_id=mp_card_id,
        card_last4=card_last4,
        card_brand=card_brand,
        email=body.email,
        usuario_id=user.user_id,
    )

    # 4. Crear/actualizar cliente en tabla clientes (para facturación futura)
    await imagiax_pagos_db.crear_o_buscar_cliente(
        email=body.email,
        centro_medico=body.centro_medico,
    )

    logger.info("Tarjeta registrada: customer=%s, card=%s, last4=%s", mp_customer_id, mp_card_id, card_last4)
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
async def get_metodo_pago_endpoint(user: UserPayload = Depends(get_current_user)) -> JSONResponse:
    """Retorna la tarjeta registrada para cobros automáticos (Oneclick o MP)."""
    # Prioridad: Oneclick > MP
    oneclick = await imagiax_pagos_db.get_inscripcion_oneclick_any(usuario_id=user.user_id)
    if oneclick:
        return JSONResponse({
            "tiene_tarjeta": True,
            "metodo": "oneclick",
            "card_last4": oneclick.get("card_number_last4", "****"),
            "card_brand": oneclick.get("card_type", ""),
            "email": oneclick.get("email", ""),
        })

    metodo = await imagiax_pagos_db.get_metodo_pago(usuario_id=user.user_id)
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
async def borrar_metodo_pago_endpoint(user: UserPayload = Depends(get_current_user)) -> JSONResponse:
    """Elimina la tarjeta guardada localmente y de MercadoPago."""
    # Intentar borrar de MP primero (best-effort, no bloquea)
    metodo = await imagiax_pagos_db.get_metodo_pago(usuario_id=user.user_id)
    if metodo and metodo.get("mp_customer_id") and metodo.get("mp_card_id"):
        try:
            await _mp_get(
                f"/v1/customers/{metodo['mp_customer_id']}/cards/{metodo['mp_card_id']}",
            )
            # Si existe, intentar borrar vía API
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.delete(
                    f"https://api.mercadopago.com/v1/customers/{metodo['mp_customer_id']}/cards/{metodo['mp_card_id']}",
                    headers=_mp_headers(),
                )
                if resp.status_code < 300:
                    logger.info("Tarjeta %s eliminada de MercadoPago.", metodo["mp_card_id"])
                else:
                    logger.warning("No se pudo borrar tarjeta de MP (status %d).", resp.status_code)
        except Exception as exc:
            logger.warning("Error al borrar tarjeta de MP (continuando): %s", exc)

    await imagiax_pagos_db.borrar_metodo_pago(usuario_id=user.user_id)
    return JSONResponse({"ok": True, "mensaje": "Método de pago eliminado correctamente."})


@router.post("/pagos/cancelar-suscripcion")
async def cancelar_suscripcion_endpoint(user: UserPayload = Depends(get_current_user)) -> JSONResponse:
    """
    Cancela la suscripción activa del usuario.
    
    Flujo:
    1. Obtener preapproval_id de la DB
    2. Cancelar en MercadoPago (PUT /preapproval/{id} status:cancelled)
    3. Desactivar en la base de datos local
    
    La cancelación en MercadoPago evita que sigan llegando cobros recurrentes.
    Los créditos restantes siguen disponibles hasta agotarse.
    """
    # Paso 1: Buscar suscripción activa sin modificar estado local
    suscripcion = await imagiax_pagos_db.get_suscripcion_activa(usuario_id=user.user_id)
    if not suscripcion:
        raise HTTPException(400, "No hay suscripción activa para cancelar.")

    preapproval_id = suscripcion.get("mp_preapproval_id")

    # Paso 2: Cancelar en MercadoPago primero (si corresponde)
    if preapproval_id:
        mp_exito, mp_mensaje = await cancelar_preapproval_mp(preapproval_id)
        if not mp_exito:
            raise HTTPException(
                502,
                f"No se pudo cancelar en MercadoPago: {mp_mensaje}. La suscripción sigue activa.",
            )
        mensaje_mp = f" {mp_mensaje}"
    else:
        mensaje_mp = " No existe preapproval_id asociado; se desactivará localmente."

    # Paso 3: Solo si MP está cancelado (o no aplica), desactivar localmente
    licencia_id = str(suscripcion.get("id", ""))
    exito_db, mensaje_db = await imagiax_pagos_db.desactivar_licencia(licencia_id)
    if not exito_db:
        raise HTTPException(500, mensaje_db)

    mensaje_final = (
        "Suscripcion cancelada exitosamente. "
        "Los creditos restantes siguen disponibles hasta agotarse."
        f"{mensaje_mp}"
    )

    return JSONResponse(
        {
            "ok": True,
            "mensaje": mensaje_final,
            "cancelado_en_mp": bool(preapproval_id),
        }
    )


# ── Endpoint: Crear sesión de pago ─────────────────────────────────────────────

@router.post("/pagos/crear-sesion")
async def crear_sesion_pago(body: CrearSesionRequest, user: UserPayload = Depends(get_current_user)) -> JSONResponse:
    """
    Crea una sesión de pago en MercadoPago y retorna la URL de checkout.
    Para packs: Checkout Pro (preference).
    Para suscripción mensual: Preapproval (cobro recurrente).
    """
    plan = PLANES.get(body.plan_id)
    if not plan:
        raise HTTPException(400, f"Plan '{body.plan_id}' no existe. Opciones: {list(PLANES)}")

    # Validar que no haya suscripción activa si intenta comprar otra
    if plan["recurrente"]:
        estado = await imagiax_pagos_db.get_estado_creditos(usuario_id=user.user_id)
        if estado.get("tiene_suscripcion"):
            raise HTTPException(
                400,
                "Ya tienes una suscripcion activa. Cancela la actual antes de adquirir otra, "
                "o espera a que expire. Puedes comprar packs adicionales si necesitas mas creditos."
            )

    # Crear o encontrar cliente
    cliente = await imagiax_pagos_db.crear_o_buscar_cliente(
        email=body.email,
        centro_medico=body.centro_medico,
        nombre_contacto=body.nombre_contacto,
    )

    external_ref = json.dumps({
        "cliente_id": cliente["id"],
        "plan_id": body.plan_id,
        "email": body.email,
        "centro_medico": body.centro_medico,
        "usuario_id": user.user_id,
    }, ensure_ascii=False)

    if plan["recurrente"]:
        checkout_url, preapproval_id = await _crear_preapproval(plan, body.email, external_ref)
        # Registrar pago pendiente en DB
        await imagiax_pagos_db.registrar_pago(
            cliente_id=cliente["id"],
            licencia_id=None,
            mp_payment_id=None,
            mp_preference_id=None,
            mp_preapproval_id=preapproval_id,
            tipo="suscripcion",
            estado="pending",
            monto_clp=plan["precio_clp"],
            descripcion=plan["titulo"],
        )
        return JSONResponse({
            "ok": True,
            "tipo": "suscripcion",
            "checkout_url": checkout_url,
            "preapproval_id": preapproval_id,
        })
    else:
        checkout_url, preference_id = await _crear_preference(plan, body.email, external_ref)
        # Registrar pago pendiente en DB
        await imagiax_pagos_db.registrar_pago(
            cliente_id=cliente["id"],
            licencia_id=None,
            mp_payment_id=None,
            mp_preference_id=preference_id,
            mp_preapproval_id=None,
            tipo="pago_unico",
            estado="pending",
            monto_clp=plan["precio_clp"],
            descripcion=plan["titulo"],
        )
        return JSONResponse({
            "ok": True,
            "tipo": "pago_unico",
            "checkout_url": checkout_url,
            "preference_id": preference_id,
        })


async def _crear_preference(
    plan: dict, payer_email: str, external_ref: str
) -> tuple[str, str]:
    """Crea una preferencia Checkout Pro. Retorna (checkout_url, preference_id)."""
    base_url = IMAGIAX_BASE_URL.rstrip("/")
    parsed = urlparse(base_url)
    is_local_base = parsed.hostname in {"localhost", "127.0.0.1", "::1"}

    body = {
        "items": [{
            "id": plan.get("titulo", "pack").lower().replace(" ", "_"),
            "title": f"IMAGIAX — {plan['titulo']}",
            "description": plan["descripcion"],
            "quantity": 1,
            "unit_price": plan["precio_clp"],
            "currency_id": "CLP",
        }],
        "back_urls": {
            "success": f"{base_url}/pagos/exito",
            "pending": f"{base_url}/pagos/pendiente",
            "failure": f"{base_url}/pagos/fallo",
        },
        "notification_url": f"{base_url}/pagos/webhook",
        "statement_descriptor": "IMAGIAX AI",
        "external_reference": external_ref,
        "metadata": {"imagiax_plan": plan.get("titulo", "")},
    }
    # En testing, no forzar payer.email evita bloqueos por cuenta compradora inválida/mal tipeada.
    if not _IS_TEST:
        body["payer"] = {"email": payer_email}
    # auto_return=approved solo si HTTPS con dominio real.
    # MP rechaza auto_return con HTTP puro o IPs (requiere HTTPS).
    # Local (localhost): no se activa. HTTP+IP (demo): no se activa. HTTPS: sí.
    if not is_local_base and base_url.startswith("https://"):
        body["auto_return"] = "approved"

    result = await _mp_post("/checkout/preferences", body)
    preference_id = result["id"]
    # Usar sandbox en test, init_point en producción
    checkout_url = result["sandbox_init_point"] if _IS_TEST else result["init_point"]
    return checkout_url, preference_id


async def _crear_preapproval(
    plan: dict, payer_email: str, external_ref: str
) -> tuple[str, str]:
    """
    Crea un preapproval (suscripción recurrente) en MercadoPago.
    Retorna (checkout_url, preapproval_id).
    NOTA: MP siempre exige back_url aunque sea testing. Se usa _CALLBACK_BASE_URL
    que en local apunta a imagiax.cl (placeholder); en producción apunta al dominio real.
    """
    body = {
        "reason": f"IMAGIAX — {plan['titulo']}",
        "external_reference": external_ref,
        "payer_email": payer_email,
        "auto_recurring": {
            "frequency": 1,
            "frequency_type": "months",
            "transaction_amount": plan["precio_clp"],
            "currency_id": "CLP",
        },
        "back_url": f"{_CALLBACK_BASE_URL}/pagos/suscripcion/exito",
        "notification_url": f"{_CALLBACK_BASE_URL}/pagos/webhook",
        "status": "pending",
    }

    result = await _mp_post("/preapproval", body)
    preapproval_id = result["id"]
    checkout_url = result.get("init_point", "")
    if not checkout_url:
        raise HTTPException(502, "MercadoPago no retornó init_point para la suscripción.")
    return checkout_url, preapproval_id


# ── Callbacks de MercadoPago ───────────────────────────────────────────────────

@router.get("/pagos/exito", response_class=HTMLResponse)
async def pago_exitoso(
    request: Request,
    payment_id: str = Query(None),
    collection_id: str = Query(None),
    collection_status: str = Query(None),
    preference_id: str = Query(None),
    status: str = Query(None),
    merchant_order_id: str = Query(None),
) -> HTMLResponse:
    """
    MercadoPago redirige aqui tras un pago aprobado.
    Verificamos el pago con la API de MP y acreditamos los creditos.

    SEC-PAY-002: Soft auth — intenta validar cookie de sesion.
    Si la cookie expiro durante el checkout, igual procesamos usando usuario_id
    de external_reference (MP redirect puede llegar con sesion vencida).
    """
    # Soft auth: intentar obtener usuario desde cookie (SEC-PAY-002)
    import imagiax_auth as _auth_mod
    _session_token = request.cookies.get("imagiax_session")
    if _session_token:
        try:
            _soft_user = _auth_mod.decode_token(_session_token)
        except Exception:
            logger.warning(
                "pagos/exito: cookie de sesion expirada durante redirect de MP — "
                "procesando con usuario_id de external_reference"
            )

    payment_id = payment_id or collection_id
    if not payment_id:
        return HTMLResponse(_page_error("No se recibió un ID de pago válido."))

    try:
        payment_data = await _mp_get(f"/v1/payments/{payment_id}")
    except HTTPException:
        return HTMLResponse(_page_error("No se pudo verificar el pago con MercadoPago."))

    if payment_data.get("status") != "approved":
        estado = payment_data.get("status", "desconocido")
        if estado == "pending":
            return HTMLResponse(_page_pendiente(payment_id))
        return HTMLResponse(_page_error(f"El pago está en estado '{estado}'. Si fue un error, contáctenos."))

    # Pago aprobado — procesar
    ext_ref_raw = payment_data.get("external_reference") or preference_id or ""
    ext_ref = _parse_external_reference(ext_ref_raw)

    plan_id = ext_ref.get("plan_id", "pack_individual")
    cliente_id = ext_ref.get("cliente_id")
    ext_uid = ext_ref.get("usuario_id") or None
    email = ext_ref.get("email", payment_data.get("payer", {}).get("email", ""))
    plan = PLANES.get(plan_id, PLANES["pack_individual"])

    # Acreditar créditos (idempotente — no duplica si ya fue procesado)
    licencia = None
    if cliente_id:
        try:
            licencia = await imagiax_pagos_db.acreditar_pago(
                cliente_id=cliente_id,
                tipo=plan_id,
                creditos=plan["creditos"],
                mp_payment_id=str(payment_id),
                mp_preference_id=preference_id,
                duracion_dias=plan.get("duracion_dias"),
                usuario_id=ext_uid,
            )
            await imagiax_pagos_db.registrar_pago(
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

    estado_actual = await imagiax_pagos_db.get_estado_creditos(usuario_id=ext_uid)
    return HTMLResponse(_page_exito(
        plan=plan,
        creditos_nuevos=plan["creditos"],
        creditos_totales=estado_actual["creditos_disponibles"],
        email=email,
    ))


@router.get("/pagos/suscripcion/exito", response_class=HTMLResponse)
async def suscripcion_exitosa(
    preapproval_id: str = Query(None),
) -> HTMLResponse:
    """Callback para cuando una suscripción es activada."""
    if not preapproval_id:
        return HTMLResponse(_page_error("No se recibió el ID de suscripción."))

    try:
        preapproval_data = await _mp_get(f"/preapproval/{preapproval_id}")
    except HTTPException:
        return HTMLResponse(_page_error("No se pudo verificar la suscripción."))

    suscripcion_status = preapproval_data.get("status", "")
    if suscripcion_status not in ("authorized", "pending"):
        return HTMLResponse(_page_error(f"Estado de suscripción: '{suscripcion_status}'."))

    ext_ref = _parse_external_reference(preapproval_data.get("external_reference", ""))

    plan_id = ext_ref.get("plan_id", "suscripcion_mensual")
    cliente_id = ext_ref.get("cliente_id")
    ext_uid = ext_ref.get("usuario_id") or None
    email = ext_ref.get("email", preapproval_data.get("payer_email", ""))
    plan = PLANES.get(plan_id, PLANES["suscripcion_mensual"])

    if cliente_id and suscripcion_status == "authorized":
        try:
            licencia = await imagiax_pagos_db.acreditar_pago(
                cliente_id=cliente_id,
                tipo=plan_id,
                creditos=plan["creditos"],
                mp_preapproval_id=preapproval_id,
                duracion_dias=plan.get("duracion_dias", 31),
                usuario_id=ext_uid,
            )
            await imagiax_pagos_db.registrar_pago(
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

    estado_actual = await imagiax_pagos_db.get_estado_creditos(usuario_id=ext_uid)
    return HTMLResponse(_page_exito(
        plan=plan,
        creditos_nuevos=plan["creditos"],
        creditos_totales=estado_actual["creditos_disponibles"],
        email=email,
        es_suscripcion=True,
    ))


@router.get("/pagos/pendiente", response_class=HTMLResponse)
async def pago_pendiente(payment_id: str = Query(None)) -> HTMLResponse:
    return HTMLResponse(_page_pendiente(payment_id or ""))


@router.get("/pagos/fallo", response_class=HTMLResponse)
async def pago_fallido() -> HTMLResponse:
    return HTMLResponse(_page_error(
        "El pago no pudo ser procesado. Por favor intente nuevamente o "
        "contacte a su banco si el problema persiste."
    ))


# ── Webhook MercadoPago ────────────────────────────────────────────────────────

@router.post("/pagos/webhook")
async def webhook_mp(request: Request) -> JSONResponse:
    """
    Recibe notificaciones IPN de MercadoPago.
    Valida X-Signature si MP_WEBHOOK_SECRET está configurado.
    Siempre retorna 200 rápidamente para que MP no reintente.
    """
    # Leer body raw para validación de firma
    body_bytes = await request.body()
    try:
        body = json.loads(body_bytes) if body_bytes else {}
    except Exception:
        body = {}

    # Validar firma del webhook (crítico en producción)
    if not _validar_webhook_signature(request, body_bytes):
        logger.warning("Webhook rechazado — firma inválida.")
        return JSONResponse({"ok": False, "detalle": "firma inválida"}, status_code=401)

    notification_type = body.get("type") or request.query_params.get("type", "")
    data_id = (body.get("data") or {}).get("id") or request.query_params.get("data.id", "")

    logger.info("Webhook MP recibido: type=%s, id=%s", notification_type, data_id)

    if not data_id:
        return JSONResponse({"ok": True, "detalle": "sin data.id"})

    try:
        if notification_type == "payment":
            await _procesar_webhook_payment(data_id, body)
        elif notification_type in ("subscription_preapproval", "preapproval"):
            await _procesar_webhook_preapproval(data_id, body)
        elif notification_type == "subscription_authorized_payment":
            await _procesar_webhook_renovacion(data_id, body)
        else:
            logger.debug("Webhook tipo '%s' ignorado.", notification_type)
    except Exception as exc:
        # No relanzar — siempre retornar 200 a MP para evitar reintentos infinitos
        logger.error("Error procesando webhook %s/%s: %s", notification_type, data_id, exc)

    return JSONResponse({"ok": True})


async def _procesar_webhook_payment(payment_id: str, raw: dict) -> None:
    """Procesa una notificación de pago único."""
    # Verificar si ya fue procesado
    existing = await imagiax_pagos_db.get_licencia_por_mp_payment(payment_id)
    if existing:
        logger.debug("Pago %s ya procesado (idempotente).", payment_id)
        return

    payment_data = await _mp_get(f"/v1/payments/{payment_id}")
    if payment_data.get("status") != "approved":
        return

    ext_ref = _parse_external_reference(payment_data.get("external_reference", ""))

    plan_id = ext_ref.get("plan_id", "pack_individual")
    cliente_id = ext_ref.get("cliente_id")
    ext_uid = ext_ref.get("usuario_id") or None
    if not cliente_id:
        logger.warning("Webhook pago %s sin cliente_id en external_reference.", payment_id)
        return

    plan = PLANES.get(plan_id, PLANES["pack_individual"])
    licencia = await imagiax_pagos_db.acreditar_pago(
        cliente_id=cliente_id,
        tipo=plan_id,
        creditos=plan["creditos"],
        mp_payment_id=payment_id,
        duracion_dias=plan.get("duracion_dias"),
        usuario_id=ext_uid,
    )
    await imagiax_pagos_db.registrar_pago(
        cliente_id=cliente_id,
        licencia_id=licencia["id"],
        mp_payment_id=payment_id,
        mp_preference_id=None,
        mp_preapproval_id=None,
        tipo="pago_unico",
        estado="approved",
        monto_clp=int(payment_data.get("transaction_amount", plan["precio_clp"])),
        descripcion=plan["titulo"],
        raw_webhook=raw,
    )
    logger.info("Webhook: pago %s acreditado — %d créditos para cliente %s.",
                payment_id, plan["creditos"], cliente_id)


async def _procesar_webhook_preapproval(preapproval_id: str, raw: dict) -> None:
    """Procesa la activación de una suscripción."""
    preapproval_data = await _mp_get(f"/preapproval/{preapproval_id}")
    if preapproval_data.get("status") != "authorized":
        return

    ext_ref = _parse_external_reference(preapproval_data.get("external_reference", ""))

    plan_id = ext_ref.get("plan_id", "suscripcion_mensual")
    cliente_id = ext_ref.get("cliente_id")
    ext_uid = ext_ref.get("usuario_id") or None
    if not cliente_id:
        return

    plan = PLANES.get(plan_id, PLANES["suscripcion_mensual"])
    licencia = await imagiax_pagos_db.acreditar_pago(
        cliente_id=cliente_id,
        tipo=plan_id,
        creditos=plan["creditos"],
        mp_preapproval_id=preapproval_id,
        duracion_dias=plan.get("duracion_dias", 31),
        usuario_id=ext_uid,
    )
    await imagiax_pagos_db.registrar_pago(
        cliente_id=cliente_id,
        licencia_id=licencia["id"],
        mp_payment_id=None,
        mp_preference_id=None,
        mp_preapproval_id=preapproval_id,
        tipo="suscripcion",
        estado="authorized",
        monto_clp=plan["precio_clp"],
        descripcion=f"Suscripción activada: {plan['titulo']}",
        raw_webhook=raw,
    )
    logger.info("Webhook: suscripción %s autorizada — cliente %s.", preapproval_id, cliente_id)


async def _procesar_webhook_renovacion(payment_id: str, raw: dict) -> None:
    """Procesa el cobro automático de una suscripción activa (renovación mensual)."""
    payment_data = await _mp_get(f"/v1/payments/{payment_id}")
    if payment_data.get("status") != "approved":
        return

    preapproval_id = str(payment_data.get("metadata", {}).get("preapproval_id", ""))
    if not preapproval_id:
        logger.warning("Renovación %s sin preapproval_id en metadata.", payment_id)
        return

    licencia = await imagiax_pagos_db.get_licencia_por_preapproval(preapproval_id)
    plan_id = (licencia or {}).get("tipo", "suscripcion_mensual")
    plan = PLANES.get(plan_id, PLANES["suscripcion_mensual"])
    renovado = await imagiax_pagos_db.renovar_suscripcion(preapproval_id, plan["creditos"])
    if renovado:
        logger.info("Webhook: renovación suscripción %s — %d créditos renovados.",
                    preapproval_id, plan["creditos"])
    else:
        logger.warning("Webhook: no se encontró suscripción activa para %s.", preapproval_id)


# ── Admin ──────────────────────────────────────────────────────────────────────

@router.get("/pagos/admin/resumen")
async def admin_resumen(user: UserPayload = Depends(require_role("admin"))) -> JSONResponse:
    """
    Resumen administrativo: clientes, licencias, pagos e ingresos.
    Sin autenticación en modo testing — proteger con middleware en producción.
    """
    resumen = await imagiax_pagos_db.get_admin_resumen()
    return JSONResponse(resumen)


# ── Verificación manual de pago (para testing local) ─────────────────────────

@router.get("/pagos/verificar/{payment_id}")
async def verificar_pago_manual(payment_id: str, user: UserPayload = Depends(require_role("admin"))) -> JSONResponse:
    """
    Verifica un pago con MercadoPago y acredita créditos si está aprobado.
    Útil para testing local donde los webhooks no llegan.
    El payment_id es el número que aparece en la pantalla de éxito de MP.
    """
    try:
        payment_data = await _mp_get(f"/v1/payments/{payment_id}")
    except HTTPException as exc:
        return JSONResponse({"ok": False, "error": f"No se pudo verificar: {exc.detail}"}, status_code=400)

    status = payment_data.get("status", "")
    if status != "approved":
        return JSONResponse({
            "ok": False,
            "status": status,
            "mensaje": f"El pago tiene estado '{status}', no se acreditaron creditos."
        })

    # Obtener datos de la referencia externa
    ext_ref = _parse_external_reference(payment_data.get("external_reference", ""))
    plan_id = ext_ref.get("plan_id", "pack_individual")
    cliente_id = ext_ref.get("cliente_id")
    ext_uid = ext_ref.get("usuario_id") or None
    plan = PLANES.get(plan_id, PLANES["pack_individual"])

    if not cliente_id:
        return JSONResponse({
            "ok": False,
            "error": "No se encontro cliente_id en el pago. Verifique la referencia externa."
        }, status_code=400)

    # Verificar si ya fue procesado
    pago_existente = await imagiax_pagos_db.buscar_pago_por_mp_id(str(payment_id))
    if pago_existente and pago_existente.get("estado") == "approved":
        estado_actual = await imagiax_pagos_db.get_estado_creditos(usuario_id=ext_uid)
        return JSONResponse({
            "ok": True,
            "mensaje": "Este pago ya fue procesado anteriormente.",
            "creditos_actuales": estado_actual["creditos_disponibles"],
        })

    # Acreditar créditos
    try:
        licencia = await imagiax_pagos_db.acreditar_pago(
            cliente_id=cliente_id,
            tipo=plan_id,
            creditos=plan["creditos"],
            mp_payment_id=str(payment_id),
            mp_preference_id=payment_data.get("preference_id"),
            duracion_dias=plan.get("duracion_dias"),
            usuario_id=ext_uid,
        )
        await imagiax_pagos_db.registrar_pago(
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
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    estado_actual = await imagiax_pagos_db.get_estado_creditos(usuario_id=ext_uid)
    return JSONResponse({
        "ok": True,
        "mensaje": f"Pago verificado y acreditado correctamente.",
        "plan": plan["titulo"],
        "creditos_acreditados": plan["creditos"],
        "creditos_totales": estado_actual["creditos_disponibles"],
    })


# ── Historial de cobros (API JSON) ────────────────────────────────────────────

@router.get("/pagos/historial-cobros")
async def historial_cobros(periodo: str = Query("total"), user: UserPayload = Depends(require_role("admin"))) -> JSONResponse:
    """
    Retorna historial de cobros filtrado por periodo.
    Valores: dia, semana, mes, trimestre, anio, total
    """
    historial = await imagiax_pagos_db.get_historial_cobros(periodo)
    return JSONResponse(historial)


@router.get("/pagos/exportar-excel")
async def exportar_excel(periodo: str = Query("total"), user: UserPayload = Depends(require_role("admin"))):
    """Genera reporte Excel profesional con KPIs, tabla detallada y graficos."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side, numbers
    from openpyxl.chart import BarChart, PieChart, Reference
    from openpyxl.utils import get_column_letter

    historial = await imagiax_pagos_db.get_historial_cobros(periodo)
    cobros = historial.get("cobros", [])
    resumen = historial.get("resumen", {})

    wb = Workbook()

    # ── Colores ────────────────────────────────────────────────────────────────
    BLUE = "1E40AF"
    BLUE_LIGHT = "DBEAFE"
    DARK = "0F172A"
    GREEN = "166534"
    GREEN_LIGHT = "DCFCE7"
    RED_LIGHT = "FEE2E2"
    GRAY = "64748B"
    WHITE = "FFFFFF"
    BORDER_COLOR = "CBD5E1"

    header_font = Font(name="Calibri", bold=True, color=WHITE, size=11)
    header_fill = PatternFill(start_color=BLUE, end_color=BLUE, fill_type="solid")
    title_font = Font(name="Calibri", bold=True, color=DARK, size=16)
    subtitle_font = Font(name="Calibri", color=GRAY, size=10)
    kpi_label_font = Font(name="Calibri", color=GRAY, size=9)
    kpi_value_font = Font(name="Calibri", bold=True, color=DARK, size=18)
    money_fmt = '#,##0" CLP"'
    thin_border = Border(
        bottom=Side(style="thin", color=BORDER_COLOR),
    )

    # ═══════════════════════════════════════════════════════════════════════════
    # HOJA 1: Resumen Ejecutivo
    # ═══════════════════════════════════════════════════════════════════════════
    ws = wb.active
    ws.title = "Resumen"
    ws.sheet_properties.tabColor = BLUE

    # Título
    ws.merge_cells("A1:F1")
    ws["A1"] = "IMAGIAX — Reporte de Facturacion"
    ws["A1"].font = title_font
    ws["A1"].alignment = Alignment(vertical="center")
    ws.row_dimensions[1].height = 36

    ws.merge_cells("A2:F2")
    periodos_label = {"dia": "Hoy", "semana": "Ultimos 7 dias", "mes": "Ultimos 30 dias",
                      "trimestre": "Ultimos 90 dias", "anio": "Ultimos 12 meses", "total": "Historico completo"}
    ws["A2"] = f"Periodo: {periodos_label.get(periodo, periodo)} | Generado: {datetime.now().strftime('%d/%m/%Y %H:%M')}"
    ws["A2"].font = subtitle_font
    ws.row_dimensions[2].height = 22

    # KPI Cards (row 4-5)
    kpi_data = [
        ("Total Facturado", f"${resumen.get('total_gastado', 0):,.0f} CLP", GREEN),
        ("Transacciones", str(resumen.get("total_transacciones", 0)), BLUE),
        ("Aprobadas", str(resumen.get("aprobados", 0)), GREEN),
        ("Rechazadas", str(resumen.get("rechazados", 0)), "DC2626"),
    ]
    for i, (label, value, color) in enumerate(kpi_data):
        col = i * 2 + 1
        cell_label = ws.cell(row=4, column=col, value=label)
        cell_label.font = kpi_label_font
        cell_label.alignment = Alignment(horizontal="center")
        ws.merge_cells(start_row=4, start_column=col, end_row=4, end_column=col + 1)

        cell_value = ws.cell(row=5, column=col, value=value)
        cell_value.font = Font(name="Calibri", bold=True, color=color, size=16)
        cell_value.alignment = Alignment(horizontal="center")
        ws.merge_cells(start_row=5, start_column=col, end_row=5, end_column=col + 1)

    # ── Pie chart: estados ─────────────────────────────────────────────────────
    aprobados = resumen.get("aprobados", 0)
    rechazados = resumen.get("rechazados", 0)
    pendientes = resumen.get("total_transacciones", 0) - aprobados - rechazados

    if aprobados or rechazados or pendientes:
        ws.cell(row=8, column=1, value="Estado")
        ws.cell(row=8, column=2, value="Cantidad")
        ws.cell(row=9, column=1, value="Aprobados")
        ws.cell(row=9, column=2, value=aprobados)
        ws.cell(row=10, column=1, value="Rechazados")
        ws.cell(row=10, column=2, value=rechazados)
        if pendientes > 0:
            ws.cell(row=11, column=1, value="Pendientes")
            ws.cell(row=11, column=2, value=pendientes)

        pie = PieChart()
        pie.title = "Distribucion por Estado"
        pie.style = 10
        pie.width = 14
        pie.height = 10
        last_row = 11 if pendientes > 0 else 10
        data_ref = Reference(ws, min_col=2, min_row=8, max_row=last_row)
        cats_ref = Reference(ws, min_col=1, min_row=9, max_row=last_row)
        pie.add_data(data_ref, titles_from_data=True)
        pie.set_categories(cats_ref)
        ws.add_chart(pie, "D8")

    # ── Bar chart: ingresos por dia (si hay datos) ─────────────────────────────
    if cobros:
        daily = {}
        for c in cobros:
            if c.get("estado") == "approved":
                d = c.get("created_at", "")[:10]
                daily[d] = daily.get(d, 0) + c.get("monto_clp", 0)

        if daily:
            sorted_days = sorted(daily.items())
            chart_start = last_row + 3 if (aprobados or rechazados) else 8
            ws.cell(row=chart_start, column=1, value="Fecha")
            ws.cell(row=chart_start, column=2, value="Ingresos CLP")
            for idx, (day, amount) in enumerate(sorted_days[-30:]):
                ws.cell(row=chart_start + 1 + idx, column=1, value=day)
                ws.cell(row=chart_start + 1 + idx, column=2, value=amount)

            bar_end = chart_start + len(sorted_days[-30:])
            bar = BarChart()
            bar.type = "col"
            bar.title = "Ingresos Diarios (CLP)"
            bar.style = 10
            bar.width = 24
            bar.height = 12
            bar.y_axis.title = "CLP"
            bar.x_axis.title = "Fecha"
            data_ref = Reference(ws, min_col=2, min_row=chart_start, max_row=bar_end)
            cats_ref = Reference(ws, min_col=1, min_row=chart_start + 1, max_row=bar_end)
            bar.add_data(data_ref, titles_from_data=True)
            bar.set_categories(cats_ref)
            bar.shape = 4
            ws.add_chart(bar, f"A{bar_end + 2}")

    # Column widths
    for col_idx in range(1, 9):
        ws.column_dimensions[get_column_letter(col_idx)].width = 16

    # ═══════════════════════════════════════════════════════════════════════════
    # HOJA 2: Detalle de Transacciones
    # ═══════════════════════════════════════════════════════════════════════════
    ws2 = wb.create_sheet("Detalle")
    ws2.sheet_properties.tabColor = "334155"

    tipos_map = {"scan_automatico": "Cobro por informe", "pago_unico": "Pago unico",
                 "suscripcion": "Suscripcion", "pack": "Pack", "trial": "Trial"}
    estados_map = {"approved": "Aprobado", "rejected": "Rechazado", "pending": "Pendiente", "error": "Error"}
    origenes_map = {"mercadopago": "MercadoPago", "transbank": "Transbank"}

    headers = ["Fecha", "Hora", "Descripcion", "Tipo", "Estado", "Monto CLP", "Via", "ID Transaccion"]
    widths = [14, 10, 36, 20, 14, 16, 14, 38]

    for col_idx, (h, w) in enumerate(zip(headers, widths), 1):
        cell = ws2.cell(row=1, column=col_idx, value=h)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
        ws2.column_dimensions[get_column_letter(col_idx)].width = w
    ws2.row_dimensions[1].height = 28

    approved_fill = PatternFill(start_color=GREEN_LIGHT, end_color=GREEN_LIGHT, fill_type="solid")
    rejected_fill = PatternFill(start_color=RED_LIGHT, end_color=RED_LIGHT, fill_type="solid")

    for row_idx, c in enumerate(cobros, 2):
        created = c.get("created_at", "")
        fecha = created[:10] if len(created) >= 10 else created
        hora = created[11:16] if len(created) >= 16 else ""

        ws2.cell(row=row_idx, column=1, value=fecha).border = thin_border
        ws2.cell(row=row_idx, column=2, value=hora).border = thin_border
        ws2.cell(row=row_idx, column=3, value=c.get("descripcion", "")).border = thin_border
        ws2.cell(row=row_idx, column=4, value=tipos_map.get(c.get("tipo", ""), c.get("tipo", ""))).border = thin_border

        estado_cell = ws2.cell(row=row_idx, column=5, value=estados_map.get(c.get("estado", ""), c.get("estado", "")))
        estado_cell.border = thin_border
        estado_cell.alignment = Alignment(horizontal="center")
        if c.get("estado") == "approved":
            estado_cell.fill = approved_fill
            estado_cell.font = Font(name="Calibri", bold=True, color=GREEN)
        elif c.get("estado") == "rejected":
            estado_cell.fill = rejected_fill
            estado_cell.font = Font(name="Calibri", bold=True, color="DC2626")

        monto_cell = ws2.cell(row=row_idx, column=6, value=c.get("monto_clp", 0))
        monto_cell.number_format = money_fmt
        monto_cell.border = thin_border
        monto_cell.font = Font(name="Calibri", bold=True)
        monto_cell.alignment = Alignment(horizontal="right")

        origen_cell = ws2.cell(row=row_idx, column=7, value=origenes_map.get(c.get("origen", ""), c.get("origen", "MP")))
        origen_cell.border = thin_border
        origen_cell.alignment = Alignment(horizontal="center")

        ws2.cell(row=row_idx, column=8, value=c.get("ref_id") or c.get("id", "")).border = thin_border
        ws2.cell(row=row_idx, column=8).font = Font(name="Calibri", color=GRAY, size=9)

    # Totals row
    if cobros:
        total_row = len(cobros) + 2
        ws2.cell(row=total_row, column=5, value="TOTAL").font = Font(name="Calibri", bold=True, size=11)
        ws2.cell(row=total_row, column=5).alignment = Alignment(horizontal="right")
        total_cell = ws2.cell(row=total_row, column=6, value=resumen.get("total_gastado", 0))
        total_cell.number_format = money_fmt
        total_cell.font = Font(name="Calibri", bold=True, color=BLUE, size=12)
        total_cell.alignment = Alignment(horizontal="right")

    # Auto-filter
    if cobros:
        ws2.auto_filter.ref = f"A1:H{len(cobros) + 1}"

    # Freeze header row
    ws2.freeze_panes = "A2"

    # ── Write to buffer ────────────────────────────────────────────────────────
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    filename = f"IMAGIAX_Reporte_{periodo}_{datetime.now().strftime('%Y%m%d')}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Página de historial de cobros (HTML) ──────────────────────────────────────

@router.get("/historial", response_class=HTMLResponse)
async def pagina_historial() -> HTMLResponse:
    test_badge = ""
    if _IS_TEST:
        test_badge = ('<div style="position:fixed;top:0;left:50%;transform:translateX(-50%);'
                      'background:#f59e0b;color:#000;font-size:11px;font-weight:700;'
                      'padding:3px 16px;border-radius:0 0 8px 8px;z-index:9999;'
                      'letter-spacing:.5px">MODO TEST &mdash; Pagos simulados</div>')
    return HTMLResponse(
        _HISTORIAL_HTML
        .replace("{test_badge}", test_badge)
        .replace("__IMAGIAX_API_BASE__", IMAGIAX_BASE_URL.rstrip("/"))
        .replace("__PAGAR_URL__", IMAGIAX_BASE_URL.rstrip("/") + "/pagar")
    )


_HISTORIAL_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="icon" href="/favicon.ico" type="image/svg+xml">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<title>Historial de cobros &mdash; IMAGIAX</title>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  :root{--bg:#060b18;--surface:#0c1222;--surface2:#111827;--border:#1e293b;
        --text:#f1f5f9;--muted:#64748b;--blue:#3b82f6;--green:#22c55e}
  body{font-family:'Inter',-apple-system,BlinkMacSystemFont,sans-serif;
       background:var(--bg);color:var(--text);min-height:100vh;overflow-x:hidden}

  /* Ambient glow */
  body::before{content:'';position:fixed;top:0;left:0;right:0;bottom:0;
    background:radial-gradient(ellipse 900px 500px at 50% -80px,rgba(59,130,246,.07),transparent);
    pointer-events:none;z-index:0}

  /* Sticky header */
  .topbar{position:sticky;top:0;z-index:100;
    background:rgba(6,11,24,.85);backdrop-filter:blur(16px) saturate(1.4);
    -webkit-backdrop-filter:blur(16px) saturate(1.4);
    border-bottom:1px solid rgba(30,41,59,.6);
    padding:0 28px;height:60px;display:flex;align-items:center;gap:14px}
  .topbar-logo{width:34px;height:34px;border-radius:9px;
    background:linear-gradient(135deg,#1e40af,#6d28d9);
    display:flex;align-items:center;justify-content:center;
    font-weight:800;font-size:13px;color:#fff;letter-spacing:-.5px;
    box-shadow:0 0 20px rgba(59,130,246,.3);flex-shrink:0}
  .topbar-name{font-size:16px;font-weight:700;letter-spacing:-.3px}
  .topbar-sub{font-size:10px;color:var(--muted);letter-spacing:.2px;margin-top:1px}
  .topbar-actions{margin-left:auto;display:flex;gap:8px;align-items:center}
  .btn-sm{color:var(--muted);text-decoration:none;font-size:13px;font-weight:500;
    padding:7px 14px;border-radius:8px;border:1px solid var(--border);background:transparent;
    cursor:pointer;transition:all .2s;font-family:inherit}
  .btn-sm:hover{border-color:#334155;color:var(--text);background:var(--surface)}

  .page{max-width:1040px;margin:0 auto;padding:36px 28px 60px;position:relative;z-index:1}

  /* Page title */
  .page-title{margin-bottom:28px}
  .page-title h1{font-size:26px;font-weight:800;letter-spacing:-.5px;
    background:linear-gradient(135deg,#f1f5f9,#94a3b8);
    -webkit-background-clip:text;-webkit-text-fill-color:transparent}
  .page-title p{color:var(--muted);font-size:13px;margin-top:4px}

  /* KPI cards */
  .kpi-row{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:28px}
  @media(max-width:700px){.kpi-row{grid-template-columns:repeat(2,1fr)}}
  .kpi{background:var(--surface);border:1px solid var(--border);border-radius:14px;
       padding:20px 20px 18px;position:relative;overflow:hidden;transition:border-color .2s}
  .kpi:hover{border-color:#334155}
  .kpi::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;
    background:linear-gradient(90deg,transparent,var(--kpi-accent,#334155),transparent)}
  .kpi-1{--kpi-accent:#22c55e}.kpi-2{--kpi-accent:#3b82f6}
  .kpi-3{--kpi-accent:#4ade80}.kpi-4{--kpi-accent:#fb923c}
  .kpi-label{font-size:10.5px;color:var(--muted);text-transform:uppercase;
    letter-spacing:.8px;font-weight:600;margin-bottom:10px}
  .kpi-value{font-size:28px;font-weight:800;color:var(--text);
    font-variant-numeric:tabular-nums;letter-spacing:-.8px}
  .kpi-value.accent{color:#34d399}
  .kpi-value.warn{color:#fb923c}

  /* Period filters */
  .filters-row{display:flex;align-items:center;justify-content:space-between;
    margin-bottom:20px;gap:12px;flex-wrap:wrap}
  .filters{display:flex;gap:4px;flex-wrap:wrap;padding:3px;
    background:var(--surface);border:1px solid var(--border);border-radius:10px;width:fit-content}
  .f-btn{background:transparent;border:none;color:var(--muted);padding:6px 14px;
    border-radius:7px;cursor:pointer;font-size:12px;font-weight:500;transition:all .2s;
    letter-spacing:.1px;font-family:inherit}
  .f-btn:hover{color:var(--text);background:rgba(255,255,255,.04)}
  .f-btn.active{background:var(--blue);color:#fff;font-weight:600;
    box-shadow:0 2px 8px rgba(59,130,246,.25)}

  /* Table */
  .table-wrap{background:var(--surface);border:1px solid var(--border);
    border-radius:14px;overflow:hidden}
  .table-header{padding:18px 20px;border-bottom:1px solid var(--border);
    display:flex;align-items:center;justify-content:space-between}
  .table-title{font-size:14px;font-weight:700}
  .table-count{font-size:12px;color:var(--muted)}
  table{width:100%;border-collapse:collapse}
  thead th{background:#080e1c;text-align:left;padding:12px 16px;font-size:10.5px;
    color:var(--muted);text-transform:uppercase;letter-spacing:.7px;font-weight:600;
    border-bottom:1px solid var(--border)}
  tbody td{padding:14px 16px;border-bottom:1px solid rgba(30,41,59,.5);font-size:13px;
    vertical-align:middle}
  tbody tr{transition:background .15s}
  tbody tr:hover td{background:rgba(30,41,59,.35)}
  tbody tr:last-child td{border-bottom:none}

  .cell-fecha{color:#94a3b8;font-size:12.5px;white-space:nowrap;font-family:monospace}
  .cell-desc{color:#cbd5e1;max-width:320px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:500}
  .cell-tipo{color:#94a3b8;font-size:12px}
  .cell-monto{font-weight:700;color:var(--text);font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap}
  .cell-via{font-size:10px;color:var(--muted);text-align:center;font-weight:600;letter-spacing:.4px}

  .badge{display:inline-flex;align-items:center;gap:5px;padding:4px 10px;border-radius:6px;
    font-size:11px;font-weight:600;letter-spacing:.2px}
  .badge-approved{background:rgba(34,197,94,.1);color:#4ade80;border:1px solid rgba(34,197,94,.18)}
  .badge-rejected{background:rgba(239,68,68,.1);color:#f87171;border:1px solid rgba(239,68,68,.15)}
  .badge-pending{background:rgba(251,191,36,.08);color:#fbbf24;border:1px solid rgba(251,191,36,.15)}
  .badge-dot{width:5px;height:5px;border-radius:50%;flex-shrink:0}
  .dot-approved{background:#4ade80}.dot-rejected{background:#f87171}.dot-pending{background:#fbbf24}

  .empty-state{text-align:center;padding:56px 20px}
  .empty-state svg{opacity:.3;margin-bottom:12px}
  .empty-state-title{font-size:14px;font-weight:600;color:var(--text);margin-bottom:4px}
  .empty-state p{font-size:13px;color:var(--muted)}

  .page-footer{margin-top:32px;text-align:center;color:#334155;font-size:11px;letter-spacing:.3px}

  @media(max-width:640px){
    .topbar{padding:0 16px}
    .page{padding:20px 14px 40px}
    .page-title h1{font-size:20px}
    .kpi-value{font-size:22px}
    tbody td{padding:10px 10px;font-size:12px}
    .cell-desc{max-width:140px}
    .filters{overflow-x:auto}
  }
</style>
</head>
<body>
{test_badge}
<!-- Sticky top bar -->
<header class="topbar">
  <div class="topbar-logo">IX</div>
  <div>
    <div class="topbar-name">IMAGIAX</div>
    <div class="topbar-sub">Medical AI Platform</div>
  </div>
  <div class="topbar-actions">
    <button class="btn-sm" onclick="exportExcel()" title="Exportar Excel">&#8615; Excel</button>
    <button class="btn-sm" onclick="exportCSV()" title="Exportar CSV">&#8615; CSV</button>
    <a class="btn-sm" href="__PAGAR_URL__">&larr; Planes</a>
  </div>
</header>

<div class="page">
  <div class="page-title">
    <h1>Historial de cobros</h1>
    <p>Registro completo de transacciones y pagos procesados</p>
  </div>

  <div class="kpi-row">
    <div class="kpi kpi-1">
      <div class="kpi-label">Total facturado</div>
      <div class="kpi-value accent" id="kpi-gastado">$0</div>
    </div>
    <div class="kpi kpi-2">
      <div class="kpi-label">Transacciones</div>
      <div class="kpi-value" id="kpi-total">0</div>
    </div>
    <div class="kpi kpi-3">
      <div class="kpi-label">Aprobadas</div>
      <div class="kpi-value" id="kpi-ok">0</div>
    </div>
    <div class="kpi kpi-4">
      <div class="kpi-label">Rechazadas</div>
      <div class="kpi-value warn" id="kpi-fail">0</div>
    </div>
  </div>

  <div class="filters-row">
    <div class="filters">
      <button class="f-btn" data-p="dia">Hoy</button>
      <button class="f-btn" data-p="semana">7 d&iacute;as</button>
      <button class="f-btn" data-p="mes">30 d&iacute;as</button>
      <button class="f-btn" data-p="trimestre">90 d&iacute;as</button>
      <button class="f-btn" data-p="anio">12 meses</button>
      <button class="f-btn active" data-p="total">Todo</button>
    </div>
  </div>

  <div class="table-wrap">
    <div class="table-header">
      <span class="table-title">Transacciones</span>
      <span class="table-count" id="table-count">&mdash;</span>
    </div>
    <table>
      <thead>
        <tr>
          <th>Fecha</th>
          <th>Descripci&oacute;n</th>
          <th>Tipo</th>
          <th>Estado</th>
          <th style="text-align:right">Monto</th>
          <th style="text-align:center">V&iacute;a</th>
        </tr>
      </thead>
      <tbody id="tb"></tbody>
    </table>
  </div>

  <div class="page-footer">IMAGIAX &mdash; Plataforma de an&aacute;lisis radiol&oacute;gico con IA</div>
</div>

<script>
const API="__IMAGIAX_API_BASE__";
const fmt=n=>'$'+Number(n).toLocaleString('es-CL');
const fdate=iso=>{const d=new Date(iso);return d.toLocaleDateString('es-CL',{day:'2-digit',month:'short',year:'numeric'})+' '+d.toLocaleTimeString('es-CL',{hour:'2-digit',minute:'2-digit'})};
const tipos={scan_automatico:'Cobro por informe',pago_unico:'Pago \u00fanico',suscripcion:'Suscripci\u00f3n',pack:'Pack',trial:'Trial'};
const estados={approved:'Aprobado',rejected:'Rechazado',pending:'Pendiente',error:'Error'};
const origenes={mercadopago:'MP',transbank:'TBK'};

let _allCobros=[];
const PAGE_SIZE=20;
let _page=1;

function renderPage(){
  const tb=document.getElementById('tb');
  const start=(_page-1)*PAGE_SIZE, end=_page*PAGE_SIZE;
  const slice=_allCobros.slice(start,end);
  const cnt=document.getElementById('table-count');
  if(cnt)cnt.textContent=_allCobros.length?_allCobros.length+' registros':'';
  if(!slice.length){tb.innerHTML='<tr><td colspan="6"><div class="empty-state"><svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#64748b" stroke-width="1.5"><rect x="2" y="5" width="20" height="14" rx="2"/><line x1="2" y1="10" x2="22" y2="10"/></svg><div class="empty-state-title">Sin transacciones</div><p>No hay registros en este per\u00edodo</p></div></td></tr>';return}
  tb.innerHTML=slice.map(c=>'<tr>'
    +'<td class="cell-fecha">'+fdate(c.created_at)+'</td>'
    +'<td class="cell-desc" title="'+(c.descripcion||'')+'">'+(c.descripcion||'&mdash;')+'</td>'
    +'<td class="cell-tipo">'+(tipos[c.tipo]||c.tipo)+'</td>'
    +'<td><span class="badge badge-'+c.estado+'"><span class="badge-dot dot-'+c.estado+'"></span>'+(estados[c.estado]||c.estado)+'</span></td>'
    +'<td class="cell-monto">'+fmt(c.monto_clp)+'</td>'
    +'<td class="cell-via">'+(origenes[c.origen]||c.origen||'MP')+'</td>'
    +'</tr>').join('');
  const pages=Math.ceil(_allCobros.length/PAGE_SIZE);
  let pg=document.getElementById('pager');
  if(!pg){pg=document.createElement('div');pg.id='pager';pg.style.cssText='display:flex;justify-content:center;gap:8px;margin-top:16px;align-items:center;font-size:13px;color:#94a3b8';
    document.querySelector('.table-wrap').after(pg)}
  if(pages<=1){pg.innerHTML='';return}
  pg.innerHTML='<button class="f-btn'+(1===_page?' active':'')+'" onclick="_page=1;renderPage()">&laquo;</button>'
    +'<button class="f-btn'+(_page<=1?' active':'')+'" onclick="_page=Math.max(1,_page-1);renderPage()">&lsaquo;</button>'
    +'<span style="padding:0 8px">'+_page+' / '+pages+'</span>'
    +'<button class="f-btn'+(_page>=pages?' active':'')+'" onclick="_page=Math.min('+pages+',_page+1);renderPage()">&rsaquo;</button>'
    +'<button class="f-btn'+(pages===_page?' active':'')+'" onclick="_page='+pages+';renderPage()">&raquo;</button>';
}

async function load(p){
  document.querySelectorAll('.f-btn[data-p]').forEach(b=>b.classList.toggle('active',b.dataset.p===p));
  try{
    const r=await fetch(API+'/pagos/historial-cobros?periodo='+p);
    const d=await r.json();
    document.getElementById('kpi-gastado').textContent=fmt(d.resumen.total_gastado);
    document.getElementById('kpi-total').textContent=d.resumen.total_transacciones;
    document.getElementById('kpi-ok').textContent=d.resumen.aprobados;
    document.getElementById('kpi-fail').textContent=d.resumen.rechazados;
    _allCobros=d.cobros||[];_page=1;renderPage();
  }catch(e){
    document.getElementById('tb').innerHTML='<tr><td colspan="6" class="empty-state"><p>Error al cargar historial</p></td></tr>';
  }
}

function exportCSV(){
  if(!_allCobros.length)return;
  const hdr='Fecha,Descripcion,Tipo,Estado,Monto CLP,Via\\n';
  const rows=_allCobros.map(c=>[
    '"'+fdate(c.created_at)+'"',
    '"'+(c.descripcion||'').replace(/"/g,'""')+'"',
    tipos[c.tipo]||c.tipo,
    estados[c.estado]||c.estado,
    c.monto_clp,
    origenes[c.origen]||c.origen||'MP'
  ].join(',')).join('\\n');
  const blob=new Blob([hdr+rows],{type:'text/csv;charset=utf-8'});
  const a=document.createElement('a');a.href=URL.createObjectURL(blob);
  a.download='imagiax_historial.csv';a.click();URL.revokeObjectURL(a.href);
}

function exportExcel(){
  const p=document.querySelector('.f-btn[data-p].active');
  const periodo=p?p.dataset.p:'total';
  window.open(API+'/pagos/exportar-excel?periodo='+periodo,'_blank');
}

document.querySelectorAll('.f-btn[data-p]').forEach(b=>b.addEventListener('click',()=>load(b.dataset.p)));
load('total');
</script>
</body>
</html>"""


# ── Página de pago (HTML) ──────────────────────────────────────────────────────

@router.get("/pagar", response_class=HTMLResponse)
async def pagina_pago(
    sin_creditos: bool = Query(False),
    trial_agotado: bool = Query(False),
    tbk_status: str = Query(""),
) -> HTMLResponse:
    """Página de selección de plan y checkout."""
    estado = await imagiax_pagos_db.get_estado_creditos()

    # Estado actual de tarjeta MP y Oneclick
    metodo_actual = await imagiax_pagos_db.get_metodo_pago()
    oneclick_inscripcion = await imagiax_pagos_db.get_inscripcion_oneclick_any()

    tiene_tarjeta = estado.get("tiene_tarjeta", False)
    tiene_oneclick = oneclick_inscripcion is not None
    tiene_mp_card = metodo_actual is not None
    creditos_disp = estado.get("creditos_disponibles", 0)
    tiene_suscripcion = estado.get("tiene_suscripcion", False)
    es_trial = estado.get("es_trial", False)

    # ── Panel de Estado (Tu Cuenta) ─────────────────────────────────────────
    estado_badge = ""
    if creditos_disp > 0 or tiene_tarjeta:
        estado_badge = '<span class="estado-panel-badge activo">Activo</span>'
    elif es_trial and creditos_disp > 0:
        estado_badge = '<span class="estado-panel-badge trial">Trial</span>'
    else:
        estado_badge = '<span class="estado-panel-badge sin-creditos">Sin creditos</span>'

    # Items del panel
    creditos_color = "verde" if creditos_disp > 10 else ("amarillo" if creditos_disp > 0 else "rojo")
    creditos_tipo = ""
    if es_trial:
        creditos_tipo = f"Trial ({imagiax_pagos_db.CREDITOS_TRIAL} iniciales)"
    elif tiene_suscripcion:
        creditos_tipo = "Suscripcion mensual"
    elif creditos_disp > 0:
        creditos_tipo = "Pack de creditos"

    # Método de pago automático
    metodo_pago_html = ""
    if tiene_oneclick:
        ct = (oneclick_inscripcion.get("card_type") or "TARJETA").upper()
        cl4 = oneclick_inscripcion.get("card_number_last4") or "****"
        metodo_pago_html = f"""
        <div class="estado-item accent-blue">
          <div class="estado-item-label">Pago automatico</div>
          <div class="estado-item-value azul">{ct} ****{cl4}</div>
          <div class="estado-item-detail">Transbank Oneclick</div>
        </div>"""
    elif tiene_mp_card:
        brand = (metodo_actual.get("card_brand") or "TARJETA").upper()
        last4 = metodo_actual.get("card_last4") or "****"
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

    # Suscripción con botón de cancelar
    suscripcion_html = ""
    if tiene_suscripcion:
        suscripcion_html = """
        <div class="estado-item accent-green">
          <div class="estado-item-label">Suscripcion</div>
          <div class="estado-item-value verde">Activa</div>
          <div class="estado-item-detail">300 creditos/mes</div>
          <button class="btn-cancelar-sub" onclick="cancelarSuscripcion()">Cancelar</button>
        </div>"""

    # Alerta de créditos bajos
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

    # Mensaje de prioridad de cobro
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

    estado_panel_html = f"""
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

    # ── Banner (solo si no hay panel de estado más informativo) ────────────
    banner_html = ""
    # No mostrar banner si ya tiene método de pago
    if not tiene_tarjeta and es_trial and creditos_disp == 0:
        banner_html = """
        <div class="banner-trial">
          <div>
            <strong>Periodo de prueba agotado</strong>
            <div style="font-size:13px;margin-top:2px;opacity:.85;">
              Has utilizado tus 5 informes gratuitos. Elige un plan o registra una tarjeta para continuar.
            </div>
          </div>
        </div>"""

    planes_html = "\n".join(_plan_card_html(pid, p, tiene_suscripcion) for pid, p in PLANES.items())

    # HTML para tarjeta registrada en sección de pago automático
    tarjeta_html = ""
    if metodo_actual:
        brand = metodo_actual["card_brand"].upper() or "TARJETA"
        tarjeta_html = f"""
        <div class="card-registered-badge">
          <div>
            <div class="card-badge-brand">{brand} ****{metodo_actual["card_last4"]}</div>
            <div class="card-badge-detail">{metodo_actual["email"]} &middot; Cobro autom&aacute;tico activo (MercadoPago)</div>
          </div>
          <button class="btn-borrar-tarjeta" onclick="borrarTarjeta()" title="Eliminar tarjeta">Eliminar</button>
        </div>"""

    # HTML para Oneclick
    oneclick_html = ""
    if oneclick_inscripcion:
        ct = (oneclick_inscripcion.get("card_type") or "TARJETA").upper()
        cl4 = oneclick_inscripcion.get("card_number_last4") or "****"
        oneclick_html = f"""
        <div class="card-registered-badge oneclick-badge">
          <div>
            <div class="card-badge-brand">{ct} ****{cl4}</div>
            <div class="card-badge-detail">Cobro autom&aacute;tico 1-Click activo (Transbank Oneclick)</div>
          </div>
          <button class="btn-borrar-tarjeta" onclick="borrarOneclick()" title="Desvincular tarjeta">Desvincular</button>
        </div>"""

    test_badge = ""
    if _IS_TEST:
        test_badge = ('<div style="position:fixed;top:0;left:50%;transform:translateX(-50%);'
                      'background:#f59e0b;color:#000;font-size:11px;font-weight:700;'
                      'padding:3px 16px;border-radius:0 0 8px 8px;z-index:9999;'
                      'letter-spacing:.5px">MODO TEST — Pagos simulados</div>')

    return HTMLResponse(_PAGO_HTML_TEMPLATE.format(
        banner=banner_html,
        estado_panel=estado_panel_html,
        planes=planes_html,
        api_base=IMAGIAX_BASE_URL,
        mp_public_key=MP_PUBLIC_KEY,
        tarjeta_registrada=tarjeta_html,
        oneclick_registrada=oneclick_html,
        oneclick_form_display="display:none" if oneclick_inscripcion else "",
        test_badge=test_badge,
        viewer_url=_VIEWER_URL,
    ))


def _plan_card_html(plan_id: str, plan: dict, tiene_suscripcion: bool = False) -> str:
    badge_text = plan.get("badge", "")
    if badge_text:
        badge_cls = "badge verde" if "valor" in badge_text.lower() else "badge"
        badge = f'<div class="{badge_cls}">{badge_text}</div>'
    else:
        badge = ""

    precio_fmt = f"${plan['precio_clp']:,}".replace(",", ".")
    frecuencia = "/mes" if plan["recurrente"] else ""
    creditos_txt = f"{plan['creditos']}/mes" if plan["recurrente"] else str(plan["creditos"])

    # Precio unitario por informe
    precio_unit = round(plan['precio_clp'] / plan['creditos'])
    precio_unit_fmt = f"${precio_unit:,}".replace(",", ".")
    unit_label = f'<div class="plan-unit-price">{precio_unit_fmt} CLP por informe</div>'

    destacado = plan.get("badge") == "Más popular"
    card_class = "plan-card destacado" if destacado else "plan-card"

    # Icono SVG por tipo de plan
    icons = {
        "pack_individual": '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><path d="M12 8v4l3 3"/></svg>',
        "pack_50":         '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/></svg>',
        "pack_200":        '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg>',
        "suscripcion_mensual": '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>',
    }
    icon_html = icons.get(plan_id, "")
    icon_color = "#3b82f6" if not destacado else "#60a5fa"

    btn_disabled = ""
    btn_text = 'Suscribirse' if plan['recurrente'] else 'Comprar ahora'
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


# ── Páginas HTML auxiliares ────────────────────────────────────────────────────

def _page_exito(
    plan: dict,
    creditos_nuevos: int,
    creditos_totales: int,
    email: str = "",
    es_suscripcion: bool = False,
) -> str:
    titulo_txt = "¡Suscripción activada!" if es_suscripcion else "¡Pago aprobado!"
    renovacion = (
        '<div class="renewal-note">🔄 Tu suscripción se renueva automáticamente cada 30 días.</div>'
    ) if es_suscripcion else ""
    email_line = f'<p class="email-confirm">Confirmación enviada a <strong>{email}</strong></p>' if email else ""
    return f"""<!DOCTYPE html>
<html lang="es"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>IMAGIAX — ¡Pago aprobado!</title>
<style>
{_CSS_BASE}
body {{ --glow-color: rgba(34,197,94,.08); }}
</style></head><body>
<div class="page-center">
  <div style="width:100%;max-width:460px">
    <nav class="result-nav">
      <div class="result-nav-logo">IX</div>
      <div>
        <div class="result-nav-name">IMAGIAX</div>
        <div class="result-nav-tag">Medical AI Platform</div>
      </div>
    </nav>
    <div class="result-card" style="--accent-line: #22c55e">
      <div class="result-icon success">
        <svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="#22c55e" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
          <polyline points="20 6 9 17 4 12"/>
        </svg>
      </div>
      <h1 class="result-title">{titulo_txt}</h1>
      <p class="result-subtitle">{plan['titulo']}</p>
      <div class="creditos-box">
        <div class="creditos-num">+{creditos_nuevos}</div>
        <div class="creditos-lbl">créditos acreditados</div>
        <div class="creditos-total">Total disponible: <strong>{creditos_totales}</strong> informes</div>
      </div>
      {renovacion}
      {email_line}
      <div class="result-actions">
        <a href="{_VIEWER_URL}" class="btn-primary">Volver al visor</a>
        <a href="{IMAGIAX_BASE_URL}/pagar" class="btn-secondary">Ver mis planes</a>
      </div>
    </div>
  </div>
</div></body></html>"""


def _page_pendiente(payment_id: str) -> str:
    id_badge = f'<div class="payment-id">ID: {payment_id}</div>' if payment_id else ""
    return f"""<!DOCTYPE html>
<html lang="es"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>IMAGIAX — Pago en proceso</title>
<style>
{_CSS_BASE}
body {{ --glow-color: rgba(245,158,11,.07); }}
@keyframes spin {{ to {{ transform: rotate(360deg); }} }}
.icon-spin {{ animation: spin 2s linear infinite; transform-origin: center; }}
</style></head><body>
<div class="page-center">
  <div style="width:100%;max-width:460px">
    <nav class="result-nav">
      <div class="result-nav-logo">IX</div>
      <div>
        <div class="result-nav-name">IMAGIAX</div>
        <div class="result-nav-tag">Medical AI Platform</div>
      </div>
    </nav>
    <div class="result-card" style="--accent-line: #f59e0b">
      <div class="result-icon pending">
        <svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="#f59e0b" stroke-width="2" stroke-linecap="round">
          <circle cx="12" cy="12" r="10" class="icon-spin" stroke-dasharray="50 14"/>
          <polyline points="12 6 12 12 16 14" stroke-width="2.5"/>
        </svg>
      </div>
      <h1 class="result-title">Pago en proceso</h1>
      <p class="result-subtitle">Tu transacción está siendo verificada</p>
      <p class="result-body">
        MercadoPago está procesando el pago. Esto puede tomar algunos minutos.
        Una vez confirmado, tus créditos se acreditarán automáticamente y recibirás una notificación.
      </p>
      {id_badge}
      <hr class="result-divider">
      <div class="result-actions">
        <a href="{_VIEWER_URL}" class="btn-primary">Volver al visor</a>
        <a href="{IMAGIAX_BASE_URL}/pagar" class="btn-secondary">Ver mis planes</a>
      </div>
    </div>
  </div>
</div></body></html>"""


def _page_error(mensaje: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="es"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>IMAGIAX — Error de pago</title>
<style>
{_CSS_BASE}
body {{ --glow-color: rgba(239,68,68,.07); }}
</style></head><body>
<div class="page-center">
  <div style="width:100%;max-width:460px">
    <nav class="result-nav">
      <div class="result-nav-logo">IX</div>
      <div>
        <div class="result-nav-name">IMAGIAX</div>
        <div class="result-nav-tag">Medical AI Platform</div>
      </div>
    </nav>
    <div class="result-card" style="--accent-line: #ef4444">
      <div class="result-icon error">
        <svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="#f87171" stroke-width="2.5" stroke-linecap="round">
          <line x1="18" y1="6" x2="6" y2="18"/>
          <line x1="6" y1="6" x2="18" y2="18"/>
        </svg>
      </div>
      <h1 class="result-title">Error en el pago</h1>
      <p class="result-body">{mensaje}</p>
      <hr class="result-divider">
      <div class="result-actions">
        <a href="{IMAGIAX_BASE_URL}/pagar" class="btn-primary">Reintentar</a>
        <a href="{_VIEWER_URL}" class="btn-secondary">Volver al visor</a>
      </div>
    </div>
  </div>
</div></body></html>"""


# ── CSS compartido ─────────────────────────────────────────────────────────────

_CSS_BASE = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --bg: #060b18; --surface: #0c1222; --surface2: #111827;
  --border: #1e293b; --text: #f1f5f9; --muted: #64748b; --muted2: #475569;
  --blue: #3b82f6; --blue-dark: #1e40af;
  --green: #22c55e; --green-dark: #16a34a; --green-bg: #052e16;
  --amber: #f59e0b; --amber-dark: #d97706; --amber-bg: #1c1100;
  --red: #f87171; --red-dark: #dc2626; --red-bg: #450a0a;
  --accent: var(--blue);
}
body {
  background: var(--bg); color: var(--text);
  font-family: 'Inter', system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
  min-height: 100vh; overflow-x: hidden;
}
body::before {
  content: ''; position: fixed; top: 0; left: 0; right: 0; bottom: 0;
  background: radial-gradient(ellipse 700px 500px at 50% -60px, var(--glow-color, rgba(59,130,246,.07)), transparent);
  pointer-events: none; z-index: 0;
}
.page-center {
  display: flex; align-items: center; justify-content: center;
  min-height: 100vh; padding: 24px; position: relative; z-index: 1;
}
/* ── Brand header ── */
.result-nav {
  display: flex; align-items: center; gap: 10px;
  padding: 20px 0 28px; justify-content: center;
}
.result-nav-logo {
  width: 32px; height: 32px; border-radius: 9px;
  background: linear-gradient(135deg, var(--blue-dark), #6d28d9);
  display: flex; align-items: center; justify-content: center;
  font-size: 12px; font-weight: 800; color: #fff; letter-spacing: -.5px;
  box-shadow: 0 0 20px rgba(59,130,246,.3);
}
.result-nav-name { font-size: 14px; font-weight: 700; color: var(--text); letter-spacing: -.2px; }
.result-nav-tag { font-size: 10px; color: var(--muted); letter-spacing: .2px; }

/* ── Card ── */
.result-card {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 20px; padding: 44px 40px 40px; max-width: 460px; width: 100%;
  text-align: center; position: relative; overflow: hidden;
  box-shadow: 0 24px 64px rgba(0,0,0,.4);
}
.result-card::before {
  content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px;
  background: linear-gradient(90deg, transparent, var(--accent-line, var(--blue)), transparent);
}
/* ── Icon ── */
.result-icon {
  width: 76px; height: 76px; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  margin: 0 auto 24px; position: relative;
}
.result-icon svg { position: relative; z-index: 1; }
.result-icon::after {
  content: ''; position: absolute; inset: -6px; border-radius: 50%;
  border: 1px solid var(--icon-ring, rgba(59,130,246,.2));
  animation: ring-pulse 2.4s ease-in-out infinite;
}
@keyframes ring-pulse { 0%,100%{opacity:.4;transform:scale(1)} 50%{opacity:.1;transform:scale(1.12)} }
.result-icon.success { background: var(--green-bg); border: 2px solid var(--green-dark); --icon-ring: rgba(34,197,94,.25); }
.result-icon.pending { background: var(--amber-bg); border: 2px solid var(--amber-dark); --icon-ring: rgba(245,158,11,.25); }
.result-icon.error   { background: var(--red-bg);   border: 2px solid var(--red-dark);   --icon-ring: rgba(239,68,68,.25); }

/* ── Typography ── */
.result-title { font-size: 23px; font-weight: 800; letter-spacing: -.4px; margin-bottom: 6px; }
.result-subtitle { color: var(--muted); font-size: 14px; line-height: 1.5; }
.result-body { color: #94a3b8; font-size: 13px; line-height: 1.65; margin-top: 14px; }

/* ── Credits box ── */
.creditos-box {
  background: rgba(6,11,24,.7); border: 1px solid rgba(34,197,94,.2);
  border-radius: 14px; padding: 22px 20px; margin: 22px 0;
  position: relative; overflow: hidden;
}
.creditos-box::before {
  content: ''; position: absolute; top: 0; left: 0; right: 0; height: 1px;
  background: linear-gradient(90deg, transparent, rgba(34,197,94,.4), transparent);
}
.creditos-num { font-size: 52px; font-weight: 800; color: var(--green); line-height: 1; letter-spacing: -2px; }
.creditos-lbl { color: #4ade80; font-size: 12px; font-weight: 600; margin-top: 6px; text-transform: uppercase; letter-spacing: .5px; }
.creditos-total { color: var(--muted); font-size: 12px; margin-top: 10px; }
.creditos-total strong { color: #86efac; }

/* ── Payment ID badge ── */
.payment-id {
  display: inline-block; background: rgba(255,255,255,.04); border: 1px solid var(--border);
  border-radius: 6px; padding: 4px 10px; font-size: 11px; color: var(--muted);
  font-family: monospace; margin-top: 10px; letter-spacing: .3px;
}

/* ── Actions ── */
.result-actions { display: flex; gap: 10px; justify-content: center; margin-top: 28px; flex-wrap: wrap; }
.btn-primary {
  background: linear-gradient(135deg, var(--blue-dark), var(--blue)); color: white;
  border: none; border-radius: 10px; padding: 11px 26px; font-size: 13px; font-weight: 700;
  cursor: pointer; text-decoration: none; display: inline-block;
  transition: all .2s; letter-spacing: -.1px;
  box-shadow: 0 4px 16px rgba(59,130,246,.25);
}
.btn-primary:hover { opacity: .9; transform: translateY(-1px); box-shadow: 0 6px 20px rgba(59,130,246,.35); }
.btn-secondary {
  background: transparent; color: #93c5fd; border: 1px solid var(--border);
  border-radius: 10px; padding: 11px 26px; font-size: 13px; font-weight: 500;
  cursor: pointer; text-decoration: none; display: inline-block; transition: all .2s;
}
.btn-secondary:hover { border-color: #3b82f6; color: var(--text); background: rgba(59,130,246,.06); }

/* ── Renewal note ── */
.renewal-note {
  background: rgba(34,197,94,.06); border: 1px solid rgba(34,197,94,.12);
  border-radius: 8px; padding: 10px 14px; font-size: 12px; color: #4ade80; margin-top: 14px;
}

/* ── Divider ── */
.result-divider { border: none; border-top: 1px solid var(--border); margin: 20px 0; }

/* ── Confirm email ── */
.email-confirm { color: var(--muted); font-size: 12px; margin-top: 6px; }
"""

# ── Template HTML de la página de pago ────────────────────────────────────────

_PAGO_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>IMAGIAX — Planes y Precios</title>
  <link rel="icon" href="/favicon.ico" type="image/svg+xml">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&display=swap" rel="stylesheet">
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    :root {{
      --bg: #060b18; --surface: #0c1222; --surface2: #111827; --surface3: #1a2332;
      --border: #1e293b; --border-hover: #334155; --text: #f1f5f9; --muted: #64748b; --muted2: #475569;
      --blue: #3b82f6; --blue-dark: #1e40af; --blue-glow: rgba(59,130,246,.15);
      --green: #22c55e; --green-dark: #166534; --amber: #f59e0b;
      --purple: #8b5cf6; --radius: 14px;
    }}
    body {{ background: var(--bg); color: var(--text);
            font-family: 'Inter', system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
            min-height: 100vh; overflow-x: hidden; }}

    /* ── Ambient background ─────────────────────────────────────────────── */
    body::before {{ content: ''; position: fixed; top: 0; left: 0; right: 0; bottom: 0;
      background: radial-gradient(ellipse 900px 560px at 50% -80px, rgba(59,130,246,.09), transparent);
      pointer-events: none; z-index: 0; }}

    /* ── Header ─────────────────────────────────────────────────────────── */
    .header {{
      position: sticky; top: 0; z-index: 100;
      background: rgba(6,11,24,.85); backdrop-filter: blur(16px) saturate(1.4);
      -webkit-backdrop-filter: blur(16px) saturate(1.4);
      border-bottom: 1px solid rgba(30,41,59,.6);
      padding: 0 28px; height: 60px;
      display: flex; align-items: center; gap: 14px;
    }}
    .header-logo {{
      width: 36px; height: 36px; border-radius: 10px;
      background: linear-gradient(135deg, var(--blue-dark), var(--purple));
      display: flex; align-items: center; justify-content: center;
      font-size: 13px; font-weight: 800; color: #fff; letter-spacing: -0.5px;
      box-shadow: 0 0 20px rgba(59,130,246,.25);
    }}
    .header-title {{ font-size: 17px; font-weight: 700; color: var(--text); letter-spacing: -0.3px; }}
    .header-subtitle {{ font-size: 10.5px; color: var(--muted); margin-top: 1px; letter-spacing: .2px; }}
    .header-nav {{ margin-left: auto; display: flex; gap: 8px; align-items: center; }}
    .header-nav a {{
      color: var(--muted); text-decoration: none; font-size: 13px; font-weight: 500;
      padding: 7px 14px; border-radius: 8px; border: 1px solid transparent;
      transition: all .2s;
    }}
    .header-nav a:hover {{ color: var(--text); border-color: var(--border); background: var(--surface); }}

    /* ── Tabs Navigation ───────────────────────────────────────────────── */
    .tabs-container {{
      max-width: 980px; margin: 24px auto 0; padding: 0 20px;
    }}
    .tabs-nav {{
      display: flex; gap: 4px; padding: 4px;
      background: var(--surface); border: 1px solid var(--border);
      border-radius: 12px; width: fit-content;
    }}
    .tab-btn {{
      padding: 10px 24px; border: none; background: transparent;
      color: var(--muted); font-size: 13px; font-weight: 600;
      border-radius: 8px; cursor: pointer; transition: all .2s;
      font-family: inherit;
    }}
    .tab-btn:hover {{ color: var(--text); background: rgba(255,255,255,.03); }}
    .tab-btn.active {{
      background: var(--blue); color: white;
      box-shadow: 0 2px 8px rgba(59,130,246,.25);
    }}
    .tab-content {{ display: none; animation: fadeIn .3s ease; }}
    .tab-content.active {{ display: block; }}
    @keyframes fadeIn {{ from {{ opacity: 0; transform: translateY(8px); }} to {{ opacity: 1; transform: translateY(0); }} }}

    /* ── Banner ─────────────────────────────────────────────────────────── */
    .banner-trial {{
      background: linear-gradient(135deg, #1c0a0a, #1a0505);
      border: 1px solid rgba(127,29,29,.6); border-radius: 12px;
      padding: 16px 20px; display: flex; align-items: center; gap: 14px;
      margin-bottom: 24px;
    }}
    .banner-trial.info {{ background: linear-gradient(135deg, #061a06, #0a1f0a); border-color: rgba(22,101,52,.5); }}
    .banner-trial strong {{ color: #fca5a5; }}
    .banner-trial.info strong {{ color: #86efac; }}

    /* ── Estado Panel (Mi cuenta) ──────────────────────────────────────── */
    .estado-panel {{
      background: var(--surface);
      border: 1px solid var(--border); border-radius: var(--radius);
      padding: 24px 28px; margin-bottom: 24px;
    }}
    .estado-panel-header {{
      display: flex; align-items: center; justify-content: space-between;
      margin-bottom: 20px; padding-bottom: 16px; border-bottom: 1px solid var(--border);
    }}
    .estado-panel-title {{ font-size: 16px; font-weight: 700; color: var(--text); }}
    .estado-panel-badge {{
      font-size: 10px; font-weight: 600; padding: 4px 10px; border-radius: 20px;
      text-transform: uppercase; letter-spacing: .5px;
    }}
    .estado-panel-badge.activo {{ background: rgba(34,197,94,.15); color: #86efac; }}
    .estado-panel-badge.trial {{ background: rgba(245,158,11,.15); color: #fbbf24; }}
    .estado-panel-badge.sin-creditos {{ background: rgba(239,68,68,.15); color: #fca5a5; }}

    .estado-grid {{
      display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px;
    }}
    .estado-item {{
      background: var(--bg); border: 1px solid var(--border); border-radius: 12px;
      padding: 18px 20px; position: relative; overflow: hidden;
    }}
    .estado-item::before {{
      content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px;
      background: linear-gradient(90deg, transparent, var(--item-accent, #334155), transparent);
    }}
    .estado-item.accent-green {{ --item-accent: #22c55e; }}
    .estado-item.accent-blue  {{ --item-accent: #3b82f6; }}
    .estado-item.accent-amber {{ --item-accent: #f59e0b; }}
    .estado-item-label {{
      font-size: 11px; font-weight: 600; color: var(--muted);
      text-transform: uppercase; letter-spacing: .5px; margin-bottom: 8px;
    }}
    .estado-item-value {{
      font-size: 24px; font-weight: 800; color: var(--text);
      font-variant-numeric: tabular-nums;
    }}
    .estado-item-value.verde {{ color: #86efac; }}
    .estado-item-value.azul {{ color: #60a5fa; }}
    .estado-item-value.amarillo {{ color: #fbbf24; }}
    .estado-item-value.rojo {{ color: #fca5a5; }}
    .estado-item-detail {{
      font-size: 12px; color: var(--muted); margin-top: 6px;
    }}
    .estado-prioridad {{
      margin-top: 20px; padding: 16px; background: var(--bg);
      border: 1px solid var(--border); border-radius: 10px;
      font-size: 12px; color: var(--muted); line-height: 1.6;
    }}
    .estado-prioridad strong {{ color: var(--text); font-weight: 600; }}

    /* Alerta de creditos bajos */
    .estado-alerta {{
      background: rgba(245,158,11,.1); border: 1px solid rgba(245,158,11,.3);
      border-radius: 10px; padding: 14px 18px; margin-bottom: 20px;
      font-size: 12px; color: #fbbf24; line-height: 1.5;
    }}
    .estado-alerta.critico {{
      background: rgba(239,68,68,.1); border-color: rgba(239,68,68,.3); color: #fca5a5;
    }}
    .estado-alerta strong {{ font-weight: 700; }}

    /* Boton cancelar suscripcion */
    .btn-cancelar-sub {{
      margin-top: 10px; background: transparent; border: 1px solid rgba(239,68,68,.4);
      color: #f87171; font-size: 11px; font-weight: 600; padding: 6px 12px;
      border-radius: 6px; cursor: pointer; transition: all .2s;
    }}
    .btn-cancelar-sub:hover {{ background: rgba(239,68,68,.15); border-color: #f87171; }}

    /* ── Historial Table ──────────────────────────────────────────────────── */
    .historial-section {{
      background: var(--surface); border: 1px solid var(--border);
      border-radius: var(--radius); overflow: hidden;
    }}
    .historial-header {{
      padding: 20px 24px; border-bottom: 1px solid var(--border);
      display: flex; align-items: center; justify-content: space-between;
    }}
    .historial-title {{ font-size: 16px; font-weight: 700; }}
    .historial-count {{ font-size: 12px; color: var(--muted); }}
    .historial-table {{ width: 100%; border-collapse: collapse; }}
    .historial-table th {{
      text-align: left; padding: 12px 16px; font-size: 10px; font-weight: 700;
      color: var(--muted); text-transform: uppercase; letter-spacing: .5px;
      background: var(--bg); border-bottom: 1px solid var(--border);
    }}
    .historial-table td {{
      padding: 14px 16px; font-size: 13px; border-bottom: 1px solid var(--border);
      vertical-align: middle;
    }}
    .historial-table tr:last-child td {{ border-bottom: none; }}
    .historial-table tr:hover td {{ background: rgba(255,255,255,.02); }}
    .historial-fecha {{ color: var(--muted); font-family: monospace; font-size: 12px; }}
    .historial-desc {{ font-weight: 500; }}
    .historial-tipo {{ color: var(--muted); font-size: 12px; }}
    .historial-badge {{
      display: inline-block; padding: 4px 10px; border-radius: 20px;
      font-size: 10px; font-weight: 600;
    }}
    .historial-badge.aprobado {{ background: rgba(34,197,94,.15); color: #86efac; }}
    .historial-badge.pendiente {{ background: rgba(245,158,11,.15); color: #fbbf24; }}
    .historial-badge.rechazado {{ background: rgba(239,68,68,.15); color: #fca5a5; }}
    .historial-monto {{ font-weight: 700; font-variant-numeric: tabular-nums; }}
    .historial-via {{ color: var(--muted); font-size: 11px; font-weight: 500; }}
    .historial-empty {{
      padding: 48px 24px; text-align: center; color: var(--muted);
    }}
    .historial-empty-title {{ font-size: 14px; font-weight: 600; margin-bottom: 6px; color: var(--text); }}
    .historial-empty-desc {{ font-size: 12px; }}

    /* ── Main ───────────────────────────────────────────────────────────── */
    .main {{ max-width: 980px; margin: 0 auto; padding: 24px 20px 60px; position: relative; z-index: 1; }}

    .section-title {{ margin-bottom: 24px; }}
    .section-title h1 {{ font-size: 28px; font-weight: 800; letter-spacing: -0.5px;
                       background: linear-gradient(135deg, #f1f5f9, #94a3b8);
                       -webkit-background-clip: text; -webkit-text-fill-color: transparent; }}
    .section-title p {{ color: var(--muted); font-size: 14px; margin-top: 6px; line-height: 1.6; }}

    .page-title {{ text-align: center; margin-bottom: 12px; }}
    .page-title h1 {{ font-size: 32px; font-weight: 800; letter-spacing: -0.5px;
                       background: linear-gradient(135deg, #f1f5f9, #94a3b8);
                       -webkit-background-clip: text; -webkit-text-fill-color: transparent; }}
    .page-title p {{ color: var(--muted); font-size: 15px; margin-top: 8px; line-height: 1.6; }}

    /* ── Plan Cards ────────────────────────────────────────────────────── */
    .planes-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-top: 28px; }}
    @media (max-width: 860px) {{ .planes-grid {{ grid-template-columns: repeat(2, 1fr); }} }}
    @media (max-width: 480px) {{ .planes-grid {{ grid-template-columns: 1fr; }} }}

    .plan-card {{
      background: var(--surface);
      border: 1px solid var(--border); border-radius: var(--radius);
      padding: 28px 22px; position: relative;
      transition: all .25s cubic-bezier(.4,0,.2,1);
      display: flex; flex-direction: column;
    }}
    .plan-card:hover {{
      border-color: var(--border-hover);
      transform: translateY(-4px);
      box-shadow: 0 12px 40px rgba(0,0,0,.3);
    }}
    .plan-card.destacado {{
      border-color: rgba(59,130,246,.4);
      background: linear-gradient(165deg, #0c1e3a 0%, var(--surface) 100%);
      box-shadow: 0 0 40px rgba(59,130,246,.08);
    }}
    .plan-card.destacado:hover {{
      border-color: var(--blue);
      box-shadow: 0 0 50px rgba(59,130,246,.15), 0 12px 40px rgba(0,0,0,.3);
    }}

    .badge {{ position: absolute; top: -11px; left: 50%; transform: translateX(-50%);
              font-size: 9.5px; font-weight: 700; padding: 4px 14px; border-radius: 20px;
              white-space: nowrap; text-transform: uppercase; letter-spacing: .8px;
              box-shadow: 0 4px 12px rgba(0,0,0,.3); }}
    .badge {{ background: linear-gradient(135deg, #2563eb, #3b82f6); color: white; }}
    .badge.verde {{ background: linear-gradient(135deg, #16a34a, #22c55e); color: white; }}

    /* ── Plan card icon ────────────────────────────────────────────────── */
    .plan-icon {{
      width: 36px; height: 36px; border-radius: 10px;
      background: rgba(59,130,246,.1); border: 1px solid rgba(59,130,246,.2);
      display: flex; align-items: center; justify-content: center;
      margin-bottom: 16px;
    }}
    .plan-card.destacado .plan-icon {{
      background: rgba(59,130,246,.18); border-color: rgba(59,130,246,.35);
      box-shadow: 0 0 16px rgba(59,130,246,.15);
    }}

    /* Plan card top accent line */
    .plan-card::before {{
      content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px;
      background: linear-gradient(90deg, transparent, rgba(59,130,246,.2), transparent);
      border-radius: var(--radius) var(--radius) 0 0;
    }}
    .plan-card.destacado::before {{
      background: linear-gradient(90deg, transparent, var(--blue), transparent);
    }}

    /* Unit price */
    .plan-unit-price {{
      font-size: 11px; color: var(--muted); font-weight: 500;
      margin-bottom: 12px; letter-spacing: .1px;
    }}

    .plan-titulo {{ font-size: 14px; font-weight: 700; margin-bottom: 8px; color: #e2e8f0; }}

    .plan-precio {{ display: flex; align-items: baseline; gap: 5px; margin-bottom: 12px; }}
    .precio-monto {{ font-size: 32px; font-weight: 800; color: var(--text);
                     letter-spacing: -1px; font-variant-numeric: tabular-nums; }}
    .precio-clp {{ font-size: 13px; color: var(--muted); font-weight: 500; }}

    .plan-desc {{ font-size: 12.5px; color: var(--muted); line-height: 1.6; margin-bottom: 20px; flex: 1; }}

    .plan-creditos {{
      background: var(--bg); border: 1px solid var(--border); border-radius: 10px;
      padding: 12px 14px; text-align: center; margin-bottom: 18px;
    }}
    .credito-num {{ font-size: 24px; font-weight: 800; color: var(--blue); }}
    .credito-lbl {{ font-size: 11px; color: var(--muted); font-weight: 500; }}

    .btn-comprar {{
      width: 100%; border: none; border-radius: 10px; padding: 12px; font-size: 13px;
      font-weight: 700; cursor: pointer; transition: all .2s;
      background: linear-gradient(135deg, var(--blue-dark), var(--blue));
      color: white; position: relative; overflow: hidden;
    }}
    .btn-comprar::after {{ content: ''; position: absolute; inset: 0;
      background: linear-gradient(135deg, transparent 40%, rgba(255,255,255,.1) 100%);
      pointer-events: none; }}
    .btn-comprar:hover {{ opacity: .9; transform: translateY(-1px);
      box-shadow: 0 6px 20px rgba(59,130,246,.3); }}

    /* ── Pricing footer ────────────────────────────────────────────────── */
    .pricing-footer {{
      text-align: center; margin-top: 36px;
      display: flex; align-items: center; justify-content: center; gap: 16px;
      flex-wrap: wrap;
    }}
    .pricing-footer span {{
      display: inline-flex; align-items: center; gap: 5px;
      color: var(--muted2); font-size: 12px; font-weight: 500;
    }}
    .pricing-footer .dot {{ width: 3px; height: 3px; border-radius: 50%; background: var(--muted2); }}

    /* ── Auto-pay Section ──────────────────────────────────────────────── */
    .section-auto {{
      margin-top: 56px; padding-top: 40px;
      border-top: 1px solid var(--border); position: relative;
    }}
    .section-auto::before {{ content: ''; position: absolute; top: -1px; left: 50%;
      transform: translateX(-50%); width: 120px; height: 1px;
      background: linear-gradient(90deg, transparent, var(--blue), transparent); }}

    .section-auto-title {{ text-align: center; margin-bottom: 28px; }}
    .section-auto-title h2 {{
      font-size: 24px; font-weight: 800; letter-spacing: -0.3px;
      background: linear-gradient(135deg, #f1f5f9, #94a3b8);
      -webkit-background-clip: text; -webkit-text-fill-color: transparent;
    }}
    .section-auto-title p {{ color: var(--muted); font-size: 14px; margin-top: 6px; }}

    .features-row {{
      display: flex; gap: 24px; justify-content: center; margin-top: 14px; flex-wrap: wrap;
    }}
    .feature-chip {{
      display: inline-flex; align-items: center; gap: 6px;
      background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
      padding: 8px 14px; font-size: 12px; color: var(--muted); font-weight: 500;
    }}
    .feature-chip svg {{ flex-shrink: 0; }}

    /* ── Oneclick badge ───────────────────────────────────────────────── */
    .oneclick-badge {{
      border-color: rgba(22,101,52,.5) !important;
      background: linear-gradient(135deg, #0a1f0a, #0c1c1c) !important;
    }}
    .oneclick-badge .card-badge-detail {{ color: #86efac !important; }}

    /* ── Card Form ─────────────────────────────────────────────────────── */
    .card-form-wrapper {{
      max-width: 460px; margin: 0 auto;
      background: var(--surface2); border: 1px solid var(--border); border-radius: 16px;
      padding: 32px 28px; position: relative; overflow: hidden;
    }}
    .card-form-wrapper::before {{ content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px;
      background: linear-gradient(90deg, var(--blue-dark), var(--blue), var(--purple)); }}

    .card-form-title {{ font-size: 16px; font-weight: 700; margin-bottom: 4px; }}
    .card-form-subtitle {{ font-size: 12px; color: var(--muted); margin-bottom: 24px; line-height: 1.5; }}

    .form-group {{ margin-bottom: 16px; }}
    .form-label {{ display: block; font-size: 11px; font-weight: 600; color: #93c5fd;
                   text-transform: uppercase; letter-spacing: .6px; margin-bottom: 7px; }}
    .form-input {{
      width: 100%; background: var(--bg); border: 1px solid var(--border); border-radius: 10px;
      padding: 11px 14px; font-size: 13px; color: var(--text); outline: none;
      transition: all .2s; font-family: inherit;
    }}
    .form-input:focus {{ border-color: var(--blue); box-shadow: 0 0 0 3px rgba(59,130,246,.1); }}
    .form-input::placeholder {{ color: var(--muted2); }}

    /* Verificar pago input row */
    .verificar-input-row {{
      display: flex; gap: 12px; align-items: stretch;
    }}
    .verificar-input {{
      flex: 1; min-width: 0;
    }}
    .btn-verificar {{
      width: auto; padding: 12px 24px; white-space: nowrap; flex-shrink: 0;
    }}
    @media (max-width: 500px) {{
      .verificar-input-row {{
        flex-direction: column; gap: 10px;
      }}
      .btn-verificar {{
        width: 100%;
      }}
    }}

    .consent-box {{
      background: rgba(6,11,24,.6); border: 1px solid var(--border); border-radius: 10px;
      padding: 14px 16px; font-size: 12px; color: var(--muted); line-height: 1.6;
      margin-bottom: 16px; display: flex; gap: 12px; align-items: flex-start;
    }}
    .consent-box input[type=checkbox] {{ margin-top: 3px; accent-color: var(--blue); flex-shrink: 0;
      width: 16px; height: 16px; cursor: pointer; }}
    .consent-box label {{ cursor: pointer; }}

    .btn-registrar {{
      width: 100%; border: none; border-radius: 10px; padding: 13px; font-size: 14px;
      font-weight: 700; cursor: pointer; transition: all .2s;
      background: linear-gradient(135deg, var(--green-dark), var(--green)); color: white;
      display: flex; align-items: center; justify-content: center; gap: 8px;
    }}
    .btn-registrar:hover {{ opacity: .9; box-shadow: 0 6px 20px rgba(34,197,94,.25); }}
    .btn-registrar:disabled {{ opacity: .35; cursor: not-allowed; box-shadow: none; }}

    .card-registered-badge {{
      background: linear-gradient(135deg, #061a06, #0a1f0a);
      border: 1px solid rgba(22,101,52,.5); border-radius: 14px;
      padding: 18px 20px; display: flex; align-items: center; gap: 14px;
      max-width: 460px; margin: 0 auto;
    }}
    .card-registered-badge > div {{ flex: 1; }}
    .card-badge-brand {{ font-size: 15px; font-weight: 700; color: var(--green); }}
    .card-badge-detail {{ font-size: 12px; color: var(--muted); margin-top: 3px; }}
    .btn-borrar-tarjeta {{
      background: transparent; border: 1px solid rgba(127,29,29,.5); border-radius: 8px;
      padding: 6px 14px; color: #f87171; cursor: pointer; font-size: 11px;
      font-weight: 600; flex-shrink: 0; transition: all .2s;
    }}
    .btn-borrar-tarjeta:hover {{ background: rgba(127,29,29,.15); border-color: #991b1b; }}

    /* ── Security badge ────────────────────────────────────────────────── */
    .security-badge {{
      display: flex; align-items: center; justify-content: center; gap: 8px;
      margin-top: 16px; color: var(--muted2); font-size: 11px; font-weight: 500;
    }}
    .security-badge svg {{ opacity: .6; }}

    /* ── Modal ──────────────────────────────────────────────────────────── */
    .modal-overlay {{ display: none; position: fixed; inset: 0; z-index: 1000;
      background: rgba(0,0,0,.6); backdrop-filter: blur(6px);
      align-items: center; justify-content: center; padding: 16px; }}
    .modal-overlay.visible {{ display: flex; }}
    .modal {{
      background: var(--surface2); border: 1px solid var(--border); border-radius: 18px;
      padding: 32px; max-width: 440px; width: 100%;
      box-shadow: 0 24px 80px rgba(0,0,0,.5);
      animation: modalIn .25s ease-out;
    }}
    @keyframes modalIn {{ from {{ opacity: 0; transform: translateY(12px) scale(.97); }}
                          to {{ opacity: 1; transform: translateY(0) scale(1); }} }}
    .modal-title {{ font-size: 17px; font-weight: 700; margin-bottom: 4px; }}
    .modal-subtitle {{ color: var(--muted); font-size: 12px; margin-bottom: 22px; }}
    .modal-actions {{ display: flex; gap: 10px; margin-top: 22px; }}
    .btn-modal-cancel {{ flex: 1; background: transparent; color: var(--muted); border: 1px solid var(--border);
      border-radius: 10px; padding: 11px; font-size: 13px; cursor: pointer; transition: all .2s; font-family: inherit; }}
    .btn-modal-cancel:hover {{ border-color: var(--border-hover); color: var(--text); }}
    .btn-modal-submit {{ flex: 2; background: linear-gradient(135deg, var(--blue-dark), var(--blue));
      color: white; border: none; border-radius: 10px; padding: 11px; font-size: 13px;
      font-weight: 700; cursor: pointer; display: flex; align-items: center; justify-content: center; gap: 6px; }}
    .btn-modal-submit:disabled {{ opacity: .4; cursor: not-allowed; }}
    .error-msg {{ color: #f87171; font-size: 12px; margin-top: 10px; display: none;
      background: rgba(69,10,10,.4); padding: 10px 12px; border-radius: 8px; border: 1px solid rgba(127,29,29,.3); }}

    .spinner {{ width: 14px; height: 14px; border: 2px solid rgba(255,255,255,.3);
      border-top-color: white; border-radius: 50%; animation: spin .7s linear infinite; }}
    @keyframes spin {{ to {{ transform: rotate(360deg); }} }}

    /* ── Page footer ───────────────────────────────────────────────────── */
    .page-footer {{
      margin-top: 56px; text-align: center; color: var(--muted2); font-size: 11px;
      letter-spacing: .3px; padding-bottom: 20px;
    }}

    /* ── Responsive ────────────────────────────────────────────────────── */
    @media (max-width: 768px) {{
      .tabs-nav {{ width: 100%; }}
      .tab-btn {{ flex: 1; text-align: center; padding: 10px 12px; font-size: 12px; }}
      .historial-table {{ font-size: 12px; }}
      .historial-table th, .historial-table td {{ padding: 10px 12px; }}
    }}
    @media (max-width: 480px) {{
      .planes-grid {{ grid-template-columns: 1fr 1fr; }}
      .main {{ padding: 20px 14px 48px; }}
      .section-title h1 {{ font-size: 22px; }}
      .card-form-wrapper {{ padding: 24px 18px; }}
      .estado-grid {{ grid-template-columns: 1fr; }}
    }}
    @media (max-width: 360px) {{
      .planes-grid {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
{test_badge}

<header class="header">
  <div class="header-logo">IX</div>
  <div style="flex:1">
    <div class="header-title">IMAGIAX</div>
    <div class="header-subtitle">An&aacute;lisis de im&aacute;genes m&eacute;dicas con IA</div>
  </div>
  <nav class="header-nav">
    <a href="{viewer_url}">Visor DICOM</a>
  </nav>
</header>

<!-- Tabs Navigation -->
<div class="tabs-container">
  <div class="tabs-nav">
    <button class="tab-btn active" onclick="cambiarTab('planes')">Planes</button>
    <button class="tab-btn" onclick="cambiarTab('cuenta')">Mi cuenta</button>
    <button class="tab-btn" onclick="cambiarTab('historial')">Historial</button>
  </div>
</div>

<main class="main">
  <!-- TAB: Planes -->
  <div id="tab-planes" class="tab-content active">
    {banner}

    <div class="page-title">
      <h1>Planes para centros m&eacute;dicos</h1>
      <p>Paga solo lo que usas. Sin contratos de permanencia. Cancela cuando quieras.</p>
    </div>

    <div class="planes-grid">
      {planes}
    </div>

    <div class="pricing-footer">
      <span>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#475569" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
        Pago seguro
      </span>
      <span class="dot"></span>
      <span>CLP (Peso chileno)</span>
      <span class="dot"></span>
      <span>IVA incluido</span>
      <span class="dot"></span>
      <span>
        <strong style="color:var(--muted)">MercadoPago</strong>
      </span>
    </div>

  </div><!-- /tab-planes -->

  <!-- TAB: Mi cuenta -->
  <div id="tab-cuenta" class="tab-content">
    <div class="section-title">
      <h1>Mi cuenta</h1>
      <p>Administra tu suscripcion, creditos y metodo de pago.</p>
    </div>
    {estado_panel}

    <!-- Seccion Pago Automatico (movida aqui) -->
    <section class="section-auto" style="margin-top:32px;padding-top:32px">
      <div class="section-auto-title">
        <h2>Pago por uso autom&aacute;tico</h2>
        <p>Registra tu tarjeta una sola vez. Se cobra <strong style="color:var(--text)">$990 CLP</strong> de forma autom&aacute;tica por cada informe generado.</p>
        <div class="features-row">
          <div class="feature-chip">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#3b82f6" stroke-width="2"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>
            Sin recargas manuales
          </div>
          <div class="feature-chip">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#3b82f6" stroke-width="2"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg>
            Ideal para uso intensivo
          </div>
          <div class="feature-chip">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#3b82f6" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M8 12l2 2 4-4"/></svg>
            Cancela cuando quieras
          </div>
        </div>
      </div>

      {tarjeta_registrada}
      {oneclick_registrada}

      <!-- Formulario de inscripcion Oneclick (Transbank) -->
      <div id="oneclick-form-section" style="{oneclick_form_display}">
        <div class="card-form-wrapper" style="margin-top:16px">
          <div class="card-form-title">Cobro 1-Click con Transbank Oneclick</div>
          <div class="card-form-subtitle">
            Inscribe tu tarjeta una sola vez en el portal seguro de Transbank.
            Cada informe se cobra autom&aacute;ticamente sin intervenci&oacute;n &mdash; sin CVV, sin pop-ups, sin salir de la p&aacute;gina.
          </div>
          <form id="oneclick-form" onsubmit="return iniciarOneclick(event)">
            <div class="form-group">
              <label class="form-label" for="oc-email">Correo electr&oacute;nico</label>
              <input id="oc-email" class="form-input" type="email" placeholder="doctor@clinica.cl" autocomplete="email" required>
            </div>
            <div class="consent-box">
              <input type="checkbox" id="oc-consent" required>
              <label for="oc-consent">
                Autorizo a <strong>IMAGIAX</strong> a realizar cargos autom&aacute;ticos de <strong>$990 CLP</strong>
                a mi tarjeta por cada informe generado. Puedo desvincular mi tarjeta en cualquier momento.
              </label>
            </div>
            <div id="oc-error" class="error-msg"></div>
            <button type="submit" class="btn-registrar" id="btn-oneclick">
              <span id="btn-oc-text">Inscribir tarjeta en Transbank</span>
              <span id="btn-oc-spinner" class="spinner" style="display:none"></span>
            </button>
            <div class="security-badge">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
              Pago seguro &middot; Procesado por Transbank &middot; Tarjeta protegida
            </div>
          </form>
        </div>
      </div>
    </section>

  </div><!-- /tab-cuenta -->

  <!-- TAB: Historial -->
  <div id="tab-historial" class="tab-content">
    <div class="section-title">
      <h1>Historial de transacciones</h1>
      <p>Registro completo de tus compras e informes generados.</p>
    </div>
    <div id="tab-h-loading" style="text-align:center;padding:40px;color:var(--muted);font-size:13px">Cargando...</div>
    <div id="tab-h-content" style="display:none">
      <div class="historial-section">
        <div class="historial-header">
          <div class="historial-title">Todas las transacciones</div>
          <div class="historial-count" id="tab-h-count">&mdash;</div>
        </div>
        <table class="historial-table">
          <thead>
            <tr>
              <th>Fecha</th><th>Descripci&oacute;n</th><th>Tipo</th>
              <th>Estado</th><th>Monto</th><th>V&iacute;a</th>
            </tr>
          </thead>
          <tbody id="tab-h-tbody"></tbody>
        </table>
      </div>
      <div style="text-align:right;margin-top:12px">
        <a href="{api_base}/historial" style="color:var(--muted);font-size:12px;text-decoration:none;transition:color .2s"
           onmouseover="this.style.color='var(--text)'" onmouseout="this.style.color='var(--muted)'">
          Ver historial completo &rarr;
        </a>
      </div>
    </div>
    <div id="tab-h-error" style="display:none;text-align:center;padding:40px;color:#f87171;font-size:13px">Error al cargar historial.</div>
  </div><!-- /tab-historial -->

  <div class="page-footer">IMAGIAX — Plataforma de an&aacute;lisis radiol&oacute;gico con IA</div>
</main>

<!-- Modal de datos del comprador -->
<div class="modal-overlay" id="modal">
  <div class="modal">
    <div class="modal-title" id="modal-plan-titulo">Completar compra</div>
    <div class="modal-subtitle">Ingresa los datos del centro m&eacute;dico para proceder al pago</div>

    <div class="form-group">
      <label class="form-label" for="inp-email">Correo electr&oacute;nico</label>
      <input id="inp-email" class="form-input" type="email" placeholder="admin@centromedico.cl" autocomplete="email" required>
    </div>
    <div class="form-group">
      <label class="form-label" for="inp-centro">Nombre del centro m&eacute;dico</label>
      <input id="inp-centro" class="form-input" type="text" placeholder="Cl&iacute;nica San Mart&iacute;n" autocomplete="organization" required>
    </div>
    <div class="form-group">
      <label class="form-label" for="inp-nombre">Nombre de contacto <span style="color:var(--muted2)">(opcional)</span></label>
      <input id="inp-nombre" class="form-input" type="text" placeholder="Dr. Juan P&eacute;rez" autocomplete="name">
    </div>

    <div id="error-msg" class="error-msg"></div>

    <div class="modal-actions">
      <button class="btn-modal-cancel" onclick="cerrarModal()">Cancelar</button>
      <button class="btn-modal-submit" id="btn-submit" onclick="procesarPago()">
        <span id="btn-submit-text">Ir a pagar con</span>
        <span id="btn-spinner" class="spinner" style="display:none"></span>
        <strong id="btn-mp-logo">MercadoPago &rarr;</strong>
      </button>
    </div>

    <div class="security-badge" style="margin-top:14px;">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
      Pago 100% seguro &middot; Tarjetas de cr&eacute;dito, d&eacute;bito y cuenta MercadoPago
    </div>
  </div>
</div>

<!-- MercadoPago SDK v2 -->
<script src="https://sdk.mercadopago.com/js/v2"></script>

<script>
  let planSeleccionado = null;
  const apiBase = "{api_base}";
  const mpPublicKey = "{mp_public_key}";

  // ── Tab Navigation ───────────────────────────────────────────────────────────
  function cambiarTab(tabId) {{
    document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
    document.querySelector(`[onclick="cambiarTab('${{tabId}}')"]`).classList.add('active');
    document.querySelectorAll('.tab-content').forEach(tab => tab.classList.remove('active'));
    document.getElementById('tab-' + tabId).classList.add('active');
    history.replaceState(null, '', '#' + tabId);
    if (tabId === 'historial') loadHistorialTab();
  }}

  // ── Historial Tab: carga desde la misma API que /historial ───────────────────
  let _historialTabLoaded = false;
  function loadHistorialTab() {{
    if (_historialTabLoaded) return;
    const tiposMap = {{scan_automatico:'Cobro por informe', pago_unico:'Pago \u00fanico',
      suscripcion:'Suscripci\u00f3n', pack:'Pack', pack_50:'Pack 50 Informes',
      pack_individual:'Escaneo Individual', trial:'Trial'}};
    const estadosMap = {{approved:'Aprobado', rejected:'Rechazado', pending:'Pendiente', error:'Error'}};
    const estadosCls = {{approved:'aprobado', rejected:'rechazado', pending:'pendiente', error:'rechazado'}};
    const origenesMap = {{mercadopago:'MP', transbank:'TBK'}};
    const fdate = iso => {{
      const d = new Date(iso);
      return d.toLocaleDateString('es-CL', {{day:'2-digit', month:'short', year:'numeric'}})
           + ' ' + d.toLocaleTimeString('es-CL', {{hour:'2-digit', minute:'2-digit'}});
    }};
    fetch(apiBase+'/pagos/historial-cobros?periodo=total')
      .then(r => r.json())
      .then(data => {{
        const cobros = data.cobros || [];
        document.getElementById('tab-h-count').textContent = cobros.length + ' registros';
        const tbody = document.getElementById('tab-h-tbody');
        if (!cobros.length) {{
          tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;padding:40px;color:var(--muted)">Sin transacciones</td></tr>';
        }} else {{
          tbody.innerHTML = cobros.map(c => '<tr>'
            + '<td class="historial-fecha">' + fdate(c.created_at) + '</td>'
            + '<td class="historial-desc">' + (c.descripcion || tiposMap[c.tipo] || c.tipo || '&mdash;') + '</td>'
            + '<td class="historial-tipo">' + (tiposMap[c.tipo] || c.tipo || '') + '</td>'
            + '<td><span class="historial-badge ' + (estadosCls[c.estado] || 'rechazado') + '">'
            + (estadosMap[c.estado] || c.estado) + '</span></td>'
            + '<td class="historial-monto">$' + Number(c.monto_clp).toLocaleString('es-CL') + '</td>'
            + '<td class="historial-via">' + (origenesMap[c.origen] || c.origen || 'MP') + '</td>'
            + '</tr>').join('');
        }}
        document.getElementById('tab-h-loading').style.display = 'none';
        document.getElementById('tab-h-content').style.display = 'block';
        _historialTabLoaded = true;
      }})
      .catch(() => {{
        document.getElementById('tab-h-loading').style.display = 'none';
        document.getElementById('tab-h-error').style.display = 'block';
      }});
  }}
  // Load tab from URL hash on page load
  document.addEventListener('DOMContentLoaded', () => {{
    const hash = window.location.hash.slice(1);
    if (['planes', 'cuenta', 'historial'].includes(hash)) {{
      cambiarTab(hash);
    }}
  }});

  // ── Oneclick Inscription (Transbank) ────────────────────────────────────────
  function iniciarOneclick(e) {{
    e.preventDefault();
    const email = document.getElementById('oc-email').value.trim();
    if (!email) return;
    const btn = document.getElementById('btn-oneclick');
    const spinner = document.getElementById('btn-oc-spinner');
    const text = document.getElementById('btn-oc-text');
    const errEl = document.getElementById('oc-error');
    btn.disabled = true;
    text.textContent = 'Conectando con Transbank...';
    spinner.style.display = 'inline-block';
    errEl.style.display = 'none';

    fetch(apiBase + '/tbk/inscribir/iniciar', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ username: email, email: email }}),
    }})
    .then(r => r.json().then(data => ({{ ok: r.ok, data }})))
    .then(({{ ok, data }}) => {{
      if (!ok) throw new Error(data.detail || 'Error al iniciar inscripcion');
      window.location.href = data.redirect_url;
    }})
    .catch(err => {{
      errEl.textContent = err.message;
      errEl.style.display = 'block';
      btn.disabled = false;
      text.textContent = 'Inscribir tarjeta en Transbank';
      spinner.style.display = 'none';
    }});
    return false;
  }}

  // ── Borrar Oneclick ─────────────────────────────────────────────────────────
  async function borrarOneclick() {{
    if (!confirm('Desvincular tarjeta Oneclick?')) return;
    try {{
      const st = await fetch(apiBase + '/tbk/estado').then(r => r.json());
      const user = st.username || 'default_user';
      await fetch(apiBase + '/tbk/tarjeta', {{
        method: 'DELETE',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ username: user }}),
      }});
      window.location.reload();
    }} catch(e) {{
      alert('Error al desvincular: ' + e.message);
    }}
  }}

  // ── MP Card Form — ELIMINADO ─────────────────────────────────────────────────
  // El pago automático ahora se gestiona exclusivamente con Transbank Oneclick.
  // MercadoPago se usa solo para comprar packs y suscripciones (checkout estándar).
  const mp = new MercadoPago(mpPublicKey, {{ locale: 'es-CL' }});

  // Mantener borrarTarjeta para tarjetas MP legacy que ya estén registradas
  async function borrarTarjeta() {{
    if (!confirm("Eliminar la tarjeta MercadoPago guardada? Los cobros automáticos pasarán a usar Oneclick si está inscrito.")) return;
    try {{
      const res = await fetch(apiBase + "/pagos/metodo-pago", {{ method: "DELETE" }});
      if (!res.ok) throw new Error("Error al eliminar la tarjeta");
      window.location.reload();
    }} catch (err) {{
      alert(err.message);
    }}
  }}

  // Cancelar suscripción activa
  async function cancelarSuscripcion() {{
    if (!confirm("Cancelar tu suscripcion mensual? Los creditos restantes seguiran disponibles hasta agotarse.")) return;
    try {{
      const res = await fetch(apiBase + "/pagos/cancelar-suscripcion", {{ method: "POST" }});
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Error al cancelar");
      alert(data.mensaje || "Suscripcion cancelada correctamente.");
      window.location.reload();
    }} catch (err) {{
      alert(err.message);
    }}
  }}

  // ── Modal Planes (packs / suscripción) ───────────────────────────────────────
  function seleccionarPlan(planId) {{
    planSeleccionado = planId;
    const titulos = {{
      pack_individual: "Escaneo Individual — $990 CLP",
      pack_50: "Pack 50 Informes — $24.990 CLP",
      pack_200: "Pack 200 Informes — $69.990 CLP",
      suscripcion_mensual: "Suscripción Mensual — $79.990 CLP/mes",
    }};
    document.getElementById("modal-plan-titulo").textContent = titulos[planId] || "Completar compra";
    document.getElementById("error-msg").style.display = "none";
    document.getElementById("modal").classList.add("visible");
    setTimeout(() => document.getElementById("inp-email").focus(), 100);
  }}

  function cerrarModal() {{
    document.getElementById("modal").classList.remove("visible");
    planSeleccionado = null;
  }}

  async function procesarPago() {{
    const email = document.getElementById("inp-email").value.trim();
    const centro = document.getElementById("inp-centro").value.trim();
    const nombre = document.getElementById("inp-nombre").value.trim();
    const errEl = document.getElementById("error-msg");

    if (!email || !/^[^@]+@[^@]+\.[^@]+$/.test(email)) {{
      errEl.textContent = "Por favor ingresa un correo electrónico válido.";
      errEl.style.display = "block";
      return;
    }}
    if (!centro) {{
      errEl.textContent = "Por favor ingresa el nombre del centro médico.";
      errEl.style.display = "block";
      return;
    }}

    errEl.style.display = "none";
    const btn = document.getElementById("btn-submit");
    btn.disabled = true;
    document.getElementById("btn-submit-text").textContent = "Procesando...";
    document.getElementById("btn-spinner").style.display = "inline-block";
    document.getElementById("btn-mp-logo").style.display = "none";

    try {{
      const res = await fetch(apiBase + "/pagos/crear-sesion", {{
        method: "POST",
        headers: {{"Content-Type": "application/json"}},
        body: JSON.stringify({{
          plan_id: planSeleccionado,
          email: email,
          centro_medico: centro,
          nombre_contacto: nombre || null
        }})
      }});
      const data = await res.json();
      if (!res.ok || !data.checkout_url) {{
        throw new Error(data.detail || "Error al crear la sesión de pago");
      }}
      window.location.href = data.checkout_url;
    }} catch (err) {{
      errEl.textContent = err.message || "Error de conexión. Intente nuevamente.";
      errEl.style.display = "block";
      btn.disabled = false;
      document.getElementById("btn-submit-text").textContent = "Ir a pagar con";
      document.getElementById("btn-spinner").style.display = "none";
      document.getElementById("btn-mp-logo").style.display = "inline";
    }}
  }}

  // Cerrar modal al hacer click fuera
  document.getElementById("modal").addEventListener("click", function(e) {{
    if (e.target === this) cerrarModal();
  }});

  // Enter en campos para enviar
  ["inp-email","inp-centro","inp-nombre"].forEach(id => {{
    const el = document.getElementById(id);
    if (el) el.addEventListener("keydown", e => {{
      if (e.key === "Enter") procesarPago();
    }});
  }});
</script>
</body>
</html>"""
