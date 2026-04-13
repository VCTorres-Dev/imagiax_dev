# Proposal: audit-fixes

## Intent

Address all CRITICAL and HIGH vulnerabilities, and selected MEDIUM issues identified during the recent IMAGIAX platform technical audit to ensure platform security, stability, and PHI data protection.

## Scope

### In Scope
- Fix 4 CRITICAL issues: Broken SQL placeholders (C-002), missing auth on /analizar and /resultados (C-001), cross-user PHI leak on /informes (C-004), and in-memory login brute-force protection (C-003).
- Fix 7 HIGH issues: Async event loop blocking by AI inference (H-001, F9/F11) and Transbank (H-002), missing Docker memory limits (H-003), Gemini language leak (H-004), IDOR on reports (H-005), missing frontend credentials (H-006), Transbank username forgery (H-007).
- Fix 3 MEDIUM issues: Remove dead _general_limiter code (M-002, F9/F11), use sessionStorage for PHI (M-008), add credentials to apiV2Request (M-009).

### Out of Scope
- Fixing LOW issues (L-001 through L-006).
- Adding new containers or external dependencies.

## Capabilities

### New Capabilities
- None

### Modified Capabilities
- `informes`: Adding stricter access control and resolving SQL broken placeholders.
- `analizar`: Implementing authentication enforcement.
- `auth`: Moving brute-force protection out of memory.
- `pagos_tbk`: Fixing event loop blocking and username forgery.

## Approach

- Update backend endpoints (`servidor_api_orthanc_v1.py`, `imagiax_auth.py`, `pagos_tbk.py`, `imagiax_db.py`) to enforce authorization checks matching the user's context (e.g., matching doctor IDs), wrap blocking calls (`AI inference`, `Transbank`) in `asyncio.run_in_executor`, fix SQL placeholders, and patch Gemini prompts.
- Refactor authentication brute-force protection to leverage the existing PostgreSQL DB instead of memory.
- Update frontend (`ImagiaxAiPanel.tsx`, `imagiaxApi.ts`) to use `sessionStorage` and include credentials in API requests.
- Update `docker-compose.prod.yml` to set memory limits to prevent OOM on the 4GB server.
- Python files will be edited using raw-byte scripts to preserve CRLF.

## Affected Areas

| Area | Impact | Description |
|------|--------|-------------|
| `backend/servidor_api_orthanc_v1.py` | Modified | Add auth to endpoints, wrap AI in executor |
| `backend/imagiax_db.py` | Modified | Fix SQL queries for `guardar_informe` |
| `backend/imagiax_auth.py` | Modified | Update brute-force protection |
| `backend/pagos_tbk.py` | Modified | Wrap TBK in executor, fix username fallback |
| `frontend/ImagiaxAiPanel.tsx` | Modified | Load credentials properly |
| `frontend/imagiaxApi.ts` | Modified | Use `sessionStorage`, add credentials to `apiV2Request` |
| `docker-compose.prod.yml` | Modified | Add memory limits |

## Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| CRLF corruption in Python files | High | strictly use Python raw-byte script replacements |
| OOM from memory limits | Medium | carefully set limits to ~1-2GB per service on 4GB node |

## Rollback Plan

Revert `docker-compose.prod.yml` changes and revert code commits from the `audit-fixes` branch. DB changes should be backward compatible.

## Dependencies

- Existing PostgreSQL database for brute-force tracking.

## Success Criteria

- [ ] All 4 Critical and 7 High issues are resolved and tested.
- [ ] M-002, M-008, and M-009 are resolved.
- [ ] No new dependencies are introduced.
- [ ] The application starts and runs stably on the 4GB DigitalOcean server.