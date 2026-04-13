# Design: Security Audit Remediation

## Technical Approach

Incremental 4-phase remediation of 27 vulnerabilities across 7 files. Each phase is independently deployable. No new dependencies; all fixes use existing patterns (asyncpg raw queries, FastAPI `Depends`, Pydantic models). No pytest — manual regression via cURL/browser.

## Architecture Decisions

| # | Decision | Alternatives | Rationale | Trade-off |
|---|----------|-------------|-----------|-----------|
| AD-1 | `/health/db`: Strip error details, keep unauthenticated | Add JWT auth; both | Health probes must work without JWT for k8s/Docker. Stripping `exc!s` removes the leak without breaking liveness checks. |
| AD-2 | Proxy: Replace `verify_session_cookie_token` with `decode_token` + `is_user_revoked` (not full `get_current_user`) | Use `get_current_user` directly | `get_current_user` depends on `Depends()` injection — proxy uses raw cookie extraction. Manual decode + revocation check achieves same security with 1 DB query (revoked_users) instead of 2. | Still 1 DB hit per proxy request; acceptable at current scale (workers=1). |
| AD-3 | Path traversal: Whitelist-based validation via regex, not blacklist | `PurePosixPath` resolve; simple `".." in path` check | DICOMweb paths follow strict patterns (`studies/X/series/Y/...`). Whitelist regex `^[a-zA-Z0-9._/-]+$` + reject `..` is safest. Blacklist-only misses encoded variants. | Slightly stricter — rejects any path with unusual chars (acceptable for DICOMweb). |
| AD-4 | IDOR fix: Keep `CobrarRequest` fields but IGNORE `username`/`amount` from body | Remove fields entirely (breaking) | Backward compatible — frontend sends same body, server silently overrides. Add deprecation log. | Old field values silently ignored; could confuse API consumers. |
| AD-5 | TOCTOU: Single `INSERT ... ON CONFLICT DO NOTHING RETURNING` instead of SELECT-then-INSERT | Wrap in `async with conn.transaction()` with isolation | `ON CONFLICT DO NOTHING RETURNING` is atomic by definition in PostgreSQL. If `RETURNING` returns nothing, the row already existed → skip. Simpler, no isolation level tuning needed. | Requires restructuring `acreditar_pago()` slightly. |
| AD-6 | Payment mode: Explicit `IMAGIAX_PAYMENT_MODE` env var replaces `_IS_TEST` | Keep `_IS_TEST` + add startup validation only | Token prefix is unreliable for security decisions. Explicit env var is unambiguous and cross-validates with `APP_ENV`. | One more env var to configure. |
| AD-7 | Async TBK SDK: `asyncio.get_running_loop().run_in_executor(None, fn)` | Write async wrapper; use `anyio.to_thread` | `run_in_executor` is stdlib, no new deps. Thread pool default (min(32, cpu+4)) is sufficient for TBK's low concurrency. | Thread pool overhead per call; negligible for 2-10s SDK calls. |

## Data Flow

### Path Traversal Fix (SEC-BE-003)

```
Browser → /pacs/dicom-web/{path}
         ↓
  sanitize_dicomweb_path(path) ──[FAIL]──→ 400 Bad Request
         ↓ [PASS]
  verify token + revocation check (AD-2)
         ↓
  Proxy to Orthanc /dicom-web/{sanitized_path}
```

### Cobrar Fix (SEC-PAY-003+004)

```
POST /tbk/cobrar { username: X, amount: Y }
         ↓
  get_current_user(JWT) → user.user_id
         ↓
  IGNORE body.username → use user.user_id
  IGNORE body.amount  → use PRECIO_SCAN_CLP
         ↓
  get_inscripcion_oneclick_any(usuario_id=user.user_id)
         ↓
  cobrar_scan_oneclick(username=db.username, amount=PRECIO_SCAN_CLP)
```

## File Changes

| File | Action | Description |
|------|--------|-------------|
| `servidor_api_orthanc_v1.py` | Modify | SEC-BE-003: Add `_sanitize_dicomweb_path()`. SEC-BE-002: Revocation check in proxy. SEC-BE-001: Strip error in `/health/db`. SEC-AI-001: `le=50` on `GenerarInformeRequest.max_instances`. SEC-BE-007: CSP header. SEC-BE-009: `le=50` on listado. |
| `pagos_tbk.py` | Modify | SEC-PAY-003+004: Override `username`/`amount` from JWT/constant. SEC-AI-002: `run_in_executor` wrapper. SEC-PAY-006+007: Replace `_IS_TEST` with `IMAGIAX_PAYMENT_MODE`. SEC-AI-003: Opaque error codes in redirects. |
| `pagos_mp.py` | Modify | SEC-PAY-001: Remove `_IS_TEST` skip in HMAC. SEC-PAY-002: Add `get_current_user` to `/pagos/exito`. SEC-PAY-006: Replace `_IS_TEST`. SEC-PAY-010: Strict `external_reference` validation. |
| `imagiax_pagos_db.py` | Modify | SEC-PAY-005: Atomic `INSERT ON CONFLICT` in `acreditar_pago()`. SEC-PAY-009: PCI masking function for `raw_webhook`/`raw_response`. |
| `imagiax_auth.py` | Modify | SEC-BE-005: Fix dead code in `is_jti_revoked()`. |
| `.env` | Modify | Add `IMAGIAX_PAYMENT_MODE=testing`, `MP_WEBHOOK_SECRET=<sandbox_secret>`. |
| `.env.production` | Modify | Add `IMAGIAX_PAYMENT_MODE=production`, rotate credentials (manual step). |

## Interfaces / Contracts

### Phase 1 — Critical

```python
# servidor_api_orthanc_v1.py — NEW FUNCTION
import re

_DICOMWEB_PATH_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9._/:-]*$')

def _sanitize_dicomweb_path(path: str) -> str:
    """Validates DICOMweb proxy path. Raises HTTPException(400) on violation."""
    if '..' in path or path.startswith('/') or not _DICOMWEB_PATH_RE.match(path):
        raise HTTPException(400, "Invalid DICOMweb path")
    return path

# servidor_api_orthanc_v1.py — pacs_dicom_proxy MODIFIED auth block
async def pacs_dicom_proxy(path: str, request: Request, response: Response):
    token = request.cookies.get("imagiax_session")
    if not token:
        return JSONResponse(status_code=401, content={"detail": "No autenticado"})
    try:
        user = imagiax_auth.decode_token(token)
    except HTTPException:
        return JSONResponse(status_code=401, content={"detail": "Sesión inválida"})
    if await imagiax_auth.is_user_revoked(user.user_id):
        return JSONResponse(status_code=401, content={"detail": "Cuenta revocada"})
    path = _sanitize_dicomweb_path(path)  # NEW — validates before proxy
    # ... rest of proxy logic unchanged

# servidor_api_orthanc_v1.py — /health/db MODIFIED
@app.get("/health/db")
async def health_db():
    try:
        pool = imagiax_db.get_pool()
    except RuntimeError:
        return JSONResponse(status_code=503, content={"ok": False, "database": "unavailable"})
    try:
        async with pool.acquire() as conn:
            n = await conn.fetchval("SELECT 1")
        if n != 1:
            return JSONResponse(status_code=503, content={"ok": False, "database": "unhealthy"})
        return {"ok": True, "database": "up"}
    except Exception:
        return JSONResponse(status_code=503, content={"ok": False, "database": "unavailable"})

# pagos_tbk.py — /tbk/cobrar MODIFIED
@router.post("/cobrar")
async def cobrar_endpoint(req: CobrarRequest, user: UserPayload = Depends(get_current_user)):
    # OVERRIDE user-controlled fields (SEC-PAY-003 + SEC-PAY-004)
    inscripcion = await imagiax_pagos_db.get_inscripcion_oneclick_any(usuario_id=user.user_id)
    if not inscripcion:
        raise HTTPException(404, "No hay tarjeta inscrita para tu cuenta")
    aprobado, buy_order, error_msg = await cobrar_scan_oneclick(
        username=inscripcion["username"],  # FROM DB, not from body
        tbk_user=inscripcion["tbk_user"],
        amount=PRECIO_SCAN_CLP,  # SERVER constant, not from body
    )
    return JSONResponse({...})

# pagos_mp.py — /pagos/exito MODIFIED (add auth)
@router.get("/pagos/exito", response_class=HTMLResponse)
async def pago_exitoso(
    request: Request,  # for cookie fallback
    payment_id: str = Query(None),
    # ... other query params ...
    user: UserPayload | None = None,  # see note below
):
    # Try to get user from cookie; if expired, still process but log warning
    token = request.cookies.get("imagiax_session")
    if token:
        try:
            user = imagiax_auth.decode_token(token)
        except Exception:
            pass  # Session expired during MP redirect — log + continue
    # ... rest of logic uses ext_ref.usuario_id for credit, not user ...

# pagos_mp.py — HMAC validation MODIFIED
def _validar_webhook_signature(request: Request, body_bytes: bytes) -> bool:
    if not MP_WEBHOOK_SECRET:
        logger.error("MP_WEBHOOK_SECRET not set — rejecting webhook")
        return False  # ALWAYS reject if no secret (remove _IS_TEST skip)
    # ... rest of HMAC logic unchanged ...

# pagos_mp.py — startup validation (in module level)
if not MP_WEBHOOK_SECRET:
    import warnings
    warnings.warn("MP_WEBHOOK_SECRET is empty — webhooks will be rejected", stacklevel=1)
```

### Phase 2 — High

```python
# pagos_tbk.py + pagos_mp.py — NEW: Replace _IS_TEST
IMAGIAX_PAYMENT_MODE = os.getenv("IMAGIAX_PAYMENT_MODE", "testing")
_IS_PAYMENT_TEST = (IMAGIAX_PAYMENT_MODE != "production")
# Startup cross-validation (in lifespan or module level):
if os.getenv("APP_ENV") == "production" and IMAGIAX_PAYMENT_MODE != "production":
    raise RuntimeError("IMAGIAX_PAYMENT_MODE must be 'production' when APP_ENV=production")

# imagiax_pagos_db.py — acreditar_pago ATOMIC (SEC-PAY-005)
async def acreditar_pago(...):
    pool = _get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """INSERT INTO licencias (...) VALUES (...)
                   ON CONFLICT (mp_payment_id) DO NOTHING
                   RETURNING *""", ...
            )
            if row is None:
                # Already processed — fetch existing
                return _row_to_dict(await conn.fetchrow(
                    "SELECT * FROM licencias WHERE mp_payment_id = $1", mp_payment_id))
            return _row_to_dict(row)

# pagos_tbk.py — Async TBK SDK wrapper (SEC-AI-002)
import asyncio

async def _tbk_call(fn, *args, **kwargs):
    """Runs sync TBK SDK calls in thread pool to avoid blocking event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))

# Usage: resp = await _tbk_call(inscription.start, username=..., email=..., response_url=...)

# imagiax_pagos_db.py — PCI masking (SEC-PAY-009)
_PCI_MASK_KEYS = {"card_number", "card_holder_name", "card_detail", "first_six_digits"}

def _mask_pci_data(data: dict) -> dict:
    """Recursively masks PCI-sensitive fields before DB storage."""
    if not isinstance(data, dict):
        return data
    masked = {}
    for k, v in data.items():
        if k.lower() in _PCI_MASK_KEYS:
            masked[k] = "***MASKED***"
        elif isinstance(v, dict):
            masked[k] = _mask_pci_data(v)
        elif isinstance(v, list):
            masked[k] = [_mask_pci_data(i) if isinstance(i, dict) else i for i in v]
        else:
            masked[k] = v
    return masked
```

### Phase 3 — Medium (patterns only)

```python
# CSP header: add to security_headers_middleware
response.headers["Content-Security-Policy"] = (
    "default-src 'self'; script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "connect-src 'self'; frame-ancestors 'none'"
)

# Role-based informe access: ver_informe
if user.rol not in ("medico", "admin", "superadmin"):
    return HTMLResponse("Acceso denegado", status_code=403)

# CORS wildcard check at startup (lifespan)
if "*" in _cors_origins and any(m == "allow_credentials" for m in ...):
    raise RuntimeError("CORS wildcard + credentials is forbidden")
```

## Testing Strategy

| Layer | What to Test | Approach |
|-------|-------------|----------|
| Manual | Path traversal blocked | `curl /pacs/dicom-web/../system` → 400 |
| Manual | IDOR prevented | `POST /tbk/cobrar {username:"other"}` → charges own card |
| Manual | Health no leak | Stop Postgres, `curl /health/db` → `{"ok":false}` no exception string |
| Manual | Webhook HMAC | `curl -X POST /pagos/webhook` without X-Signature → 401 |
| Manual | Revoked proxy | Logout user, reuse old JWT on `/pacs/dicom-web/studies` → 401 |
| Manual | Atomic payment | Two concurrent `curl` to `/pagos/exito?payment_id=X` → credited once |

## Migration / Rollout

1. **Phase 1 PR**: Deploy immediately. No DB migration. Backward compatible (AD-4).
2. **Phase 2 PR**: Add `IMAGIAX_PAYMENT_MODE` to `.env` files before deploy. Cross-validates at startup.
3. **Secrets rotation** (manual, parallel to Phase 2): Rotate Gemini key, admin password, JWT secret.
4. **Phase 3+4**: Standard deploy, no special migration.

## Regression Risk

| Change | Risk | Mitigation |
|--------|------|------------|
| Path sanitization | Legit DICOMweb paths rejected | Regex allows `a-zA-Z0-9._/:-` covering all standard paths |
| `/pagos/exito` auth | MP redirect fails if session expired | Graceful fallback: try cookie, if expired use `ext_ref.usuario_id` from MP |
| `_IS_TEST` removal | Dev env webhooks always fail | Require `MP_WEBHOOK_SECRET` even for sandbox (MP sandbox supports real secrets) |
| Proxy revocation check | Performance hit | 1 query to `revoked_users` (indexed PK lookup, <1ms) |
| `CobrarRequest` override | Frontend sends unused fields | Fields accepted but ignored — no 400 error |

## Open Questions

- [x] `/pagos/exito` auth: MP redirects back after payment — user cookie may have expired during checkout. **Resolved**: Soft auth (try cookie, fallback to `external_reference.usuario_id`).
- [ ] Secrets rotation timeline: Who rotates the Gemini key? Needs ops coordination.
