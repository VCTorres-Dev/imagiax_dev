"""
app.ai.dicom_service — DICOM preprocessing for AI inference.

Extracted from servidor_api_orthanc_v1.py (God Object refactor, Phase 5).

Contains:
- dicom_to_tensor()       — DICOM bytes → PyTorch tensor [1,1,512,512] for NIH ResNet50-512
- dicom_to_png_bytes()    — DICOM bytes → normalised PNG bytes for Gemini Vision
- infer_multilabel()      — Run the loaded NIH model; returns structured pathology dict
- analyze_single_instance() — Orchestrates download → preprocess → inference

Usage:
    from app.ai.dicom_service import (
        dicom_to_tensor,
        dicom_to_png_bytes,
        infer_multilabel,
        analyze_single_instance,
    )
"""

from __future__ import annotations

import asyncio
import io
import logging
from typing import Any

import cv2
import httpx
import numpy as np
import pydicom
import skimage.transform
import torch
from fastapi import HTTPException
from pydicom.pixel_data_handlers.util import apply_modality_lut

from app.ai.orthanc_client import get_instance_dicom_bytes, get_instance_tags
from app.ai.prompt_orchestrator import sanitize_dicom_text

logger = logging.getLogger("imagiax.dicom")

# Image size expected by the NIH ResNet50-512 model
IMG_SIZE: int = 512

# NIH ChestX-ray14 pathology labels (18 classes for ResNet50-512-all)
NIH_PATOLOGIAS: list[str] = [
    "Atelectasis", "Consolidation", "Infiltration", "Pneumothorax",
    "Edema", "Emphysema", "Fibrosis", "Effusion", "Pneumonia",
    "Pleural_Thickening", "Cardiomegaly", "Nodule", "Mass", "Hernia",
    "Lung Lesion", "Fracture", "Lung Opacity", "Enlarged Cardiomediastinum",
]

# Per-pathology thresholds.
# Lung Lesion and Enlarged Cardiomediastinum have op_thresh=nan in the model →
# op_norm always returns 0.5; threshold 0.6 prevents constant false positives.
UMBRALES: dict[str, float] = {p: 0.5 for p in NIH_PATOLOGIAS}
UMBRALES["Lung Lesion"] = 0.6
UMBRALES["Enlarged Cardiomediastinum"] = 0.6


# ============================================================
# Shared DICOM pixel preprocessing helper
# ============================================================


def _preprocess_dicom_pixels(ds: pydicom.Dataset) -> np.ndarray:
    """Extract and normalize pixel data from a DICOM dataset.

    Shared by both ``dicom_to_tensor`` and ``dicom_to_png_bytes`` to avoid
    duplicate MONOCHROME1 inversion and percentile-scaling logic.

    Steps:
      1. Read pixel_array as float32.
      2. Apply Modality LUT (RescaleSlope/Intercept) if present.
      3. Invert MONOCHROME1 images (luminance is inverted in that encoding).
      4. Robust percentile normalisation (p1–p99) → uint8.

    Args:
        ds: pydicom Dataset (already read from bytes).

    Returns:
        Normalised 8-bit NumPy array (same H×W shape as the original).
    """
    pixel_data = ds.pixel_array.astype(np.float32)
    pixel_data = apply_modality_lut(pixel_data, ds).astype(np.float32)

    photometric = str(getattr(ds, "PhotometricInterpretation", ""))
    if photometric.upper() == "MONOCHROME1":
        pixel_data = np.max(pixel_data) - pixel_data

    p1 = np.percentile(pixel_data, 1)
    p99 = np.percentile(pixel_data, 99)
    if p99 <= p1:
        p1 = float(np.min(pixel_data))
        p99 = float(np.max(pixel_data)) + 1e-6

    img_8u = np.clip((pixel_data - p1) / (p99 - p1) * 255, 0, 255).astype(np.uint8)
    return img_8u


# ============================================================
# DICOM → Tensor (ResNet50 inference path)
# ============================================================


def dicom_to_tensor(dicom_bytes: bytes, device: torch.device | None = None) -> torch.Tensor:
    """Convert raw DICOM bytes to a ``[1, 1, 512, 512]`` float tensor.

    Pipeline:
      1. Read pixel_array with pydicom.
      2. Apply Modality LUT (RescaleSlope/Intercept) if present.
      3. Correct MONOCHROME1 by inverting intensities.
      4. Robust percentile scaling to 8-bit for CLAHE.
      5. CLAHE contrast enhancement.
      6. Resize to ``IMG_SIZE × IMG_SIZE``.
      7. Apply TorchXRayVision normalisation (× 2048 − 1024).

    Args:
        dicom_bytes: Raw DICOM file bytes.
        device: Target PyTorch device. Defaults to CPU if not provided.

    Returns:
        Float tensor shaped ``[1, 1, IMG_SIZE, IMG_SIZE]``.

    Raises:
        ValueError: If the DICOM file contains no PixelData.
    """
    if device is None:
        device = torch.device("cpu")

    dataset = pydicom.dcmread(io.BytesIO(dicom_bytes), force=True)

    if "PixelData" not in dataset:
        raise ValueError("The DICOM file contains no PixelData")

    # Shared MONOCHROME1 correction + percentile normalisation
    img_8u = _preprocess_dicom_pixels(dataset)

    # ResNet50-specific: CLAHE contrast enhancement
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    img_8u = clahe.apply(img_8u)

    resized = skimage.transform.resize(img_8u, (IMG_SIZE, IMG_SIZE), anti_aliasing=True)
    normalized = resized * 2048.0 - 1024.0

    return torch.FloatTensor(normalized).unsqueeze(0).unsqueeze(0).to(device)


# ============================================================
# DICOM → PNG (Gemini Vision path)
# ============================================================


def dicom_to_png_bytes(raw_bytes: bytes) -> bytes:
    """Convert DICOM bytes to a normalised PNG ready for Gemini Vision.

    Applies the same MONOCHROME1 correction and percentile normalisation as
    ``dicom_to_tensor``, but outputs an 8-bit PNG (optionally downscaled to
    max 1536 px) instead of a float tensor.

    Args:
        raw_bytes: Raw DICOM file bytes.

    Returns:
        PNG image as bytes.
    """
    ds = pydicom.dcmread(io.BytesIO(raw_bytes), force=True)

    # Shared MONOCHROME1 correction + percentile normalisation
    img_8u = _preprocess_dicom_pixels(ds)

    h, w = img_8u.shape[:2]
    if max(h, w) > 1536:
        scale = 1536 / max(h, w)
        img_8u = cv2.resize(
            img_8u,
            (int(w * scale), int(h * scale)),
            interpolation=cv2.INTER_LANCZOS4,
        )

    _, png_buf = cv2.imencode(".png", img_8u)
    return png_buf.tobytes()


# ============================================================
# Inference
# ============================================================


def infer_multilabel(
    tensor: torch.Tensor,
    model: Any,
) -> dict[str, Any]:
    """Run NIH ResNet50-512-all inference on a preprocessed tensor.

    Args:
        tensor: Float tensor ``[1, 1, 512, 512]`` from ``dicom_to_tensor()``.
        model: Loaded TorchXRayVision ResNet model (from ``app.ai.model_loader``).

    Returns:
        Dict with keys:
          - ``nivel_alerta``: "NORMAL" | "BAJA" | "MEDIA" | "ALTA"
          - ``hallazgos``: list of ``{patologia, probabilidad_pct}`` for positive findings
          - ``sin_hallazgos``: bool
          - ``todas_patologias``: dict keyed by pathology name with probability/threshold info
    """
    with torch.no_grad():
        probs = model(tensor)[0].cpu().numpy()  # sigmoid + op_norm applied internally

    all_labels: dict[str, Any] = {}
    findings: list[dict[str, Any]] = []

    for index, pathology in enumerate(NIH_PATOLOGIAS):
        probability = float(probs[index])
        threshold = float(UMBRALES[pathology])
        positive = probability >= threshold

        all_labels[pathology] = {
            "probabilidad_pct": round(probability * 100, 2),
            "positivo": positive,
            "umbral_pct": round(threshold * 100, 0),
        }

        if positive:
            findings.append(
                {
                    "patologia": pathology,
                    "probabilidad_pct": round(probability * 100, 2),
                }
            )

    findings.sort(key=lambda item: item["probabilidad_pct"], reverse=True)

    if not findings:
        alert_level = "NORMAL"
    else:
        top_probability = findings[0]["probabilidad_pct"]
        alert_level = (
            "ALTA" if top_probability >= 80 else ("MEDIA" if top_probability >= 60 else "BAJA")
        )

    return {
        "nivel_alerta": alert_level,
        "hallazgos": findings,
        "sin_hallazgos": len(findings) == 0,
        "todas_patologias": all_labels,
    }


# ============================================================
# High-level orchestrator
# ============================================================


async def analyze_single_instance(
    orthanc_client: httpx.AsyncClient,
    orthanc_instance_id: str,
    model: Any,
    device: torch.device | None = None,
) -> dict[str, Any]:
    """Download, preprocess, and run AI inference on one DICOM instance.

    Args:
        orthanc_client: Shared httpx client pointing at Orthanc.
        orthanc_instance_id: Orthanc internal instance ID.
        model: Loaded NIH ResNet model (from ``app.state.model``).
        device: PyTorch device for tensor placement. Defaults to CPU.

    Returns:
        Dict with ``orthanc_instance_id``, ``dicom`` metadata, and inference
        results (``nivel_alerta``, ``hallazgos``, ``sin_hallazgos``,
        ``todas_patologias``).

    Raises:
        HTTPException(404): Instance not found in Orthanc.
        HTTPException(422): DICOM could not be processed.
    """
    dicom_bytes = await get_instance_dicom_bytes(orthanc_client, orthanc_instance_id)
    tags = await get_instance_tags(orthanc_client, orthanc_instance_id)

    loop = asyncio.get_running_loop()

    # Offload CPU-bound preprocessing (H-001)
    try:
        tensor = await loop.run_in_executor(
            None,
            lambda: dicom_to_tensor(dicom_bytes, device),
        )
    except Exception as error:
        raise HTTPException(
            status_code=422,
            detail=f"Could not process DICOM: {error}",
        ) from error

    # Offload CPU-bound inference (H-001)
    inference = await loop.run_in_executor(
        None,
        lambda: infer_multilabel(tensor, model),
    )

    return {
        "orthanc_instance_id": orthanc_instance_id,
        "dicom": {
            "StudyInstanceUID": tags.get("StudyInstanceUID"),
            "SeriesInstanceUID": tags.get("SeriesInstanceUID"),
            "SOPInstanceUID": tags.get("SOPInstanceUID"),
            "Modality": tags.get("Modality"),
            "ViewPosition": tags.get("ViewPosition"),
            "BodyPartExamined": tags.get("BodyPartExamined"),
            "StudyDate": tags.get("StudyDate"),
            "PatientID": tags.get("PatientID"),
            "PatientName": tags.get("PatientName"),
            "PatientAge": tags.get("PatientAge"),
            "PatientBirthDate": tags.get("PatientBirthDate"),
            "AccessionNumber": tags.get("AccessionNumber"),
            "InstanceNumber": tags.get("InstanceNumber"),
            "SeriesDescription": tags.get("SeriesDescription"),
            # Enriched tags for prompt orchestrator classification
            "PatientSex": tags.get("PatientSex") or "No especificado",
            "StudyDescription": sanitize_dicom_text(tags.get("StudyDescription") or ""),
            "RequestedProcedureDescription": sanitize_dicom_text(
                tags.get("RequestedProcedureDescription") or ""
            ),
        },
        **inference,
    }
