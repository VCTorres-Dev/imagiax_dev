# Tasks: Security Audit Remediation

## Phase 1: Critical (P0) — 7 vulnerabilities, deploy immediately

- [x] 1.1 `servidor_api_orthanc_v1.py`: Add `_sanitize_dicomweb_path()` with whitelist regex `^[a-zA-Z0-9][a-zA-Z0-9._/:-]*$` + `..` rejection. Call it in `pacs_dicom_proxy()` before proxy. (SEC-BE-003, AD-3)
- [x] 1.2 `servidor_api_orthanc_v1.py`: Replace `verify_session_cookie_token()` in proxy with `decode_token()` + `is_user_revoked()` check. Return 401 JSON on revoked. (SEC-BE-002, AD-2)
- [x] 1.3 `servidor_api_orthanc_v1.py`: Strip exception string from `/health/db` — return `{"ok": false, "database": "unavailable"}` on any error. (SEC-BE-001, AD-1)
- [x] 1.4 `pagos_tbk.py`: In `/tbk/cobrar`, override `username` from DB via `get_inscripcion_oneclick_any(user.user_id)` and `amount` from `PRECIO_SCAN_CLP`. Log deprecation warning for body fields. (SEC-PAY-003+004, AD-4)
- [x] 1.5 `pagos_mp.py`: Remove `_IS_TEST` skip in `_validar_webhook_signature()`. Reject if `MP_WEBHOOK_SECRET` is empty. Add startup warning. (SEC-PAY-001)
- [x] 1.6 `pagos_mp.py`: Add soft auth to `/pagos/exito` — try cookie via `decode_token()`, fallback to `external_reference.usuario_id`. Log when cookie is missing/expired. (SEC-PAY-002)
- [ ] 1.7 Manual test: `curl /pacs/dicom-web/../system` → 400; revoked token → 401; `/health/db` (DB down) → no exception leak; `/tbk/cobrar {username:"other"}` → charges self; unsigned webhook → rejected.

## Phase 2: High (P1) — 8 vulnerabilities, next sprint

- [x] 2.1 `pagos_tbk.py` + `pagos_mp.py`: Replace `_IS_TEST` with `IMAGIAX_PAYMENT_MODE` env var. Add startup cross-validation vs `APP_ENV`. (SEC-PAY-006+007, AD-6)
- [x] 2.2 `.env` + `.env.production`: Add `IMAGIAX_PAYMENT_MODE=testing|production` and `MP_WEBHOOK_SECRET`. Document rotation for Gemini key + admin password. (SEC-PAY-008, SEC-INFRA-001+002)
- [x] 2.3 `imagiax_pagos_db.py`: Rewrite `acreditar_pago()` with `INSERT ... ON CONFLICT (mp_payment_id) DO NOTHING RETURNING *`. Remove SELECT-then-INSERT pattern. (SEC-PAY-005, AD-5)
- [x] 2.4 `pagos_tbk.py`: Add `_tbk_call()` async wrapper using `run_in_executor()`. Wrap all sync TBK SDK calls (`inscription.start`, `inscription.finish`, `transaction.authorize`). (SEC-AI-002, AD-7)
- [x] 2.5 `servidor_api_orthanc_v1.py`: Set `le=50` on `GenerarInformeRequest.max_instances` Pydantic field. (SEC-AI-001)
- [x] 2.6 `imagiax_auth.py`: Fix dead code in `is_jti_revoked()` — ensure it returns `True` only when the specific JTI is revoked, not unconditionally. (SEC-BE-005)
- [ ] 2.7 Manual test: Concurrent `curl` to `/pagos/exito?payment_id=X` → credited once; `IMAGIAX_PAYMENT_MODE` mismatch → startup crash; `max_instances=500` → 422.

## Phase 3: Medium (P2) — 8 vulnerabilities, hardening

- [x] 3.1 `servidor_api_orthanc_v1.py`: Add CSP header in `security_headers_middleware`. (SEC-BE-007)
- [x] 3.2 `servidor_api_orthanc_v1.py`: Add role check (`medico`, `admin`, `superadmin`) in `ver_informe()`. (SEC-BE-006)
- [x] 3.3 `servidor_api_orthanc_v1.py`: Set `le=50` on `/informes/listado` limit param. Add CORS wildcard startup check. (SEC-BE-009, SEC-BE-008)
- [x] 3.4 `imagiax_pagos_db.py`: Add `_mask_pci_data()` recursive function. Apply before storing `raw_webhook`/`raw_response`. (SEC-PAY-009)
- [x] 3.5 `pagos_tbk.py`: Replace exception strings in TBK redirect URLs with opaque codes (`tbk_auth_failed`, `tbk_timeout`). (SEC-AI-003)
- [x] 3.6 `pagos_mp.py`: Validate `external_reference` format strictly (must contain `usuario_id` + `paquete`). Reject malformed with log. (SEC-PAY-010)
- [ ] 3.7 Manual test: CSP header present; `recepcion` user → 403 on ver_informe; card data masked in DB; opaque error codes in TBK redirects.

## Phase 4: Low (P3) — Defense-in-depth polish

- [x] 4.1 Apply input size limits on remaining text fields (AI prompts, patient names). Add `Dockerfile` with non-root user. (SEC-AI-004+005, SEC-INFRA-004)
- [x] 4.2 Add `X-Forwarded-For` trusted proxy validation in production. Document secure deployment checklist. (SEC-INFRA-003, SEC-BE-010+011+012)
- [x] 4.3 Final regression pass: all Phase 1-3 tests re-verified after Phase 4 changes.
