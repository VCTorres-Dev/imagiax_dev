"""
IMAGIAX — Módulo de base de datos (PostgreSQL via asyncpg)

Gestiona la persistencia de informes médicos generados por IA y las
decisiones del médico (aceptado, rechazado, editado_aceptado).

Esquema de la tabla `informes`:
  - id                  UUID PK
  - study_instance_uid  TEXT  (DICOM StudyInstanceUID)
  - patient_id          TEXT
  - patient_name        TEXT
  - study_date          TEXT  (YYYYMMDD)
  - modalidad           TEXT  (ej: DX, CR)
  - ai_nivel_alerta     TEXT  (NORMAL | BAJA | MEDIA | ALTA)
  - ai_hallazgos        JSONB (lista de hallazgos)
  - ai_todas_patologias JSONB (probabilidades de las 18 patologías)
  - informe_generado    TEXT  (informe original del LLM)
  - informe_final       TEXT  (informe aceptado, posiblemente editado)
  - modelo_llm          TEXT  (ej: gemini-2.5-flash)
  - estado              TEXT  (pendiente | aceptado | rechazado | editado_aceptado)
  - fue_editado         BOOL
  - notas_rechazo       TEXT  (comentario opcional al rechazar)
  - timestamp_decision  TIMESTAMPTZ
  - created_at          TIMESTAMPTZ
  
  Campos de cobro (integrados para trazabilidad completa):
  - cobro_estado        TEXT  (pagado | pendiente | fallido | no_aplica)
  - cobro_metodo        TEXT  (creditos | oneclick | mp_card | NULL)
  - cobro_monto_clp     INTEGER
  - cobro_ref_id        TEXT  (buy_order o payment_id)
  - cobro_error         TEXT
  - cobro_timestamp     TIMESTAMPTZ
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

import asyncpg

# -----------------------------------------------------------------------
# Pool global — inicializado por init_pool() en el lifespan de la API
# -----------------------------------------------------------------------
_pool: asyncpg.Pool | None = None


async def init_pool(dsn: str) -> None:
    """Crea el connection pool y garantiza que el schema existe."""
    global _pool
    _pool = await asyncpg.create_pool(dsn=dsn, min_size=2, max_size=10)
    await _create_tables()
    print("IMAGIAX DB: conexión PostgreSQL establecida y schema listo.")


async def close_pool() -> None:
    """Cierra el connection pool al apagar la API."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        print("IMAGIAX DB: conexión PostgreSQL cerrada.")


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Pool de base de datos no inicializado. Verifica POSTGRES_URL.")
    return _pool


# -----------------------------------------------------------------------
# Schema
# -----------------------------------------------------------------------

# Estados válidos para el informe médico
ESTADOS_INFORME_VALIDOS = ("pendiente", "aceptado", "rechazado", "editado_aceptado")
# Estados válidos para el cobro
ESTADOS_COBRO_VALIDOS = ("pagado", "pendiente", "fallido", "no_aplica")
# Métodos de cobro válidos
METODOS_COBRO_VALIDOS = ("creditos", "oneclick", "mp_card", None)


async def _create_tables() -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        # Tabla principal de informes con campos de cobro integrados
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS informes (
                id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                study_instance_uid  TEXT NOT NULL,
                patient_id          TEXT,
                patient_name        TEXT,
                study_date          TEXT,
                modalidad           TEXT,
                ai_nivel_alerta     TEXT NOT NULL,
                ai_hallazgos        JSONB NOT NULL DEFAULT '[]',
                ai_todas_patologias JSONB NOT NULL DEFAULT '{}',
                informe_generado    TEXT NOT NULL,
                informe_final       TEXT,
                modelo_llm          TEXT NOT NULL DEFAULT 'gemini-2.5-flash',
                estado              TEXT NOT NULL DEFAULT 'pendiente',
                fue_editado         BOOLEAN NOT NULL DEFAULT FALSE,
                notas_rechazo       TEXT,
                timestamp_decision  TIMESTAMPTZ,
                created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                
                -- Campos de cobro (agregados para trazabilidad completa)
                cobro_estado        TEXT NOT NULL DEFAULT 'no_aplica',
                cobro_metodo        TEXT,
                cobro_monto_clp     INTEGER,
                cobro_ref_id        TEXT,
                cobro_error         TEXT,
                cobro_timestamp     TIMESTAMPTZ
            )
            """
        )
        
        # Migración primero: garantiza columnas nuevas en DBs existentes
        await _migrate_add_cobro_columns(conn)

        # Índices para búsquedas frecuentes
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_informes_study_uid ON informes (study_instance_uid)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_informes_estado ON informes (estado)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_informes_cobro_estado ON informes (cobro_estado)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_informes_created_at ON informes (created_at DESC)"
        )


async def _migrate_add_cobro_columns(conn) -> None:
    """
    Agrega columnas de cobro a tabla informes existente.
    Idempotente: no falla si ya existen.
    """
    migrations = [
        "ALTER TABLE informes ADD COLUMN IF NOT EXISTS cobro_estado TEXT NOT NULL DEFAULT 'no_aplica'",
        "ALTER TABLE informes ADD COLUMN IF NOT EXISTS cobro_metodo TEXT",
        "ALTER TABLE informes ADD COLUMN IF NOT EXISTS cobro_monto_clp INTEGER",
        "ALTER TABLE informes ADD COLUMN IF NOT EXISTS cobro_ref_id TEXT",
        "ALTER TABLE informes ADD COLUMN IF NOT EXISTS cobro_error TEXT",
        "ALTER TABLE informes ADD COLUMN IF NOT EXISTS cobro_timestamp TIMESTAMPTZ",
    ]
    for sql in migrations:
        try:
            await conn.execute(sql)
        except Exception:
            pass  # La columna ya existe o error menor, ignorar
    
    # CHECK constraints para integridad de datos
    # Previene que scripts o queries manuales inserten valores inválidos
    await _add_check_constraints(conn)


async def _add_check_constraints(conn) -> None:
    """
    Agrega CHECK constraints a columnas de estado.
    Idempotente: ignora si ya existen.
    """
    constraints = [
        # CHECK en estado del informe
        (
            "chk_informes_estado",
            "ALTER TABLE informes ADD CONSTRAINT chk_informes_estado "
            "CHECK (estado IN ('pendiente', 'aceptado', 'rechazado', 'editado_aceptado'))"
        ),
        # CHECK en estado del cobro
        (
            "chk_informes_cobro_estado",
            "ALTER TABLE informes ADD CONSTRAINT chk_informes_cobro_estado "
            "CHECK (cobro_estado IN ('pagado', 'pendiente', 'fallido', 'no_aplica'))"
        ),
        # CHECK en método de cobro
        (
            "chk_informes_cobro_metodo",
            "ALTER TABLE informes ADD CONSTRAINT chk_informes_cobro_metodo "
            "CHECK (cobro_metodo IS NULL OR cobro_metodo IN ('creditos', 'oneclick', 'mp_card'))"
        ),
    ]
    for name, sql in constraints:
        try:
            await conn.execute(sql)
        except Exception as e:
            # El constraint ya existe o error menor
            if "already exists" not in str(e).lower():
                print(f"IMAGIAX DB: CHECK constraint {name} no aplicado: {e}")


# -----------------------------------------------------------------------
# Deduplicación
# -----------------------------------------------------------------------
async def buscar_informe_reciente(
    study_instance_uid: str,
    ventana_segundos: int = 120
) -> dict | None:
    """
    Busca si ya existe un informe para este estudio creado en los últimos N segundos.
    Retorna el informe completo si existe, None si no.
    Usado para evitar análisis duplicados (doble-click, race conditions).
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT 
                id, study_instance_uid, informe_generado, ai_nivel_alerta,
                ai_hallazgos, ai_todas_patologias, modelo_llm, estado,
                cobro_estado, cobro_metodo, cobro_error, created_at
            FROM informes
            WHERE study_instance_uid = $1
              AND created_at > NOW() - INTERVAL '1 second' * $2
            ORDER BY created_at DESC
            LIMIT 1
            """,
            study_instance_uid,
            ventana_segundos,
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
        }
    return None


# -----------------------------------------------------------------------
# CRUD
# -----------------------------------------------------------------------
async def guardar_informe(
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
    # Campos de cobro
    cobro_estado: str = "pendiente",
    cobro_metodo: str | None = None,
    cobro_monto_clp: int | None = None,
    cobro_ref_id: str | None = None,
    cobro_error: str | None = None,
) -> str:
    """
    Inserta un nuevo informe con estado de cobro integrado.
    Retorna el UUID del informe como string.
    
    cobro_estado: 'pagado', 'pendiente', 'fallido', 'no_aplica'
    cobro_metodo: 'creditos', 'oneclick', 'mp_card', None
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO informes (
                study_instance_uid, patient_id, patient_name, study_date, modalidad,
                ai_nivel_alerta, ai_hallazgos, ai_todas_patologias,
                informe_generado, modelo_llm,
                cobro_estado, cobro_metodo, cobro_monto_clp, cobro_ref_id, cobro_error,
                cobro_timestamp
            ) VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb, $9, $10, $11, $12, $13, $14, $15, 
                CASE WHEN $11 IN ('pagado', 'fallido') THEN NOW() ELSE NULL END
            )
            RETURNING id
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
            cobro_estado,
            cobro_metodo,
            cobro_monto_clp,
            cobro_ref_id,
            cobro_error,
        )
    return str(row["id"])


async def actualizar_cobro(
    *,
    informe_id: str,
    cobro_estado: str,
    cobro_metodo: str | None = None,
    cobro_monto_clp: int | None = None,
    cobro_ref_id: str | None = None,
    cobro_error: str | None = None,
) -> bool:
    """
    Actualiza el estado de cobro de un informe existente.
    Usado para reintentos de cobro o para marcar pagos pendientes como pagados.
    Retorna True si se actualizó, False si el informe no existe.
    """
    pool = get_pool()
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
    *,
    informe_id: str,
    estado: str,
    informe_final: str,
    notas_rechazo: str | None = None,
) -> dict[str, Any] | None:
    """
    Actualiza el estado de decisión de un informe existente.
    Retorna el registro actualizado o None si el UUID no existe.
    """
    pool = get_pool()
    fue_editado = estado == "editado_aceptado"
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE informes
            SET
                estado             = $2,
                informe_final      = $3,
                fue_editado        = $4,
                notas_rechazo      = $5,
                timestamp_decision = NOW()
            WHERE id = $1::uuid
            RETURNING id, study_instance_uid, estado, fue_editado,
                      timestamp_decision, informe_final
            """,
            informe_id,
            estado,
            informe_final,
            fue_editado,
            notas_rechazo,
        )
    if row is None:
        return None
    return _row_to_dict(row)


async def get_informes_por_estudio(study_uid: str) -> list[dict[str, Any]]:
    """Retorna todos los informes de un estudio, del más reciente al más antiguo."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                id, study_instance_uid, patient_id, patient_name,
                study_date, modalidad, ai_nivel_alerta,
                ai_hallazgos, ai_todas_patologias,
                informe_generado, informe_final, modelo_llm,
                estado, fue_editado, notas_rechazo,
                timestamp_decision, created_at
            FROM informes
            WHERE study_instance_uid = $1
            ORDER BY created_at DESC
            """,
            study_uid,
        )
    return [_row_to_dict(r) for r in rows]


async def get_informe_por_id(informe_id: str) -> dict[str, Any] | None:
    """Retorna un informe por su UUID. Usado para la página de verificación (QR)."""
    import uuid as _uuid
    try:
        uid = _uuid.UUID(informe_id)
    except ValueError:
        return None
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                id, study_instance_uid, patient_id, patient_name,
                study_date, modalidad, ai_nivel_alerta,
                ai_hallazgos, informe_generado, informe_final,
                modelo_llm, estado, fue_editado, created_at
            FROM informes
            WHERE id = $1
            """,
            uid,
        )
    return _row_to_dict(row) if row else None


async def get_todos_informes(
    estado: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """
    Retorna informes paginados, opcionalmente filtrados por estado.
    Solo devuelve columnas de resumen (sin informe_generado/final completos).
    
    Returns:
        {
            "items": [...],
            "total": int,
            "limit": int,
            "offset": int,
            "has_more": bool
        }
    """
    # Límite máximo para evitar queries demasiado grandes
    limit = min(limit, 500)
    
    pool = get_pool()
    async with pool.acquire() as conn:
        if estado:
            # Conteo total
            total = await conn.fetchval(
                "SELECT COUNT(*) FROM informes WHERE estado = $1",
                estado,
            )
            rows = await conn.fetch(
                """
                SELECT
                    id, study_instance_uid, patient_id, patient_name,
                    study_date, modalidad, ai_nivel_alerta,
                    estado, fue_editado, timestamp_decision, created_at
                FROM informes
                WHERE estado = $1
                ORDER BY created_at DESC
                LIMIT $2 OFFSET $3
                """,
                estado, limit, offset,
            )
        else:
            # Conteo total
            total = await conn.fetchval("SELECT COUNT(*) FROM informes")
            rows = await conn.fetch(
                """
                SELECT
                    id, study_instance_uid, patient_id, patient_name,
                    study_date, modalidad, ai_nivel_alerta,
                    estado, fue_editado, timestamp_decision, created_at
                FROM informes
                ORDER BY created_at DESC
                LIMIT $1 OFFSET $2
                """,
                limit, offset,
            )
    
    items = [_row_to_dict(r) for r in rows]
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": (offset + len(items)) < total,
    }


# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------
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
