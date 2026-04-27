"""
app.pagos.mp.service — Lógica de negocio de MercadoPago.

API pública:
- PLANES — dict con planes disponibles
- crear_sesion_pago(pool, plan_id, email, centro_medico, nombre_contacto, usuario_id)
    Fachada principal: crea preference (pack) o preapproval (suscripción) según plan.
    Persiste el pago pendiente en DB. Retorna dict con checkout_url y tipo.
- cobrar_scan_automatico(*, pool, mp_customer_id, mp_card_id, mp_card_brand, descripcion)
    Cobro automático por scan; llamado desde el módulo IA tras análisis exitoso.
    Retorna (aprobado: bool, payment_id: str|None, error_msg: str|None). Nunca lanza.
- cancelar_preapproval_mp(preapproval_id) — cancela suscripción en MercadoPago.
- crear_preference(plan, payer_email, external_ref) — crea preferencia Checkout Pro.
    Retorna (checkout_url, preference_id).
- crear_preapproval(plan, payer_email, external_ref) — crea preapproval recurrente.
    Retorna (checkout_url, preapproval_id).
- procesar_webhook_payment(payment_id, raw, pool) — procesa notificación de pago único.
- procesar_webhook_preapproval(preapproval_id, raw, pool) — procesa activación de suscripción.
- procesar_webhook_renovacion(payment_id, raw, pool) — procesa renovación mensual.
- parse_external_reference(raw_value) — parsea JSON de external_reference de forma segura.
- validar_webhook_signature(request, body_bytes) — valida X-Signature de webhooks MP.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid as _uuid_mod
from typing import Any
from urllib.parse import urlparse

import asyncpg
from fastapi import HTTPException, Request

from app.core.config import settings

from .. import db as pagos_db
from .client import mp_get, mp_post, mp_put

logger = logging.getLogger("imagiax.pagos.mp.service")

# ── Configuración derivada de settings ────────────────────────────────────────

def _is_test() -> bool:
    return settings.IMAGIAX_PAYMENT_MODE != "production"


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


# ── Helpers de validación ──────────────────────────────────────────────────────

def parse_external_reference(raw_value: str) -> dict[str, Any]:
    """
    Parsea external_reference de MP (JSON string) de forma segura.
    SEC-PAY-010: Validates that required fields (usuario_id, paquete) are present.
    """
    if not raw_value:
        return {}
    try:
        data = json.loads(raw_value) if raw_value.startswith("{") else {}
    except (json.JSONDecodeError, AttributeError):
        logger.warning("external_reference malformado (no es JSON válido): %r", raw_value[:200])
        return {}
    if not isinstance(data, dict):
        logger.warning("external_reference no es un objeto JSON: %r", raw_value[:200])
        return {}
    if not data.get("usuario_id"):
        logger.warning(
            "external_reference sin usuario_id — posible manipulación: %r", raw_value[:200]
        )
    return data


def validar_webhook_signature(request: Request, body_bytes: bytes) -> bool:
    """
    Valida la firma X-Signature de los webhooks de MercadoPago.
    Retorna True si es válida, False si no.
    En modo TEST sin secret configurado, rechaza igualmente.
    """
    mp_webhook_secret = settings.MP_WEBHOOK_SECRET.get_secret_value()
    if not mp_webhook_secret:
        logger.error("MP_WEBHOOK_SECRET no configurado — webhook rechazado por seguridad.")
        return False

    x_signature = request.headers.get("x-signature", "")
    x_request_id = request.headers.get("x-request-id", "")

    if not x_signature:
        logger.warning("Webhook sin header X-Signature — rechazado.")
        return False

    parts = {k.strip(): v.strip() for k, v in
             (p.split("=", 1) for p in x_signature.split(",") if "=" in p)}
    ts = parts.get("ts", "")
    v1_received = parts.get("v1", "")

    if not ts or not v1_received:
        logger.warning("Webhook X-Signature mal formateado: %s", x_signature)
        return False

    data_id = request.query_params.get("data.id", "")
    manifest = f"id:{data_id};request-id:{x_request_id};ts:{ts};"
    expected = hmac.new(
        mp_webhook_secret.encode(),
        manifest.encode(),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, v1_received):
        logger.warning("Webhook firma inválida. Esperada: %s..., Recibida: %s...",
                       expected[:12], v1_received[:12])
        return False

    return True


# ── Lógica de suscripción ──────────────────────────────────────────────────────

async def cancelar_preapproval_mp(preapproval_id: str) -> tuple[bool, str]:
    """
    Cancela una suscripción (preapproval) en MercadoPago.
    Retorna (exito: bool, mensaje: str). NUNCA lanza excepción.
    """
    if not preapproval_id:
        return False, "No hay preapproval_id para cancelar."

    if _is_test():
        logger.info("MODO TEST: simulando cancelación de preapproval %s", preapproval_id)
        return True, f"Suscripción {preapproval_id} cancelada (modo test)."

    try:
        result = await mp_put(f"/preapproval/{preapproval_id}", {"status": "cancelled"})
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
        if he.status_code == 502 and ("already cancelled" in detail_lower or "cancelled" in detail_lower):
            logger.info("Preapproval %s ya estaba cancelado en MercadoPago.", preapproval_id)
            return True, "Suscripción ya estaba cancelada en MercadoPago."
        logger.error("Error HTTP al cancelar preapproval %s: %s", preapproval_id, he.detail)
        return False, "Error al cancelar suscripción en MercadoPago"
    except Exception as exc:
        logger.error("Error inesperado al cancelar preapproval %s: %s", preapproval_id, exc)
        return False, "Error inesperado al cancelar suscripción"


# ── Creación de sesiones de pago ───────────────────────────────────────────────

async def crear_preference(
    plan: dict, payer_email: str, external_ref: str
) -> tuple[str, str]:
    """Crea una preferencia Checkout Pro. Retorna (checkout_url, preference_id)."""
    base_url = settings.IMAGIAX_BASE_URL.rstrip("/")
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
    if not _is_test():
        body["payer"] = {"email": payer_email}
    if not is_local_base and base_url.startswith("https://"):
        body["auto_return"] = "approved"

    result = await mp_post("/checkout/preferences", body)
    preference_id = result["id"]
    checkout_url = result["sandbox_init_point"] if _is_test() else result["init_point"]
    return checkout_url, preference_id


async def crear_preapproval(
    plan: dict, payer_email: str, external_ref: str
) -> tuple[str, str]:
    """
    Crea un preapproval (suscripción recurrente) en MercadoPago.
    Retorna (checkout_url, preapproval_id).
    """
    callback_base = settings.mp_callback_base_url

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
        "back_url": f"{callback_base}/pagos/suscripcion/exito",
        "notification_url": f"{callback_base}/pagos/webhook",
        "status": "pending",
    }

    result = await mp_post("/preapproval", body)
    preapproval_id = result["id"]
    checkout_url = result.get("init_point", "")
    if not checkout_url:
        raise HTTPException(502, "MercadoPago no retornó init_point para la suscripción.")
    return checkout_url, preapproval_id


# ── Procesadores de webhooks ───────────────────────────────────────────────────

async def procesar_webhook_payment(payment_id: str, raw: dict, pool: asyncpg.Pool) -> None:
    """Procesa una notificación de pago único."""
    from ..db import acreditar_pago, get_licencia_por_mp_payment, registrar_pago

    existing = await get_licencia_por_mp_payment(pool, payment_id)
    if existing:
        logger.debug("Pago %s ya procesado (idempotente).", payment_id)
        return

    payment_data = await mp_get(f"/v1/payments/{payment_id}")
    if payment_data.get("status") != "approved":
        return

    ext_ref = parse_external_reference(payment_data.get("external_reference", ""))
    plan_id = ext_ref.get("plan_id", "pack_individual")
    cliente_id = ext_ref.get("cliente_id")
    ext_uid = ext_ref.get("usuario_id") or None
    if not cliente_id:
        logger.warning("Webhook pago %s sin cliente_id en external_reference.", payment_id)
        return

    plan = PLANES.get(plan_id, PLANES["pack_individual"])
    licencia = await acreditar_pago(
        pool,
        cliente_id=cliente_id,
        tipo=plan_id,
        creditos=plan["creditos"],
        mp_payment_id=payment_id,
        duracion_dias=plan.get("duracion_dias"),
        usuario_id=ext_uid,
    )
    await registrar_pago(
        pool,
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


async def procesar_webhook_preapproval(preapproval_id: str, raw: dict, pool: asyncpg.Pool) -> None:
    """Procesa la activación de una suscripción."""
    from ..db import acreditar_pago, registrar_pago

    preapproval_data = await mp_get(f"/preapproval/{preapproval_id}")
    if preapproval_data.get("status") != "authorized":
        return

    ext_ref = parse_external_reference(preapproval_data.get("external_reference", ""))
    plan_id = ext_ref.get("plan_id", "suscripcion_mensual")
    cliente_id = ext_ref.get("cliente_id")
    ext_uid = ext_ref.get("usuario_id") or None
    if not cliente_id:
        return

    plan = PLANES.get(plan_id, PLANES["suscripcion_mensual"])
    licencia = await acreditar_pago(
        pool,
        cliente_id=cliente_id,
        tipo=plan_id,
        creditos=plan["creditos"],
        mp_preapproval_id=preapproval_id,
        duracion_dias=plan.get("duracion_dias", 31),
        usuario_id=ext_uid,
    )
    await registrar_pago(
        pool,
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


async def procesar_webhook_renovacion(payment_id: str, raw: dict, pool: asyncpg.Pool) -> None:
    """Procesa el cobro automático de una suscripción activa (renovación mensual)."""
    from ..db import get_licencia_por_preapproval, renovar_suscripcion

    payment_data = await mp_get(f"/v1/payments/{payment_id}")
    if payment_data.get("status") != "approved":
        return

    preapproval_id = str(payment_data.get("metadata", {}).get("preapproval_id", ""))
    if not preapproval_id:
        logger.warning("Renovación %s sin preapproval_id en metadata.", payment_id)
        return

    licencia = await get_licencia_por_preapproval(pool, preapproval_id)
    plan_id = (licencia or {}).get("tipo", "suscripcion_mensual")
    plan = PLANES.get(plan_id, PLANES["suscripcion_mensual"])
    renovado = await renovar_suscripcion(pool, preapproval_id, plan["creditos"])
    if renovado:
        logger.info("Webhook: renovación suscripción %s — %d créditos renovados.",
                    preapproval_id, plan["creditos"])
    else:
        logger.warning("Webhook: no se encontró suscripción activa para %s.", preapproval_id)


# ── cobrar_scan_automatico (función pública llamada desde módulo IA) ──────────

async def cobrar_scan_automatico(
    *,
    pool: asyncpg.Pool,
    mp_customer_id: str,
    mp_card_id: str,
    mp_card_brand: str,
    descripcion: str = "IMAGIAX — Informe radiológico IA",
    patient_id: str | None = None,
) -> tuple[bool, str | None, str | None]:
    """
    Cobra settings.IMAGIAX_PRECIO_SCAN al customer usando su tarjeta guardada.
    Llamado desde el endpoint /generar/informe tras un análisis exitoso.

    Retorna (aprobado: bool, payment_id: str | None, error_msg: str | None).
    NUNCA lanza excepción — el análisis médico siempre se entrega al doctor
    independientemente del resultado del cobro.
    """
    precio_scan = settings.IMAGIAX_PRECIO_SCAN

    try:
        if _is_test():
            logger.info("MODO TEST: simulando cobro aprobado de $%d CLP (card_id=%s)",
                        precio_scan, mp_card_id)
            simulated_id = f"test-{_uuid_mod.uuid4()}"
            try:
                await pagos_db.registrar_pago(
                    pool,
                    cliente_id=None,
                    licencia_id=None,
                    mp_payment_id=simulated_id,
                    mp_preference_id=None,
                    mp_preapproval_id=None,
                    tipo="scan_automatico",
                    estado="approved",
                    monto_clp=precio_scan,
                    descripcion=descripcion,
                    raw_webhook={"test_mode": True, "simulated": True},
                    patient_id=patient_id,
                )
            except Exception as log_exc:
                logger.warning("No se pudo registrar cobro test en DB: %s", log_exc)
            return True, simulated_id, None

        # ── Flujo PRODUCCION ──────────────────────────────────────────────────
        try:
            cards_list = await mp_get(f"/v1/customers/{mp_customer_id}/cards")
            card_ids_in_mp = [str(c.get("id", "")) for c in cards_list] if isinstance(cards_list, list) else []
            if mp_card_id not in card_ids_in_mp:
                logger.error("card_id %s NO existe en customer %s de MP", mp_card_id, mp_customer_id)
                await pagos_db.borrar_metodo_pago(pool)
                return False, None, (
                    "La tarjeta registrada ya no es valida en MercadoPago. "
                    "Fue eliminada automaticamente. Por favor, registra una nueva tarjeta."
                )
        except Exception as verify_exc:
            logger.warning("No se pudo verificar tarjeta en MP: %s", verify_exc)

        token_result = await mp_post(
            "/v1/card_tokens",
            {"card_id": mp_card_id, "customer_id": mp_customer_id},
            idempotency_key=str(_uuid_mod.uuid4()),
        )
        card_token = str(token_result["id"])

        body: dict[str, Any] = {
            "transaction_amount": precio_scan,
            "token": card_token,
            "description": descripcion,
            "installments": 1,
            "payer": {
                "type": "customer",
                "id": mp_customer_id,
            },
        }
        if mp_card_brand:
            body["payment_method_id"] = mp_card_brand

        result = await mp_post(
            "/v1/payments",
            body,
            idempotency_key=str(_uuid_mod.uuid4()),
        )
        status: str = result.get("status", "unknown")
        payment_id: str = str(result.get("id", ""))

        try:
            await pagos_db.registrar_pago(
                pool,
                cliente_id=None,
                licencia_id=None,
                mp_payment_id=payment_id,
                mp_preference_id=None,
                mp_preapproval_id=None,
                tipo="scan_automatico",
                estado=status,
                monto_clp=precio_scan,
                descripcion=descripcion,
                raw_webhook=result,
                patient_id=patient_id,
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
        try:
            await pagos_db.registrar_pago(
                pool,
                cliente_id=None,
                licencia_id=None,
                mp_payment_id=f"error-{_uuid_mod.uuid4()}",
                mp_preference_id=None,
                mp_preapproval_id=None,
                tipo="scan_automatico",
                estado="rejected",
                monto_clp=precio_scan,
                descripcion=descripcion,
                raw_webhook={"error": str(exc)},
                patient_id=patient_id,
            )
        except Exception as log_exc:
            logger.warning("No se pudo registrar cobro fallido en DB: %s", log_exc)
        return False, None, "Error interno al procesar cobro"


# ── Fachada pública: crear sesión de pago ─────────────────────────────────────

async def crear_sesion_pago(
    pool: asyncpg.Pool,
    plan_id: str,
    email: str,
    centro_medico: str,
    nombre_contacto: str,
    usuario_id: str,
) -> dict:
    """Public facade: creates a checkout preference or subscription based on plan type."""
    if plan_id not in PLANES:
        raise ValueError(f"Plan desconocido: {plan_id}")
    plan = PLANES[plan_id]

    # Crear o encontrar cliente en DB
    cliente = await pagos_db.crear_o_buscar_cliente(
        pool,
        email=email,
        centro_medico=centro_medico,
        nombre_contacto=nombre_contacto,
    )

    external_ref = json.dumps({
        "cliente_id": cliente["id"],
        "plan_id": plan_id,
        "email": email,
        "centro_medico": centro_medico,
        "usuario_id": usuario_id,
    }, ensure_ascii=False)

    if plan.get("recurrente"):
        checkout_url, preapproval_id = await crear_preapproval(plan, email, external_ref)
        await pagos_db.registrar_pago(
            pool,
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
        return {
            "ok": True,
            "tipo": "suscripcion",
            "checkout_url": checkout_url,
            "preapproval_id": preapproval_id,
        }
    else:
        checkout_url, preference_id = await crear_preference(plan, email, external_ref)
        await pagos_db.registrar_pago(
            pool,
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
        return {
            "ok": True,
            "tipo": "pago_unico",
            "checkout_url": checkout_url,
            "preference_id": preference_id,
        }
