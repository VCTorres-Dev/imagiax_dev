"""
app.core.config — IMAGIAX centralised configuration.

All os.getenv() calls from across the monolith are consolidated here into a
single immutable Settings object validated at startup via pydantic-settings.

Usage:
    from app.core.config import settings

    settings.POSTGRES_URL          # str
    settings.GEMINI_API_KEY        # SecretStr  → settings.GEMINI_API_KEY.get_secret_value()
    settings.JWT_SECRET            # SecretStr
"""

from __future__ import annotations

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Immutable application settings loaded once at process start.

    Sources (in priority order):
    1. Environment variables
    2. .env file in the working directory (utf-8)
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # Extra fields are silently ignored — safe to add env vars without breaking startup
        extra="ignore",
    )

    # ── Application ───────────────────────────────────────────────────────────
    # APP_ENV: "development" | "production"
    APP_ENV: str = "development"
    IMAGIAX_BASE_URL: str = "http://localhost:8010"
    IMAGIAX_VIEWER_URL: str = "http://localhost:3000"
    CORS_ORIGINS: str = "http://localhost:3000,http://127.0.0.1:3000"
    # ── Bootstrap mode ────────────────────────────────────────────────────
    # IMAGIAX_BOOTSTRAP_MODE: when "1" AND APP_ENV=development, tenant checks
    # are relaxed for initial setup. When absent or "0", tenant validation is
    # enforced on all operations. When "1" AND APP_ENV=production, the app
    # refuses to start — bootstrap mode is not permitted in production.
    IMAGIAX_BOOTSTRAP_MODE: str = "0"

    # Doctor / brand identity (exposed to frontend via /config endpoint)
    IMAGIAX_DOCTOR_NAME: str = ""
    IMAGIAX_DOCTOR_SPECIALTY: str = ""
    IMAGIAX_DOCTOR_LICENSE: str = ""
    IMAGIAX_BRAND_NAME: str = ""
    IMAGIAX_BRAND_SUBTITLE: str = ""

    # ── Database ──────────────────────────────────────────────────────────────
    # POSTGRES_URL is REQUIRED — startup raises RuntimeError if absent.
    # Format: postgresql://user:password@host:port/dbname
    POSTGRES_URL: str = ""

    # ── Authentication / JWT ──────────────────────────────────────────────────
    # JWT_SECRET is REQUIRED — must be ≥32 chars. Generate with:
    #   python -c "import secrets; print(secrets.token_hex(32))"
    JWT_SECRET: SecretStr = SecretStr("")
    JWT_EXPIRE_HOURS: int = 24
    # "Remember me" session lifetime (panel admin)
    JWT_REMEMBER_HOURS: int = 48
    # Optional issuer/audience claims — empty string means omitted from token
    JWT_ISSUER: str = "imagiax.dev"
    JWT_AUDIENCE: str = "imagiax-api"

    # ── Orthanc PACS ─────────────────────────────────────────────────────────
    ORTHANC_BASE_URL: str = "http://localhost:8042"
    # Basic-auth credentials for Orthanc (optional)
    ORTHANC_USER: str = ""
    ORTHANC_PASSWORD: SecretStr = SecretStr("")
    # Pre-computed "Basic <base64>" header value (takes precedence over user/password if set)
    ORTHANC_AUTH_HEADER: str = ""
    IMAGIAX_HTTP_TIMEOUT: float = 30.0

    # ── Google Gemini ─────────────────────────────────────────────────────────
    GEMINI_API_KEY: SecretStr = SecretStr("")
    # Model name is code-constant, not env-driven — but kept here for override capability
    GEMINI_MODEL: str = "gemini-2.5-flash"

    # ── Pipeline v1.1 settings (ai-pipeline-v1.1, T-02) ─────────────────────
    # Default pipeline version ("1.0" = v1 only, "1.1" = enable new phases)
    PIPELINE_VERSION: str = "1.1"
    # Gemini Pro model for selective auditor
    GEMINI_PRO_MODEL: str = "gemini-2.5-pro"
    # Global hard deadline for the entire v1.1 pipeline (seconds).
    # Must be > Gemini worst-case: 4 attempts × 60 s + 35 s backoff = 275 s.
    # Set to 300 s to give a 25 s buffer above the Gemini retry worst case.
    PIPELINE_GLOBAL_TIMEOUT: int = 300
    # Per-phase timeouts (seconds)
    QUALITY_GATE_TIMEOUT: int = 10
    AUDITOR_TIMEOUT: int = 20
    # Feature flags
    QUALITY_GATE_ENABLED: bool = True
    AUDITOR_ENABLED: bool = True

    # ── MercadoPago ───────────────────────────────────────────────────────────
    MP_ACCESS_TOKEN: SecretStr = SecretStr("")
    MP_PUBLIC_KEY: str = ""
    # "testing" | "production"
    IMAGIAX_PAYMENT_MODE: str = "testing"
    # Webhook HMAC secret from MP panel → Webhooks → Secret key
    MP_WEBHOOK_SECRET: SecretStr = SecretStr("")
    # Override for ngrok/public tunnel during local testing
    MP_CALLBACK_BASE_URL: str = ""
    # Price per scan in CLP (Chilean Peso)
    IMAGIAX_PRECIO_SCAN: int = 990

    # ── Transbank Oneclick Mall ───────────────────────────────────────────────
    TBK_MALL_COMMERCE_CODE: str = "597055555541"
    TBK_CHILD_COMMERCE_CODE: str = "597055555542"
    TBK_API_KEY: SecretStr = SecretStr("")  # Set TBK_API_KEY env var (never hardcode)
    # "integration" | "production"
    TBK_ENVIRONMENT: str = "integration"

    # ── Derived / computed properties ─────────────────────────────────────────

    @property
    def is_production(self) -> bool:
        """True when APP_ENV == 'production'."""
        return self.APP_ENV == "production"

    @property
    def bootstrap_mode(self) -> bool:
        """True when IMAGIAX_BOOTSTRAP_MODE == '1' (only safe in development)."""
        return self.IMAGIAX_BOOTSTRAP_MODE.strip() == "1"

    @property
    def cors_origins_list(self) -> list[str]:
        """CORS_ORIGINS split into a list, with whitespace trimmed."""
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def jwt_issuer(self) -> str | None:
        """None when JWT_ISSUER is empty (omit claim from token)."""
        return self.JWT_ISSUER.strip() or None

    @property
    def jwt_audience(self) -> str | None:
        """None when JWT_AUDIENCE is empty (omit claim from token)."""
        return self.JWT_AUDIENCE.strip() or None

    @property
    def mp_callback_base_url(self) -> str:
        """Resolve callback base URL with the same fallback logic as the monolith."""
        if self.MP_CALLBACK_BASE_URL:
            return self.MP_CALLBACK_BASE_URL.rstrip("/")
        if not self.IMAGIAX_BASE_URL.startswith("http://localhost"):
            return self.IMAGIAX_BASE_URL.rstrip("/")
        # Local dev placeholder — won't receive webhooks but checkout still works
        return "https://imagiax.cl"

    # ── Validators ───────────────────────────────────────────────────────────

    @field_validator("POSTGRES_URL")
    @classmethod
    def postgres_url_required(cls, v: str) -> str:
        if not v:
            raise ValueError(
                "POSTGRES_URL is required. "
                "Add POSTGRES_URL=postgresql://user:pass@host:port/db to your .env file."
            )
        return v

    @field_validator("JWT_SECRET")
    @classmethod
    def jwt_secret_required(cls, v: SecretStr) -> SecretStr:
        raw = v.get_secret_value()
        if not raw:
            raise ValueError(
                "JWT_SECRET is required. "
                "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
            )
        if len(raw) < 32:
            raise ValueError(
                f"JWT_SECRET must be at least 32 characters (got {len(raw)}). "
                "Generate a secure one with: python -c \"import secrets; print(secrets.token_hex(32))\""
            )
        return v

    @model_validator(mode="after")
    def payment_mode_matches_app_env(self) -> "Settings":
        """SEC-PAY-006: production APP_ENV must use production payment mode."""
        if self.APP_ENV == "production" and self.IMAGIAX_PAYMENT_MODE != "production":
            raise ValueError(
                "IMAGIAX_PAYMENT_MODE must be 'production' when APP_ENV=production. "
                "Set IMAGIAX_PAYMENT_MODE=production in your .env.production file."
            )
        return self

    @model_validator(mode="after")
    def cors_no_wildcard(self) -> "Settings":
        """SEC-BE-008: wildcard CORS is forbidden when credentials=True."""
        if "*" in self.cors_origins_list:
            raise ValueError(
                "CORS_ORIGINS contains wildcard '*' — forbidden when allow_credentials=True. "
                "Specify explicit origins in CORS_ORIGINS env var."
            )
        return self

    @model_validator(mode="after")
    def bootstrap_not_in_production(self) -> "Settings":
        """Bootstrap mode is not permitted in production — would bypass tenant checks."""
        if self.APP_ENV == "production" and self.IMAGIAX_BOOTSTRAP_MODE.strip() == "1":
            raise ValueError(
                "IMAGIAX_BOOTSTRAP_MODE=1 is not permitted when APP_ENV=production. "
                "Tenant validation must be enforced in production. "
                "Set IMAGIAX_BOOTSTRAP_MODE=0 or remove it from your .env file."
            )
        return self


# ── Singleton ─────────────────────────────────────────────────────────────────
# Loaded once at import time. Raises ValidationError on missing required fields.
# All modules import this directly — no Depends() needed (settings are immutable).
settings = Settings()
