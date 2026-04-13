# IMAGIAX — Secure Deployment Checklist

> Revisar esta lista **antes de cada deploy a producción**. Cada ítem tiene una referencia al finding de seguridad que originó el requisito.

---

## 🔐 Variables de Entorno (obligatorias)

| Variable | Valor esperado en prod | Referencia |
|----------|----------------------|------------|
| `APP_ENV` | `production` | SEC-PAY-006 |
| `IMAGIAX_PAYMENT_MODE` | `production` | SEC-PAY-006/007 |
| `MP_WEBHOOK_SECRET` | Secreto real de MercadoPago (no vacío) | SEC-PAY-001 |
| `ORTHANC_PASSWORD` | Contraseña PACS actual (no valor default) | SEC-INFRA-001 |
| `ORTHANC_AUTH_HEADER` | `Basic <base64(imagiax:<ORTHANC_PASSWORD>)>` | SEC-INFRA-001 |
| `JWT_SECRET` | ≥32 chars, generado con `openssl rand -hex 32` | SEC-INFRA-002 |
| `GEMINI_API_KEY` | Clave activa de Google AI Studio | SEC-INFRA-003 |
| `DATABASE_URL` | Apunta a DB de producción | — |

**Cómo generar ORTHANC_AUTH_HEADER:**
```bash
echo -n "imagiax:<ORTHANC_PASSWORD>" | base64
# Prefijarlo con "Basic ": Basic <resultado>
```

---

## 🚀 Proceso de Deploy

### Pre-deploy

- [ ] Verificar que `APP_ENV=production` y `IMAGIAX_PAYMENT_MODE=production` están seteadas
- [ ] Confirmar que `MP_WEBHOOK_SECRET` no está vacío (el servidor rechazará webhooks si está vacío)
- [ ] Confirmar que `JWT_SECRET` fue rotada en los últimos 90 días
- [ ] Confirmar que `ORTHANC_PASSWORD` no es el valor default (`imagiax2024`)
- [ ] Confirmar que `GEMINI_API_KEY` es válida y tiene cuota suficiente

### Deploy

- [ ] Build de imagen Docker: `docker build -t imagiax-backend .`
- [ ] Verificar que el proceso corre como usuario no-root: `docker run --rm imagiax-backend whoami` → debe retornar `imagiax`
- [ ] Levantar con docker-compose: `docker-compose -f docker-compose.prod.yml up -d`

### Post-deploy

- [ ] Health check básico: `curl https://<domain>/ping` → `{"pong": true}`
- [ ] Health check DB: `curl https://<domain>/health/db` → `{"ok": true, "database": "up"}`
- [ ] Verificar headers de seguridad: `curl -I https://<domain>/ping` debe incluir `Content-Security-Policy` y `X-Frame-Options`
- [ ] Verificar que `/health/db` NO expone strings de excepción cuando la DB falla

---

## 🌐 Configuración de Proxy Reverso (Nginx/Traefik)

### X-Forwarded-For (SEC-BE-010/011/012)

El servidor FastAPI confía en headers `X-Forwarded-For` **solo desde subredes privadas Docker** (configurado en `CMD` del Dockerfile):

```
--proxy-headers
--forwarded-allow-ips "172.16.0.0/12,192.168.0.0/16,10.0.0.0/8"
```

**Requisito**: El proxy reverso **DEBE** estar en la misma red Docker interna para que esto funcione. Si el proxy está en una IP pública, agregar su IP a `--forwarded-allow-ips`.

### Headers que el proxy DEBE inyectar

```nginx
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header Host $host;
```

### Headers que el proxy NO debe exponer al exterior

```nginx
# Nunca exponer estos endpoints directamente al internet:
# /orthanc/* — Orthanc internal API
# /docs — FastAPI Swagger UI (en producción, comentar en main app)
# /redoc — FastAPI ReDoc
```

---

## 🔑 Rotación de Secretos

| Secreto | Frecuencia | Responsable |
|---------|-----------|-------------|
| `JWT_SECRET` | Cada 90 días o ante sospecha de compromiso | DevOps |
| `ORTHANC_PASSWORD` | Cada 180 días | DevOps |
| `GEMINI_API_KEY` | Ante sospecha de leak (revisar GCP logs) | Dev Lead |
| `MP_WEBHOOK_SECRET` | Ante cambio de cuenta MP o sospecha de leak | Dev Lead |

**Procedimiento de rotación de JWT_SECRET:**
1. Generar nueva clave: `openssl rand -hex 32`
2. Actualizar `.env.production` y reiniciar servicio
3. **Todos los usuarios activos serán deslogueados** (tokens anteriores invalidados)
4. Notificar al equipo antes del reinicio

---

## 🛡️ Verificaciones de Seguridad Post-Deploy

```bash
# 1. Path traversal bloqueado
curl -v "https://<domain>/pacs/dicom-web/..%2Fsystem" 
# → Esperado: 400 Bad Request

# 2. Webhook sin firma rechazado
curl -X POST "https://<domain>/pagos/webhook" \
  -H "Content-Type: application/json" \
  -d '{"action":"payment.created","data":{"id":"123"}}'
# → Esperado: 401 Unauthorized

# 3. Health DB sin leak de excepción (con DB caída)
curl "https://<domain>/health/db"
# → Esperado: {"ok": false, "database": "unavailable"} (sin stack trace)

# 4. Campos con max_length respetados
curl -X POST "https://<domain>/informes/guardar-decision" \
  -H "Content-Type: application/json" \
  -d '{"informe_id":"test","estado":"aceptado","informe_final":"'"$(python3 -c "print('A'*60000)")"'"}'
# → Esperado: 422 Unprocessable Entity

# 5. CORS no es wildcard en producción
curl -I -H "Origin: https://evil.com" "https://<domain>/ping"
# → Esperado: NO debe aparecer Access-Control-Allow-Origin: *
```

---

## 📋 Referencia de Findings

| Finding ID | Descripción | Remediado en |
|------------|-------------|-------------|
| SEC-BE-001 | Health endpoint leakaba exception strings | Phase 1 - Task 1.3 |
| SEC-BE-002 | Proxy DICOM no verificaba revocación de token | Phase 1 - Task 1.2 |
| SEC-BE-003 | Path traversal en proxy DICOMweb | Phase 1 - Task 1.1 |
| SEC-BE-005 | Dead code en is_jti_revoked() | Phase 2 - Task 2.6 |
| SEC-BE-006 | ver_informe() sin check de rol | Phase 3 - Task 3.2 |
| SEC-BE-007 | Falta CSP header | Phase 3 - Task 3.1 |
| SEC-BE-008 | CORS wildcard + credenciales | Phase 3 - Task 3.3 |
| SEC-BE-009 | Sin límite en parámetro limit de listado | Phase 3 - Task 3.3 |
| SEC-BE-010/011/012 | X-Forwarded-For sin validación de proxy confiable | Phase 4 - Task 4.2 (Dockerfile CMD) |
| SEC-PAY-001 | HMAC skip en modo test | Phase 1 - Task 1.5 |
| SEC-PAY-002 | /pagos/exito sin autenticación | Phase 1 - Task 1.6 |
| SEC-PAY-003/004 | IDOR en /tbk/cobrar | Phase 1 - Task 1.4 |
| SEC-PAY-005 | TOCTOU en acreditar_pago() | Phase 2 - Task 2.3 |
| SEC-PAY-006/007 | _IS_TEST derivado de token prefix | Phase 2 - Task 2.1 |
| SEC-PAY-008 | Credenciales hardcodeadas / faltantes | Phase 2 - Task 2.2 |
| SEC-PAY-009 | PAN/CVV en logs de DB | Phase 3 - Task 3.4 |
| SEC-PAY-010 | external_reference sin validación estricta | Phase 3 - Task 3.6 |
| SEC-AI-001 | Sin límite en max_instances | Phase 2 - Task 2.5 |
| SEC-AI-002 | SDK TBK bloqueaba event loop | Phase 2 - Task 2.4 |
| SEC-AI-003 | Exception strings en redirect URLs | Phase 3 - Task 3.5 |
| SEC-AI-004/005 | Sin límites de tamaño en campos de texto libre | Phase 4 - Task 4.1 |
| SEC-INFRA-001 | Credencial PACS default en producción | Phase 2 - Task 2.2 |
| SEC-INFRA-002 | JWT_SECRET sin rotación documentada | Phase 4 - Task 4.2 (este checklist) |
| SEC-INFRA-003 | GEMINI_API_KEY sin rotación documentada | Phase 4 - Task 4.2 (este checklist) |
| SEC-INFRA-004 | Dockerfile corría como root | Phase 4 - Task 4.1 (Dockerfile USER imagiax) |
