# ============================================================
# IMAGIAX — Backend FastAPI
# Compatible con ARM64 (Oracle Cloud Always Free) y x86_64
#
# El modelo resnet50-res512-all se descarga automáticamente
# la primera vez que arranca (~90 MB, se cachea en volumen Docker)
# ============================================================

FROM python:3.11-slim

# Dependencias del sistema para opencv-headless, scikit-image y HEALTHCHECK
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    libgomp1 \
    libgl1 \
    curl \
    gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Usuario no-root usado por la imagen para alinear permisos de cache/runtime.
RUN useradd --create-home --shell /usr/sbin/nologin imagiax

COPY requirements.txt .

# PyTorch CPU — instalado primero, separado del resto.
# --index-url cpu garantiza la variante sin CUDA, compatible con ARM64.
RUN pip install --no-cache-dir \
    torch==2.6.0 \
    torchvision==0.21.0 \
    --index-url https://download.pytorch.org/whl/cpu

# Resto de dependencias (lee requirements.txt)
RUN pip install --no-cache-dir -r requirements.txt

# Runtime factory observado en producción viva.
# PYTHONPATH=/app/src permite que `api.main:create_app` resuelva a /app/src/api/main.py,
# manteniendo compatibilidad con imports internos tipo `from infrastructure...`.
ENV PYTHONPATH=/app/src

# Código runtime observado en el contenedor productivo: /app/src, /app/app y /app/prompts.
COPY src/ ./src/
COPY app/ ./app/
COPY prompts/ ./prompts/

# Directorio de cache para TorchXRayVision.
# En docker-compose.prod.yml se monta como volumen para persistir entre reinicios.
RUN mkdir -p /home/imagiax/.torchxrayvision \
    && chown -R imagiax:imagiax /app /home/imagiax/.torchxrayvision

USER imagiax

EXPOSE 8010

# Liveness probe observado en producción viva: /salud responde 200.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fsS http://localhost:8010/salud || exit 1

# workers=1: PyTorch no es thread-safe con múltiples workers en inferencia.
# Runtime esperado por el gate de deploy: api.main:create_app --factory.
CMD ["uvicorn", "api.main:create_app", \
     "--factory", \
     "--host", "0.0.0.0", \
     "--port", "8010", \
     "--workers", "1", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "172.16.0.0/12,192.168.0.0/16,10.0.0.0/8"]
