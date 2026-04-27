"""
app.admin.service - Admin business logic.

All functions receive pool: asyncpg.Pool as first argument.
No global pool. admin/ MUST NOT import from pagos or ai modules.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import asyncpg

# ── Simple TTL cache — single-process safe. For multi-worker, use Redis. ──────
_CACHE_TTL = 30  # seconds

_dashboard_cache: dict = {"data": None, "ts": 0.0}
_pagos_cache: dict = {"data": None, "ts": 0.0}

async def get_dashboard_stats(pool: asyncpg.Pool) -> dict[str, Any]:
    """Estadisticas generales del sistema para el dashboard de admin.

    PERF-C1: Consolidado en 2 queries (1 CTE principal + 1 fallback para
    licencias) en lugar de 9 queries secuenciales separados.
    Elimina la carga completa de usuarios a memoria — usa COUNT(*) FILTER.
    PERF-MED-4: TTL cache de 30s — evita hit a DB en cada render del dashboard.
    """
    now = time.monotonic()
    if _dashboard_cache["data"] is not None and (now - _dashboard_cache["ts"]) < _CACHE_TTL:
        return _dashboard_cache["data"]

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            WITH user_counts AS (
                SELECT
                    COUNT(*)                                        AS total,
                    COUNT(*) FILTER (WHERE activo = TRUE)          AS activos,
                    COUNT(*) FILTER (WHERE rol = 'admin')          AS admins,
                    COUNT(*) FILTER (WHERE rol = 'medico')         AS medicos,
                    COUNT(*) FILTER (WHERE rol = 'recepcion')      AS recepcion
                FROM usuarios
            ),
            informe_counts AS (
                SELECT
                    COUNT(*)                                                          AS total,
                    COUNT(*) FILTER (WHERE estado = 'pendiente')                     AS pendiente,
                    COUNT(*) FILTER (WHERE estado IN ('aceptado', 'editado_aceptado')) AS aceptado,
                    COUNT(*) FILTER (WHERE estado = 'rechazado')                     AS rechazado,
                    COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '24 hours') AS hoy,
                    COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '7 days')   AS semana,
                    COALESCE(SUM(cobro_monto_clp) FILTER (WHERE cobro_estado = 'pagado'), 0) AS ingresos_total
                FROM informes
            ),
            audit_count AS (
                SELECT COUNT(*) AS total FROM sesiones_audit
            )
            SELECT
                uc.total            AS u_total,
                uc.activos          AS u_activos,
                uc.admins           AS u_admins,
                uc.medicos          AS u_medicos,
                uc.recepcion        AS u_recepcion,
                ic.total            AS inf_total,
                ic.pendiente        AS inf_pendiente,
                ic.aceptado         AS inf_aceptado,
                ic.rechazado        AS inf_rechazado,
                ic.hoy              AS inf_hoy,
                ic.semana           AS inf_semana,
                ic.ingresos_total   AS ingresos_total,
                ac.total            AS audit_total
            FROM user_counts uc, informe_counts ic, audit_count ac
            """
        )

        # Licencias: wrapped separately — tabla puede no existir en todos los entornos
        try:
            lic_row = await conn.fetchrow(
                """
                SELECT
                    COALESCE(SUM(creditos_total - creditos_usados) FILTER (WHERE activa = TRUE), 0) AS disponibles,
                    COALESCE(SUM(creditos_total), 0) AS total_comprados
                FROM licencias
                """
            )
            creditos_disponibles = int(lic_row["disponibles"]) if lic_row else 0
            creditos_total_comprados = int(lic_row["total_comprados"]) if lic_row else 0
        except Exception:
            creditos_disponibles = 0
            creditos_total_comprados = 0

    total_users = int(row["u_total"])
    active_users = int(row["u_activos"])

    result = {
        "usuarios": {
            "total": total_users,
            "activos": active_users,
            "inactivos": total_users - active_users,
            "por_rol": {
                "admin": int(row["u_admins"]),
                "medico": int(row["u_medicos"]),
                "recepcion": int(row["u_recepcion"]),
            },
        },
        "informes": {
            "total": int(row["inf_total"]),
            "pendiente": int(row["inf_pendiente"]),
            "aceptado": int(row["inf_aceptado"]),
            "rechazado": int(row["inf_rechazado"]),
            "hoy": int(row["inf_hoy"]),
            "semana": int(row["inf_semana"]),
        },
        "ingresos_total_clp": int(row["ingresos_total"]),
        "audit_eventos": int(row["audit_total"]),
        "creditos": {
            "disponibles": creditos_disponibles,
            "total_comprados": creditos_total_comprados,
        },
    }
    _dashboard_cache["data"] = result
    _dashboard_cache["ts"] = time.monotonic()
    return result

async def _get_cobros_por_metodo(pool: asyncpg.Pool) -> list[dict]:
    """Ingresos agrupados por metodo de cobro (helper para gather)."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT cobro_metodo, COUNT(*) as cantidad, COALESCE(SUM(cobro_monto_clp), 0) as total_clp"
            " FROM informes WHERE cobro_estado = 'pagado' GROUP BY cobro_metodo ORDER BY total_clp DESC"
        )
    return [
        {"metodo": r["cobro_metodo"] or "desconocido", "cantidad": r["cantidad"], "total_clp": r["total_clp"]}
        for r in rows
    ]


async def _get_cobros_fallidos(pool: asyncpg.Pool) -> int:
    """Conteo de cobros fallidos (helper para gather)."""
    async with pool.acquire() as conn:
        result = await conn.fetchval(
            "SELECT COUNT(*) FROM informes WHERE cobro_estado = 'fallido'"
        )
    return result or 0


async def _get_top_medicos(pool: asyncpg.Pool) -> list[dict]:
    """Top 10 medicos por cantidad de informes (helper para gather)."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT usuario_email, COUNT(*) as total FROM informes"
            " WHERE usuario_email IS NOT NULL GROUP BY usuario_email ORDER BY total DESC LIMIT 10"
        )
    return [{"email": r["usuario_email"], "informes": r["total"]} for r in rows]


async def _get_licencias_activas(pool: asyncpg.Pool) -> list[dict]:
    """Licencias activas con datos de cliente/usuario (helper para gather)."""
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT l.tipo, l.creditos_total, l.creditos_usados, l.activa, l.fecha_expiracion,"
                " c.email AS cliente_email, c.nombre_contacto, c.centro_medico,"
                " u.email AS usuario_email, u.nombre AS usuario_nombre"
                " FROM licencias l"
                " LEFT JOIN clientes c ON c.id = l.cliente_id"
                " LEFT JOIN usuarios u ON u.id = l.usuario_id"
                " ORDER BY l.activa DESC, l.created_at DESC LIMIT 20"
            )
        return [dict(r) for r in rows]
    except Exception:
        return []


async def get_pagos_summary(pool: asyncpg.Pool) -> dict[str, Any]:
    """Resumen de pagos e ingresos (sin acceso a modulo pagos).

    PERF-H1: Las 4 queries se ejecutan en paralelo con asyncio.gather().
    Cada helper adquiere su propia conexion del pool — no comparten conn.
    PERF-MED-4: TTL cache de 30s — evita hit a DB en cada render del dashboard.
    """
    now = time.monotonic()
    if _pagos_cache["data"] is not None and (now - _pagos_cache["ts"]) < _CACHE_TTL:
        return _pagos_cache["data"]

    cobros_metodo, cobros_fallidos, top_medicos, licencias = await asyncio.gather(
        _get_cobros_por_metodo(pool),
        _get_cobros_fallidos(pool),
        _get_top_medicos(pool),
        _get_licencias_activas(pool),
    )
    total_clp = sum(r["total_clp"] for r in cobros_metodo)
    result = {
        "ingresos_total_clp": total_clp,
        "ingresos_por_metodo": cobros_metodo,
        "cobros_fallidos": cobros_fallidos,
        "top_medicos": top_medicos,
        "licencias": licencias,
    }
    _pagos_cache["data"] = result
    _pagos_cache["ts"] = time.monotonic()
    return result

def serialize_user(u: dict) -> dict:
    """Serializa un registro de usuario para la API."""
    return {
        "id": str(u["id"]),
        "email": u["email"],
        "nombre": u["nombre"],
        "rol": u["rol"],
        "activo": u["activo"],
        "created_at": u["created_at"].isoformat() if u.get("created_at") else None,
    }
