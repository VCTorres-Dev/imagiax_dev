"""
app.pagos.mp.client — HTTP helpers for MercadoPago API.

All functions are thin async wrappers over httpx.
They raise HTTPException(502) on non-2xx responses.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import HTTPException

from app.core.config import settings

logger = logging.getLogger("imagiax.pagos.mp.client")


def _access_token() -> str:
    token = settings.MP_ACCESS_TOKEN.get_secret_value()
    if not token:
        raise HTTPException(500, "MP_ACCESS_TOKEN no configurado en .env")
    return token


def _auth_headers(idempotency_key: str = "") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_access_token()}",
        "Content-Type": "application/json",
        "X-Idempotency-Key": idempotency_key,
    }


async def mp_get(path: str) -> dict[str, Any] | list:
    """GET a la API de MercadoPago. Lanza HTTPException(502) si no es 2xx."""
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            f"https://api.mercadopago.com{path}",
            headers=_auth_headers(),
        )
    if resp.status_code not in (200, 201):
        logger.error("MP GET %s → %d: %s", path, resp.status_code, resp.text[:300])
        raise HTTPException(502, f"MercadoPago error {resp.status_code}")
    return resp.json()


async def mp_post(path: str, body: dict, idempotency_key: str = "") -> dict[str, Any]:
    """POST a la API de MercadoPago. Lanza HTTPException(502) si no es 2xx."""
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            f"https://api.mercadopago.com{path}",
            json=body,
            headers=_auth_headers(idempotency_key),
        )
    if resp.status_code not in (200, 201):
        logger.error("MP POST %s → %d: %s", path, resp.status_code, resp.text[:300])
        raise HTTPException(502, f"MercadoPago error {resp.status_code}: {resp.text[:200]}")
    return resp.json()


async def mp_put(path: str, body: dict) -> dict[str, Any]:
    """PUT a la API de MercadoPago (usado para actualizar suscripciones). Lanza HTTPException(502) si no es 2xx."""
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.put(
            f"https://api.mercadopago.com{path}",
            json=body,
            headers=_auth_headers(),
        )
    if resp.status_code not in (200, 201):
        logger.error("MP PUT %s → %d: %s", path, resp.status_code, resp.text[:300])
        raise HTTPException(502, f"MercadoPago error {resp.status_code}: {resp.text[:200]}")
    return resp.json()


async def mp_delete(path: str) -> None:
    """DELETE a la API de MercadoPago (best-effort, no lanza en 404)."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.delete(
            f"https://api.mercadopago.com{path}",
            headers=_auth_headers(),
        )
    if resp.status_code >= 300 and resp.status_code != 404:
        logger.warning("MP DELETE %s → %d (ignorado)", path, resp.status_code)
