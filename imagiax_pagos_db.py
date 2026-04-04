"""
IMAGIAX — Módulo de base de datos para pagos, licencias y créditos.

Tablas gestionadas:
  configuracion  → clave/valor por instalación (ej: installation_id)
  clientes       → centros médicos identificados por email
  licencias      → licencias de créditos (trial, packs, suscripciones)
  pagos_mp       → registro de todas las transacciones MercadoPago

Comparte el pool de asyncpg con imagiax_db (misma base de datos PostgreSQL).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg

logger = logging.getLogger("imagiax.pagos_db")

# ── PCI Data Masking (SEC-PAY-009) ────────────────────────────────────────────
_PCI_MASK_KEYS = frozenset({
    "card_number", "card_holder_name", "card_detail",
    "first_six_digits", "last_four_digits", "cvv", "pan",
})


def _mask_pci_data(data: Any) -> Any:
    """Recursively masks PCI-sensitive fields before DB storage. (SEC-PAY-009)"""
    if isinstance(data, dict):
        return {
            k: "***MASKED***" if k.lower() in _PCI_MASK_KEYS else _mask_pci_data(v)
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [_mask_pci_data(item) for item in data]
    return data


# ── Pool compartido con imagiax_db ────────────────────────────────────────────
# get_pool() se importa en tiempo de ejecución para evitar dependencia circular.


def _get_pool() -> asyncpg.Pool:
    from imagiax_db import get_pool
    return get_pool()


# ── Creación de tablas ────────────────────────────────────────────────────────

async def create_tables() -> None:
    """Crea o migra las tablas de pagos. Idempotente (IF NOT EXISTS)."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        # Configuración por instalación (clave/valor)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS configuracion (
                clave      TEXT PRIMARY KEY,
                valor      TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        # Clientes: centros médicos identificados por email
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS clientes (
                id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                email            TEXT NOT NULL UNIQUE,
                nombre_contacto  TEXT,
                centro_medico    TEXT NOT NULL DEFAULT 'Sin nombre',
                rut_empresa      TEXT,
                created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        # Licencias: trial, packs o suscripciones con créditos
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS licencias (
                id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                cliente_id        UUID REFERENCES clientes(id),
                tipo              TEXT NOT NULL,
                creditos_total    INT  NOT NULL DEFAULT 0,
                creditos_usados   INT  NOT NULL DEFAULT 0,
                activa            BOOL NOT NULL DEFAULT TRUE,
                fecha_expiracion  TIMESTAMPTZ,
                mp_payment_id     TEXT,
                mp_preference_id  TEXT,
                mp_preapproval_id TEXT,
                created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        # Pagos MercadoPago: log de cada transacción
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS pagos_mp (
                id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                cliente_id        UUID REFERENCES clientes(id),
                licencia_id       UUID REFERENCES licencias(id),
                mp_payment_id     TEXT UNIQUE,
                mp_preference_id  TEXT,
                mp_preapproval_id TEXT,
                tipo              TEXT NOT NULL,
                estado            TEXT NOT NULL DEFAULT 'pending',
                monto_clp         INT  NOT NULL DEFAULT 0,
                descripcion       TEXT,
                raw_webhook       JSONB,
                created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        # Índices
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_licencias_tipo ON licencias(tipo)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_licencias_activa ON licencias(activa)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pagos_mp_payment ON pagos_mp(mp_payment_id)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pagos_mp_preference ON pagos_mp(mp_preference_id)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pagos_mp_preapproval ON pagos_mp(mp_preapproval_id)"
        )
        # Índices para JOINs y queries temporales (corrigen gaps de rendimiento)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_licencias_cliente ON licencias(cliente_id)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pagos_mp_cliente ON pagos_mp(cliente_id)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pagos_mp_licencia ON pagos_mp(licencia_id)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pagos_mp_created ON pagos_mp(created_at DESC)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pagos_mp_estado ON pagos_mp(estado)"
        )

        # Tarjetas Oneclick (Transbank): inscripciones para cobro silencioso
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS tarjetas_oneclick (
                id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                username         TEXT NOT NULL UNIQUE,
                tbk_user         TEXT NOT NULL,
                card_type        TEXT,
                card_number_last4 TEXT,
                authorization_code TEXT,
                email            TEXT,
                activa           BOOL NOT NULL DEFAULT TRUE,
                created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_oneclick_username ON tarjetas_oneclick(username)"
        )

        # ── Migraciones de aislamiento per-usuario ────────────────────────────
        # Cada usuario tiene sus propios créditos, tarjetas y métodos de pago.
        for migration_sql in [
            "ALTER TABLE licencias ADD COLUMN IF NOT EXISTS usuario_id UUID",
            "ALTER TABLE tarjetas_oneclick ADD COLUMN IF NOT EXISTS usuario_id UUID",
            "CREATE INDEX IF NOT EXISTS idx_licencias_usuario ON licencias(usuario_id)",
            "CREATE INDEX IF NOT EXISTS idx_oneclick_usuario ON tarjetas_oneclick(usuario_id)",
        ]:
            try:
                await conn.execute(migration_sql)
            except Exception:
                pass  # Columna/índice ya existe

        # Pagos Transbank: log de transacciones Oneclick
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS pagos_tbk (
                id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                username          TEXT NOT NULL,
                buy_order         TEXT NOT NULL UNIQUE,
                child_buy_order   TEXT,
                authorization_code TEXT,
                amount            INT NOT NULL,
                installments_number INT NOT NULL DEFAULT 1,
                status            TEXT NOT NULL DEFAULT 'pending',
                response_code     INT,
                raw_response      JSONB,
                created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pagos_tbk_username ON pagos_tbk(username)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pagos_tbk_created ON pagos_tbk(created_at DESC)"
        )

        # Inscripciones TBK en curso: mapea TBK_TOKEN → usuario_id (10 min TTL)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS tbk_pending (
                tbk_token   TEXT PRIMARY KEY,
                usuario_id  TEXT NOT NULL,
                username    TEXT NOT NULL,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        # Migración: corregir usernames con URL-encoding (%40 en lugar de @)
        fixed = await conn.execute(
            """
            UPDATE tarjetas_oneclick
            SET username = REPLACE(username, '%40', '@')
            WHERE username LIKE '%\\%40%'
            """
        )
        if fixed and fixed != "UPDATE 0":
            logger.info("Migración: usernames Oneclick corregidos — %s", fixed)

    print("IMAGIAX Pagos DB: tablas de pagos/licencias listas.")


# ── Configuración / Installation ID ──────────────────────────────────────────

async def get_or_create_installation_id() -> str:
    """
    Retorna el installation_id único de esta instancia IMAGIAX.
    Si no existe lo crea (primera vez que corre el servidor).
    """
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT valor FROM configuracion WHERE clave = 'installation_id'"
        )
        if row:
            return row["valor"]

        new_id = str(uuid.uuid4())
        await conn.execute(
            """
            INSERT INTO configuracion (clave, valor)
            VALUES ('installation_id', $1)
            ON CONFLICT (clave) DO NOTHING
            """,
            new_id,
        )
        return new_id


async def get_config(clave: str) -> str | None:
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT valor FROM configuracion WHERE clave = $1", clave
        )
    return row["valor"] if row else None


async def set_config(clave: str, valor: str) -> None:
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO configuracion (clave, valor, updated_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (clave) DO UPDATE
                SET valor = EXCLUDED.valor, updated_at = NOW()
            """,
            clave,
            valor,
        )


# ── Trial automático ──────────────────────────────────────────────────────────

CREDITOS_TRIAL = 5


async def get_or_create_trial(usuario_id: str | None = None) -> dict[str, Any]:
    """
    Retorna la licencia trial activa del usuario.
    Si no existe, la crea automáticamente con CREDITOS_TRIAL créditos.
    Si usuario_id es None, usa el modelo legacy de instalación compartida.
    """
    pool = _get_pool()
    _uid = uuid.UUID(usuario_id) if usuario_id else None
    async with pool.acquire() as conn:
        if _uid:
            row = await conn.fetchrow(
                """
                SELECT * FROM licencias
                WHERE tipo = 'trial'
                  AND usuario_id = $1
                  AND activa = TRUE
                ORDER BY created_at ASC
                LIMIT 1
                """,
                _uid,
            )
        else:
            row = await conn.fetchrow(
                """
                SELECT * FROM licencias
                WHERE tipo = 'trial'
                  AND cliente_id IS NULL
                  AND usuario_id IS NULL
                  AND activa = TRUE
                ORDER BY created_at ASC
                LIMIT 1
                """
            )
        if row:
            return _row_to_dict(row)

        # Crear trial por primera vez para este usuario
        row = await conn.fetchrow(
            """
            INSERT INTO licencias (tipo, creditos_total, usuario_id)
            VALUES ('trial', $1, $2)
            RETURNING *
            """,
            CREDITOS_TRIAL,
            _uid,
        )
        logger.info("Trial creado para usuario=%s (%d créditos)", usuario_id or "instalación", CREDITOS_TRIAL)
        return _row_to_dict(row)


# ── Créditos disponibles (agregado de todas las licencias activas) ────────────

async def get_estado_creditos(usuario_id: str | None = None) -> dict[str, Any]:
    """
    Retorna el estado consolidado de créditos del usuario.
    Si usuario_id es None, usa el modelo legacy de instalación compartida.
    """
    pool = _get_pool()
    _uid = uuid.UUID(usuario_id) if usuario_id else None
    async with pool.acquire() as conn:
        if _uid:
            rows = await conn.fetch(
                """
                SELECT id, tipo, creditos_total, creditos_usados, activa,
                       fecha_expiracion, created_at
                FROM licencias
                WHERE activa = TRUE
                  AND usuario_id = $1
                  AND (fecha_expiracion IS NULL OR fecha_expiracion > NOW())
                ORDER BY
                    CASE tipo
                        WHEN 'trial' THEN 3
                        WHEN 'suscripcion_mensual' THEN 1
                        ELSE 2
                    END,
                    created_at ASC
                """,
                _uid,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT id, tipo, creditos_total, creditos_usados, activa,
                       fecha_expiracion, created_at
                FROM licencias
                WHERE activa = TRUE
                  AND (fecha_expiracion IS NULL OR fecha_expiracion > NOW())
                ORDER BY
                    CASE tipo
                        WHEN 'trial' THEN 3
                        WHEN 'suscripcion_mensual' THEN 1
                        ELSE 2
                    END,
                    created_at ASC
                """
            )

    if not rows:
        # No hay ninguna licencia activa — crear trial para este usuario
        trial = await get_or_create_trial(usuario_id=usuario_id)
        rows_data = [trial]
    else:
        rows_data = [_row_to_dict(r) for r in rows]

    total_disponible = sum(
        max(0, r["creditos_total"] - r["creditos_usados"]) for r in rows_data
    )
    total_comprados = sum(r["creditos_total"] for r in rows_data)
    es_solo_trial = all(r["tipo"] == "trial" for r in rows_data)
    tiene_suscripcion = any(str(r["tipo"]).startswith("suscripcion_") for r in rows_data)

    # Verificar si hay tarjeta registrada para cobro automático (per-usuario)
    metodo = await get_metodo_pago(usuario_id=usuario_id)
    tiene_tarjeta = metodo is not None

    # Verificar si hay inscripción Oneclick (Transbank) — prioridad sobre MP (per-usuario)
    oneclick = await get_inscripcion_oneclick_any(usuario_id=usuario_id)
    tiene_oneclick = oneclick is not None

    return {
        "creditos_disponibles": total_disponible,
        "creditos_total": total_comprados,
        "es_trial": es_solo_trial,
        "tiene_suscripcion": tiene_suscripcion,
        "tiene_tarjeta": tiene_tarjeta or tiene_oneclick,
        "tiene_oneclick": tiene_oneclick,
        "card_last4": (
            oneclick["card_number_last4"] if oneclick
            else (metodo["card_last4"] if metodo else None)
        ),
        "card_type": oneclick["card_type"] if oneclick else None,
        "metodo_cobro": (
            "oneclick" if tiene_oneclick
            else ("mp_card" if tiene_tarjeta else None)
        ),
        "activa": total_disponible > 0 or tiene_tarjeta or tiene_oneclick,
        "licencias": rows_data,
    }


async def descontar_credito(usuario_id: str | None = None) -> bool:
    """
    Descuenta 1 crédito de la licencia más prioritaria con saldo disponible del usuario.
    Prioridad: suscripción > packs comprados > trial.
    Retorna True si se descontó con éxito, False si sin créditos.
    """
    try:
        pool = _get_pool()
        _uid = uuid.UUID(usuario_id) if usuario_id else None
        async with pool.acquire() as conn:
            async with conn.transaction():
                if _uid:
                    row = await conn.fetchrow(
                        """
                        SELECT id FROM licencias
                        WHERE activa = TRUE
                          AND usuario_id = $1
                          AND creditos_usados < creditos_total
                          AND (fecha_expiracion IS NULL OR fecha_expiracion > NOW())
                        ORDER BY
                            CASE tipo
                                WHEN 'suscripcion_mensual' THEN 1
                                WHEN 'trial' THEN 3
                                ELSE 2
                            END,
                            created_at ASC
                        LIMIT 1
                        FOR UPDATE SKIP LOCKED
                        """,
                        _uid,
                    )
                else:
                    row = await conn.fetchrow(
                        """
                        SELECT id FROM licencias
                        WHERE activa = TRUE
                          AND creditos_usados < creditos_total
                          AND (fecha_expiracion IS NULL OR fecha_expiracion > NOW())
                        ORDER BY
                            CASE tipo
                                WHEN 'suscripcion_mensual' THEN 1
                                WHEN 'trial' THEN 3
                                ELSE 2
                            END,
                            created_at ASC
                        LIMIT 1
                        FOR UPDATE SKIP LOCKED
                        """
                    )
                if not row:
                    return False

                await conn.execute(
                    "UPDATE licencias SET creditos_usados = creditos_usados + 1 WHERE id = $1",
                    row["id"],
                )
        return True
    except Exception as exc:
        logger.error("Error descontando credito: %s", exc)
        return False


# ── Método de pago (tarjeta guardada para cobros automáticos) ─────────────────

def _mp_card_keys(usuario_id: str | None) -> tuple[str, str, str, str, str]:
    """Retorna las 5 claves de configuracion para la tarjeta MP de un usuario."""
    suffix = f":{usuario_id}" if usuario_id else ""
    return (
        f"mp_customer_id{suffix}",
        f"mp_card_id{suffix}",
        f"mp_card_last4{suffix}",
        f"mp_card_brand{suffix}",
        f"metodo_pago_email{suffix}",
    )


async def get_metodo_pago(usuario_id: str | None = None) -> dict[str, Any] | None:
    """
    Retorna la tarjeta guardada para cobros automáticos del usuario, o None si no hay.
    Cada usuario tiene sus propias claves prefijadas con su usuario_id.
    """
    try:
        pool = _get_pool()
        keys = _mp_card_keys(usuario_id)
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT clave, valor FROM configuracion WHERE clave = ANY($1::text[])",
                list(keys),
            )
        if not rows:
            return None
        # Normalizar claves removiendo el sufijo del usuario_id para acceso uniforme
        suffix = f":{usuario_id}" if usuario_id else ""
        data = {r["clave"].replace(suffix, ""): r["valor"] for r in rows}
        if not data.get("mp_customer_id") or not data.get("mp_card_id"):
            return None
        return {
            "mp_customer_id": data["mp_customer_id"],
            "mp_card_id":     data["mp_card_id"],
            "card_last4":     data.get("mp_card_last4", "****"),
            "card_brand":     data.get("mp_card_brand", ""),
            "email":          data.get("metodo_pago_email", ""),
        }
    except Exception as exc:
        logger.error("Error obteniendo metodo de pago: %s", exc)
        return None


async def guardar_metodo_pago(
    *,
    mp_customer_id: str,
    mp_card_id: str,
    card_last4: str,
    card_brand: str,
    email: str,
    usuario_id: str | None = None,
) -> None:
    """Persiste los datos de la tarjeta del usuario en configuracion."""
    keys = _mp_card_keys(usuario_id)
    for clave, valor in zip(keys, [mp_customer_id, mp_card_id, card_last4, card_brand, email]):
        await set_config(clave, valor)


async def borrar_metodo_pago(usuario_id: str | None = None) -> None:
    """Elimina la tarjeta guardada del usuario."""
    pool = _get_pool()
    keys = list(_mp_card_keys(usuario_id))
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM configuracion WHERE clave = ANY($1::text[])",
            keys,
        )


# ── TBK Pending (mapeo seguro TBK_TOKEN → usuario_id) ────────────────────────

async def tbk_pending_save(tbk_token: str, usuario_id: str, username: str) -> None:
    """Guarda la relación TBK_TOKEN → usuario_id antes del redirect a Transbank."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tbk_pending (tbk_token, usuario_id, username) "
            "VALUES ($1, $2, $3) ON CONFLICT (tbk_token) DO UPDATE "
            "SET usuario_id=$2, username=$3, created_at=NOW()",
            tbk_token, usuario_id, username,
        )


async def tbk_pending_pop(tbk_token: str) -> dict[str, str] | None:
    """Recupera y elimina el registro pendiente para el token dado (uso único, TTL 10 min)."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "DELETE FROM tbk_pending WHERE tbk_token = $1 "
            "AND created_at > NOW() - INTERVAL '10 minutes' RETURNING usuario_id, username",
            tbk_token,
        )
        return dict(row) if row else None


# ── Tarjetas Oneclick (Transbank) ─────────────────────────────────────────────

async def guardar_inscripcion_oneclick(
    *,
    username: str,
    tbk_user: str,
    card_type: str | None = None,
    card_number_last4: str | None = None,
    authorization_code: str | None = None,
    email: str | None = None,
    usuario_id: str | None = None,
) -> None:
    """Guarda o actualiza la inscripción Oneclick de un usuario."""
    from urllib.parse import unquote
    username = unquote(username)  # Prevenir %40 en lugar de @
    _uid = uuid.UUID(usuario_id) if usuario_id else None
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO tarjetas_oneclick
                (username, tbk_user, card_type, card_number_last4, authorization_code, email, usuario_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (username) DO UPDATE SET
                tbk_user = EXCLUDED.tbk_user,
                card_type = EXCLUDED.card_type,
                card_number_last4 = EXCLUDED.card_number_last4,
                authorization_code = EXCLUDED.authorization_code,
                email = COALESCE(EXCLUDED.email, tarjetas_oneclick.email),
                usuario_id = COALESCE(EXCLUDED.usuario_id, tarjetas_oneclick.usuario_id),
                activa = TRUE,
                created_at = NOW()
            """,
            username, tbk_user, card_type, card_number_last4, authorization_code, email, _uid,
        )


async def get_inscripcion_oneclick(username: str) -> dict[str, Any] | None:
    """Retorna la inscripción Oneclick activa del usuario, o None."""
    try:
        pool = _get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT username, tbk_user, card_type, card_number_last4,
                       authorization_code, email, activa, created_at
                FROM tarjetas_oneclick
                WHERE username = $1 AND activa = TRUE
                """,
                username,
            )
        if not row:
            return None
        return dict(row)
    except Exception as exc:
        logger.error("Error obteniendo inscripción Oneclick para %s: %s", username, exc)
        return None


async def get_inscripcion_oneclick_any(usuario_id: str | None = None) -> dict[str, Any] | None:
    """
    Retorna la inscripción Oneclick activa del usuario especificado.
    Si usuario_id es None, usa modelo legacy (primera activa — solo para compatibilidad).
    """
    try:
        pool = _get_pool()
        async with pool.acquire() as conn:
            if usuario_id:
                _uid = uuid.UUID(usuario_id)
                row = await conn.fetchrow(
                    """
                    SELECT username, tbk_user, card_type, card_number_last4,
                           authorization_code, email, activa, created_at
                    FROM tarjetas_oneclick
                    WHERE activa = TRUE AND usuario_id = $1
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    _uid,
                )
            else:
                row = await conn.fetchrow(
                    """
                    SELECT username, tbk_user, card_type, card_number_last4,
                           authorization_code, email, activa, created_at
                    FROM tarjetas_oneclick
                    WHERE activa = TRUE
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                )
        if not row:
            return None
        return dict(row)
    except Exception as exc:
        logger.error("Error obteniendo inscripción Oneclick: %s", exc)
        return None


async def borrar_inscripcion_oneclick(username: str) -> None:
    """Desactiva la inscripción Oneclick del usuario."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE tarjetas_oneclick SET activa = FALSE WHERE username = $1",
            username,
        )


async def registrar_pago_tbk(
    *,
    username: str,
    buy_order: str,
    child_buy_order: str | None = None,
    authorization_code: str | None = None,
    amount: int,
    installments_number: int = 1,
    status: str = "pending",
    response_code: int | None = None,
    raw_response: dict | None = None,
) -> None:
    """Registra un pago Transbank en pagos_tbk."""
    try:
        pool = _get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO pagos_tbk
                    (username, buy_order, child_buy_order, authorization_code,
                     amount, installments_number, status, response_code, raw_response)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                """,
                username, buy_order, child_buy_order, authorization_code,
                amount, installments_number, status, response_code,
                json.dumps(_mask_pci_data(raw_response)) if raw_response else None,  # SEC-PAY-009
            )
    except Exception as exc:
        logger.error("Error registrando pago TBK: %s", exc)


# ── Clientes ──────────────────────────────────────────────────────────────────

async def crear_o_buscar_cliente(
    email: str,
    centro_medico: str,
    nombre_contacto: str | None = None,
    rut_empresa: str | None = None,
) -> dict[str, Any]:
    """Upsert de cliente por email. Retorna el registro."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO clientes (email, centro_medico, nombre_contacto, rut_empresa)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (email) DO UPDATE
                SET centro_medico   = EXCLUDED.centro_medico,
                    nombre_contacto = COALESCE(EXCLUDED.nombre_contacto, clientes.nombre_contacto),
                    rut_empresa     = COALESCE(EXCLUDED.rut_empresa, clientes.rut_empresa)
            RETURNING id, email, centro_medico, nombre_contacto, rut_empresa, created_at
            """,
            email,
            centro_medico,
            nombre_contacto,
            rut_empresa,
        )
    return _row_to_dict(row)


# ── Licencias / Créditos comprados ────────────────────────────────────────────

async def acreditar_pago(
    *,
    cliente_id: str,
    tipo: str,
    creditos: int,
    mp_payment_id: str | None = None,
    mp_preference_id: str | None = None,
    mp_preapproval_id: str | None = None,
    duracion_dias: int | None = None,
    usuario_id: str | None = None,
) -> dict[str, Any]:
    """
    Crea una nueva licencia con créditos después de un pago aprobado.
    Idempotente: usa INSERT ON CONFLICT DO NOTHING RETURNING para atomicidad (SEC-PAY-005, AD-5).
    Elimina TOCTOU race condition del SELECT-then-INSERT pattern anterior.
    """
    pool = _get_pool()
    fecha_exp = None
    if duracion_dias:
        fecha_exp = datetime.now(timezone.utc) + timedelta(days=duracion_dias)

    _uid = uuid.UUID(usuario_id) if usuario_id else None
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO licencias
                    (cliente_id, tipo, creditos_total, mp_payment_id,
                     mp_preference_id, mp_preapproval_id, fecha_expiracion, usuario_id)
                VALUES (, , , , , , , )
                ON CONFLICT (mp_payment_id) DO NOTHING
                RETURNING *
                """,
                uuid.UUID(cliente_id),
                tipo,
                creditos,
                mp_payment_id,
                mp_preference_id,
                mp_preapproval_id,
                fecha_exp,
                _uid,
            )
            if row is None:
                # Already processed (idempotent) — fetch the existing row
                row = await conn.fetchrow(
                    "SELECT * FROM licencias WHERE mp_payment_id = ",
                    mp_payment_id,
                )
    return _row_to_dict(row)


async def renovar_suscripcion(mp_preapproval_id: str, creditos_nuevos: int) -> bool:
    """
    Renueva los créditos de una suscripción activa.
    Llamado desde el webhook de cargo recurrente aprobado.
    """
    try:
        pool = _get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE licencias
                SET creditos_usados   = 0,
                    fecha_expiracion  = NOW() + INTERVAL '31 days',
                    creditos_total    = $2
                WHERE mp_preapproval_id = $1
                  AND activa = TRUE
                """,
                mp_preapproval_id,
                creditos_nuevos,
            )
        # asyncpg execute() returns command tag like "UPDATE 1"
        return result is not None and result.split()[-1] != "0"
    except Exception as exc:
        logger.error("Error renovando suscripcion %s: %s", mp_preapproval_id, exc)
        return False


async def get_licencia_por_mp_payment(mp_payment_id: str) -> dict[str, Any] | None:
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM licencias WHERE mp_payment_id = $1",
            mp_payment_id,
        )
    return _row_to_dict(row) if row else None


async def get_licencia_por_preference(mp_preference_id: str) -> dict[str, Any] | None:
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM licencias WHERE mp_preference_id = $1",
            mp_preference_id,
        )
    return _row_to_dict(row) if row else None


async def get_licencia_por_preapproval(mp_preapproval_id: str) -> dict[str, Any] | None:
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM licencias WHERE mp_preapproval_id = $1",
            mp_preapproval_id,
        )
    return _row_to_dict(row) if row else None


# ── Registro de pagos ─────────────────────────────────────────────────────────

async def registrar_pago(
    *,
    cliente_id: str | None,
    licencia_id: str | None,
    mp_payment_id: str | None,
    mp_preference_id: str | None,
    mp_preapproval_id: str | None,
    tipo: str,
    estado: str,
    monto_clp: int,
    descripcion: str,
    raw_webhook: dict | None = None,
) -> str:
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO pagos_mp (
                cliente_id, licencia_id, mp_payment_id, mp_preference_id,
                mp_preapproval_id, tipo, estado, monto_clp, descripcion, raw_webhook
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb)
            ON CONFLICT (mp_payment_id) DO UPDATE
                SET estado = EXCLUDED.estado,
                    raw_webhook = EXCLUDED.raw_webhook
            RETURNING id
            """,
            uuid.UUID(cliente_id) if cliente_id else None,
            uuid.UUID(licencia_id) if licencia_id else None,
            mp_payment_id,
            mp_preference_id,
            mp_preapproval_id,
            tipo,
            estado,
            monto_clp,
            descripcion,
            json.dumps(_mask_pci_data(raw_webhook), ensure_ascii=False, default=str) if raw_webhook else None,  # SEC-PAY-009
        )
    return str(row["id"])


# ── Admin ─────────────────────────────────────────────────────────────────────

async def get_admin_resumen() -> dict[str, Any]:
    pool = _get_pool()
    async with pool.acquire() as conn:
        total_clientes = await conn.fetchval("SELECT COUNT(*) FROM clientes")
        total_licencias_activas = await conn.fetchval(
            "SELECT COUNT(*) FROM licencias WHERE activa = TRUE"
        )
        total_pagos_aprobados = await conn.fetchval(
            "SELECT COUNT(*) FROM pagos_mp WHERE estado = 'approved'"
        )
        total_ingresos_clp = await conn.fetchval(
            "SELECT COALESCE(SUM(monto_clp), 0) FROM pagos_mp WHERE estado = 'approved'"
        )
        licencias = await conn.fetch(
            """
            SELECT
                c.email, c.centro_medico,
                l.id, l.tipo, l.creditos_total, l.creditos_usados,
                l.activa, l.fecha_expiracion, l.created_at,
                l.mp_payment_id, l.mp_preapproval_id
            FROM licencias l
            LEFT JOIN clientes c ON c.id = l.cliente_id
            ORDER BY l.created_at DESC
            LIMIT 100
            """
        )

    return {
        "total_clientes": total_clientes,
        "total_licencias_activas": total_licencias_activas,
        "total_pagos_aprobados": total_pagos_aprobados,
        "total_ingresos_clp": total_ingresos_clp,
        "licencias": [_row_to_dict(r) for r in licencias],
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

async def buscar_pago_por_mp_id(mp_payment_id: str) -> dict[str, Any] | None:
    """
    Busca un pago por su mp_payment_id en la tabla pagos_mp.
    Retorna None si no existe.
    """
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM pagos_mp WHERE mp_payment_id = $1",
            mp_payment_id
        )
    return _row_to_dict(row) if row else None


async def get_historial_cobros(periodo: str = "total") -> dict[str, Any]:
    """
    Obtiene el historial de cobros unificado (MercadoPago + Transbank) con filtro temporal.
    periodo: "dia", "semana", "mes", "trimestre", "anio", "total"
    """
    pool = _get_pool()

    # Mapeo a timedelta — asyncpg los convierte nativamente a interval de PG
    _INTERVALOS = {
        "dia":       timedelta(days=1),
        "semana":    timedelta(days=7),
        "mes":       timedelta(days=30),
        "trimestre": timedelta(days=90),
        "anio":      timedelta(days=365),
    }
    td = _INTERVALOS.get(periodo)  # None si "total"

    # Query unificada: UNION ALL de pagos_mp + pagos_tbk con columnas homogéneas
    _UNION_BASE = """
        SELECT id, tipo, estado, monto_clp, descripcion, created_at, mp_payment_id AS ref_id, 'mercadopago' AS origen
        FROM pagos_mp
        WHERE estado IN ('approved', 'rejected', 'pending')
        UNION ALL
        SELECT id, 'scan_automatico' AS tipo, status AS estado, amount AS monto_clp,
               'Informe radiologico IA (Oneclick)' AS descripcion, created_at, buy_order AS ref_id, 'transbank' AS origen
        FROM pagos_tbk
        WHERE status IN ('approved', 'rejected', 'error')
    """

    try:
        async with pool.acquire() as conn:
            if td is not None:
                cutoff = datetime.now(timezone.utc) - td
                rows = await conn.fetch(
                    f"""
                    SELECT * FROM ({_UNION_BASE}) AS cobros
                    WHERE created_at >= $1
                    ORDER BY created_at DESC
                    """,
                    cutoff,
                )
                total_row = await conn.fetchrow(
                    f"""
                    SELECT COALESCE(SUM(monto_clp) FILTER (WHERE estado = 'approved'), 0) AS total_gastado,
                           COUNT(*) AS total_transacciones,
                           COUNT(*) FILTER (WHERE estado = 'approved') AS aprobados,
                           COUNT(*) FILTER (WHERE estado IN ('rejected', 'error')) AS rechazados
                    FROM ({_UNION_BASE}) AS cobros
                    WHERE created_at >= $1
                    """,
                    cutoff,
                )
            else:
                rows = await conn.fetch(
                    f"""
                    SELECT * FROM ({_UNION_BASE}) AS cobros
                    ORDER BY created_at DESC
                    """
                )
                total_row = await conn.fetchrow(
                    f"""
                    SELECT COALESCE(SUM(monto_clp) FILTER (WHERE estado = 'approved'), 0) AS total_gastado,
                           COUNT(*) AS total_transacciones,
                           COUNT(*) FILTER (WHERE estado = 'approved') AS aprobados,
                           COUNT(*) FILTER (WHERE estado IN ('rejected', 'error')) AS rechazados
                    FROM ({_UNION_BASE}) AS cobros
                    """
                )
    except Exception as exc:
        logger.error("Error consultando historial de cobros: %s", exc)
        return {
            "periodo": periodo,
            "cobros": [],
            "resumen": {"total_gastado": 0, "total_transacciones": 0, "aprobados": 0, "rechazados": 0},
        }

    cobros = [_row_to_dict(row) for row in rows]

    return {
        "periodo": periodo,
        "cobros": cobros,
        "resumen": {
            "total_gastado": total_row["total_gastado"],
            "total_transacciones": total_row["total_transacciones"],
            "aprobados": total_row["aprobados"],
            "rechazados": total_row["rechazados"],
        },
    }


async def get_historial_completo(limite: int = 100) -> list[dict[str, Any]]:
    """
    Obtiene el historial completo de transacciones para mostrar en la pestaña Historial.
    Incluye cobros, informes generados, packs comprados.
    """
    pool = _get_pool()
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT created_at, descripcion, tipo_detalle, estado, monto, via, origen FROM (
                    -- Cobros MercadoPago (packs y suscripciones)
                    SELECT
                        created_at,
                        CASE
                            WHEN tipo = 'pack_individual' THEN 'Escaneo Individual'
                            WHEN tipo = 'pack_50' THEN 'Pack 50 Informes'
                            WHEN tipo = 'pack_200' THEN 'Pack 200 Informes'
                            WHEN tipo = 'suscripcion_mensual' THEN 'Suscripcion Mensual'
                            WHEN tipo = 'scan_automatico' THEN 'Informe radiologico IA (MP)'
                            ELSE tipo
                        END AS descripcion,
                        CASE
                            WHEN tipo LIKE 'pack%' THEN 'Compra de creditos'
                            WHEN tipo = 'suscripcion_mensual' THEN 'Suscripcion'
                            ELSE 'Cobro por informe'
                        END AS tipo_detalle,
                        estado,
                        monto_clp AS monto,
                        'MP' AS via,
                        'mercadopago' AS origen
                    FROM pagos_mp
                    WHERE estado IN ('approved', 'pending', 'rejected')

                    UNION ALL

                    -- Cobros Transbank Oneclick
                    SELECT
                        created_at,
                        'Informe radiologico IA (Oneclick)' AS descripcion,
                        'Cobro por informe' AS tipo_detalle,
                        status AS estado,
                        amount AS monto,
                        'TBK' AS via,
                        'transbank' AS origen
                    FROM pagos_tbk
                    WHERE status IN ('approved', 'rejected')
                ) AS historial
                ORDER BY created_at DESC
                LIMIT $1
                """,
                limite,
            )
        return [_row_to_dict(r) for r in rows]
    except Exception as exc:
        logger.error("Error obteniendo historial completo: %s", exc)
        return []


async def get_consumo_reciente(limite: int = 5) -> list[dict[str, Any]]:
    """
    Obtiene los últimos N usos/cobros para mostrar en el panel de estado.
    Retorna lista simplificada: fecha, tipo, monto, estado.
    """
    pool = _get_pool()
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT fecha, tipo, monto, estado FROM (
                    SELECT created_at AS fecha, 'cobro' AS tipo, monto_clp AS monto, estado
                    FROM pagos_mp WHERE estado = 'approved'
                    UNION ALL
                    SELECT created_at AS fecha, 'cobro' AS tipo, amount AS monto, status AS estado
                    FROM pagos_tbk WHERE status = 'approved'
                ) AS consumos
                ORDER BY fecha DESC
                LIMIT $1
                """,
                limite,
            )
        return [_row_to_dict(r) for r in rows]
    except Exception as exc:
        logger.error("Error obteniendo consumo reciente: %s", exc)
        return []


async def get_suscripcion_activa(usuario_id: str | None = None) -> dict[str, Any] | None:
    """Obtiene la suscripción activa del usuario sin modificar estado."""
    pool = _get_pool()
    _uid = uuid.UUID(usuario_id) if usuario_id else None
    try:
        async with pool.acquire() as conn:
            if _uid:
                row = await conn.fetchrow(
                    """
                    SELECT id, tipo, mp_preapproval_id, activa, created_at
                    FROM licencias
                    WHERE activa = TRUE AND tipo LIKE 'suscripcion_%' AND usuario_id = $1
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    _uid,
                )
            else:
                row = await conn.fetchrow(
                    """
                    SELECT id, tipo, mp_preapproval_id, activa, created_at
                    FROM licencias
                    WHERE activa = TRUE AND tipo LIKE 'suscripcion_%'
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                )
        return _row_to_dict(row) if row else None
    except Exception as exc:
        logger.error("Error obteniendo suscripcion activa: %s", exc)
        return None


async def desactivar_licencia(licencia_id: str) -> tuple[bool, str]:
    """Desactiva una licencia específica por id."""
    pool = _get_pool()
    try:
        async with pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE licencias SET activa = FALSE WHERE id = $1::uuid AND activa = TRUE",
                licencia_id,
            )
            if result == "UPDATE 0":
                return False, "La licencia ya estaba inactiva o no existe."
        return True, "Licencia desactivada correctamente."
    except Exception as exc:
        logger.error("Error desactivando licencia %s: %s", licencia_id, exc)
        return False, f"Error al desactivar licencia: {exc}"


def _row_to_dict(row: asyncpg.Record | dict) -> dict[str, Any]:
    result = {}
    items = dict(row).items() if hasattr(row, "items") else row.items()
    for key, value in items:
        if isinstance(value, uuid.UUID):
            result[key] = str(value)
        elif isinstance(value, datetime):
            result[key] = value.isoformat()
        else:
            result[key] = value
    # Alias para compatibilidad frontend: created_at → timestamp_creacion
    if "created_at" in result and "timestamp_creacion" not in result:
        result["timestamp_creacion"] = result["created_at"]
    return result
