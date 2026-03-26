# ============================================================
# IMAGIAX — Backend FastAPI
# Compatible con ARM64 (Oracle Cloud Always Free) y x86_64
#
# El modelo resnet50-res512-all se descarga automáticamente
# la primera vez que arranca (~90 MB, se cachea en volumen Docker)
# ============================================================

FROM python:3.11-slim

# Dependencias del sistema para opencv-headless y scikit-image
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    libgomp1 \
    libgl1 \
    gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

# PyTorch CPU — instalado primero, separado del resto.
# --index-url cpu garantiza la variante sin CUDA, compatible con ARM64.
RUN pip install --no-cache-dir \
    torch==2.6.0 \
    torchvision==0.21.0 \
    --index-url https://download.pytorch.org/whl/cpu

# Resto de dependencias (lee requirements.txt)
RUN pip install --no-cache-dir -r requirements.txt

# Solo los archivos que servidor_api_orthanc_v1 importa directamente.
# imagiax_core.py NO se incluye: no es importado por el servidor principal.
COPY servidor_api_orthanc_v1.py \
     servidor_api_v6.py \
     imagiax_db.py \
     imagiax_pagos_db.py \
     pagos_mp.py \
     pagos_tbk.py \
     ./

# Directorio de cache para TorchXRayVision.
# En docker-compose.prod.yml se monta como volumen para persistir entre reinicios.
RUN mkdir -p /root/.torchxrayvision

EXPOSE 8010

# workers=1: PyTorch no es thread-safe con múltiples workers en inferencia
CMD ["uvicorn", "servidor_api_orthanc_v1:app", \
     "--host", "0.0.0.0", \
     "--port", "8010", \
     "--workers", "1", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "*"]
