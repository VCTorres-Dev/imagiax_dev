# ============================================================
# IMAGIAX — Backend FastAPI — Multi-stage build
# Compatible con ARM64 (Oracle Cloud Always Free) y x86_64
#
# LOW-5: Two stages — builder installs gcc + compiles wheels,
# runtime image receives only the installed packages (no gcc).
#
# El modelo resnet50-res512-all se descarga automáticamente
# la primera vez que arranca (~90 MB, se cachea en volumen Docker)
# ============================================================

# ── Builder ──────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

# Build-time system dependencies (gcc, python3-dev needed to compile C extensions
# in some packages like psutil, cryptography, etc.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

COPY requirements.txt .

# PyTorch CPU — instalado primero, separado del resto.
# --index-url cpu garantiza la variante sin CUDA, compatible con ARM64.
# --prefix=/install puts everything into a single relocatable directory.
RUN pip install --no-cache-dir --prefix=/install \
    torch==2.6.0 \
    torchvision==0.21.0 \
    --index-url https://download.pytorch.org/whl/cpu

# Resto de dependencias (lee requirements.txt).
# PYTHONPATH permite que pip vea el torch CPU instalado en /install en el paso anterior;
# sin esto, dependencias como torchxrayvision pueden intentar descargar otro torch GPU enorme.
RUN PYTHONPATH=/install/lib/python3.11/site-packages \
    pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Runtime ──────────────────────────────────────────────────────────────────
FROM python:3.11-slim

# Runtime-only system libraries (no gcc, no build tools)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    libgomp1 \
    libgl1 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy compiled packages from builder stage
COPY --from=builder /install /usr/local

# Usuario no-root dedicado — el proceso nunca corre como root en producción
RUN groupadd --gid 1001 imagiax \
    && useradd --uid 1001 --gid 1001 --create-home --shell /sbin/nologin imagiax

WORKDIR /app

# Copiar el paquete app/ (nuevo entry point: app.main:create_app)
COPY app/ ./app/

# Copiar plantillas de prompts requeridas por app.ai.prompt_orchestrator.
# Sin este directorio la imagen arranca con warnings y la calidad/runtime de IA se degrada.
COPY prompts/ ./prompts/

# Directorio de cache para TorchXRayVision.
# En docker-compose.prod.yml se monta como volumen para persistir entre reinicios.
RUN mkdir -p /home/imagiax/.torchxrayvision \
    && chown -R imagiax:imagiax /app /home/imagiax/.torchxrayvision

# Cambiar al usuario no-root antes de exponer el puerto y definir el comando
USER imagiax

EXPOSE 8010

# Liveness probe: GET /salud debe responder 200 en menos de 5 s
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fsS http://localhost:8010/salud || exit 1

# workers=1: PyTorch no es thread-safe con múltiples workers en inferencia
# forwarded-allow-ips: solo confiar en el proxy nginx (red Docker interna)
CMD ["uvicorn", "app.main:create_app", \
     "--factory", \
     "--host", "0.0.0.0", \
     "--port", "8010", \
     "--workers", "1", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "172.16.0.0/12,192.168.0.0/16,10.0.0.0/8"]
