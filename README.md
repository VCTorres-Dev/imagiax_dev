# IMAGIAX — Plataforma de Soporte Radiológico con IA

> Sistema clínico de análisis automatizado de imágenes DICOM con inteligencia artificial, gestión de informes médicos y pagos integrados. Desplegado en producción en [imagiax.dev](https://imagiax.dev).

> **Nuevo equipo?** Lee primero el paquete de handoff en [`docs/handoff/`](docs/handoff/README.md). Ese paquete es la referencia vigente para estado de deploy, gap de migraciones y próximos pasos. Las secciones históricas de este README pueden describir la arquitectura legacy.

---

## Arquitectura

```
                        ┌─────────────────────────────────────┐
                        │           imagiax.dev (HTTPS)        │
                        │         DigitalOcean Droplet          │
                        │         2vCPU · 4GB RAM · Ubuntu      │
                        └──────────────┬──────────────────────┘
                                       │ nginx (80 / 443)
                       ┌───────────────┼───────────────┐
                       ▼               ▼               ▼
               /api/ → FastAPI    /pacs/ → Orthanc    / → OHIF
               (puerto 8010)     (puerto 8042)      (puerto 8080)
                       │               │
                       └───────┬───────┘
                               ▼
                          PostgreSQL 16
                         (red interna Docker)
```

| Servicio | Tecnología | Rol |
|----------|-----------|-----|
| **fastapi** | Python 3.11 + FastAPI + PyTorch | API principal, análisis IA, pagos |
| **orthanc** | Orthanc + plugins | PACS DICOM (almacenamiento y recuperación) |
| **ohif** | OHIF v3 + extensión imagiax-ai | Visor DICOM web con panel IA |
| **postgres** | PostgreSQL 16 | Base de datos (licencias, informes, pagos) |
| **nginx** | nginx:alpine | Reverse proxy + SSL Let's Encrypt |

---

## Funcionalidades principales

### Análisis radiológico IA
- Modelo NIH multilabel (TorchXRayVision) para detección de 18 patologías en radiografía de tórax
- Generación de informes médicos narrativos vía Gemini LLM
- Niveles de alerta (normal / hallazgo leve / hallazgo significativo / crítico)
- Soporte para instancias, series y estudios DICOM completos

### Informes médicos
- Generación de PDF con datos del paciente, hallazgos y código QR de verificación
- Página pública de verificación de autenticidad (`/api/informes/{id}/ver`)
- Edición y firma por médico radiólogo
- Historial por estudio DICOM

### Pagos
- **MercadoPago** Checkout Pro: pago único y autopago por tarjeta registrada
- **Transbank** Webpay Plus Mall: pago con tarjetas chilenas
- Webhooks con validación de firma HMAC-SHA256
- Exportación de historial en Excel (con gráficos) y CSV

### Visor DICOM
- OHIF v3 con extensión personalizada `imagiax-ai`
- Panel lateral: resultados IA, generación de informe, descarga PDF, QR
- Configuración dinámica vía `window.__IMAGIAX_CONFIG__`

---

## Estructura del repositorio

```
imagiax_dev/
├── servidor_api_orthanc_v1.py        # API FastAPI principal
├── imagiax_db.py                     # Capa de datos — informes y estudios
├── imagiax_pagos_db.py               # Capa de datos — pagos y licencias
├── pagos_mp.py                       # Módulo MercadoPago (router FastAPI)
├── pagos_tbk.py                      # Módulo Transbank (router FastAPI)
├── requirements.txt                  # Dependencias Python
├── Dockerfile                        # Build imagen FastAPI
├── .dockerignore
│
├── OHIF Medical Imaging Viewer/
│   └── orthanc-imagiax/
│       ├── docker-compose.yml        # Stack de desarrollo local
│       ├── docker-compose.prod.yml   # Stack de producción
│       └── config/
│           ├── nginx-proxy.conf      # Configuración nginx (reverse proxy + SSL)
│           └── orthanc.json         # Configuración Orthanc PACS
│
└── ohif-extension-imagiax-ai/        # Extensión OHIF personalizada
    ├── package.json
    └── src/
        ├── ImagiaxAiPanel.tsx        # Panel principal IA (React)
        ├── services/imagiaxApi.ts    # Cliente HTTP hacia FastAPI
        ├── index.tsx                 # Registro de extensión
        └── ...
```

> El directorio `OHIF Medical Imaging Viewer/Viewers/` (código fuente base de OHIF) no se versiona en este repo — se reconstruye con `bun install && bun run build` desde [OHIF/Viewers](https://github.com/OHIF/Viewers). Solo se versiona la extensión personalizada.

---

## Variables de entorno

El archivo `.env.production` **no se versiona** (contiene credenciales). Variables requeridas:

```env
# Google Gemini
GEMINI_API_KEY=

# PostgreSQL
POSTGRES_URL=postgresql://user:pass@postgres:5432/imagiax
POSTGRES_PASSWORD=

# Orthanc
ORTHANC_BASE_URL=http://orthanc:8042

# URLs públicas
IMAGIAX_BASE_URL=https://imagiax.dev/api
CORS_ORIGINS=https://imagiax.dev,https://www.imagiax.dev
IMAGIAX_VIEWER_URL=/

# MercadoPago
MP_PUBLIC_KEY=
MP_ACCESS_TOKEN=
MP_WEBHOOK_SECRET=

# Transbank
TBK_ENVIRONMENT=integration   # o "production"
TBK_MALL_COMMERCE_CODE=
TBK_CHILD_COMMERCE_CODE=
TBK_API_KEY=

# Precio por análisis (CLP)
IMAGIAX_PRECIO_SCAN=990
```

---

## Deploy en producción

> **Alto:** para handoff y continuidad, esta sección queda supersedida por [`docs/handoff/DEPLOYMENT_STATUS.md`](docs/handoff/DEPLOYMENT_STATUS.md). No ejecutes estos pasos en producción sin resolver primero el mismatch `api.main` vs `app.main` y el gap de migraciones documentado.

### Requisitos
- Docker + Docker Compose v2
- Dominio con DNS apuntando al servidor
- Certificado SSL (Let's Encrypt vía Certbot)

### Pasos
```bash
# 1. Clonar repo en el servidor
git clone https://github.com/VCTorres-Dev/imagiax_dev.git

# 2. Crear .env.production con las variables reales

# 3. Levantar el stack
cd "OHIF Medical Imaging Viewer/orthanc-imagiax"
docker compose -f docker-compose.prod.yml up -d --build

# 4. Verificar
docker compose -f docker-compose.prod.yml ps
curl https://imagiax.dev/api/salud
```

### Actualizar un servicio sin downtime
```bash
# Solo backend (rápido, ~2 min)
docker compose -f docker-compose.prod.yml up -d --build fastapi

# Solo visor OHIF (requiere rebuild, ~25 min)
docker compose -f docker-compose.prod.yml up -d --build ohif
```

---

## Desarrollo local

```bash
# Levantar stack local (HTTP, sin SSL)
cd "OHIF Medical Imaging Viewer/orthanc-imagiax"
docker compose up -d --build

# API disponible en: http://localhost:8010
# Visor OHIF en:    http://localhost:3000
# Orthanc en:       http://localhost:8042
# Docs Swagger:     http://localhost:8010/docs
```

---

## Endpoints principales

| Método | Ruta | Descripción |
|--------|------|-------------|
| `GET` | `/salud` | Health check |
| `POST` | `/analizar/estudio` | Analizar estudio DICOM completo |
| `POST` | `/analizar/serie` | Analizar serie DICOM |
| `POST` | `/analizar/instancia` | Analizar instancia DICOM |
| `GET` | `/informes/listado` | Listar informes paginados |
| `GET` | `/informes/{id}/ver` | Verificación pública de informe (QR) |
| `GET` | `/pagar` | Portal de pagos |
| `GET` | `/pagos/historial` | Historial de cobros |
| `GET` | `/pagos/exportar-excel` | Exportar reporte Excel |
| `GET` | `/docs` | Documentación Swagger UI |

---

## Stack tecnológico

| Capa | Tecnología |
|------|-----------|
| Lenguaje backend | Python 3.11 |
| Framework API | FastAPI + Uvicorn |
| IA — visión | TorchXRayVision (NIH multilabel) |
| IA — lenguaje | Google Gemini (google-genai) |
| Base de datos | PostgreSQL 16 + asyncpg |
| PACS | Orthanc + plugins |
| Visor DICOM | OHIF v3 (React + TypeScript) |
| Pagos | MercadoPago SDK + Transbank SDK |
| Contenedores | Docker Compose |
| Proxy / SSL | nginx + Let's Encrypt (Certbot) |
| Infraestructura | DigitalOcean (Ubuntu 22.04) |

---

## Seguridad

- Credenciales excluidas del repositorio via `.gitignore`
- Datos médicos (DICOMs, reportes con pacientes) excluidos del repositorio
- Webhooks MercadoPago validados con firma HMAC-SHA256
- HTTPS obligatorio en producción (HTTP redirige automáticamente)
- Rate limiting en endpoints de análisis IA
- Transacciones atómicas con `FOR UPDATE SKIP LOCKED` para créditos

---

*Proyecto privado — todos los derechos reservados.*
