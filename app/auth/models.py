"""
app.auth.models — Pydantic models for authentication.

Data-only: no business logic here.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class UserPayload(BaseModel):
    """Payload extracted from a validated JWT. Available on every authenticated request."""

    user_id: str
    email: str
    nombre: str
    rol: str
    jti: str | None = None
    fp: str | None = None  # Session fingerprint (User-Agent hash, 16 hex chars)
    # SEC-H03: Session type preserved from login. False for old JWTs without this claim.
    # Used by sliding-window renewal to avoid extending normal sessions to remember-me duration.
    remember: bool = False
    # Tenant identity: clinic_id and scope carried in JWT for request-level scoping.
    # Old JWTs (issued before tenant fields were added) decode with both as None —
    # fail-closed logic treats None scope as missing tenant context (401).
    clinic_id: str | None = None
    scope: str | None = None  # "clinic" | "global"


class LoginRequest(BaseModel):
    # SEC-M01: max_length guards against oversized payloads that could cause
    # bcrypt DoS (bcrypt silently truncates at 72 bytes; long strings waste CPU).
    # RFC 5321 limits email to 254 chars; 128 is a sane upper bound for passwords.
    email: str = Field(..., max_length=254)
    password: str = Field(..., max_length=128)
    remember: bool = False


class RegisterRequest(BaseModel):
    # SEC-M01: Consistent max_length on all string inputs in auth models.
    email: str = Field(..., max_length=254)
    password: str = Field(..., max_length=128)
    nombre: str = Field(..., max_length=100)
    rol: str = Field(default="medico", max_length=20)


class ChangePasswordRequest(BaseModel):
    # SEC-M01: Same limits as LoginRequest — current_password is bcrypt-verified,
    # new_password goes through validate_password_complexity before hashing.
    current_password: str = Field(..., max_length=128)
    new_password: str = Field(..., max_length=128)
