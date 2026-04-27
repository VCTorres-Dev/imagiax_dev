"""
saccade_service.py — Anatomy-agnostic high-resolution ROI crop extraction.

Extracts patches from the native DICOM pixel array (before any downscale) and
returns them as PNG bytes for injection into Gemini's multimodal prompt.

The intelligence of WHERE to crop lives exclusively in the YAML templates
(saccade_rois key). This module only knows HOW to crop.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import cv2
import numpy as np

from infrastructure.dicom_service import get_preprocessed_pixels

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SaccadeROI:
    """ROI definition loaded from YAML template saccade_rois entry."""
    center_x: float
    center_y: float
    window_size_rel: float
    label: str
    output_px: int = 384
    min_window_px: int = 200


@dataclass(frozen=True, slots=True)
class SaccadePatch:
    """High-resolution PNG crop from the native DICOM pixel array."""
    label: str
    png_bytes: bytes
    effective_mm_per_pixel: float = 0.0


def extract_roi_patches(
    raw_bytes: bytes,
    rois: list,
    pixel_spacing_mm: tuple[float, float] | None = None,
) -> list:
    """Extract high-resolution ROI patches from native DICOM pixel array.

    Callers MUST wrap this in try/except (the router owns the fallback contract).
    Memory: full-res pixel array is explicitly deleted after all crops are extracted.

    Args:
        raw_bytes: Raw DICOM file bytes (NOT PNG -- native DICOM).
        rois: List of SaccadeROI definitions from YAML template.
        pixel_spacing_mm: Optional (row_spacing_mm, col_spacing_mm) from DICOM metadata.
            When provided, each SaccadePatch will carry the effective mm/pixel scale
            after the resize. Defaults to None (backward-compatible: eff=0.0).

    Returns:
        List of SaccadePatch with PNG bytes. May be shorter than rois if
        some ROIs were skipped (min_window_px guard or encode failure).
    """
    pixel_array = get_preprocessed_pixels(raw_bytes)
    h, w = pixel_array.shape[:2]
    logger.info("[saccade] native array shape: %dx%d, extracting %d ROIs", h, w, len(rois))

    avg_spacing = (pixel_spacing_mm[0] + pixel_spacing_mm[1]) / 2.0 if pixel_spacing_mm else 0.0

    patches = []
    for roi in rois:
        result = _crop_and_encode(pixel_array, roi, h, w, avg_spacing)
        if result is not None:
            patch_bytes, eff_mm = result
            logger.info(
                "[saccade] patch '%s': effective_mm_per_pixel=%.4f",
                roi.label, eff_mm,
            )
            patches.append(SaccadePatch(label=roi.label, png_bytes=patch_bytes, effective_mm_per_pixel=eff_mm))

    # Explicit memory release -- full-res array no longer needed
    del pixel_array

    logger.info("[saccade] extracted %d/%d patches", len(patches), len(rois))
    return patches


def _crop_and_encode(
    pixel_array: np.ndarray,
    roi,
    h: int,
    w: int,
    avg_spacing_mm: float = 0.0,
) -> tuple[bytes, float] | None:
    """Crop a single ROI from the pixel array and encode as PNG.

    Coordinate system: normalized [0.0, 1.0] -> pixel coords clamped to image bounds.
    If the native window is smaller than min_window_px, the ROI is skipped.

    Args:
        pixel_array: Native DICOM pixel array.
        roi: SaccadeROI definition.
        h: Image height in pixels.
        w: Image width in pixels.
        avg_spacing_mm: Average pixel spacing in mm (row+col / 2). 0.0 means unknown.

    Returns:
        Tuple of (png_bytes, effective_mm_per_pixel), or None if the ROI was skipped.
    """
    min_dim = min(h, w)
    window_px = int(min_dim * roi.window_size_rel)

    if window_px < roi.min_window_px:
        logger.info(
            "[saccade] skipping '%s': native window %dpx < min_window_px %dpx",
            roi.label, window_px, roi.min_window_px,
        )
        return None

    cx = int(roi.center_x * w)
    cy = int(roi.center_y * h)
    half = window_px // 2

    x1 = max(cx - half, 0)
    y1 = max(cy - half, 0)
    x2 = min(x1 + window_px, w)
    y2 = min(y1 + window_px, h)

    # Re-anchor if clamping shrunk the window
    x1 = max(x2 - window_px, 0)
    y1 = max(y2 - window_px, 0)

    crop = pixel_array[y1:y2, x1:x2]

    if crop.size == 0:
        logger.warning("[saccade] empty crop for '%s' -- skipping", roi.label)
        return None

    resized = cv2.resize(
        crop,
        (roi.output_px, roi.output_px),
        interpolation=cv2.INTER_LANCZOS4,
    )

    success, buf = cv2.imencode(".png", resized)
    if not success:
        logger.warning("[saccade] PNG encode failed for '%s' -- skipping", roi.label)
        return None

    eff_mm_per_pixel = avg_spacing_mm * (window_px / roi.output_px) if avg_spacing_mm > 0 else 0.0

    return buf.tobytes(), eff_mm_per_pixel
