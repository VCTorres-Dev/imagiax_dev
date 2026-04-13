"""
app.auth.models — Pydantic models for authentication.

Data-only: no business logic here.
"""

from __future__ import annotations

from pydantic import BaseModel


class UserPayload(BaseModel):
    """Payload extracted from a validated JWT. Available on every authenticated request."""

    user_id: str
    email: str
    nombre: str
    rol: str
    jti: str | None = None
    fp: str | None = None  # Session fingerprint (User-Agent hash, 16 hex chars)


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
