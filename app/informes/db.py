"""
app.informes.db - Informe CRUD with explicit pool injection.

All functions receive pool as first argument.
No global pool references.
"""

from __future__ import annotations

import json
import secrets
import string
import uuid
from datetime import datetime
from typing import Any

import asyncpg
import logging

logger = logging.getLogger("imagiax.informes.db")

ESTADOS_INFORME_VALIDOS = ("pendiente", "aceptado", "rechazado", "editado_aceptado")
ESTADOS_COBRO_VALIDOS = ("pagado", "pendiente", "fallido", "no_aplica")
METODOS_COBRO_VALIDOS = ("creditos", "oneclick", "mp_card", None)

_SHORT_ID_ALPHABET = string.ascii_letters + string.digits
_SHORT_ID_LENGTH = 10


def generate_short_id() -> str:
    """Genera un ID corto URL-safe de 10 caracteres."""
    return "".join(secrets.choice(_SHORT_ID_ALPHABET) for _ in range(_SHORT_ID_LENGTH))


def _row_to_dict(row: asyncpg.Record) -> dict[str, Any]:
    result = {}
    for key, value in dict(row).items():
        if isinstance(value, uuid.UUID):
            result[key] = str(value)
        elif isinstance(value, datetime):
            result[key] = value.isoformat()
        else:
            result[key] = value
    return result


async def buscar_informe_reciente(
    pool: asyncpg.Pool,
    study_instance_uid: str,
    ventana_segundos: int = 120,
    usuario_id: str | None = None,
    pipeline_version: str | None = None,
) -> dict | None:
    """Busca informe reciente para deduplicacion."""
    async with pool.acquire() as conn:
        if usuario_id:
            _uid = uuid.UUID(usuario_id)
            row = await conn.fetchrow(
                """
                SELECT
                    id, study_instance_uid, informe_generado, ai_nivel_alerta,
                    ai_hallazgos, ai_todas_patologias, modelo_llm, estado,
                    cobro_estado, cobro_metodo, cobro_error, created_at,
                    pipeline_version
                FROM informes
                WHERE study_instance_uid = $1
                  AND created_at > NOW() - INTERVAL '1 second' * $2
                  AND usuario_id = $3
                  AND (pipeline_version = $4 OR ($4 IS NULL AND pipeline_version IS NULL))
                ORDER BY created_at DESC
                LIMIT 1
                """,
                study_instance_uid,
                ventana_segundos,
                _uid,
                pipeline_version,
            )
        else:
            row = await conn.fetchrow(
                """
                SELECT
                    id, study_instance_uid, informe_generado, ai_nivel_alerta,
                    ai_hallazgos, ai_todas_patologias, modelo_llm, estado,
                    cobro_estado, cobro_metodo, cobro_error, created_at,
                    pipeline_version
                FROM informes
                WHERE study_instance_uid = $1
                  AND created_at > NOW() - INTERVAL '1 second' * $2
                  AND (pipeline_version = $3 OR ($3 IS NULL AND pipeline_version IS NULL))
                ORDER BY created_at DESC
                LIMIT 1
                """,
                study_instance_uid,
                ventana_segundos,
                pipeline_version,
            )
    if row:
        return {
            "id": str(row["id"]),
            "study_instance_uid": row["study_instance_uid"],
            "informe_generado": row["informe_generado"],
            "ai_nivel_alerta": row["ai_nivel_alerta"],
            "ai_hallazgos": row["ai_hallazgos"],
            "ai_todas_patologias": row["ai_todas_patologias"],
            "modelo_llm": row["modelo_llm"],
            "estado": row["estado"],
            "cobro_estado": row["cobro_estado"],
            "cobro_metodo": row["cobro_metodo"],
            "cobro_error": row["cobro_error"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "pipeline_version": row["pipeline_version"],
        }
    return None


async def guardar_informe(
    pool: asyncpg.Pool,
    *,
    study_instance_uid: str,
    patient_id: str | None,
    patient_name: str | None,
    study_date: str | None,
    modalidad: str | None,
    ai_nivel_alerta: str,
    ai_hallazgos: list,
    ai_todas_patologias: dict,
    informe_generado: str,
    modelo_llm: str = "gemini-2.5-flash",
    usuario_id: str | None = None,
    usuario_email: str | None = None,
    cobro_estado: str = "pendiente",
    cobro_metodo: str | None = None,
    cobro_monto_clp: int | None = None,
    cobro_ref_id: str | None = None,
    cobro_error: str | None = None,
    short_id: str | None = None,
    exam_region: str | None = None,
    exam_modality: str | None = None,
    exam_confidence: float | None = None,
    classification_source: str | None = None,
    # ── v1.1 pipeline fields (ai-pipeline-v1.1, T-03) ────────────────────────
    nota_clinica: str | None = None,
    quality_gate_result: dict | None = None,
    audit_result: dict | None = None,
    pipeline_version: str | None = None,
    pipeline_trace: dict | None = None,
    # ── BUG-008: patient demographic fields ────────────────────────────────────────────
    patient_age: str | None = None,
) -> tuple[str, str]:
    """Inserta informe. Retorna (informe_uuid, short_id).

    v1.1 fields (nota_clinica, quality_gate_result, audit_result,
    pipeline_version, pipeline_trace) are all optional with None defaults
    so existing v1 callers remain unaffected.
    """
    _uid = uuid.UUID(usuario_id) if usuario_id else None
    _short_id = short_id or generate_short_id()
    async with pool.acquire() as conn:
        for _attempt in range(5):
            try:
                row = await conn.fetchrow(
                    """
                    INSERT INTO informes (
                        study_instance_uid, patient_id, patient_name, study_date, modalidad,
                        ai_nivel_alerta, ai_hallazgos, ai_todas_patologias,
                        informe_generado, modelo_llm,
                        usuario_id, usuario_email,
                        cobro_estado, cobro_metodo, cobro_monto_clp, cobro_ref_id, cobro_error,
                        cobro_timestamp,
                        short_id,
                        exam_region, exam_modality, exam_confidence, classification_source,
                        nota_clinica, quality_gate_result, audit_result,
                        pipeline_version, pipeline_trace,
                        patient_age
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb, $9, $10,
                        $11, $12,
                        $13, $14, $15, $16, $17,
                        CASE WHEN $13 IN ('pagado', 'fallido') THEN NOW() ELSE NULL END,
                        $18, $19, $20, $21, $22,
                        $23, $24::jsonb, $25::jsonb, $26, $27::jsonb,
                        $28
                    )
                    RETURNING id, short_id
                    """,
                    study_instance_uid,
                    patient_id,
                    patient_name,
                    study_date,
                    modalidad,
                    ai_nivel_alerta,
                    json.dumps(ai_hallazgos, ensure_ascii=False),
                    json.dumps(ai_todas_patologias, ensure_ascii=False),
                    informe_generado,
                    modelo_llm,
                    _uid,
                    usuario_email,
                    cobro_estado,
                    cobro_metodo,
                    cobro_monto_clp,
                    cobro_ref_id,
                    cobro_error,
                    _short_id,
                    exam_region,
                    exam_modality,
                    exam_confidence,
                    classification_source,
                    nota_clinica,
                    json.dumps(quality_gate_result, ensure_ascii=False) if quality_gate_result is not None else None,
                    json.dumps(audit_result, ensure_ascii=False) if audit_result is not None else None,
                    pipeline_version,
                    json.dumps(pipeline_trace, ensure_ascii=False) if pipeline_trace is not None else None,
                    patient_age,
                )
                return str(row["id"]), row["short_id"]
            except asyncpg.UniqueViolationError:
                _short_id = generate_short_id()
        raise RuntimeError("No se pudo generar short_id unico")


async def actualizar_cobro(
    pool: asyncpg.Pool,
    *,
    informe_id: str,
    cobro_estado: str,
    cobro_metodo: str | None = None,
    cobro_monto_clp: int | None = None,
    cobro_ref_id: str | None = None,
    cobro_error: str | None = None,
) -> bool:
    """Actualiza cobro de informe. Retorna True si se actualizo."""
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE informes
            SET cobro_estado = $2,
                cobro_metodo = COALESCE($3, cobro_metodo),
                cobro_monto_clp = COALESCE($4, cobro_monto_clp),
                cobro_ref_id = COALESCE($5, cobro_ref_id),
                cobro_error = $6,
                cobro_timestamp = NOW()
            WHERE id = $1::uuid
            """,
            informe_id,
            cobro_estado,
            cobro_metodo,
            cobro_monto_clp,
            cobro_ref_id,
            cobro_error,
        )
    return result == "UPDATE 1"


async def actualizar_decision(
    pool: asyncpg.Pool,
    *,
    informe_id: str,
    estado: str,
    informe_final: str,
    notas_rechazo: str | None = None,
    decision_usuario_id: str | None = None,
    decision_usuario_email: str | None = None,
    requiere_propiedad: str | None = None,
) -> dict | None:
    """Actualiza decision medica. requiere_propiedad filtra por propietario."""
    fue_editado = estado == "editado_aceptado"
    _uid = uuid.UUID(decision_usuario_id) if decision_usuario_id else None
    async with pool.acquire() as conn:
        if requiere_propiedad:
            _owner_uid = uuid.UUID(requiere_propiedad)
            row = await conn.fetchrow(
                """
                UPDATE informes
                SET
                    estado                 = $2,
                    informe_final          = $3,
                    fue_editado            = $4,
                    notas_rechazo          = $5,
                    decision_usuario_id    = $6,
                    decision_usuario_email = $7,
                    timestamp_decision     = NOW()
                WHERE id = $1::uuid AND usuario_id = $8
                RETURNING id, study_instance_uid, estado, fue_editado,
                          timestamp_decision, informe_final
                """,
                informe_id,
                estado,
                informe_final,
                fue_editado,
                notas_rechazo,
                _uid,
                decision_usuario_email,
                _owner_uid,
            )
        else:
            row = await conn.fetchrow(
                """
                UPDATE informes
                SET
                    estado                 = $2,
                    informe_final          = $3,
                    fue_editado            = $4,
                    notas_rechazo          = $5,
                    decision_usuario_id    = $6,
                    decision_usuario_email = $7,
                    timestamp_decision     = NOW()
                WHERE id = $1::uuid
                RETURNING id, study_instance_uid, estado, fue_editado,
                          timestamp_decision, informe_final
                """,
                informe_id,
                estado,
                informe_final,
                fue_editado,
                notas_rechazo,
                _uid,
                decision_usuario_email,
            )
    return _row_to_dict(row) if row else None


async def get_informes_por_estudio(
    pool: asyncpg.Pool,
    study_uid: str,
    usuario_id: str | None = None,
) -> list[dict]:
    """Retorna informes de un estudio."""
    async with pool.acquire() as conn:
        if usuario_id:
            rows = await conn.fetch(
                """
                SELECT
                    id, study_instance_uid, patient_id, patient_name,
                    study_date, modalidad, ai_nivel_alerta,
                    ai_hallazgos, ai_todas_patologias,
                    informe_generado, informe_final, modelo_llm,
                    estado, fue_editado, notas_rechazo,
                    usuario_id, usuario_email,
                    timestamp_decision, created_at, patient_age
                FROM informes
                WHERE study_instance_uid = $1 AND usuario_id = $2
                ORDER BY created_at DESC
                """,
                study_uid,
                uuid.UUID(usuario_id),
            )
        else:
            rows = await conn.fetch(
                """
                SELECT
                    id, study_instance_uid, patient_id, patient_name,
                    study_date, modalidad, ai_nivel_alerta,
                    ai_hallazgos, ai_todas_patologias,
                    informe_generado, informe_final, modelo_llm,
                    estado, fue_editado, notas_rechazo,
                    usuario_id, usuario_email,
                    timestamp_decision, created_at, patient_age
                FROM informes
                WHERE study_instance_uid = $1
                ORDER BY created_at DESC
                """,
                study_uid,
            )
    return [_row_to_dict(r) for r in rows]


async def get_informe_por_id(
    pool: asyncpg.Pool,
    informe_id: str,
) -> dict | None:
    """Retorna un informe por UUID."""
    try:
        uid = uuid.UUID(informe_id)
    except ValueError:
        return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                id, study_instance_uid, patient_id, patient_name,
                study_date, modalidad, ai_nivel_alerta,
                ai_hallazgos, informe_generado, informe_final,
                modelo_llm, estado, fue_editado, created_at, usuario_id, usuario_email,
                patient_age, nota_clinica, short_id
            FROM informes
            WHERE id = $1
            """,
            uid,
        )
    return _row_to_dict(row) if row else None


async def get_todos_informes(
    pool: asyncpg.Pool,
    estado: str | None = None,
    usuario_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """Retorna informes paginados con filtros opcionales.

    P4-FIX: Uses COUNT(*) OVER() window function to get total in the same query
    instead of a separate COUNT query (2 round-trips → 1).
    Also uses NULL-coalescing WHERE pattern to avoid f-string SQL (W4 pattern).
    """
    limit = min(limit, 500)
    _uid = uuid.UUID(usuario_id) if usuario_id else None
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, study_instance_uid, patient_id, patient_name, study_date, modalidad,
                   ai_nivel_alerta, ai_hallazgos, ai_todas_patologias, informe_generado, informe_final,
                   modelo_llm, estado, fue_editado, notas_rechazo, usuario_id, usuario_email,
                   cobro_estado, cobro_metodo, cobro_monto_clp, cobro_ref_id,
                   timestamp_decision, created_at, created_at AS timestamp_creacion, patient_age,
                   nota_clinica, short_id,
                   COUNT(*) OVER() AS _total_count
            FROM informes
            WHERE ($1::text IS NULL OR estado = $1)
              AND ($2::uuid IS NULL OR usuario_id = $2)
            ORDER BY created_at DESC
            LIMIT $3 OFFSET $4
            """,
            estado,
            _uid,
            limit,
            offset,
        )
    total = rows[0]["_total_count"] if rows else 0
    items = [{k: v for k, v in _row_to_dict(r).items() if k != "_total_count"} for r in rows]
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": (offset + len(items)) < total,
    }


async def get_study_by_short_id(
    pool: asyncpg.Pool,
    short_id: str,
) -> dict | None:
    """Busca informe por short_id."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, study_instance_uid FROM informes WHERE short_id = $1",
            short_id,
        )
    if row is None:
        return None
    return {
        "informe_id": str(row["id"]),
        "study_instance_uid": row["study_instance_uid"],
    }


async def get_pipeline_trace(
    pool: asyncpg.Pool,
    informe_id: str,
    usuario_id: str,
) -> dict | None:
    """Return pipeline trace metadata for a report (v1.1+).

    Only returns data for reports owned by the requesting user to preserve
    auth scope — no PHI is exposed beyond what the user already has access to.

    Args:
        pool: asyncpg connection pool.
        informe_id: UUID of the informe.
        usuario_id: ID of the authenticated user making the request.

    Returns:
        Dict with pipeline trace fields, or None if not found / not authorized.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                id,
                pipeline_version,
                pipeline_trace,
                quality_gate_result,
                audit_result
            FROM informes
            WHERE id = $1
              AND usuario_id = $2
            """,
            informe_id,
            usuario_id,
        )
    if row is None:
        return None

    def _parse_json_field(val: Any) -> Any:
        if val is None:
            return None
        if isinstance(val, str):
            try:
                return json.loads(val)
            except (json.JSONDecodeError, ValueError):
                return None
        return val

    return {
        "informe_id": str(row["id"]),
        "pipeline_version": row["pipeline_version"],
        "pipeline_trace": _parse_json_field(row["pipeline_trace"]),
        "quality_gate_result": _parse_json_field(row["quality_gate_result"]),
        "audit_result": _parse_json_field(row["audit_result"]),
    }
