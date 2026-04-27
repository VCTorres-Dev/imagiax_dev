"""
app.pagos.tbk.service — Lógica de negocio de Transbank Oneclick Mall.

API pública:
- get_inscription() — construye MallInscription configurada (integration/production)
- get_transaction() — construye MallTransaction configurada (integration/production)
- tbk_call()        — async wrapper para llamadas síncronas al SDK de Transbank
- cobrar_scan_oneclick() — cobro silencioso automático (nunca lanza excepción)
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any
from urllib.parse import unquote

import asyncpg

from app.core.config import settings

from .. import db as pagos_db

from transbank.webpay.oneclick.mall_inscription import MallInscription
from transbank.webpay.oneclick.mall_transaction import MallTransaction
from transbank.webpay.oneclick.request import MallTransactionAuthorizeDetails

logger = logging.getLogger("imagiax.tbk")


# ── Builders de SDK ───────────────────────────────────────────────────────────

def get_inscription() -> MallInscription:
    """Construye un MallInscription apuntando al ambiente correcto."""
    mall_code = settings.TBK_MALL_COMMERCE_CODE
    api_key = settings.TBK_API_KEY.get_secret_value()
    if settings.TBK_ENVIRONMENT == "production":
        return MallInscription.build_for_production(mall_code, api_key)
    return MallInscription.build_for_integration(mall_code, api_key)


def get_transaction() -> MallTransaction:
    """Construye un MallTransaction apuntando al ambiente correcto."""
    mall_code = settings.TBK_MALL_COMMERCE_CODE
    api_key = settings.TBK_API_KEY.get_secret_value()
    if settings.TBK_ENVIRONMENT == "production":
        return MallTransaction.build_for_production(mall_code, api_key)
    return MallTransaction.build_for_integration(mall_code, api_key)


# ── Async wrapper (SEC-AI-002, AD-7) ──────────────────────────────────────────

async def tbk_call(fn, *args, **kwargs) -> Any:
    """
    Ejecuta una función síncrona del SDK de Transbank en un thread pool executor.
    Evita bloquear el event loop de asyncio durante I/O del SDK.
    (SEC-AI-002, AD-7)
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))


# ── Cobro silencioso Oneclick ─────────────────────────────────────────────────

async def cobrar_scan_oneclick(
    *,
    pool: asyncpg.Pool,
    username: str,
    tbk_user: str,
    amount: int | None = None,
    descripcion: str = "IMAGIAX — Informe radiológico IA",
    patient_id: str | None = None,
) -> tuple[bool, str | None, str | None]:
    """
    Cobra silenciosamente usando Oneclick Mall.
    NUNCA lanza excepción — el análisis médico siempre se entrega.

    Si amount es None, usa settings.PRECIO_SCAN_CLP.

    Retorna (aprobado: bool, buy_order: str | None, error_msg: str | None).
    """
    charge_amount = amount if amount is not None else settings.IMAGIAX_PRECIO_SCAN

    # Sanitizar: Transbank requiere el username exacto con que se inscribió
    username = unquote(username)
    parent_buy_order = f"IMG-{uuid.uuid4().hex[:16]}"
    child_buy_order = f"IMGc-{uuid.uuid4().hex[:14]}"

    try:
        transaction = get_transaction()
        details = MallTransactionAuthorizeDetails(
            commerce_code=settings.TBK_CHILD_COMMERCE_CODE,
            buy_order=child_buy_order,
            installments_number=1,
            amount=int(charge_amount),
        )

        resp = await tbk_call(
            transaction.authorize,
            username=username,
            tbk_user=tbk_user,
            parent_buy_order=parent_buy_order,
            details=details,
        )

        # Analizar respuesta
        resp_details = resp.get("details", [])

        child_status = None
        child_response_code = None
        child_auth_code = None
        if resp_details and len(resp_details) > 0:
            child = resp_details[0]
            child_status = child.get("status", "unknown")
            child_response_code = child.get("response_code")
            child_auth_code = child.get("authorization_code")

        aprobado = child_response_code == 0 and child_status == "AUTHORIZED"

        # Registrar en DB
        try:
            await pagos_db.registrar_pago_tbk(
                pool,
                username=username,
                buy_order=parent_buy_order,
                child_buy_order=child_buy_order,
                authorization_code=child_auth_code,
                amount=charge_amount,
                status="approved" if aprobado else "rejected",
                response_code=child_response_code,
                raw_response=resp,
                patient_id=patient_id,
            )
        except Exception as db_exc:
            logger.warning("No se pudo registrar cobro TBK en DB: %s", db_exc)

        if aprobado:
            logger.info(
                "Cobro Oneclick aprobado: $%d CLP, order=%s, auth=%s",
                charge_amount,
                parent_buy_order,
                child_auth_code,
            )
            return True, parent_buy_order, None
        else:
            error_msg = (
                f"Cobro rechazado por Transbank "
                f"(code={child_response_code}, status={child_status})"
            )
            logger.warning(
                "Cobro Oneclick rechazado: %s — resp=%s", error_msg, resp
            )
            return False, parent_buy_order, error_msg

    except Exception as exc:
        logger.error("Excepción en cobro Oneclick: %s", exc)
        # Registrar intento fallido
        try:
            await pagos_db.registrar_pago_tbk(
                pool,
                username=username,
                buy_order=parent_buy_order,
                child_buy_order=child_buy_order,
                amount=charge_amount,
                status="error",
                raw_response={"error": str(exc)},
                patient_id=patient_id,
            )
        except Exception:
            pass
        return False, None, "Error interno al procesar cobro Transbank"
