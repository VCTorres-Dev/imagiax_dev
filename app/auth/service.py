"""
app.auth.service — Core authentication business logic.

JWT token creation/verification, password hashing, token revocation,
audit logging, and all auth database operations.

All functions that need database access accept an explicit `pool: asyncpg.Pool`
parameter — no global pool references here.
"""

from __future__ import annotations

import hashlib
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg
from fastapi import HTTPException, Request
import jwt as _jwt
from jwt.exceptions import InvalidTokenError as JWTError
from passlib.context import CryptContext

from app.auth.models import UserPayload
from app.core.config import settings

logger = logging.getLogger("imagiax.auth")

# ── Constants ──────────────────────────────────────────────────────────────────

ROLES_VALIDOS = ("superadmin", "admin", "medico", "recepcion")

# Login brute-force settings (read from env, same as monolith)
_LOGIN_MAX_ATTEMPTS = int(os.getenv("LOGIN_MAX_ATTEMPTS", "5"))
_LOGIN_LOCK_MINUTES = int(os.getenv("LOGIN_LOCK_MINUTES", "5"))

# Password hashing — bcrypt with explicit work factor (12 rounds minimum for PHI)
_pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)


# ── Password helpers ───────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    """Generate bcrypt hash of a plain-text password."""
    return _pwd_ctx.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """Verify a plain-text password against a bcrypt hash."""
    return _pwd_ctx.verify(plain, hashed)


def validate_password_complexity(password: str) -> None:
    """
    Enforce minimum password requirements for PHI medical data.
    Raises ValueError with a descriptive message if the password does not meet requirements.
    """
    if len(password) < 12:
        raise ValueError("La contraseña debe tener al menos 12 caracteres")
    if not any(c.isupper() for c in password):
        raise ValueError("La contraseña debe incluir al menos una letra mayúscula")
    if not any(c.isdigit() for c in password):
        raise ValueError("La contraseña debe incluir al menos un número")
    if not any(c in "!@#$%^&*()-_=+[]{}|;:,.<>?/" for c in password):
        raise ValueError("La contraseña debe incluir al menos un carácter especial (!@#$%...)")


# ── Session fingerprint ────────────────────────────────────────────────────────

def generate_session_fingerprint(request: Request) -> str:
    """
    Generate a session fingerprint based on User-Agent to bind the JWT to a device.
    Uses truncated SHA-256 (16 hex chars = 64 bits) — enough to detect browser changes,
    compact enough to not bloat the JWT.
    """
    ua = (request.headers.get("user-agent") or "unknown").strip()
    return hashlib.sha256(ua.encode("utf-8")).hexdigest()[:16]


# ── JWT ────────────────────────────────────────────────────────────────────────

def create_access_token(
    user_id: str,
    email: str,
    nombre: str,
    rol: str,
    *,
    expire_hours: float | None = None,
    fingerprint: str | None = None,
) -> str:
    """Create a signed JWT with user payload. Includes 'iat' for sliding-window renewal."""
    jwt_secret = settings.JWT_SECRET.get_secret_value()
    hours = float(settings.JWT_EXPIRE_HOURS if expire_hours is None else expire_hours)
    now = datetime.now(timezone.utc)
    expire = now + timedelta(hours=hours)
    payload: dict[str, Any] = {
        "sub": user_id,
        "email": email,
        "nombre": nombre,
        "rol": rol,
        "iat": now,
        "exp": expire,
        "jti": uuid.uuid4().hex,
    }
    if fingerprint:
        payload["fp"] = fingerprint
    if settings.jwt_issuer:
        payload["iss"] = settings.jwt_issuer
    if settings.jwt_audience:
        payload["aud"] = settings.jwt_audience
    return _jwt.encode(payload, jwt_secret, algorithm="HS256")


def should_renew_token(raw_payload: dict[str, Any]) -> bool:
    """
    Return True when the token has passed 50% of its lifetime.

    Sliding window logic:
      lifetime  = exp - iat
      elapsed   = now - iat
      renew     = elapsed > lifetime / 2

    Falls back to False on any clock or claim issue so the caller never crashes.
    """
    try:
        iat = raw_payload.get("iat")
        exp = raw_payload.get("exp")
        if iat is None or exp is None:
            return False
        # PyJWT decodes iat/exp as int (UNIX timestamps) when options["verify_iat"] is off,
        # but may also return datetime objects depending on decode_options — handle both.
        now_ts = datetime.now(timezone.utc).timestamp()
        iat_ts = iat.timestamp() if isinstance(iat, datetime) else float(iat)
        exp_ts = exp.timestamp() if isinstance(exp, datetime) else float(exp)
        lifetime = exp_ts - iat_ts
        if lifetime <= 0:
            return False
        elapsed = now_ts - iat_ts
        return elapsed > lifetime / 2
    except Exception:
        return False


def extract_raw_token(request: Request) -> str | None:
    """Extract raw token from Bearer header or httpOnly cookie (same priority as get_current_user)."""
    auth = request.headers.get("authorization") or request.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.cookies.get("imagiax_session")


def get_token_expires_at_utc(token: str) -> datetime:
    """Read the exp claim from an already-signed JWT."""
    jwt_secret = settings.JWT_SECRET.get_secret_value()
    payload = _jwt.decode(token, jwt_secret, algorithms=["HS256"])
    return datetime.fromtimestamp(int(payload["exp"]), tz=timezone.utc)


def _decode_raw_payload(token: str) -> dict[str, Any]:
    """
    Decode and validate a JWT, returning the raw payload dict.
    Raises HTTPException 401 if the token is invalid or expired.
    Internal helper — use decode_token() for typed UserPayload.
    """
    jwt_secret = settings.JWT_SECRET.get_secret_value()
    decode_kwargs: dict = {
        "algorithms": ["HS256"],
        "options": {
            "verify_signature": True,
            "verify_exp": True,
            "require": ["exp"],
        },
    }
    if settings.jwt_issuer:
        decode_kwargs["issuer"] = settings.jwt_issuer
    if settings.jwt_audience:
        decode_kwargs["audience"] = settings.jwt_audience
    try:
        return _jwt.decode(token, jwt_secret, **decode_kwargs)
    except JWTError:
        raise HTTPException(status_code=401, detail="Token inválido o expirado")


def decode_token(token: str) -> UserPayload:
    """Decode and validate a JWT. Raises HTTPException 401 if invalid."""
    payload = _decode_raw_payload(token)
    return UserPayload(
        user_id=payload["sub"],
        email=payload["email"],
        nombre=payload["nombre"],
        rol=payload["rol"],
        jti=payload.get("jti"),
        fp=payload.get("fp"),
    )


def decode_token_with_renewal_check(token: str) -> tuple[UserPayload, bool]:
    """
    Decode a JWT and also determine whether a sliding-window renewal is needed.

    Returns:
        (UserPayload, should_renew: bool)

    Decodes only once — no double-decode overhead.
    """
    payload = _decode_raw_payload(token)
    user = UserPayload(
        user_id=payload["sub"],
        email=payload["email"],
        nombre=payload["nombre"],
        rol=payload["rol"],
        jti=payload.get("jti"),
        fp=payload.get("fp"),
    )
    return user, should_renew_token(payload)


def verify_session_cookie_token(token: str | None) -> bool:
    """
    Minimal verification for /auth/verify (no DB):
    - valid JWT signature
    - exp required and valid
    - nbf valid when present
    - iss/aud only if configured in env
    """
    if not token:
        return False
    jwt_secret = settings.JWT_SECRET.get_secret_value()
    try:
        _jwt.decode(
            token,
            jwt_secret,
            algorithms=["HS256"],
            options={
                "verify_signature": True,
                "verify_exp": True,
                "verify_nbf": True,
                "require": ["exp"],
            },
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
        )
        return True
    except JWTError:
        return False


# ── Token revocation ───────────────────────────────────────────────────────────

async def revoke_token(pool: asyncpg.Pool, jti: str, user_id: str, expires_at: Any) -> None:
    """Add a JTI to the token blacklist (explicit logout)."""
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO revoked_tokens (jti, user_id, expires_at) "
            "VALUES ($1, $2, $3) ON CONFLICT (jti) DO NOTHING",
            jti, uuid.UUID(user_id), expires_at,
        )


async def revoke_all_user_tokens(pool: asyncpg.Pool, user_id: str) -> None:
    """Revoke ALL active tokens of a user (used when deleting account or forcing global logout)."""
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO revoked_users (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING",
            uuid.UUID(user_id),
        )


async def is_jti_revoked(pool: asyncpg.Pool, jti: str) -> bool:
    """
    Return True if the JTI was explicitly revoked (logout).
    SEC-BE-005: Only True if this specific JTI exists and has not expired.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT jti FROM revoked_tokens WHERE jti = $1 AND expires_at > NOW()",
            jti,
        )
        return row is not None


async def is_user_revoked(pool: asyncpg.Pool, user_id: str) -> bool:
    """Return True if the user was deleted, forced global logout, or deactivated."""
    uid = uuid.UUID(user_id)
    async with pool.acquire() as conn:
        # Check revoked_users table (explicit revocation / global logout)
        revoked = bool(await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM revoked_users WHERE user_id = $1)",
            uid,
        ))
        if revoked:
            return True
        # Check usuarios.activo = FALSE (admin deactivation) — VER-001 gap fix
        active = await conn.fetchval(
            "SELECT activo FROM usuarios WHERE id = $1",
            uid,
        )
        # If user not found or inactive → treat as revoked
        return active is None or not active


# ── Login rate limiter — PostgreSQL-backed (C-003) ────────────────────────────

async def check_login_rate_pg(pool: asyncpg.Pool, email: str) -> bool:
    """
    Return True if login is allowed, False if blocked.
    C-003: Brute-force tracking in PostgreSQL — survives restarts.
    """
    key = email.lower().strip()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT attempt_count, locked_until FROM login_attempts WHERE email = $1",
            key,
        )
    if row is None:
        return True
    if row["locked_until"] is not None and row["locked_until"] > datetime.now(timezone.utc):
        return False
    return True


async def record_login_attempt_pg(pool: asyncpg.Pool, email: str) -> None:
    """
    Record a failed attempt. If _LOGIN_MAX_ATTEMPTS is reached, lock for _LOGIN_LOCK_MINUTES.
    C-003: INSERT ... ON CONFLICT DO UPDATE for atomic upsert.
    """
    key = email.lower().strip()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO login_attempts (email, attempt_count, last_attempt_at)
            VALUES ($1, 1, NOW())
            ON CONFLICT (email) DO UPDATE
                SET attempt_count   = login_attempts.attempt_count + 1,
                    last_attempt_at = NOW()
            RETURNING attempt_count
            """,
            key,
        )
        attempt_count = row["attempt_count"] if row else 1
        if attempt_count >= _LOGIN_MAX_ATTEMPTS:
            await conn.execute(
                "UPDATE login_attempts SET locked_until = NOW() + $1::interval WHERE email = $2",
                f"{_LOGIN_LOCK_MINUTES} minutes",
                key,
            )


async def clear_login_attempts_pg(pool: asyncpg.Pool, email: str) -> None:
    """Clear failed attempts on successful login. C-003."""
    key = email.lower().strip()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM login_attempts WHERE email = $1", key)


async def cleanup_old_login_attempts(pool: asyncpg.Pool) -> None:
    """
    Delete login_attempts rows older than 1 hour.
    Call at lifespan startup and as a periodic task. C-003.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM login_attempts WHERE last_attempt_at < NOW() - INTERVAL '1 hour'"
        )


# ── Database table creation ────────────────────────────────────────────────────

async def create_auth_tables(pool: asyncpg.Pool) -> None:
    """Create auth tables (usuarios, sesiones_audit, revoked_tokens, etc.). Call in lifespan."""
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS usuarios (
                id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                email           TEXT UNIQUE NOT NULL,
                password_hash   TEXT NOT NULL,
                nombre          TEXT NOT NULL,
                rol             TEXT NOT NULL DEFAULT 'medico',
                activo          BOOLEAN NOT NULL DEFAULT TRUE,
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT chk_rol CHECK (rol IN ('superadmin', 'admin', 'medico', 'recepcion'))
            )
        """)
        # Migration: update constraint to include superadmin on existing instances
        try:
            await conn.execute("ALTER TABLE usuarios DROP CONSTRAINT IF EXISTS chk_rol")
            await conn.execute(
                "ALTER TABLE usuarios ADD CONSTRAINT chk_rol "
                "CHECK (rol IN ('superadmin', 'admin', 'medico', 'recepcion'))"
            )
        except Exception:
            pass
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS sesiones_audit (
                id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                usuario_id  UUID REFERENCES usuarios(id),
                accion      TEXT NOT NULL,
                recurso     TEXT,
                ip          TEXT,
                detalle     TEXT,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_usuario ON sesiones_audit (usuario_id)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_created ON sesiones_audit (created_at DESC)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_usuarios_email ON usuarios (email)"
        )
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS revoked_tokens (
                jti         TEXT PRIMARY KEY,
                user_id     UUID,
                revoked_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                expires_at  TIMESTAMPTZ NOT NULL
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_revoked_expires ON revoked_tokens (expires_at)"
        )
        # Table to revoke all tokens for a user (used when deleting account)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS revoked_users (
                user_id     UUID PRIMARY KEY,
                revoked_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        # C-003: Table for brute-force tracking in PostgreSQL
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS login_attempts (
                email           TEXT PRIMARY KEY,
                attempt_count   INTEGER NOT NULL DEFAULT 1,
                locked_until    TIMESTAMPTZ,
                last_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_login_attempts_email ON login_attempts (email)"
        )


# ── User CRUD ──────────────────────────────────────────────────────────────────

async def get_user_by_email(pool: asyncpg.Pool, email: str) -> dict[str, Any] | None:
    """Look up a user by email. Returns a dict with all fields or None."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, email, password_hash, nombre, rol, activo FROM usuarios WHERE email = $1",
            email,
        )
        return dict(row) if row else None


async def get_user_by_id(pool: asyncpg.Pool, user_id: str) -> dict[str, Any] | None:
    """Look up a user by UUID."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, email, nombre, rol, activo, created_at FROM usuarios WHERE id = $1",
            uuid.UUID(user_id),
        )
        return dict(row) if row else None


async def create_user(
    pool: asyncpg.Pool,
    email: str,
    password: str,
    nombre: str,
    rol: str = "medico",
) -> dict[str, Any]:
    """Create a new user. Returns a dict with id, email, nombre, rol."""
    if rol not in ROLES_VALIDOS:
        raise ValueError(f"Rol inválido: {rol}")
    validate_password_complexity(password)
    pw_hash = hash_password(password)
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO usuarios (email, password_hash, nombre, rol)
                VALUES ($1, $2, $3, $4)
                RETURNING id, email, nombre, rol, created_at
                """,
                email.lower().strip(),
                pw_hash,
                nombre.strip(),
                rol,
            )
            return dict(row)
        except Exception as exc:
            if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
                raise HTTPException(status_code=409, detail="El email ya está registrado")
            raise


async def update_password(pool: asyncpg.Pool, user_id: str, new_password: str) -> None:
    """Update a user's password."""
    validate_password_complexity(new_password)
    pw_hash = hash_password(new_password)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE usuarios SET password_hash = $1, updated_at = NOW() WHERE id = $2",
            pw_hash,
            uuid.UUID(user_id),
        )


async def list_users(pool: asyncpg.Pool) -> list[dict[str, Any]]:
    """List all users (without password_hash)."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, email, nombre, rol, activo, created_at FROM usuarios ORDER BY created_at DESC"
        )
        return [dict(r) for r in rows]


async def toggle_user_active(pool: asyncpg.Pool, user_id: str, activo: bool) -> None:
    """Activate or deactivate a user."""
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE usuarios SET activo = $1, updated_at = NOW() WHERE id = $2",
            activo,
            uuid.UUID(user_id),
        )


async def update_user(
    pool: asyncpg.Pool,
    user_id: str,
    *,
    nombre: str | None = None,
    rol: str | None = None,
    activo: bool | None = None,
) -> dict[str, Any] | None:
    """
    Update user fields. Only updates the fields that are passed.
    Returns the updated user or None if not found.
    The superadmin account cannot be deactivated or have its role changed.
    """
    if rol is not None and rol not in ROLES_VALIDOS:
        raise HTTPException(status_code=400, detail=f"Rol inválido: {rol}")
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, rol FROM usuarios WHERE id = $1", uuid.UUID(user_id)
        )
        if row is None:
            return None
        if row["rol"] == "superadmin":
            if activo is False:
                raise HTTPException(
                    status_code=403,
                    detail="La cuenta superadmin no puede ser desactivada."
                )
            if rol is not None and rol != "superadmin":
                raise HTTPException(
                    status_code=403,
                    detail="El rol de la cuenta superadmin no puede ser cambiado."
                )
        if nombre is not None:
            await conn.execute(
                "UPDATE usuarios SET nombre = $1, updated_at = NOW() WHERE id = $2",
                nombre.strip(), uuid.UUID(user_id),
            )
        if rol is not None:
            await conn.execute(
                "UPDATE usuarios SET rol = $1, updated_at = NOW() WHERE id = $2",
                rol, uuid.UUID(user_id),
            )
        if activo is not None:
            await conn.execute(
                "UPDATE usuarios SET activo = $1, updated_at = NOW() WHERE id = $2",
                activo, uuid.UUID(user_id),
            )
        row = await conn.fetchrow(
            "SELECT id, email, nombre, rol, activo, created_at, updated_at FROM usuarios WHERE id = $1",
            uuid.UUID(user_id),
        )
        return dict(row) if row else None


async def delete_user(pool: asyncpg.Pool, user_id: str) -> bool:
    """
    Permanently delete a user. Returns True if deleted.
    Never deletes superadmin accounts. Does not delete the last active admin.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT rol FROM usuarios WHERE id = $1", uuid.UUID(user_id)
        )
        if row is None:
            return False
        if row["rol"] == "superadmin":
            raise HTTPException(
                status_code=403,
                detail="La cuenta superadmin no puede ser eliminada."
            )
        if row["rol"] == "admin":
            admin_count = await conn.fetchval(
                "SELECT COUNT(*) FROM usuarios WHERE rol = 'admin' AND activo = TRUE"
            )
            if admin_count <= 1:
                raise HTTPException(
                    status_code=400,
                    detail="No se puede eliminar el único administrador activo del sistema."
                )
        result = await conn.execute(
            "DELETE FROM usuarios WHERE id = $1", uuid.UUID(user_id)
        )
        return result == "DELETE 1"


# ── Audit logging ──────────────────────────────────────────────────────────────

async def log_audit(
    pool: asyncpg.Pool,
    usuario_id: str | None,
    accion: str,
    recurso: str | None = None,
    ip: str | None = None,
    detalle: str | None = None,
) -> None:
    """Record an action in the audit log. Never raises — audit logging must not block operations."""
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO sesiones_audit (usuario_id, accion, recurso, ip, detalle)
                VALUES ($1, $2, $3, $4, $5)
                """,
                uuid.UUID(usuario_id) if usuario_id else None,
                accion,
                recurso,
                ip,
                detalle,
            )
    except Exception:
        pass  # Audit logging never blocks the main operation


async def get_audit_logs(
    pool: asyncpg.Pool,
    limit: int = 100,
    offset: int = 0,
    usuario_id: str | None = None,
    accion: str | None = None,
) -> list[dict[str, Any]]:
    """Return audit logs with pagination and optional filters.

    W4-FIX: Uses a single static query with WHERE TRUE and AND clauses
    conditioned by NULL checks — no f-string SQL interpolation.
    All filter values are passed as positional $N params to asyncpg.
    """
    uid = uuid.UUID(usuario_id) if usuario_id else None
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT a.id, a.accion, a.recurso, a.ip, a.detalle, a.created_at,
                   u.email AS usuario_email, u.nombre AS usuario_nombre
            FROM sesiones_audit a
            LEFT JOIN usuarios u ON u.id = a.usuario_id
            WHERE ($1::uuid IS NULL OR a.usuario_id = $1)
              AND ($2::text IS NULL OR a.accion = $2)
            ORDER BY a.created_at DESC
            LIMIT $3 OFFSET $4
            """,
            uid,
            accion,
            limit,
            offset,
        )
        return [dict(r) for r in rows]


async def count_audit_logs(
    pool: asyncpg.Pool,
    usuario_id: str | None = None,
    accion: str | None = None,
) -> int:
    """Count audit logs with optional filters.

    W4-FIX: Static query with NULL-coalescing WHERE — no f-string SQL.
    """
    uid = uuid.UUID(usuario_id) if usuario_id else None
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM sesiones_audit
            WHERE ($1::uuid IS NULL OR usuario_id = $1)
              AND ($2::text IS NULL OR accion = $2)
            """,
            uid,
            accion,
        )


# ── Bootstrap / startup helpers ────────────────────────────────────────────────

async def ensure_admin_exists(pool: asyncpg.Pool) -> None:
    """
    Ensure at least one superadmin always exists.
    - If a superadmin already exists, do nothing.
    - If an admin with the configured email exists, promote it to superadmin.
    - If none exists, create the superadmin from environment variables.
    """
    async with pool.acquire() as conn:
        superadmin_exists = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM usuarios WHERE rol = 'superadmin')"
        )
        if superadmin_exists:
            return

        admin_email = os.getenv("IMAGIAX_ADMIN_EMAIL", "admin@imagiax.dev")
        admin_password = os.getenv("IMAGIAX_ADMIN_PASSWORD", "")
        admin_name = os.getenv("IMAGIAX_ADMIN_NAME", "Superadmin IMAGIAX")
        if not admin_password:
            raise RuntimeError("IMAGIAX_ADMIN_PASSWORD must be set in environment")

        try:
            validate_password_complexity(admin_password)
        except ValueError as exc:
            raise RuntimeError(
                "IMAGIAX_ADMIN_PASSWORD no cumple la política de contraseñas: "
                f"{exc}. Ajusta el .env antes de arrancar."
            ) from exc

        # Promote existing admin with that email to superadmin
        promoted = await conn.fetchval(
            "UPDATE usuarios SET rol = 'superadmin', updated_at = NOW() "
            "WHERE email = $1 AND rol = 'admin' RETURNING id",
            admin_email,
        )
        if promoted:
            logger.info("IMAGIAX Auth: usuario promovido a superadmin", extra={"email": admin_email})
            return

        # If not found, create as superadmin
        pw_hash = hash_password(admin_password)
        await conn.execute(
            """
            INSERT INTO usuarios (email, password_hash, nombre, rol)
            VALUES ($1, $2, $3, 'superadmin')
            ON CONFLICT (email) DO UPDATE SET rol = 'superadmin', updated_at = NOW()
            """,
            admin_email,
            pw_hash,
            admin_name,
        )
        logger.info("IMAGIAX Auth: usuario superadmin creado", extra={"email": admin_email})


async def upsert_bootstrap_superadmin_from_env(pool: asyncpg.Pool) -> None:
    """
    Align the IMAGIAX_ADMIN_* user in DB (same criteria as scripts/sync_imagiax_bootstrap_user.py).

    Resolves the case: another superadmin already existed and the env email was never inserted,
    so login with the documented credentials failed.

    In production (APP_ENV=production) this is not called from the API to avoid overwriting
    rotated passwords. Disable in local: IMAGIAX_ADMIN_UPSERT_FROM_ENV=0
    """
    if os.getenv("APP_ENV", "development").strip().lower() == "production":
        return
    if os.getenv("IMAGIAX_ADMIN_UPSERT_FROM_ENV", "1").strip().lower() in ("0", "false", "no"):
        return

    admin_email = os.getenv("IMAGIAX_ADMIN_EMAIL", "admin@imagiax.dev").lower().strip()
    admin_password = os.getenv("IMAGIAX_ADMIN_PASSWORD", "").strip()
    admin_name = os.getenv("IMAGIAX_ADMIN_NAME", "Superadmin IMAGIAX").strip()
    if not admin_password:
        return
    try:
        validate_password_complexity(admin_password)
    except ValueError:
        return

    pw_hash = hash_password(admin_password)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO usuarios (email, password_hash, nombre, rol)
            VALUES ($1, $2, $3, 'superadmin')
            ON CONFLICT (email) DO UPDATE SET
                password_hash = EXCLUDED.password_hash,
                nombre = EXCLUDED.nombre,
                rol = 'superadmin',
                activo = TRUE,
                updated_at = NOW()
            """,
            admin_email,
            pw_hash,
            admin_name,
        )
    logger.info("IMAGIAX Auth: usuario bootstrap sincronizado desde .env", extra={"email": admin_email})
