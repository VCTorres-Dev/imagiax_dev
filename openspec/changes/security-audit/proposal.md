# Proposal: Security Audit Remediation (security-audit)

## 1. Intent
The intent of this change is to address and remediate 27 security vulnerabilities identified during the recent security audit of the IMAGIAX platform. As a system handling Protected Health Information (PHI), medical imaging (DICOM), and financial transactions (Transbank/MercadoPago), resolving these issues is absolutely critical for production readiness. These vulnerabilities expose the platform to catastrophic data breaches, financial theft, and unauthorized system access.

## 2. Scope
The remediation will be divided into four distinct phases based on severity:

### Phase 1: IMMEDIATE (Critical)
Must be fixed before ANY production deployment:
- **SEC-BE-003**: Path Traversal in DICOMweb proxy (`../` access to Orthanc).
- **SEC-PAY-003**: IDOR in `/tbk/cobrar` (charging arbitrary users).
- **SEC-PAY-004**: Client-controlled charge amount validation.
- **SEC-PAY-002**: Unauthenticated `/pagos/exito` callback.
- **SEC-PAY-001**: MP Webhook bypass in development (`_IS_TEST=True`).
- **SEC-BE-002**: DICOMweb proxy revoked token verification.
- **SEC-BE-001**: `/health/db` unauthenticated internal topology exposure.

### Phase 2: NEXT SPRINT (High)
- **SEC-PAY-005**: TOCTOU race condition in `acreditar_pago()`.
- **SEC-PAY-006**: `_IS_TEST` flag derived from token prefix.
- **SEC-PAY-007**: `cobrar_scan_automatico()` test mode auto-approval.
- **SEC-PAY-008**: Production credentials in plaintext `.env.production`.
- **SEC-AI-001**: DoS vulnerability via massive inference (`max_instances=500`).
- **SEC-AI-002**: Synchronous Transbank SDK calls blocking event loop.
- **SEC-INFRA-001**: Shared Gemini API key between dev and prod.
- **SEC-INFRA-002**: Predictable admin password pattern.

### Phase 3: HARDENING (Medium)
- **SEC-BE-006**: PHI exposure in `/informes/{id}/ver` without role isolation.
- **SEC-BE-007**: Lack of Content Security Policy (CSP).
- **SEC-PAY-009**: Raw webhook/response storing card data (PCI-DSS).
- **SEC-AI-003**: Information leakage in TBK redirect URL exception.
- **SEC-BE-008**: Inadequate CORS wildcard validation.
- **SEC-BE-009**: Massive PHI extraction via `/informes/listado`.
- **SEC-PAY-010**: Malformed `external_reference` default fallback.
- **SEC-INFRA-003**: X-Forwarded-For spoofability.

### Phase 4: POLISH (Low)
- Address LOW findings (SEC-AI-004, SEC-PAY-011, SEC-AI-005, SEC-BE-010, SEC-BE-011, SEC-BE-012, SEC-INFRA-004) and minor infrastructure improvements.

## 3. Approach
We will use an **Incremental Remediation** approach (Option A). Each phase will have its own isolated Pull Request to ensure thorough review and prevent blocking critical fixes with lower-priority hardening tasks.
1. **Phase 1 PR** will be opened immediately targeting the highest risk endpoints.
2. Followed by Phase 2, 3, and 4 in subsequent iterations.

## 4. Risk Assessment
- **If CRITICALs are not fixed**: Complete system compromise. Attackers can access any patient's DICOM images, steal money via payment bypasses, charge other users' cards, and map internal databases.
- **If HIGHs are not fixed**: Financial loss via race conditions (double spending), potential DoS taking down the AI inference engine, and credential leakage.
- **If MEDIUMs/LOWs are not fixed**: Compliance violations (HIPAA/PCI-DSS), potential PHI scraping, and weakened defense-in-depth.

## 5. Dependencies
- **Secrets Rotation**: Must rotate all compromised secrets (Gemini API keys, Admin passwords, `.env.production` credentials) before or immediately alongside the deployment of Phase 2.

## 6. Out of Scope
- New feature development.
- UI/UX redesigns.
- Major architectural overhauls not strictly related to the identified security vulnerabilities.

## 7. Future Improvements
For future consideration and architectural hardening:
- Install pytest + create test infrastructure.
- Add linter (ruff) + type checker (mypy).
- Redis for rate limiting and session management.
- Docker Compose for orchestration.
- CI/CD pipeline with automated security scanning.
- Secrets manager integration.
- Formal HIPAA compliance audit.
- Third-party Penetration testing.
- API versioning implementation.
- Monitoring & alerting (Sentry, Prometheus).
- Database migrations framework (Alembic).
- Formal Backup and disaster recovery plan.
