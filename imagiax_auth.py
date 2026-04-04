"""
IMAGIAX — Módulo de autenticación JWT

Gestiona usuarios, sesiones y control de acceso basado en roles (RBAC).
Roles: admin, medico, recepcion.

Uso desde FastAPI:
    from imagiax_auth import get_current_user, require_role, UserPayload

    @app.get("/protected")
    async def ruta_protegida(user: UserPayload = Depends(get_current_user)):
        ...

    @app.post("/admin-only")
    async def admin_only(user: UserPayload = Depends(require_role("admin"))):
        ...
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import jwt as _jwt
from jwt.exceptions import InvalidTokenError as JWTError
from passlib.context import CryptContext
from pydantic import BaseModel

# ── Configuración ──────────────────────────────────────────────────────────────

# Clave secreta para firmar tokens JWT. OBLIGATORIO — falla en startup si no está configurado.
JWT_SECRET = os.getenv("JWT_SECRET", "")
if not JWT_SECRET:
    raise RuntimeError(
        "JWT_SECRET env var is required. "
        "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
    )
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = int(os.getenv("JWT_EXPIRE_HOURS", "24"))
# Sesión “recordarme” en el panel (tope superior; requiere HTTPS + cookie Secure en prod)
JWT_REMEMBER_HOURS = int(os.getenv("JWT_REMEMBER_HOURS", "168"))
JWT_ISSUER = os.getenv("JWT_ISSUER", "").strip() or None
JWT_AUDIENCE = os.getenv("JWT_AUDIENCE", "").strip() or None

# Hash de contraseñas con bcrypt — work factor explícito (12 rounds mínimo para PHI médica)
_pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)

# Esquema de autenticación Bearer
_bearer_scheme = HTTPBearer(auto_error=False)

# ── Login rate limiter (proteccion brute force) ──────────────────────────────
_LOGIN_MAX_ATTEMPTS = int(os.getenv("LOGIN_MAX_ATTEMPTS", "5"))
_LOGIN_WINDOW_SECONDS = int(os.getenv("LOGIN_WINDOW_SECONDS", "300"))  # 5 min
_login_attempts: dict[str, list[float]] = {}  # email → [timestamps]
_LOGIN_CLEANUP_INTERVAL = 600  # 10 minutos
_last_login_cleanup: float = 0.0

import time as _time


def _cleanup_login_attempts() -> None:
    """Limpia intentos expirados para evitar memory leak por emails aleatorios (DoS)."""
    global _last_login_cleanup
    now = _time.time()
    if now - _last_login_cleanup < _LOGIN_CLEANUP_INTERVAL:
        return
    _last_login_cleanup = now
    cutoff = now - _LOGIN_WINDOW_SECONDS
    # Eliminar entries completamente vacías
    expired = [k for k, v in _login_attempts.items() if not v or all(t < cutoff for t in v)]
    for k in expired:
        del _login_attempts[k]
    # Limpiar timestamps viejos de entries activas
    for k in _login_attempts:
        _login_attempts[k] = [t for t in _login_attempts[k] if t >= cutoff]


def _check_login_rate(email: str) -> bool:
    """Retorna True si el login esta permitido, False si excede el limite."""
    _cleanup_login_attempts()
    now = _time.time()
    key = email.lower().strip()
    attempts = _login_attempts.get(key, [])
    # Limpiar intentos fuera de la ventana
    attempts = [t for t in attempts if now - t < _LOGIN_WINDOW_SECONDS]
    _login_attempts[key] = attempts
    return len(attempts) < _LOGIN_MAX_ATTEMPTS

def _record_login_attempt(email: str) -> None:
    """Registra un intento de login."""
    key = email.lower().strip()
    if key not in _login_attempts:
        _login_attempts[key] = []
    _login_attempts[key].append(_time.time())

# Roles permitidos — superadmin es el nivel más alto, por encima de admin
ROLES_VALIDOS = ("superadmin", "admin", "medico", "recepcion")


# ── Modelos ────────────────────────────────────────────────────────────────────

class UserPayload(BaseModel):
    """Payload extraído del JWT. Disponible en cada request autenticado."""
    user_id: str
    email: str
    nombre: str
    rol: str
    jti: str | None = None


class LoginRequest(BaseModel):
    email: str
    password: str
    remember: bool = False


class RegisterRequest(BaseModel):
    email: str
    password: str
    nombre: str
    rol: str = "medico"


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


# ── Funciones de hash ──────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    """Genera hash bcrypt de una contraseña."""
    return _pwd_ctx.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """Verifica contraseña contra su hash bcrypt."""
    return _pwd_ctx.verify(plain, hashed)


# ── JWT ────────────────────────────────────────────────────────────────────────

def create_access_token(
    user_id: str,
    email: str,
    nombre: str,
    rol: str,
    *,
    expire_hours: float | None = None,
) -> str:
    """Crea un JWT con payload del usuario."""
    hours = float(JWT_EXPIRE_HOURS if expire_hours is None else expire_hours)
    expire = datetime.now(timezone.utc) + timedelta(hours=hours)
    payload = {
        "sub": user_id,
        "email": email,
        "nombre": nombre,
        "rol": rol,
        "exp": expire,
        "jti": uuid.uuid4().hex,
    }
    return _jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def extract_raw_token(request: Request) -> str | None:
    """Bearer o cookie httpOnly (misma prioridad que get_current_user)."""
    auth = request.headers.get("authorization") or request.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.cookies.get("imagiax_session")


def get_token_expires_at_utc(token: str) -> datetime:
    """Lee el claim exp del JWT (ya firmado)."""
    payload = _jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    return datetime.fromtimestamp(int(payload["exp"]), tz=timezone.utc)


def decode_token(token: str) -> UserPayload:
    """Decodifica y valida un JWT. Lanza HTTPException si es inválido."""
    try:
        payload = _jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return UserPayload(
            user_id=payload["sub"],
            email=payload["email"],
            nombre=payload["nombre"],
            rol=payload["rol"],
            jti=payload.get("jti"),
        )
    except JWTError:
        raise HTTPException(status_code=401, detail="Token inválido o expirado")


def verify_session_cookie_token(token: str | None) -> bool:
    """
    Verificación minimalista para /auth/verify (sin DB):
    - firma JWT válida
    - exp requerido y válido
    - nbf válido cuando existe
    - iss/aud sólo si están configurados en env
    """
    if not token:
        return False
    try:
        _jwt.decode(
            token,
            JWT_SECRET,
            algorithms=[JWT_ALGORITHM],
            options={
                "verify_signature": True,
                "verify_exp": True,
                "verify_nbf": True,
                "require": ["exp"],
            },
            issuer=JWT_ISSUER,
            audience=JWT_AUDIENCE,
        )
        return True
    except JWTError:
        return False


# ── Dependencias FastAPI ───────────────────────────────────────────────────────

async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    request: Request = None,
) -> UserPayload:
    """
    Dependencia FastAPI: extrae el usuario desde Bearer token o cookie httpOnly.
    Bearer tiene precedencia (para clientes API). Cookie para el panel admin.
    Verifica también si el usuario fue eliminado (revoked_users).
    """
    token: str | None = None
    if credentials:
        token = credentials.credentials
    elif request:
        token = request.cookies.get("imagiax_session")
    if not token:
        raise HTTPException(status_code=401, detail="Autenticación requerida")
    user = decode_token(token)
    if user.jti and await is_jti_revoked(user.jti):
        raise HTTPException(status_code=401, detail="Sesión revocada")
    # Verificar si el usuario completo fue revocado (eliminado o logout global)
    if await is_user_revoked(user.user_id):
        raise HTTPException(status_code=401, detail="Cuenta desactivada o eliminada")
    return user


async def is_user_revoked(user_id: str) -> bool:
    """Retorna True si el usuario fue eliminado o forzado a logout global."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        return bool(await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM revoked_users WHERE user_id = $1)",
            uuid.UUID(user_id),
        ))


def require_role(*allowed_roles: str):
    """
    Factory de dependencia para restringir por rol.
    superadmin pasa SIEMPRE — tiene privilegios sobre todos los roles.
    Uso: user = Depends(require_role("admin", "medico"))
    """
    async def _check(user: UserPayload = Depends(get_current_user)) -> UserPayload:
        if user.rol != "superadmin" and user.rol not in allowed_roles:
            raise HTTPException(
                status_code=403,
                detail=f"Permiso denegado. Rol requerido: {', '.join(allowed_roles)}"
            )
        return user
    return _check


# ── Funciones de base de datos ─────────────────────────────────────────────────
# Usan el pool de imagiax_db para no crear otro pool separado.

async def _get_pool():
    """Obtiene el pool de conexiones de imagiax_db."""
    import imagiax_db
    return imagiax_db.get_pool()


async def create_auth_tables() -> None:
    """Crea las tablas de usuarios y audit. Llamar en lifespan."""
    pool = await _get_pool()
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
        # Migración: actualizar constraint para incluir superadmin en instancias existentes
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
        # Tabla para revocar todos los tokens de un usuario (usado al eliminar cuenta)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS revoked_users (
                user_id     UUID PRIMARY KEY,
                revoked_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)


async def revoke_token(jti: str, user_id: str, expires_at: Any) -> None:
    """Agrega un JTI a la lista negra de tokens (logout explícito)."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO revoked_tokens (jti, user_id, expires_at) "
            "VALUES ($1, $2, $3) ON CONFLICT (jti) DO NOTHING",
            jti, uuid.UUID(user_id), expires_at,
        )


async def revoke_all_user_tokens(user_id: str) -> None:
    """Revoca TODOS los tokens activos de un usuario (usado al eliminar cuenta o forzar logout global)."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO revoked_users (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING",
            uuid.UUID(user_id),
        )


async def is_jti_revoked(jti: str) -> bool:
    """
    Retorna True si el JTI fue explícitamente revocado (logout).
    Note: is_user_revoked() is checked separately in decode_token() — no need to duplicate here.
    SEC-BE-005: Fixed dead code — previously always returned True if JTI existed in revoked_tokens.
    """
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT jti FROM revoked_tokens WHERE jti = $1 AND expires_at > NOW()",
            jti,
        )
        return row is not None  # True only if this specific JTI was explicitly revoked


async def get_user_by_email(email: str) -> dict[str, Any] | None:
    """Busca un usuario por email. Retorna dict con todos los campos o None."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, email, password_hash, nombre, rol, activo FROM usuarios WHERE email = $1",
            email,
        )
        return dict(row) if row else None


async def get_user_by_id(user_id: str) -> dict[str, Any] | None:
    """Busca un usuario por UUID."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, email, nombre, rol, activo, created_at FROM usuarios WHERE id = $1",
            uuid.UUID(user_id),
        )
        return dict(row) if row else None


def validate_password_complexity(password: str) -> None:
    """
    Verifica que la contraseña cumpla los requisitos mínimos para datos médicos PHI.
    Lanza ValueError con mensaje descriptivo si no cumple.
    """
    if len(password) < 12:
        raise ValueError("La contraseña debe tener al menos 12 caracteres")
    if not any(c.isupper() for c in password):
        raise ValueError("La contraseña debe incluir al menos una letra mayúscula")
    if not any(c.isdigit() for c in password):
        raise ValueError("La contraseña debe incluir al menos un número")
    if not any(c in "!@#$%^&*()-_=+[]{}|;:,.<>?/" for c in password):
        raise ValueError("La contraseña debe incluir al menos un carácter especial (!@#$%...)")


async def create_user(email: str, password: str, nombre: str, rol: str = "medico") -> dict[str, Any]:
    """Crea un nuevo usuario. Retorna dict con id, email, nombre, rol."""
    if rol not in ROLES_VALIDOS:
        raise ValueError(f"Rol inválido: {rol}")
    validate_password_complexity(password)
    pw_hash = hash_password(password)
    pool = await _get_pool()
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


async def update_password(user_id: str, new_password: str) -> None:
    """Actualiza la contraseña de un usuario."""
    validate_password_complexity(new_password)
    pw_hash = hash_password(new_password)
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE usuarios SET password_hash = $1, updated_at = NOW() WHERE id = $2",
            pw_hash,
            uuid.UUID(user_id),
        )


async def list_users() -> list[dict[str, Any]]:
    """Lista todos los usuarios (sin password_hash)."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, email, nombre, rol, activo, created_at FROM usuarios ORDER BY created_at DESC"
        )
        return [dict(r) for r in rows]


async def toggle_user_active(user_id: str, activo: bool) -> None:
    """Activa o desactiva un usuario."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE usuarios SET activo = $1, updated_at = NOW() WHERE id = $2",
            activo,
            uuid.UUID(user_id),
        )


async def log_audit(
    usuario_id: str | None,
    accion: str,
    recurso: str | None = None,
    ip: str | None = None,
    detalle: str | None = None,
) -> None:
    """Registra una acción en el log de auditoría."""
    try:
        pool = await _get_pool()
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
        pass  # Audit logging nunca debe bloquear la operación principal


async def ensure_admin_exists() -> None:
    """
    Garantiza que siempre exista al menos un superadmin.
    - Si ya existe un superadmin, no hace nada.
    - Si existe un admin con el email configurado en env, lo promueve a superadmin.
    - Si no existe ninguno, crea el superadmin desde las variables de entorno.
    """
    pool = await _get_pool()
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

        # Misma política que create_user / registro (PHI): evita superadmin débil en primer arranque
        try:
            validate_password_complexity(admin_password)
        except ValueError as exc:
            raise RuntimeError(
                "IMAGIAX_ADMIN_PASSWORD no cumple la política de contraseñas: "
                f"{exc}. Ajusta el .env antes de arrancar."
            ) from exc

        # Promover admin existente con ese email a superadmin
        promoted = await conn.fetchval(
            "UPDATE usuarios SET rol = 'superadmin', updated_at = NOW() "
            "WHERE email = $1 AND rol = 'admin' RETURNING id",
            admin_email,
        )
        if promoted:
            print(f"IMAGIAX Auth: usuario '{admin_email}' promovido a superadmin")
            return

        # Si no existe, crearlo como superadmin
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
        print(f"IMAGIAX Auth: usuario superadmin creado ({admin_email})")


async def upsert_bootstrap_superadmin_from_env() -> None:
    """
    Alinea en DB el usuario IMAGIAX_ADMIN_* (mismo criterio que scripts/sync_imagiax_bootstrap_user.py).

    Resuelve el caso: ya existía otro superadmin (p. ej. admin@imagiax.dev) y el email del .env
    nunca se insertó, por lo que el login con las credenciales documentadas fallaba.

    En producción (APP_ENV=production) no se llama desde la API para no pisar contraseñas rotadas.
    Desactivar en local: IMAGIAX_ADMIN_UPSERT_FROM_ENV=0
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
    pool = await _get_pool()
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
    print(f"IMAGIAX Auth: usuario bootstrap sincronizado desde .env ({admin_email})")


async def update_user(
    user_id: str,
    *,
    nombre: str | None = None,
    rol: str | None = None,
    activo: bool | None = None,
) -> dict[str, Any] | None:
    """
    Actualiza campos de un usuario. Solo actualiza los campos que se pasen.
    Retorna el usuario actualizado o None si no existe.
    La cuenta superadmin no puede ser desactivada ni cambiarle el rol.
    """
    if rol is not None and rol not in ROLES_VALIDOS:
        raise HTTPException(status_code=400, detail=f"Rol inválido: {rol}")
    pool = await _get_pool()
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


async def delete_user(user_id: str) -> bool:
    """
    Elimina un usuario permanentemente. Retorna True si fue eliminado.
    Nunca elimina cuentas superadmin. No elimina el último admin activo.
    """
    pool = await _get_pool()
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


async def get_audit_logs(
    limit: int = 100,
    offset: int = 0,
    usuario_id: str | None = None,
    accion: str | None = None,
) -> list[dict[str, Any]]:
    """Retorna logs de auditoría con paginación y filtros opcionales."""
    pool = await _get_pool()
    conditions: list[str] = []
    params: list[Any] = []
    idx = 1

    if usuario_id:
        conditions.append(f"a.usuario_id = ${idx}")
        params.append(uuid.UUID(usuario_id))
        idx += 1
    if accion:
        conditions.append(f"a.accion = ${idx}")
        params.append(accion)
        idx += 1

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params += [limit, offset]

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT a.id, a.accion, a.recurso, a.ip, a.detalle, a.created_at,
                   u.email AS usuario_email, u.nombre AS usuario_nombre
            FROM sesiones_audit a
            LEFT JOIN usuarios u ON u.id = a.usuario_id
            {where}
            ORDER BY a.created_at DESC
            LIMIT ${idx} OFFSET ${idx + 1}
            """,
            *params,
        )
        return [dict(r) for r in rows]


async def count_audit_logs(
    usuario_id: str | None = None,
    accion: str | None = None,
) -> int:
    """Cuenta logs de auditoría con filtros opcionales."""
    pool = await _get_pool()
    conditions: list[str] = []
    params: list[Any] = []
    idx = 1

    if usuario_id:
        conditions.append(f"usuario_id = ${idx}")
        params.append(uuid.UUID(usuario_id))
        idx += 1
    if accion:
        conditions.append(f"accion = ${idx}")
        params.append(accion)
        idx += 1

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    async with pool.acquire() as conn:
        return await conn.fetchval(
            f"SELECT COUNT(*) FROM sesiones_audit {where}", *params
        )
