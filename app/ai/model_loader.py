"""
app.ai.model_loader — PyTorch model loading for IMAGIAX AI pipeline.

Extracted from servidor_api_v6.py (God Object refactor, Phase 5).

Provides a singleton loader for the TorchXRayVision ResNet50-512-all model.
The model is loaded once at application startup (via lifespan) and stored in
``app.state.model``.

Usage (lifespan):
    from app.ai.model_loader import load_model

    async def lifespan(app: FastAPI):
        app.state.model = load_model()
        yield

Usage (dependency / service call):
    model = request.app.state.model
    from app.ai.dicom_service import infer_multilabel
    result = infer_multilabel(tensor, model)
"""

from __future__ import annotations

import logging
from typing import Any

import torch

logger = logging.getLogger("imagiax.model")

# ── Device selection ──────────────────────────────────────────────────────────
DEVICE: torch.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

# ── Module-level singleton (populated by load_model()) ───────────────────────
_model: Any | None = None


def load_model() -> Any:
    """Load and return the NIH ResNet50-512-all TorchXRayVision model.

    The model is loaded once; subsequent calls return the cached instance.
    Should be called exactly once from the FastAPI lifespan so the heavy
    ~90 MB download / weight loading happens before the first request.

    Architecture:
        ResNet50-512-all from TorchXRayVision.
        - Input: ``[B, 1, 512, 512]`` float tensor (TXV normalisation: ×2048 − 1024).
        - Output: probability vector of length 18 (sigmoid + op_norm applied internally).
        - Benchmark NIH ChestX-ray14: Pneumothorax AUC 0.91, Emphysema 0.88,
          Cardiomegaly 0.90.

    Returns:
        Loaded, eval-mode model on ``DEVICE``.
    """
    global _model
    if _model is not None:
        return _model

    import torchxrayvision as xrv  # deferred import — heavy optional dependency

    _model = xrv.models.ResNet(weights="resnet50-res512-all")
    _model.to(DEVICE)
    _model.eval()

    logger.info(
        "ResNet50-512-all loaded on %s — 18 pathologies | AUC Pneumothorax ~0.91",
        DEVICE,
    )
    return _model


def get_model() -> Any:
    """Return the cached model instance, loading it if necessary.

    Convenience alias for :func:`load_model` — identical behaviour.
    Kept for backward compatibility with callers that use ``get_model()``.
    """
    return load_model()
