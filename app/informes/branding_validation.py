"""
app.informes.branding_validation — Shared validation helpers for branding payloads.

Provides:
- BraningRequest: Pydantic model for /me/branding PUT payload.
  Extra fields (e.g. client-supplied user_id) are silently ignored;
  identity is derived exclusively from the authenticated session.
- Size validators for base64-encoded logo and signature images.

Size limits (design spec):
- logo_b64: ≤ 512 KiB decoded
- signature_b64: ≤ 256 KiB decoded
"""

from __future__ import annotations

import base64

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ── Size limits (decoded bytes) ──────────────────────────────────────────────

LOGO_MAX_BYTES: int = 512 * 1024  # 512 KiB
SIGNATURE_MAX_BYTES: int = 256 * 1024  # 256 KiB


def validate_base64_size(
    value: str | None, max_bytes: int, field_name: str
) -> str | None:
    """Validate that a base64-encoded string decodes to at most ``max_bytes``.

    Returns the value unchanged if valid. Raises ``ValueError`` otherwise.
    ``None`` is always valid (optional fields).
    """
    if value is None:
        return value
    try:
        decoded = base64.b64decode(value, validate=True)
    except Exception:
        raise ValueError(f"{field_name} no es base64 válido")
    if len(decoded) > max_bytes:
        max_kib = max_bytes // 1024
        actual_kib = len(decoded) // 1024
        raise ValueError(
            f"{field_name} excede el límite de {max_kib} KiB "
            f"({actual_kib} KiB recibidos)"
        )
    return value


class BrandingRequest(BaseModel):
    """Request model for self-service branding updates (/me/branding PUT).

    All fields are optional — None means "keep existing value".
    Extra fields (e.g. client-supplied user_id) are silently ignored;
    identity is derived exclusively from the authenticated session.
    """

    model_config = ConfigDict(extra="ignore")

    # Always forced to None — identity comes from auth session, never from client.
    # extra="ignore" drops unknown fields; this validator drops the known one.
    user_id: str | None = None
    brand_name: str | None = Field(None, max_length=100)
    brand_subtitle: str | None = Field(None, max_length=200)
    doctor_name: str | None = Field(None, max_length=200)
    doctor_specialty: str | None = Field(None, max_length=200)
    doctor_license: str | None = Field(None, max_length=50)
    logo_b64: str | None = None
    signature_b64: str | None = None

    @field_validator("user_id", mode="before")
    @classmethod
    def _ignore_user_id(cls, v: str | None) -> None:
        """Always clear user_id. Identity comes exclusively from session."""
        return None

    @field_validator("logo_b64", mode="before")
    @classmethod
    def _validate_logo_size(cls, v: str | None) -> str | None:
        return validate_base64_size(v, LOGO_MAX_BYTES, "logo_b64")

    @field_validator("signature_b64", mode="before")
    @classmethod
    def _validate_signature_size(cls, v: str | None) -> str | None:
        return validate_base64_size(v, SIGNATURE_MAX_BYTES, "signature_b64")