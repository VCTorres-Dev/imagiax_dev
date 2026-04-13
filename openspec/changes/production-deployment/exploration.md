# Exploration: Production Deployment — imagiax.dev

> **Date**: 2026-04-03  
> **Change**: production-deployment  
> **Platform**: Oracle Cloud, domain imagiax.dev  
> **Status**: RESEARCH COMPLETE — ready for proposal phase

---

## 1. CREDENTIALS INVENTORY

Table of every secret required for production, current status, and action required.

| # | Variable | Current Value (sanitized) | Status | Action Required |
|---|----------|--------------------------|--------|-----------------|
| 1 | `JWT_SECRET` | `0bfbd13f...` (dev) / `2ac6cc71...` (prod file) | ⚠️ DIFFERENT KEYS — good, but prod key is in plaintext `.env.production` | **ROTATE**: generate new 48-byte hex: `python3 -c "import secrets; print(secrets.token_hex(48))"` |
| 2 | `IMAGIAX_ADMIN_PASSWORD` | `ImagiaxAdmin2024!` in `.env.production` | 🔴 WEAK + EXPOSED — visible in `.env.production` | **CHANGE**: set a strong 16+ char password, change immediately after first deploy |
| 3 | `IMAGIAX_ADMIN_EMAIL` | `admin@imagiax.dev` | ✅ Fine | Keep as-is |
| 4 | `POSTGRES_URL` | `postgresql://imagiax:ImagiaxDemo2024@postgres:5432/imagiax` | 🔴 WEAK PASSWORD: `ImagiaxDemo2024` — exposed in plaintext | **CHANGE**: use strong random password, update both `POSTGRES_URL` and `POSTGRES_PASSWORD` |
| 5 | `POSTGRES_PASSWORD` | `ImagiaxDemo2024` | 🔴 Same as above | **CHANGE** (must match `POSTGRES_URL`) |
| 6 | `ORTHANC_BASE_URL` | `http://orthanc:8042` (in .env.production) | ✅ Correct Docker service name | Keep as-is |
| 7 | `ORTHANC_USER` | `imagiax` | ✅ Fine | Keep as-is |
| 8 | `ORTHANC_PASSWORD` | `ImagiaxPACS_2026!` | ✅ Acceptable strength | **VERIFY** matches `orthanc.json` `RegisteredUsers` |
| 9 | `GEMINI_API_KEY` | `AIzaSyCHZ3JDxddgVQmTTirW6GGtilSG-BHfHSQ` | 🔴 SAME KEY in both `.env` (dev) and `.env.production` — rotate immediately | **ROTATE**: get a new key from Google AI Studio, use separate keys for dev/prod |
| 10 | `MP_ACCESS_TOKEN` | `TEST-918232103812133-...` | 🔴 TEST credentials — NOT production | **REPLACE**: get `APP_USR-...` production tokens from mercadopago.cl/developers |
| 11 | `MP_PUBLIC_KEY` | `TEST-facd9b99-...` | 🔴 TEST credentials | **REPLACE**: get production `APP_USR-...` public key |
| 12 | `MP_WEBHOOK_SECRET` | `18fab28c15c331d3...` | ⚠️ Set in `.env.production`, good. But it's a test webhook secret | **REPLACE**: get production webhook secret from MP panel → Webhooks section |
| 13 | `TBK_ENVIRONMENT` | `integration` | 🔴 NOT production | **CHANGE**: `TBK_ENVIRONMENT=production` |
| 14 | `TBK_MALL_COMMERCE_CODE` | `597055555541` (integration) | 🔴 TEST commerce code | **REPLACE**: obtain real commerce codes from Transbank production onboarding |
| 15 | `TBK_CHILD_COMMERCE_CODE` | `597055555542` (integration) | 🔴 TEST commerce code | **REPLACE**: same as above |
| 16 | `TBK_API_KEY` | `579B532A7440BB0C9079...` (integration) | 🔴 TEST API key | **REPLACE**: production API key from Transbank |
| 17 | `CORS_ORIGINS` | `https://imagiax.dev,https://www.imagiax.dev` | ✅ Correct in `.env.production` | Keep as-is |
| 18 | `IMAGIAX_BASE_URL` | `https://imagiax.dev/api` | ⚠️ Note: has `/api` suffix — affects MP webhook/callback URLs | **VERIFY**: check if MP callbacks work with `/api` suffix (see §2) |
| 19 | `IMAGIAX_VIEWER_URL` | `/` | ✅ Correct — relative path, nginx routes to OHIF | Keep as-is |
| 20 | `IMAGIAX_PRECIO_SCAN` | `990` | ✅ Fine | Keep as-is |
| 21 | `JWT_EXPIRE_HOURS` | `24` | ✅ Fine | Keep as-is |
| 22 | `ORTHANC_AUTH_HEADER` | **NOT SET IN .env.production** | 🔴 MISSING — required by nginx template | **ADD**: `ORTHANC_AUTH_HEADER=Basic $(echo -n "imagiax:ImagiaxPACS_2026!" | base64)` |
| 23 | `APP_ENV` | Not set in `.env.production` | 🔴 MISSING — needed for production mode | **ADD**: `APP_ENV=production` |

### What the user must obtain before deploying:
1. **Gemini API key** — rotate to a new production-only key at https://aistudio.google.com/apikey
2. **MercadoPago production credentials** — `APP_USR-*` tokens at https://mercadopago.cl/developers
3. **Transbank production commerce codes and API key** — via official Transbank onboarding process
4. **Strong random passwords** for PostgreSQL and admin
5. **Oracle Cloud VM** or DigitalOcean Droplet (≥4 GB RAM, 2 vCPU, 50 GB storage)
6. **Domain DNS** pointed to server IP (A records for `imagiax.dev` and `www.imagiax.dev`)

---

## 2. CODE CHANGES REQUIRED

These are changes required BEFORE production deployment. No code was modified during this investigation.

### 2.1 `.env.production` — Missing and wrong variables

**File**: `.env.production` (in project root)

| Issue | Current | Required |
|-------|---------|---------|
| `APP_ENV` not set | (missing) | `APP_ENV=production` |
| `ORTHANC_AUTH_HEADER` not set | (missing) | `ORTHANC_AUTH_HEADER=Basic aW1hZ2lheDpJbWFnaWF4UEFDU18yMDI2IQ==` (base64 of `imagiax:ImagiaxPACS_2026!`) |
| PostgreSQL password weak | `ImagiaxDemo2024` | Strong random password |
| JWT_SECRET — use rotated key | `2ac6cc71...` | New 48-byte hex |
| Admin password — weak | `ImagiaxAdmin2024!` | Strong 16+ char |
| MP tokens — TEST mode | `TEST-...` | `APP_USR-...` production tokens |
| TBK — integration mode | `integration` + test codes | `production` + real commerce codes |
| Gemini key — shared with dev | Same as `.env` | New production-only key |

### 2.2 `servidor_api_orthanc_v1.py` — Production validation already handled

The code already handles production correctly:
- Line 100: `_IS_PROD = os.getenv("APP_ENV", "development") == "production"` ✅
- Line 694-696: Raises `RuntimeError` if `TBK_ENVIRONMENT != "production"` when `APP_ENV=production` ✅
- Lines 748-750: `docs_url=None if _IS_PROD` — hides API docs in production ✅
- Line 878: Dev-only code already guarded ✅

**NO code changes needed** in this file — the env variables drive behavior correctly.

### 2.3 CORS — Already correct

`CORS_ORIGINS` in `.env.production` is already set to `https://imagiax.dev,https://www.imagiax.dev`. The backend reads from env with no hardcoding.

### 2.4 `IMAGIAX_BASE_URL` — Potential issue with `/api` suffix

In `.env.production`:
```
IMAGIAX_BASE_URL=https://imagiax.dev/api
```

This is used in `pagos_mp.py` for MercadoPago callbacks:
```python
"success": f"{base_url}/pagos/exito"  # → https://imagiax.dev/api/pagos/exito
```
And in `pagos_tbk.py`:
```python
response_url = f"{IMAGIAX_BASE_URL}/tbk/inscribir/finalizar"
# → https://imagiax.dev/api/tbk/inscribir/finalizar
```

**This is CORRECT** because nginx routes `/api/ → fastapi:8010/` — MercadoPago and Transbank callbacks will land at FastAPI correctly via nginx. ✅

### 2.5 OHIF `ImagiaxAiPanel.tsx` — API URL hardcoded fallback

**File**: `ohif-extension-imagiax-ai/src/ImagiaxAiPanel.tsx` line ~55:
```typescript
const IMAGIAX_BACKEND = _cfg.backend_url || 'http://localhost:8010';
```

**File**: `ohif-extension-imagiax-ai/src/services/imagiaxApi.ts` lines ~12-17:
```typescript
const API_BASE_URL =
  (typeof window !== 'undefined' && (window as any).__IMAGIAX_API_URL__) ||
  'http://localhost:8010';  // ← FALLBACK
```

These fallbacks to `localhost:8010` are **never reached in production** because `docker-compose.prod.yml` sets:
```yaml
APP_CONFIG: |
  window.__IMAGIAX_API_URL__ = '/api';
  window.__IMAGIAX_V2_API_URL__ = '/api';
  window.__IMAGIAX_CONFIG__ = { backend_url: '/api' };
```

The `APP_CONFIG` env var is injected at OHIF container startup by the official OHIF `entrypoint.sh`. **No code changes needed** — the config injection mechanism is already in place. ✅

### 2.6 `orthanc.json` — Production security concern

**File**: `OHIF Medical Imaging Viewer/orthanc-imagiax/config/orthanc.json`

```json
"HttpDescribeErrors": true   // ← exposes internal error details
"DicomAlwaysAllowStore": true // ← any AE can push DICOMs
"DicomAlwaysAllowEcho": true  // ← any AE can C-ECHO
```

Orthanc in production is **not exposed externally** (docker-compose.prod.yml doesn't publish port 8042). These settings are acceptable given the firewall protection, but could be tightened.

**Optional hardening** (not blocking):
- `"HttpDescribeErrors": false` — don't expose error details even internally
- `"DicomCheckCalledAet": true` — restrict DICOM AE access
- Change `RegisteredUsers` password if deploying to a new environment

### 2.7 Dockerfile — Production ready

The Dockerfile is already production-optimized:
- Non-root user (uid=1001) ✅
- CPU-only PyTorch (ARM64/x86_64 compatible) ✅  
- Health check defined ✅
- `--workers 1` for PyTorch safety ✅
- `--proxy-headers` for nginx integration ✅

**No changes needed.** ✅

### 2.8 `nginx-proxy.conf` — Production ready

The nginx config in `config/nginx-proxy.conf` is already production-ready:
- HTTP → HTTPS redirect ✅
- TLS 1.2/1.3 only ✅
- HSTS, X-Frame-Options, X-Content-Type-Options, CSP ✅
- Let's Encrypt ACME challenge path ✅
- `auth_request /api/auth/verify` on `/` and `/pacs/` ✅
- `${ORTHANC_AUTH_HEADER}` env substitution via nginx envsubst ✅

**Dependency**: requires `ORTHANC_AUTH_HEADER` in `.env.production` (currently missing — see §2.1)

---

## 3. INFRASTRUCTURE REQUIREMENTS

### 3.1 Server Specifications

| Component | Minimum | Recommended | Notes |
|-----------|---------|-------------|-------|
| CPU | 2 vCPU | 4 vCPU | PyTorch inference is CPU-bound (~15-30s/scan) |
| RAM | 4 GB | 8 GB | PyTorch model uses ~2 GB, OHIF build ~1 GB |
| Storage | 50 GB | 100 GB | DICOMs can be large (100MB+/study) |
| OS | Ubuntu 22.04 LTS | Ubuntu 22.04 LTS | Best Docker compatibility |
| Network | 1 Gbps | 1 Gbps | DICOM upload speed matters |

**Oracle Cloud Always Free** tier: 4 ARM64 OCPUs + 24 GB RAM — perfect for this stack.  
**Minimum paid option**: DigitalOcean Droplet 4 GB ($24/mo) — acceptable.

### 3.2 Docker Stack (docker-compose.prod.yml)

Services defined in `OHIF Medical Imaging Viewer/orthanc-imagiax/docker-compose.prod.yml`:
```
orthanc    — PACS (NOT exposed to internet — only Docker network)
postgres   — Database (NOT exposed to internet — only Docker network)
fastapi    — Backend API (NOT exposed to internet — only /api/ via nginx)
ohif       — Frontend viewer (NOT exposed to internet — only / via nginx)
nginx      — Reverse proxy (ports 80, 443 → internet)
```

### 3.3 SSL/TLS

Let's Encrypt via Certbot (webroot method). The nginx config already has the ACME challenge path configured at `/var/www/certbot`. 

Initial cert requires:
1. DNS A records pointing to server
2. Port 80 open (for HTTP challenge)
3. Run certbot BEFORE starting nginx with SSL config

### 3.4 Firewall Rules (Oracle Cloud Security List)

| Protocol | Port | Source | Purpose |
|----------|------|--------|---------|
| TCP | 22 | Your IP only | SSH admin |
| TCP | 80 | 0.0.0.0/0 | HTTP → HTTPS redirect + ACME |
| TCP | 443 | 0.0.0.0/0 | HTTPS (main traffic) |
| TCP | 4242 | Trusted DICOM sources only | DICOM C-STORE (optional) |
| All others | — | DENY | Block all |

**Internal Docker ports** (8010, 8042, 5432, 3000) must NOT be exposed to the internet.

### 3.5 Storage Volumes

Docker volumes defined in docker-compose.prod.yml:
- `orthanc-data` — DICOM files (grows with usage, plan 50+ GB)
- `imagiax-pgdata` — PostgreSQL data (small, <1 GB initially)
- `torchxrayvision-cache` — AI model cache (~90 MB, downloaded once)

**Backup**: PostgreSQL `pg_dump` + `orthanc-data` volume snapshots.

---

## 4. DEPLOYMENT PROCEDURE

Step-by-step from a clean Ubuntu 22.04 LTS server (Oracle Cloud).

### Phase 0: Pre-flight Checklist

Before touching the server:
- [ ] Domain A records set: `imagiax.dev` → server IP, `www.imagiax.dev` → server IP
- [ ] DNS propagated (check with `nslookup imagiax.dev`)
- [ ] All production credentials obtained (Gemini, MP production, TBK production)
- [ ] `.env.production` prepared with all real values (see §1 and §2.1)

### Phase 1: Server Setup

```bash
# 1. SSH into the server
ssh ubuntu@<SERVER_IP>

# 2. Update system
sudo apt-get update && sudo apt-get upgrade -y

# 3. Install Docker
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
newgrp docker  # or logout/login

# 4. Verify Docker
docker --version
docker compose version

# 5. Install certbot
sudo apt-get install -y certbot

# 6. Create app directory
sudo mkdir -p /opt/imagiax
sudo chown $USER:$USER /opt/imagiax
```

### Phase 2: Deploy Code

```bash
# Option A: Git clone (if repo is accessible)
cd /opt
git clone https://github.com/YOUR_ORG/imagiax.git imagiax
cd imagiax

# Option B: SCP from local machine
# scp -r "IMAGIAX - copia (2)" ubuntu@<SERVER_IP>:/opt/imagiax
# Then rename/organize as needed

# The working directory for docker-compose.prod.yml is:
# /opt/imagiax/OHIF Medical Imaging Viewer/orthanc-imagiax/
```

### Phase 3: Configure Production Secrets

```bash
cd /opt/imagiax

# Create .env.production with ALL required values
# (Do NOT use nano on a shared screen — use a secure method)
cat > .env.production << 'EOF'
APP_ENV=production

# Generate with: python3 -c "import secrets; print(secrets.token_hex(48))"
JWT_SECRET=<48_BYTE_HEX_GENERATED_FRESH>
JWT_EXPIRE_HOURS=24

IMAGIAX_ADMIN_EMAIL=admin@imagiax.dev
IMAGIAX_ADMIN_PASSWORD=<STRONG_16_CHAR_PASSWORD>
IMAGIAX_ADMIN_NAME=Administrador IMAGIAX

# Generate with: python3 -c "import secrets; print(secrets.token_urlsafe(32))"
POSTGRES_PASSWORD=<STRONG_RANDOM_PASSWORD>
POSTGRES_URL=postgresql://imagiax:<SAME_STRONG_PASSWORD>@postgres:5432/imagiax

ORTHANC_BASE_URL=http://orthanc:8042
ORTHANC_USER=imagiax
ORTHANC_PASSWORD=ImagiaxPACS_2026!
# Base64 of "imagiax:ImagiaxPACS_2026!" — run: echo -n "imagiax:ImagiaxPACS_2026!" | base64
ORTHANC_AUTH_HEADER=Basic aW1hZ2lheDpJbWFnaWF4UEFDU18yMDI2IQ==

GEMINI_API_KEY=<NEW_PRODUCTION_ONLY_GEMINI_KEY>

IMAGIAX_BASE_URL=https://imagiax.dev/api
CORS_ORIGINS=https://imagiax.dev,https://www.imagiax.dev
IMAGIAX_VIEWER_URL=/

# MercadoPago PRODUCTION
MP_ACCESS_TOKEN=APP_USR-<YOUR_PRODUCTION_ACCESS_TOKEN>
MP_PUBLIC_KEY=APP_USR-<YOUR_PRODUCTION_PUBLIC_KEY>
MP_WEBHOOK_SECRET=<YOUR_PRODUCTION_WEBHOOK_SECRET>

# Transbank PRODUCTION
TBK_ENVIRONMENT=production
TBK_MALL_COMMERCE_CODE=<YOUR_PRODUCTION_MALL_CODE>
TBK_CHILD_COMMERCE_CODE=<YOUR_PRODUCTION_CHILD_CODE>
TBK_API_KEY=<YOUR_PRODUCTION_TBK_API_KEY>

IMAGIAX_PRECIO_SCAN=990
IMAGIAX_HTTP_TIMEOUT=30
EOF

# Secure the file
chmod 600 .env.production
```

### Phase 4: SSL Certificate (Bootstrap Order)

The nginx config expects certs at `/etc/letsencrypt/live/imagiax.dev/`. The trick: get the cert BEFORE starting nginx with SSL.

```bash
# 4.1 Get temporary cert (standalone mode, requires port 80 free)
sudo certbot certonly --standalone \
  -d imagiax.dev \
  -d www.imagiax.dev \
  --email admin@imagiax.dev \
  --agree-tos \
  --non-interactive

# 4.2 Create webroot dir for future renewals
sudo mkdir -p /var/www/certbot
```

### Phase 5: Build and Start Services

```bash
# Navigate to docker-compose.prod.yml location
cd "/opt/imagiax/OHIF Medical Imaging Viewer/orthanc-imagiax"

# 5.1 Build images (first time: takes 15-30 min for OHIF build)
docker compose -f docker-compose.prod.yml build

# Note: OHIF build is heavy (~8 GB Node.js build context)
# If RAM is limited, build OHIF separately first:
# docker compose -f docker-compose.prod.yml build ohif
# docker compose -f docker-compose.prod.yml build fastapi

# 5.2 Start infrastructure first (postgres, orthanc)
docker compose -f docker-compose.prod.yml up -d postgres orthanc

# Wait for postgres to be healthy
docker compose -f docker-compose.prod.yml ps

# 5.3 Start FastAPI (will auto-create DB schema on first run)
docker compose -f docker-compose.prod.yml up -d fastapi

# 5.4 Wait for FastAPI to load PyTorch model (~60s first start)
docker compose -f docker-compose.prod.yml logs -f fastapi
# Wait for: "IMAGIAX Orthanc API lista" + "IMAGIAX Auth: tablas de autenticación listas"

# 5.5 Start OHIF viewer
docker compose -f docker-compose.prod.yml up -d ohif

# 5.6 Start nginx (last — it depends on all others)
docker compose -f docker-compose.prod.yml up -d nginx

# 5.7 Verify all containers running
docker compose -f docker-compose.prod.yml ps
```

### Phase 6: Verify Deployment

```bash
# 6.1 Check health endpoint
curl https://imagiax.dev/api/ping
# Expected: {"status": "ok"}

# 6.2 Check database health
curl https://imagiax.dev/api/health/db
# Expected: {"status": "healthy", "database": "connected"}

# 6.3 Check AI model
curl -s https://imagiax.dev/api/salud | python3 -m json.tool
# Expected: modelo loaded, orthanc connected

# 6.4 Test OHIF loads
curl -I https://imagiax.dev/
# Expected: 302 redirect to /admin (auth_request redirects unauthenticated)

# 6.5 Test admin login
curl -c cookies.txt -X POST https://imagiax.dev/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@imagiax.dev","password":"<YOUR_ADMIN_PASSWORD>"}'
```

### Phase 7: Post-Deploy Hardening

```bash
# 7.1 Change admin password immediately via API (or /admin panel)
# Login first, then PATCH /auth/usuarios/{id}/password

# 7.2 Set up automatic cert renewal
echo "0 12 * * * root certbot renew --quiet --deploy-hook 'docker exec nginx-imagiax nginx -s reload'" \
  | sudo tee -a /etc/crontab

# 7.3 Set up log rotation
sudo cat > /etc/logrotate.d/imagiax << 'EOF'
/opt/imagiax/logs/*.log {
    daily
    rotate 30
    compress
    missingok
    notifempty
}
EOF

# 7.4 Set up PostgreSQL backup
# Daily backup to /opt/imagiax/backups/
cat > /opt/imagiax/backup-db.sh << 'EOF'
#!/bin/bash
mkdir -p /opt/imagiax/backups
docker exec postgres-imagiax pg_dump -U imagiax imagiax \
  | gzip > /opt/imagiax/backups/imagiax-$(date +%Y%m%d-%H%M%S).sql.gz
# Keep only last 30 days
find /opt/imagiax/backups -name "*.sql.gz" -mtime +30 -delete
EOF
chmod +x /opt/imagiax/backup-db.sh
echo "0 2 * * * $USER /opt/imagiax/backup-db.sh" | sudo tee -a /etc/crontab
```

### Phase 8: Database Schema

**No migration scripts needed.** Schema is auto-created on FastAPI startup:
- `imagiax_db.py` → `_create_tables()` runs in `lifespan()`
- `imagiax_auth.py` → `create_auth_tables()` runs in `lifespan()`
- `imagiax_pagos_db.py` → `create_tables()` runs in `lifespan()`

All tables use `CREATE TABLE IF NOT EXISTS` — safe to re-run.

---

## 5. ENVIRONMENT STRATEGY

### 5.1 How the same codebase works in both environments

The architecture uses env var injection at every layer:

| Layer | Dev | Production |
|-------|-----|-----------|
| Backend | `.env` file (`APP_ENV=development`) | `.env.production` (`APP_ENV=production`) |
| Docker | `docker-compose.yml` (dev infra) | `docker-compose.prod.yml` (full stack) |
| OHIF Config | `yarn dev:orthanc` (local webpack dev server) | `APP_CONFIG` env var → injected by `entrypoint.sh` |
| API URLs | `http://localhost:8010` | `/api` (relative, nginx proxies) |
| PACS URLs | `http://localhost:8042` | `http://orthanc:8042` (Docker network) |
| DB URLs | `localhost:5432` | `postgres:5432` (Docker network) |

### 5.2 Critical env differences

```
DEV:
  APP_ENV=development
  POSTGRES_URL=postgresql://imagiax:imagiax2024@localhost:5432/imagiax
  CORS_ORIGINS=http://localhost:3000,http://127.0.0.1:3000
  IMAGIAX_BASE_URL=http://localhost:8010
  TBK_ENVIRONMENT=integration (test)

PRODUCTION:
  APP_ENV=production          ← ENABLES: hides /docs, /redoc, disables upsert_bootstrap_superadmin
  POSTGRES_URL=postgresql://imagiax:<STRONG_PW>@postgres:5432/imagiax
  CORS_ORIGINS=https://imagiax.dev,https://www.imagiax.dev
  IMAGIAX_BASE_URL=https://imagiax.dev/api
  TBK_ENVIRONMENT=production   ← REQUIRED by code (raises RuntimeError if missing)
```

### 5.3 What `APP_ENV=production` changes in the code

From `servidor_api_orthanc_v1.py`:
- `docs_url=None` — FastAPI Swagger UI disabled
- `redoc_url=None` — ReDoc disabled  
- `openapi_url=None` — OpenAPI JSON disabled

From `imagiax_auth.py` (`upsert_bootstrap_superadmin_from_env`):
- Superadmin upsert from env is DISABLED — won't overwrite passwords you've changed in DB

From `servidor_api_orthanc_v1.py` (lifespan):
- Validates `TBK_ENVIRONMENT == "production"` — fails fast if wrong

---

## 6. DNS & SSL SETUP

### 6.1 DNS Records Required

Add at your DNS provider (e.g., Cloudflare, GoDaddy, Namecheap):

```
Type  Name              Value            TTL
A     imagiax.dev       <SERVER_IP>      300
A     www.imagiax.dev   <SERVER_IP>      300
```

If using Cloudflare: **disable the proxy (orange cloud → gray)** during initial certbot setup. Enable it after.

### 6.2 SSL Certificate Timeline

```
1. Server started, port 80 open
2. DNS propagated (verify: nslookup imagiax.dev)
3. Run certbot standalone → gets cert
4. Start nginx with SSL config
5. Test: https://imagiax.dev
6. Set up cron for auto-renewal
```

### 6.3 Let's Encrypt Limits

- Rate limit: 5 certificates per domain per 7 days
- Use `--dry-run` to test before real cert: `certbot certonly --dry-run --standalone -d imagiax.dev`
- Certs expire in 90 days — cron renewal is mandatory

### 6.4 Nginx SSL Notes

The `nginx-proxy.conf` requires:
```
ssl_certificate     /etc/letsencrypt/live/imagiax.dev/fullchain.pem;
ssl_certificate_key /etc/letsencrypt/live/imagiax.dev/privkey.pem;
```

These paths are mounted in docker-compose.prod.yml:
```yaml
- /etc/letsencrypt:/etc/letsencrypt:ro
```

---

## 7. MONITORING & OPERATIONS

### 7.1 Health Check Endpoints

| Endpoint | Auth | Response | Purpose |
|----------|------|----------|---------|
| `GET /api/ping` | None | `{"status": "ok"}` | Liveness probe |
| `GET /api/health/db` | None | `{"status": "healthy"/"degraded"}` | DB readiness |
| `GET /api/salud` | JWT | Full system status | Comprehensive health |

⚠️ **Security note**: `/api/health/db` is publicly accessible (no auth). See SEC-BE-001.

Docker health check is defined in Dockerfile:
```
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3
CMD curl -fsS http://localhost:8010/ping || exit 1
```

### 7.2 Log Management

```bash
# View all services
docker compose -f docker-compose.prod.yml logs -f

# View specific service
docker compose -f docker-compose.prod.yml logs -f fastapi
docker compose -f docker-compose.prod.yml logs -f nginx

# Tail last 100 lines
docker compose -f docker-compose.prod.yml logs --tail=100 fastapi

# nginx access logs
docker exec nginx-imagiax cat /var/log/nginx/access.log
```

### 7.3 Database Backup & Restore

```bash
# Backup
docker exec postgres-imagiax pg_dump -U imagiax imagiax | gzip > imagiax-backup.sql.gz

# Restore (careful — drops existing data)
gunzip < imagiax-backup.sql.gz | docker exec -i postgres-imagiax psql -U imagiax imagiax
```

### 7.4 DICOM Storage Backup

```bash
# Backup Orthanc DICOM data volume
docker run --rm \
  --volumes-from orthanc-imagiax \
  -v /opt/imagiax/backups:/backup \
  alpine tar czf /backup/orthanc-data-$(date +%Y%m%d).tar.gz /var/lib/orthanc/db
```

### 7.5 Common Operations

```bash
# Restart a service
docker compose -f docker-compose.prod.yml restart fastapi

# Update FastAPI code (after git pull)
docker compose -f docker-compose.prod.yml build fastapi
docker compose -f docker-compose.prod.yml up -d --no-deps fastapi

# Check resource usage
docker stats

# Check disk usage
df -h
docker system df
```

---

## 8. RISKS & WARNINGS

### 🔴 CRITICAL — Must resolve before ANY production deployment

| Risk | Description | Impact | Fix |
|------|------------|--------|-----|
| **R1: Gemini API Key Shared** | Same key in `.env` (dev) and `.env.production` | Dev laptop compromise = production Gemini access | Get a new production-only key immediately |
| **R2: Weak Production Passwords** | `ImagiaxDemo2024` (PostgreSQL), `ImagiaxAdmin2024!` (admin) in plaintext | Database compromise, admin account takeover | Rotate before deploy |
| **R3: Payment in TEST Mode** | MP and TBK are using integration/test credentials | No real payments will be processed | Replace with production credentials |
| **R4: `APP_ENV` not set** | Missing from `.env.production` — API docs publicly accessible | `/docs` exposes all endpoints, schemas, auth models | Add `APP_ENV=production` |
| **R5: `TBK_ENVIRONMENT=integration` in prod** | The code raises RuntimeError if TBK is not `production` when `APP_ENV=production` | App won't start | Set `TBK_ENVIRONMENT=production` OR don't set `APP_ENV=production` until TBK is ready |
| **R6: `ORTHANC_AUTH_HEADER` missing** | nginx envsubst will fail silently or set empty header | `/pacs/` traffic to Orthanc will fail with 401 | Add `ORTHANC_AUTH_HEADER=Basic aW1hZ2lheDpJbWFnaWF4UEFDU18yMDI2IQ==` |

### 🟡 HIGH — Should resolve before production

| Risk | Description | Impact | Fix |
|------|------------|--------|-----|
| **R7: Path Traversal in DICOMweb Proxy (SEC-BE-003)** | `path` parameter not sanitized → access to non-DICOMweb Orthanc endpoints | Attacker can delete/modify DICOM studies | Sanitize path parameter (whitelist or prefix check) |
| **R8: Revoked Token Still Accesses PACS** | `/pacs/dicom-web/*` uses JWT-only verification, no DB revocation check (SEC-BE-002) | Deleted user retains PACS access for up to 7 days | Add DB revocation check to pacs_dicom_proxy |
| **R9: OHIF Build Takes >30 min** | OHIF is a massive React monorepo — first `docker build` takes 15-30+ min and ~8 GB | Deployment time, memory pressure on small VM | Pre-build OHIF image locally and push to registry, OR use a larger instance for build |
| **R10: PyTorch Model Download on First Start** | `torchxrayvision-cache` volume must be populated (~90 MB) | First startup takes longer, needs internet access | Volume persists between restarts via `torchxrayvision-cache` volume |
| **R11: No Error Tracking** | No Sentry/Rollbar integration | Silent failures in production go undetected | Add Sentry to FastAPI (optional but recommended for medical platform) |
| **R12: /informes/{id}/ver PHI leakage (SEC-BE-006)** | Any authenticated user (any role) can view any medical report | PHI leakage across roles | Add ownership/role check to `ver_informe()` endpoint |

### 🟠 MEDIUM — Important to know

| Risk | Description |
|------|------------|
| **R13: Rate Limiter is in-memory** | Not shared across workers (OK for `--workers 1`, breaks if scaled) |
| **R14: In-memory rate limiter resets on restart** | Brute-force window resets on container restart (DoS risk) |
| **R15: `TBK_ENVIRONMENT=production` blocks staging** | Can't test payments in staging with `APP_ENV=production` set |
| **R16: DICOM port 4242 security** | `docker-compose.prod.yml` does not publish port 4242 — external DICOM push requires reconfiguration |
| **R17: No docker-compose override for staging** | Only `docker-compose.yml` (dev) and `docker-compose.prod.yml` exist. No staging environment defined. |
| **R18: OHIF build in production** | OHIF is built from source in the Docker image. Changes require full rebuild (slow). Consider splitting build/runtime. |

### ✅ Production-Ready Security (from security audit)

These are positives that confirm the codebase handles production concerns:
- Non-root Docker user ✅
- `docs_url/redoc_url=None` in production ✅
- httpOnly cookies with SameSite=strict ✅
- bcrypt 12 rounds for PHI ✅
- JTI-based token revocation ✅
- Password complexity validation (12+ chars, uppercase, digit, special) ✅
- Superadmin cannot be deleted or deactivated ✅
- Audit logging on critical actions ✅
- DICOM size validation (200 MB limit) ✅
- Modality whitelist (only CR, DX, DR, etc.) ✅
- MP webhook HMAC-SHA256 validation ✅

---

## 9. PRODUCTION DEPLOYMENT DECISION MATRIX

### Can we deploy today?

| Gate | Status | Blocker? |
|------|--------|---------|
| Docker stack defined and complete | ✅ `docker-compose.prod.yml` exists with all 5 services | No |
| Nginx config production-ready | ✅ SSL, HSTS, CSP, auth_request | No |
| Backend handles production env | ✅ via `APP_ENV=production` | No |
| OHIF configured dynamically | ✅ via `APP_CONFIG` env var | No |
| Database schema auto-created | ✅ on startup | No |
| `APP_ENV` missing from `.env.production` | 🔴 Must add | **YES** |
| `ORTHANC_AUTH_HEADER` missing | 🔴 Must add | **YES** |
| Gemini key shared dev/prod | 🔴 Must rotate | **YES** |
| Passwords weak/default | 🔴 Must change | **YES** |
| MP/TBK still in test mode | 🔴 For real payments | YES (if taking payments) |
| Server not provisioned | 🔴 Need Oracle Cloud VM | **YES** |
| DNS not configured | 🔴 Need A records | **YES** |
| Cert not issued | 🔴 Need certbot run | **YES** |
| SEC-BE-003 path traversal | 🟡 Security risk | Recommended |
| SEC-BE-002 revocation gap | 🟡 Security risk | Recommended |

**Verdict**: The codebase is well-structured for production deployment. The blockers are all configuration/infrastructure, not code. With a properly filled `.env.production` and a provisioned server, deployment is straightforward.

---

## 10. RECOMMENDED PHASED APPROACH

### Phase 0 (Now): Secrets Rotation
1. Rotate Gemini API key
2. Generate strong JWT_SECRET
3. Set strong PostgreSQL and admin passwords
4. Decide: deploy MP/TBK in test mode or get production credentials?

### Phase 1: Infrastructure (Day 1)
1. Provision Oracle Cloud VM (4 OCPUs / 24 GB ARM64 — Always Free)
2. Install Docker
3. Set DNS A records
4. Copy project files

### Phase 2: First Deploy (Day 1-2)
1. Set up `.env.production` with all values
2. Get SSL cert (certbot standalone)
3. Build Docker images (plan 30-60 min for OHIF)
4. Start all services
5. Verify health endpoints

### Phase 3: Security Hardening (Day 2-3)
1. Fix SEC-BE-003 (path traversal) — add path sanitization
2. Fix SEC-BE-002 (PACS revocation) — add DB check to proxy
3. Change admin password after first login
4. Enable MercadoPago production credentials
5. Enable Transbank production credentials

### Phase 4: Operations (Ongoing)
1. Set up cron for cert renewal + DB backup
2. Monitor logs daily
3. Add Sentry for error tracking (optional)

---

## Affected Areas

- `.env.production` — Missing `APP_ENV`, `ORTHANC_AUTH_HEADER`; weak passwords; test payment credentials
- `OHIF Medical Imaging Viewer/orthanc-imagiax/docker-compose.prod.yml` — Complete, production-ready
- `OHIF Medical Imaging Viewer/orthanc-imagiax/config/nginx-proxy.conf` — Complete, production-ready
- `OHIF Medical Imaging Viewer/orthanc-imagiax/config/orthanc.json` — Functional, optional hardening
- `Dockerfile` — Complete, production-ready
- `servidor_api_orthanc_v1.py` — Complete, no code changes needed
- `imagiax_auth.py` — Complete, no code changes needed
- `pagos_mp.py` — Complete, only credentials need to change
- `pagos_tbk.py` — Complete, only credentials need to change
- `ohif-extension-imagiax-ai/src/services/imagiaxApi.ts` — Complete, `window.__IMAGIAX_API_URL__` injection handles production
- `ohif-extension-imagiax-ai/src/ImagiaxAiPanel.tsx` — Complete, `window.__IMAGIAX_CONFIG__` injection handles production

## Recommendation

**Deploy immediately is possible** once the configuration blockers are resolved. The stack is well-engineered for production — the docker-compose.prod.yml, nginx config, and env var strategy are all solid. The path traversal (SEC-BE-003) and PACS revocation (SEC-BE-002) security issues should be patched in the same PR as the deploy configuration.

**Estimated time to production**: 1-2 days with the right credentials and server access.

## Ready for Proposal
**Yes** — the exploration is complete. The orchestrator should present the user with:
1. A list of actions THEY must take (credentials, server provisioning, DNS)
2. A list of code changes needed (SEC-BE-002, SEC-BE-003, `.env.production` additions)
3. Confirmation that the codebase is already production-ready architecturally

## Risks
- **R1 (Critical)**: Gemini API key shared dev/prod — rotate before deploy
- **R5 (Critical)**: `TBK_ENVIRONMENT=production` + `APP_ENV=production` = app won't start if TBK credentials are still integration
- **R6 (Critical)**: `ORTHANC_AUTH_HEADER` missing — `/pacs/` routes will fail
- **R9 (High)**: OHIF build is extremely heavy — plan for 30-60 min and 8+ GB RAM on build machine
