# Proposal: Production Deployment — imagiax.dev

## Intent

Deploy IMAGIAX to production at `imagiax.dev` on Oracle Cloud. The codebase is architecturally ready; all blockers are configuration and infrastructure. This change resolves 6 critical blockers, 2 security vulnerabilities, and establishes the operational baseline for a live medical imaging platform.

## Scope

### In Scope
- Fix `.env.production` — add `APP_ENV`, `ORTHANC_AUTH_HEADER`, rotate weak passwords
- Patch SEC-BE-003 (path traversal in DICOMweb proxy)
- Patch SEC-BE-002 (revoked token still accesses PACS)
- Oracle Cloud VM provisioning guide + firewall rules
- SSL certificate (Let's Encrypt via certbot)
- Step-by-step deployment procedure (docker-compose.prod.yml)
- Operational runbook: health checks, logs, backup, cert renewal

### Out of Scope
- MercadoPago/Transbank production credentials (user must obtain externally)
- Sentry/error tracking integration
- Staging environment
- DICOM port 4242 external access
- Rate limiter persistence (Redis)

## Capabilities

### New Capabilities
- None

### Modified Capabilities
- `production-config`: `.env.production` — adds 2 missing vars, rotates credentials
- `dicomweb-proxy-security`: SEC-BE-003 path sanitization + SEC-BE-002 DB revocation check

## Approach

**Configuration-first, then patch, then deploy.**

1. Rotate/add all missing env vars in `.env.production`
2. Patch the two HIGH security vulnerabilities (30 min each)
3. Provision Oracle Cloud ARM64 VM (Always Free: 4 OCPU / 24 GB)
4. Bootstrap SSL cert via `certbot --standalone` before nginx starts
5. Build and start services in dependency order: postgres → orthanc → fastapi → ohif → nginx
6. Verify health endpoints; change admin password

No source-code restructuring — only targeted security patches and env configuration.

## Affected Areas

| Area | Impact | Description |
|------|--------|-------------|
| `.env.production` | Modified | Add `APP_ENV`, `ORTHANC_AUTH_HEADER`; rotate passwords, Gemini key |
| `servidor_api_orthanc_v1.py` | Modified | SEC-BE-003: sanitize `path` in DICOMweb proxy; SEC-BE-002: DB revocation check |
| Oracle Cloud VM | New | Provision, harden firewall, install Docker |
| `/etc/letsencrypt` | New | Let's Encrypt cert for `imagiax.dev` + `www.imagiax.dev` |
| `docker-compose.prod.yml` | None | Already production-ready |
| `nginx-proxy.conf` | None | Already production-ready |

## Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| TBK + APP_ENV mismatch → RuntimeError on startup | High | Set `TBK_ENVIRONMENT=production` only when real TBK credentials are ready; deploy without real payments first |
| OHIF build OOM on small VM (≥8 GB needed) | Med | Pre-build OHIF locally and push image to registry before server deploy |
| Certbot rate limit (5 certs/7 days) | Low | Use `--dry-run` to validate before issuing real cert |
| PHI exposure during deploy window | Low | Deploy at low-traffic time; nginx auth_request active from first start |

## Rollback Plan

```bash
# Stop all services
docker compose -f docker-compose.prod.yml down

# Restore previous .env.production from backup
cp .env.production.backup .env.production

# Restore DB if needed
gunzip < imagiax-backup.sql.gz | docker exec -i postgres-imagiax psql -U imagiax imagiax

# Restart
docker compose -f docker-compose.prod.yml up -d
```

## Dependencies

- User must provide: Gemini production API key, MP `APP_USR-*` tokens, TBK production commerce codes
- DNS A records for `imagiax.dev` → server IP must propagate before certbot runs
- Oracle Cloud account with Always Free quota available

## Success Criteria

- [ ] `curl https://imagiax.dev/api/ping` returns `{"status": "ok"}`
- [ ] `https://imagiax.dev/` redirects unauthenticated users to `/login`
- [ ] OHIF viewer loads and displays DICOM studies after login
- [ ] AI analysis runs end-to-end (scan → Gemini report visible in panel)
- [ ] `/docs` and `/redoc` return 404 (API docs hidden)
- [ ] Admin password changed from default on first login
- [ ] SSL cert valid with A+ rating (Qualys SSL Labs)
- [ ] Certbot auto-renewal cron configured
- [ ] Daily PostgreSQL backup running
