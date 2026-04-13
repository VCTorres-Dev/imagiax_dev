"""
app.admin.service - Admin business logic.

All functions receive pool: asyncpg.Pool as first argument.
No global pool. admin/ MUST NOT import from pagos or ai modules.
"""

from __future__ import annotations

from typing import Any

import asyncpg

from app.auth import service as auth_service

async def get_dashboard_stats(pool: asyncpg.Pool) -> dict[str, Any]:
    """Estadisticas generales del sistema para el dashboard de admin."""
    usuarios = await auth_service.list_users(pool)
    total_users = len(usuarios)
    active_users = sum(1 for u in usuarios if u["activo"])
    users_by_role = {
        "admin": sum(1 for u in usuarios if u["rol"] == "admin"),
        "medico": sum(1 for u in usuarios if u["rol"] == "medico"),
        "recepcion": sum(1 for u in usuarios if u["rol"] == "recepcion"),
    }
    async with pool.acquire() as conn:
        inf_total = await conn.fetchval("SELECT COUNT(*) FROM informes") or 0
        inf_pendiente = await conn.fetchval("SELECT COUNT(*) FROM informes WHERE estado = 'pendiente'") or 0
        inf_aceptado = await conn.fetchval("SELECT COUNT(*) FROM informes WHERE estado IN ('aceptado', 'editado_aceptado')") or 0
        inf_rechazado = await conn.fetchval("SELECT COUNT(*) FROM informes WHERE estado = 'rechazado'") or 0
        inf_hoy = await conn.fetchval("SELECT COUNT(*) FROM informes WHERE created_at >= NOW() - INTERVAL '24 hours'") or 0
        inf_semana = await conn.fetchval("SELECT COUNT(*) FROM informes WHERE created_at >= NOW() - INTERVAL '7 days'") or 0
        ingresos_total = await conn.fetchval("SELECT COALESCE(SUM(cobro_monto_clp), 0) FROM informes WHERE cobro_estado = 'pagado'") or 0
        try:
            creditos_disponibles = await conn.fetchval(
                "SELECT COALESCE(SUM(creditos_total - creditos_usados), 0) FROM licencias WHERE activa = true"
            ) or 0
            creditos_total = await conn.fetchval(
                "SELECT COALESCE(SUM(creditos_total), 0) FROM licencias"
            ) or 0
        except Exception:
            creditos_disponibles = 0
            creditos_total = 0
    audit_count = await auth_service.count_audit_logs(pool)
    return {
        "usuarios": {
            "total": total_users,
            "activos": active_users,
            "inactivos": total_users - active_users,
            "por_rol": users_by_role,
        },
        "informes": {
            "total": inf_total,
            "pendiente": inf_pendiente,
            "aceptado": inf_aceptado,
            "rechazado": inf_rechazado,
            "hoy": inf_hoy,
            "semana": inf_semana,
        },
        "ingresos_total_clp": ingresos_total,
        "audit_eventos": audit_count,
        "creditos": {
            "disponibles": creditos_disponibles,
            "total_comprados": creditos_total,
        },
    }

async def get_pagos_summary(pool: asyncpg.Pool) -> dict[str, Any]:
    """Resumen de pagos e ingresos (sin acceso a modulo pagos)."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT cobro_metodo, COUNT(*) as cantidad, COALESCE(SUM(cobro_monto_clp), 0) as total_clp"
            " FROM informes WHERE cobro_estado = 'pagado' GROUP BY cobro_metodo ORDER BY total_clp DESC"
        )
        ingresos_por_metodo = [
            {"metodo": r["cobro_metodo"] or "desconocido", "cantidad": r["cantidad"], "total_clp": r["total_clp"]}
            for r in rows
        ]
        total_clp = sum(r["total_clp"] for r in ingresos_por_metodo)
        cobros_fallidos = await conn.fetchval("SELECT COUNT(*) FROM informes WHERE cobro_estado = 'fallido'") or 0
        top_rows = await conn.fetch(
            "SELECT usuario_email, COUNT(*) as total FROM informes"
            " WHERE usuario_email IS NOT NULL GROUP BY usuario_email ORDER BY total DESC LIMIT 10"
        )
        top_medicos = [{"email": r["usuario_email"], "informes": r["total"]} for r in top_rows]
        try:
            licencias_rows = await conn.fetch(
                "SELECT l.tipo, l.creditos_total, l.creditos_usados, l.activa, l.fecha_expiracion,"
                " c.email AS cliente_email, c.nombre_contacto, c.centro_medico,"
                " u.email AS usuario_email, u.nombre AS usuario_nombre"
                " FROM licencias l"
                " LEFT JOIN clientes c ON c.id = l.cliente_id"
                " LEFT JOIN usuarios u ON u.id = l.usuario_id"
                " ORDER BY l.activa DESC, l.created_at DESC LIMIT 20"
            )
            licencias = [dict(r) for r in licencias_rows]
        except Exception:
            licencias = []
    return {
        "ingresos_total_clp": total_clp,
        "ingresos_por_metodo": ingresos_por_metodo,
        "cobros_fallidos": cobros_fallidos,
        "top_medicos": top_medicos,
        "licencias": licencias,
    }

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
