"""app.pagos.mp — MercadoPago payment integration."""
from app.pagos.mp.client import mp_get, mp_post, mp_put, mp_delete
from app.pagos.mp.service import (
    PLANES,
    crear_sesion_pago,
    cobrar_scan_automatico,
    cancelar_preapproval_mp,
    parse_external_reference,
    validar_webhook_signature,
)
from app.pagos.mp.router import router

__all__ = [
    "mp_get", "mp_post", "mp_put", "mp_delete",
    "PLANES", "crear_sesion_pago", "cobrar_scan_automatico",
    "cancelar_preapproval_mp", "parse_external_reference",
    "validar_webhook_signature", "router",
]
