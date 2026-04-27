"""
app.ai.medical_logic — Deterministic clinical classification engine.

Contains:
- evaluate_tonnis()       — Tönnis hip OA grading from calibrated measurements
- evaluate_lumbar()       — Lumbar spine spondylolisthesis with safety veto
- TonnisVerdict           — Structured verdict for hip grading
- LumbarVerdict           — Structured verdict for lumbar spine

Calibration reference (7-patient benchmark):
  CASO_PELVIS_A: bilateral Tönnis 1 — medial ~3-4mm, minor osteophytes, minimal sclerosis
  CASO_MSK_A: right Tönnis 2 (medial ~2.0mm, sclerosis, geodes), left Tönnis 1
  The weighted scoring correctly assigns Grade 2 to CASO_MSK_A D while keeping
  CASO_PELVIS_A D at Grade 1 (medial ~1.8mm + minor osteophytes → score ~2.5).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger("imagiax.medical_logic")


@dataclass(frozen=True, slots=True)
class TonnisVerdict:
    """Deterministic Tönnis grading result for one hip side."""
    grade: int                    # 0-3
    grade_label: str              # "Normal", "Leve", "Moderada", "Severa"
    reasoning: str                # Human-readable explanation of why this grade
    medial_gap_mm: float          # Calibrated medial gap in mm
    superolateral_gap_mm: float   # Calibrated superolateral gap in mm
    calibration_source: str       # "calibrated" or "uncalibrated_fallback"


@dataclass(frozen=True, slots=True)
class LumbarVerdict:
    """Deterministic lumbar spine verdict."""
    spondylolisthesis_present: bool
    grade: str                    # "none", "grade_I", "grade_II", etc.
    displacement_mm: float
    override_applied: bool        # True if safety veto was triggered
    override_reason: str          # Empty if no override
    original_llm_finding: str     # What the LLM originally reported


# ── Tönnis Grade Labels ──────────────────────────────────────────────────────
_TONNIS_LABELS = {
    0: "Normal",
    1: "Leve",
    2: "Moderada",
    3: "Severa",
}


def _tonnis_weighted_score(
    cal_medial: float,
    cal_supero_lat: float,
    osteophyte_max_mm: float,
    subchondral_sclerosis: bool,
    geodes_present: bool,
    femoral_head_deformity: bool,
    joint_space_obliterated: bool,
) -> tuple[float, list[str]]:
    """Weighted point scoring for Tönnis grading.

    Continuous scoring system replacing binary medial veto. Correctly separates
    CASO_MSK_A D (medial ~2.0mm + osteophytes + geodes → Grade 2) from CASO_PELVIS_A D
    (medial ~3-4mm + minor osteophytes → Grade 1).

    COR (Cortical Opacity Ratio): Inspector reports subchondral_sclerosis=true
    when acetabular/femoral cortical density exceeds 1.2× normal cortical baseline.
    Any COR ≥ 1.2 → sclerosis boolean=true → +1.0 pts here.

    Point contributions (max ~12 points):
      medial_joint_space  : 0-4 pts (continuous — lower space = more points)
      superolateral_space : 0-2 pts (continuous)
      osteophyte_size     : 0-1.5 pts (tiered by mm)
      sclerosis (COR≥1.2) : 1 pt (boolean)
      geodes              : 1.5 pts (boolean)
      femoral_deformity   : 1.5 pts (boolean)
      obliteration        : 2 pts (boolean — reserved for Grade 3 Dual-Lock gate)

    Grade thresholds:
      [0.0, 1.5) → Grade 0 (normal)
      [1.5, 4.0) → Grade 1 (leve)
      [4.0, 8.0) → Grade 2 (moderada)
      [8.0, ∞)   → Grade 3 (severa — subject to Dual-Lock: score≥8.0 AND cal_medial≤1.0mm)

    Returns (score, contributors) for transparent reasoning.
    """
    score = 0.0
    contributors: list[str] = []

    # ── Medial joint space: 0-4 pts ──────────────────────────────────────────
    if cal_medial < 1.0:
        score += 4.0
        contributors.append(f"espacio medial {cal_medial:.1f}mm (<1.0mm): +4.0 pts")
    elif cal_medial < 1.5:
        score += 3.0
        contributors.append(f"espacio medial {cal_medial:.1f}mm (<1.5mm): +3.0 pts")
    elif cal_medial <= 2.0:
        score += 2.0
        contributors.append(f"espacio medial {cal_medial:.1f}mm (<=2.0mm): +2.0 pts")
    elif cal_medial < 2.5:
        score += 1.0
        contributors.append(f"espacio medial {cal_medial:.1f}mm (<2.5mm): +1.0 pts")
    # >= 2.5mm: 0 pts (bien conservado)

    # ── Supero-lateral joint space: 0-2 pts ──────────────────────────────────
    if cal_supero_lat < 1.5:
        score += 2.0
        contributors.append(f"espacio supero-lateral {cal_supero_lat:.1f}mm (<1.5mm): +2.0 pts")
    elif cal_supero_lat < 2.5:
        score += 1.0
        contributors.append(f"espacio supero-lateral {cal_supero_lat:.1f}mm (<2.5mm): +1.0 pts")

    # ── Osteophytes: 0-1.5 pts ───────────────────────────────────────────────
    if osteophyte_max_mm > 2.0:
        score += 1.5
        contributors.append(f"osteofito {osteophyte_max_mm:.1f}mm (>2.0mm): +1.5 pts")
    elif osteophyte_max_mm > 0.0:
        score += 0.5
        contributors.append(f"osteofito {osteophyte_max_mm:.1f}mm (presente): +0.5 pts")

    # ── Sclerosis: 1 pt ──────────────────────────────────────────────────────
    if subchondral_sclerosis:
        score += 1.0
        contributors.append("esclerosis subcondral: +1.0 pts")

    # ── Geodes: 1.5 pts ──────────────────────────────────────────────────────
    if geodes_present:
        score += 1.5
        contributors.append("geodas subcondrales: +1.5 pts")

    # ── Femoral head deformity: 1.5 pts ──────────────────────────────────────
    if femoral_head_deformity:
        score += 1.5
        contributors.append("deformidad cabeza femoral: +1.5 pts")

    # ── Obliteration: 2 pts (triggers Grade 3 gate when combined with AP) ────
    if joint_space_obliterated:
        score += 2.0
        contributors.append("obliteración articular: +2.0 pts")

    return score, contributors


def evaluate_tonnis(
    *,
    gap_medial_mm: float,
    gap_supero_lateral_mm: float,
    subchondral_sclerosis: bool,
    osteophytes_present: bool,
    osteophyte_max_mm: float,
    geodes_present: bool,
    femoral_head_deformity: bool,
    joint_space_obliterated: bool,
    view_type: str = "AP",
    mm_per_pixel: float = 0.0,
    gap_medial_px: float = 0.0,
    gap_supero_lateral_px: float = 0.0,
) -> TonnisVerdict:
    """Deterministic Tönnis grading from calibrated measurements.

    Uses pixel measurements + mm_per_pixel when available; falls back to direct mm values
    when the Inspector does not provide calibrated pixel data.

    Scoring ratios and thresholds:
      COR (Cortical Opacity Ratio): 1.2× normal density → sclerosis=true → +1.0 pts
      DHR (Disc Height Ratio): not applicable to pelvis (see evaluate_lumbar)
      Dual-Lock (Grade 3): score ≥ 8.0 AND cal_medial ≤ 1.0mm must both be satisfied.
        — The 8.0 threshold forces near-complete joint destruction evidence.
        — The 1.0mm gate prevents narrative-driven Grade 3 when measurements
          report residual medial space despite obliteration flag.

    Score thresholds:
      [0.0, 1.5) → Grade 0: no degenerative signs
      [1.5, 4.0) → Grade 1: mild changes, preserved space
      [4.0, 8.0) → Grade 2: moderate narrowing + multiple findings
      [8.0, ∞)   → Grade 3: severe — Dual-Lock required (AP + score≥8.0 + cal_medial≤1.0mm)

    View-Type Gate: Grade 3 requires AP view. Frog-leg/unknown caps at Grade 2.

    Benchmark reference:
      CASO_PELVIS_A D: medial ~1.8mm → 2.0 + 0.5 osteo = 2.5 → Grade 1
      CASO_MSK_A D: medial ~2.0mm → 2.0 + sclerosis 1.0 + geodes 1.5 = 4.5 → Grade 2
    """
    is_ap = view_type.lower() == "ap"

    # ── Resolve calibrated measurements ──────────────────────────────────────
    if mm_per_pixel > 0 and (gap_medial_px > 0 or gap_supero_lateral_px > 0):
        cal_medial = gap_medial_px * mm_per_pixel
        cal_supero_lat = gap_supero_lateral_px * mm_per_pixel
        calibration = "calibrated"
        logger.info(
            "[medical_logic] Calibrated: medial=%.1fpx*%.4fmm=%.2fmm, "
            "supero_lat=%.1fpx*%.4fmm=%.2fmm",
            gap_medial_px, mm_per_pixel, cal_medial,
            gap_supero_lateral_px, mm_per_pixel, cal_supero_lat,
        )
    else:
        cal_medial = gap_medial_mm
        cal_supero_lat = gap_supero_lateral_mm
        calibration = "uncalibrated_fallback"
        logger.info(
            "[medical_logic] Uncalibrated fallback: medial=%.2fmm, supero_lat=%.2fmm",
            cal_medial, cal_supero_lat,
        )

    # ── Grade 0 short-circuit: no findings at all ────────────────────────────
    has_any_finding = (
        subchondral_sclerosis
        or osteophytes_present
        or geodes_present
        or femoral_head_deformity
        or joint_space_obliterated
    )
    if not has_any_finding and cal_medial >= 2.5 and cal_supero_lat >= 2.5:
        return TonnisVerdict(
            grade=0,
            grade_label=_TONNIS_LABELS[0],
            reasoning=(
                "Sin signos degenerativos. Espacios articulares conservados "
                f"(medial {cal_medial:.1f}mm, supero-lateral {cal_supero_lat:.1f}mm)."
            ),
            medial_gap_mm=cal_medial,
            superolateral_gap_mm=cal_supero_lat,
            calibration_source=calibration,
        )

    # ── Compute weighted score ────────────────────────────────────────────────
    score, contributors = _tonnis_weighted_score(
        cal_medial=cal_medial,
        cal_supero_lat=cal_supero_lat,
        osteophyte_max_mm=osteophyte_max_mm,
        subchondral_sclerosis=subchondral_sclerosis,
        geodes_present=geodes_present,
        femoral_head_deformity=femoral_head_deformity,
        joint_space_obliterated=joint_space_obliterated,
    )

    # ── Sclerosis minimum grade enforcer ─────────────────────────────────────
    # Clinical consensus: any visible subchondral sclerosis (COR ≥ 1.2) means
    # arthrosis is present → minimum Tönnis Grade 1, regardless of joint space width.
    # Sclerosis is the most sensitive early sign of arthrosis — its presence alone
    # precludes Grade 0 ("no degenerative signs") by definition.
    if subchondral_sclerosis and score < 1.5:
        score = 1.5
        contributors.append("esclerosis subcondral (COR≥1.2) → mínimo Grado 1: score=1.5")
        logger.info("[medical_logic] Sclerosis floor applied: score raised to 1.5 (Grade 1 minimum)")

    contributors_str = "; ".join(contributors) if contributors else "sin hallazgos"
    logger.info(
        "[medical_logic] Tönnis weighted score=%.1f | %s",
        score, contributors_str,
    )

    # ── Gap Ceiling Constraint: preserved space caps grade at ≤1 ────────────
    # If medial joint space > 2.5mm, the articular space is substantially
    # preserved. Grade 2+ requires meaningful narrowing — this constraint
    # prevents LLM-driven over-diagnosis where secondary signs (osteophytes,
    # sclerosis) accumulate score but the medial gap remains normal.
    gap_ceiling_active = cal_medial > 2.5
    if gap_ceiling_active:
        logger.info(
            "[medical_logic] Gap ceiling active: medial=%.2fmm > 2.5mm — grade capped to ≤1",
            cal_medial,
        )

    # ── Grade 3: Triple-Lock — Dual-Lock + structural evidence gate ──────────
    # Three independent gates must pass simultaneously:
    #   (A) score >= 8.0 — forces extreme evidence for true obliteration.
    #   (B) cal_medial <= 1.0mm — measurement confirms medial collapse.
    #   (C) geodes_present OR femoral_head_deformity — structural radiological
    #       proof of advanced articular destruction. Pinzamiento + sclerosis alone
    #       cannot support Grade 3; bone deformation or cyst formation is required.
    # If cal_medial > 1.0mm or no structural evidence → falls through to Grade 2.
    grade3_medial_collapsed = cal_medial <= 1.0
    grade3_structural = geodes_present or femoral_head_deformity
    if score >= 8.0 and joint_space_obliterated and is_ap and not gap_ceiling_active and grade3_medial_collapsed:
        if not grade3_structural:
            logger.warning(
                "[SAFETY-VETO][TONNIS-GRADE3] Score=%.1f, obliteration=True, medial=%.1fmm — "
                "Grade 3 BLOCKED: no structural evidence (geodes=%s, femoral_deformity=%s) — "
                "falling to Grade 2",
                score, cal_medial, geodes_present, femoral_head_deformity,
            )
        else:
            return TonnisVerdict(
                grade=3,
                grade_label=_TONNIS_LABELS[3],
                reasoning=(
                    f"Coxartrosis severa (score={score:.1f}). "
                    f"Obliteración articular confirmada en AP. "
                    f"Colapso medial: {cal_medial:.1f}mm ≤ 1.0mm [Triple-Lock]. "
                    f"Evidencia estructural: geodes={geodes_present}, deformidad femoral={femoral_head_deformity}. "
                    f"Supero-lateral {cal_supero_lat:.1f}mm. "
                    f"Contribuciones: {contributors_str}."
                ),
                medial_gap_mm=cal_medial,
                superolateral_gap_mm=cal_supero_lat,
                calibration_source=calibration,
            )

    # ── Grade 2: score 4.0–7.9 ───────────────────────────────────────────────
    # Requires AP view for obliteration confirmation.
    # Gap Ceiling: skip Grade 2 if medial > 2.5mm — space substantially preserved.
    if score >= 4.0 and not gap_ceiling_active:
        return TonnisVerdict(
            grade=2,
            grade_label=_TONNIS_LABELS[2],
            reasoning=(
                f"Coxartrosis moderada (score={score:.1f}). "
                f"Medial {cal_medial:.1f}mm, supero-lateral {cal_supero_lat:.1f}mm. "
                f"Contribuciones: {contributors_str}."
            ),
            medial_gap_mm=cal_medial,
            superolateral_gap_mm=cal_supero_lat,
            calibration_source=calibration,
        )

    # ── Grade 1: score 1.5–3.9 (or gap ceiling applied from higher score) ────
    if score >= 1.5:
        ceiling_note = (
            f" [Gap ceiling: medial {cal_medial:.1f}mm > 2.5mm — capped from score {score:.1f}]"
            if gap_ceiling_active and score >= 4.0 else ""
        )
        return TonnisVerdict(
            grade=1,
            grade_label=_TONNIS_LABELS[1],
            reasoning=(
                f"Coxartrosis leve (score={score:.1f}).{ceiling_note} "
                f"Medial {cal_medial:.1f}mm, supero-lateral {cal_supero_lat:.1f}mm. "
                f"Contribuciones: {contributors_str}."
            ),
            medial_gap_mm=cal_medial,
            superolateral_gap_mm=cal_supero_lat,
            calibration_source=calibration,
        )

    # ── Grade 0: score < 1.5 ─────────────────────────────────────────────────
    return TonnisVerdict(
        grade=0,
        grade_label=_TONNIS_LABELS[0],
        reasoning=(
            f"Sin criterios significativos para coxartrosis (score={score:.1f}). "
            f"Medial {cal_medial:.1f}mm, supero-lateral {cal_supero_lat:.1f}mm."
            + (f" Contribuciones menores: {contributors_str}." if contributors else "")
        ),
        medial_gap_mm=cal_medial,
        superolateral_gap_mm=cal_supero_lat,
        calibration_source=calibration,
    )


# ── Lumbar Spine — Spondylolisthesis Safety Veto ────────────────────────────

_ROUTINE_PATTERNS = re.compile(
    r"(rutina|control|screening|chequeo|preventiv|general|anual)",
    re.IGNORECASE,
)

# Text-based veto: catches hallucinated high-grade spondylolisthesis regardless
# of clinical_indication wording (e.g. CASO_LUMBAR_A: "RADIOGRAFIA COLUMNA LUMBAR AP-L-5° ESP")
_SPONDY_HIGH_GRADE = re.compile(
    r"espondilolistesis\b.{0,80}(?:grado\s+)?(?:III|IV|V|3|4|5)\b",
    re.IGNORECASE | re.DOTALL,
)


def evaluate_lumbar(
    *,
    spondylolisthesis_reported: bool,
    displacement_mm: float,
    clinical_indication: str | None = None,
    llm_original_text: str = "",
) -> LumbarVerdict:
    """Deterministic lumbar spondylolisthesis evaluation with safety veto.

    Python classifies; the LLM only observes. Rules unknown to the LLM cannot
    be gamed. For disc height: DHR (Disc Height Ratio) < 0.32 (disc height /
    vertebral body height) indicates frank disc narrowing. The two-pass pipeline
    uses text-pattern matching instead of DHR when pixel calibration is unavailable.

    Safety veto rules:
      1. Routine indication + displacement < 8mm → force conservative (no listhesis)
      2. Report text claims Grade ≥III but displacement < 16mm → veto unconditionally

    Args:
        spondylolisthesis_reported: Whether the LLM detected spondylolisthesis
        displacement_mm: LLM-measured anterior displacement in mm
        clinical_indication: Free text clinical context (may be None)
        llm_original_text: Original LLM finding text for audit trail
    """
    is_routine = bool(
        clinical_indication and _ROUTINE_PATTERNS.search(clinical_indication)
    )

    # Text-based veto (AD-5): catches hallucinated high-grade findings even when
    # clinical_indication doesn't match routine patterns (e.g. CASO_LUMBAR_A case).
    # If the report text claims Grade ≥ III spondylolisthesis but there is no
    # structured displacement measurement supporting it (< 16 mm threshold for
    # Grade III), veto unconditionally.
    text_has_high_grade = bool(_SPONDY_HIGH_GRADE.search(llm_original_text)) if llm_original_text else False
    if text_has_high_grade and displacement_mm < 16.0:
        logger.warning(
            "[SAFETY-VETO][LUMBAR-SPONDYLOLISTHESIS] High-grade claim (≥III) without structural "
            "support: displacement=%.1fmm < 16mm threshold — vetoed. Indication: '%s'",
            displacement_mm,
            clinical_indication or "N/A",
        )
        return LumbarVerdict(
            spondylolisthesis_present=False,
            grade="none",
            displacement_mm=displacement_mm,
            override_applied=True,
            override_reason=(
                f"Texto del informe menciona espondilolistesis grado ≥III pero "
                f"desplazamiento {displacement_mm:.1f}mm < 16mm (sin soporte estructural). "
                f"Hallazgo de alto grado vetado por lógica de seguridad."
            ),
            original_llm_finding=llm_original_text,
        )

    # Safety veto: routine exam + small displacement → force conservative
    if spondylolisthesis_reported and is_routine and displacement_mm < 8.0:
        logger.warning(
            "[SAFETY-VETO][LUMBAR-SPONDYLOLISTHESIS] Routine indication + displacement=%.1fmm < 8mm: "
            "'%s' — forcing conservative",
            displacement_mm,
            clinical_indication,
        )
        return LumbarVerdict(
            spondylolisthesis_present=False,
            grade="none",
            displacement_mm=displacement_mm,
            override_applied=True,
            override_reason=(
                f"Indicación clínica rutinaria + desplazamiento {displacement_mm:.1f}mm < 8mm. "
                f"Hallazgo del LLM vetado por lógica de seguridad."
            ),
            original_llm_finding=llm_original_text,
        )

    # No veto — pass through LLM finding
    if not spondylolisthesis_reported:
        grade = "none"
    elif displacement_mm < 8.0:
        grade = "grade_I"
    elif displacement_mm < 16.0:
        grade = "grade_II"
    elif displacement_mm < 24.0:
        grade = "grade_III"
    else:
        grade = "grade_IV"

    return LumbarVerdict(
        spondylolisthesis_present=spondylolisthesis_reported,
        grade=grade,
        displacement_mm=displacement_mm,
        override_applied=False,
        override_reason="",
        original_llm_finding=llm_original_text,
    )


# ── v36: Severity Short-Circuit ─────────────────────────────────────────────

def severity_short_circuit(verdict: TonnisVerdict, measurements: dict) -> TonnisVerdict:
    """Cap unsupported Grade ≥3 verdicts to Grade 1 (v36 safety net).

    If the Tönnis grade is ≥3 but there is no radiological evidence of
    advanced joint destruction (joint obliteration in AP view OR geodes),
    the grade is capped to 1 to prevent over-diagnosis.

    This runs AFTER apply_contralateral_consistency() so CCC corrections are
    respected before the short-circuit check (AD-2).

    Args:
        verdict: TonnisVerdict from evaluate_tonnis() or post-CCC
        measurements: Measurement dict with keys ``obliteration_ap`` and
            ``geodes_present`` (as returned by _sanity_check_measurements).

    Returns:
        Original verdict if grade < 3 or evidence supports severity.
        New Grade 1 verdict if grade ≥3 without supporting evidence.
    """
    if verdict.grade < 3:
        return verdict

    has_severe_evidence = (
        measurements.get("obliteration_ap", False)
        or measurements.get("geodes_present", False)
    )

    if has_severe_evidence:
        return verdict

    # Cap: high score but no radiological proof of severe disease
    capped_grade = 1
    logger.warning(
        "[SAFETY-VETO][TONNIS-SEVERITY] Grade %d unsupported by evidence — capped to %d: "
        "obliteration_ap=%s, geodes=%s, medial=%.1fmm",
        verdict.grade,
        capped_grade,
        measurements.get("obliteration_ap", False),
        measurements.get("geodes_present", False),
        verdict.medial_gap_mm,
    )
    return TonnisVerdict(
        grade=capped_grade,
        grade_label=_TONNIS_LABELS[capped_grade],
        reasoning=(
            f"SHORT-CIRCUIT: grado {verdict.grade} reducido a {capped_grade} — "
            f"sin obliteración articular AP ni geodas que soporten severidad. "
            f"Medial {verdict.medial_gap_mm:.1f}mm, supero-lateral "
            f"{verdict.superolateral_gap_mm:.1f}mm. "
            f"(Razonamiento original: {verdict.reasoning})"
        ),
        medial_gap_mm=verdict.medial_gap_mm,
        superolateral_gap_mm=verdict.superolateral_gap_mm,
        calibration_source=verdict.calibration_source,
    )


# ── v35: Measurement Sanity Checks ──────────────────────────────────────────

def _sanity_check_measurements(m: dict) -> dict:
    """Correct physically impossible measurement combinations."""
    m = dict(m)  # shallow copy
    medial = m.get("cal_medial_joint_space_mm", 4.0)
    osteo = m.get("osteophyte_max_mm", 0.0)

    # Contradiction: obliteration claimed but medial space > 1.5mm.
    # True obliteration requires near-zero joint space — any gap above 1.5mm
    # is inconsistent with complete obliteration.
    if m.get("obliteration_ap", False) and medial > 1.5:
        m["obliteration_ap"] = False

    # Contradiction: geodes claimed but joint still substantially preserved
    # Tightened threshold: geodes require significant narrowing (medial ≤ 2.0mm)
    # AND relevant osteophytes (≥ 2.0mm). Avoids hallucinated cysts in mild disease.
    if m.get("geodes_present", False) and (medial > 2.0 or osteo < 2.0):
        m["geodes_present"] = False

    # Contradiction: femoral head deformity claimed but joint space too preserved
    # True femoral deformity in OA implies significant narrowing (medial ≤ 2.5mm)
    if m.get("femoral_head_deformity", False) and medial > 2.5 and not m.get("geodes_present", False):
        m["femoral_head_deformity"] = False

    # Negative values are impossible
    for k in ("cal_medial_joint_space_mm", "cal_superolateral_joint_space_mm", "osteophyte_max_mm"):
        if k in m and m[k] < 0:
            m[k] = 0.0

    return m


# ── v35: Contralateral Consistency Check ────────────────────────────────────

def apply_contralateral_consistency(
    left: TonnisVerdict, right: TonnisVerdict,
    left_m: dict, right_m: dict
) -> tuple[TonnisVerdict, TonnisVerdict]:
    """Cap asymmetric grades when measurements are contradictory."""
    diff = abs(left.grade - right.grade)
    if diff <= 1:
        return left, right

    # Identify severe and mild sides
    if left.grade > right.grade:
        severe, mild, severe_m = left, right, left_m
    else:
        severe, mild, severe_m = right, left, right_m

    # Check if severe side has contradictory evidence
    medial = severe_m.get("cal_medial_joint_space_mm", 4.0)
    osteo = severe_m.get("osteophyte_max_mm", 0.0)
    geodes = severe_m.get("geodes_present", False)
    obliteration = severe_m.get("obliteration_ap", False)

    contradictory = (
        (medial > 2.0 and obliteration) or
        (geodes and medial > 3.0 and osteo < 1.0) or
        (medial > 2.5 and severe.grade >= 3)  # Grade 3 needs near-obliteration
    )

    if contradictory:
        capped_grade = mild.grade + 1
        capped = TonnisVerdict(
            grade=capped_grade,
            grade_label=_TONNIS_LABELS.get(capped_grade, str(capped_grade)),
            reasoning=f"CCC: capped from {severe.grade} to {capped_grade} (contralateral={mild.grade}, contradictory measurements: medial={medial:.1f}mm)",
            medial_gap_mm=severe.medial_gap_mm,
            superolateral_gap_mm=severe.superolateral_gap_mm,
            calibration_source=severe.calibration_source,
        )
        if left.grade > right.grade:
            return capped, right
        else:
            return left, capped

    return left, right
