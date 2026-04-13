"""
app.ai.constants — Shared constants for the AI module.

Centralises values that are used across multiple files (router, llm_service, etc.)
to avoid duplication and keep translations in sync.
"""

from __future__ import annotations

# ── Pathology names in Spanish — NIH ChestX-ray14 labels ─────────────────────
#: Maps NIH model class names → Spanish display names.
#: Used by report generation (llm_service) and pipeline informe assembly (router).
NOMBRES_ES: dict[str, str] = {
    "Atelectasis": "Atelectasia",
    "Cardiomegaly": "Cardiomegalia",
    "Effusion": "Derrame pleural",
    "Infiltrate": "Infiltrado pulmonar",
    "Infiltration": "Infiltrado pulmonar",
    "Mass": "Masa pulmonar",
    "Nodule": "Nódulo pulmonar",
    "Pneumonia": "Neumonía",
    "Pneumothorax": "Neumotórax",
    "Consolidation": "Consolidación pulmonar",
    "Edema": "Edema pulmonar",
    "Emphysema": "Enfisema pulmonar",
    "Fibrosis": "Fibrosis pulmonar",
    "Pleural_Thickening": "Engrosamiento pleural",
    "Hernia": "Hernia diafragmática",
    "Lung Lesion": "Lesión pulmonar",
    "Fracture": "Fractura",
    "Lung Opacity": "Opacidad pulmonar",
    "Enlarged Cardiomediastinum": "Ensanchamiento mediastínico",
}
