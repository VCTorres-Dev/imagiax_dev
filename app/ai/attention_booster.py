"""
app.ai.attention_booster — AttentionBooster v15 (Pipeline PCCR)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PIPELINE PCCR: Producción → Cognición → Confirmación → Reporte
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Motivación clínica:
  En estudios MSK/Pelvis, los MLLMs tienden hacia el sesgo inverso al tórax:
  sub-reportan hallazgos degenerativos sutiles (osteofitos incipientes, esclerosis
  subcondral, irregularidades corticales) porque están entrenados para evitar
  alucinaciones, y los umbrales de certeza llevan al modelo a ignorar señales
  débiles pero clínicamente relevantes.

  El Caso CARRASCO (artrosis lumbar incipiente) es el ejemplo paradigmático:
  la columna lumbar mostraba esclerosis subcondral L4-L5 y osteofitos menores
  que el modelo v14 suprimió por incertidumbre, resultando en un informe
  que certificaba normalidad absoluta en una paciente con dolor crónico real.

Pipeline:
  Fase 1 (Borrador)     — Ejecutada en llm_service.py antes de llamar al Booster.
  Fase 2 (Boost)        — Re-consulta a Gemini con instrucción de "zoom cognitivo"
                          focalizado en interfases articulares y entesis.
  Fase 3 (Persistencia) — Validación cruzada: ¿el hallazgo persiste bajo
                          reformulación del contexto? Solo pasa hallazgos robustos.

Restricción de activación:
  - SOLO activo para BodyPart.SKELETAL (MSK, columna, extremidades).
    BodyPart.PELVIS fue removido en v17 tras confirmar regresión en benchmark CARRASCO (v6→v7: 2→1).
  - BodyPart.CHEST: INACTIVO. El riesgo de regresión hacia falsos positivos de
    neumonía supera el beneficio. El ClinicalAuditEngine ya maneja tórax.
  - Todos los demás body parts: INACTIVO (sin evidencia suficiente para activar).

Taxonomía de Hedging Preventivo (hallazgos Booster):
  Cuando el Booster detecta un hallazgo nuevo, usa lenguaje de sospecha
  calibrada — nunca diagnóstico definitivo:
  - "Hallazgos sutiles sugestivos de [X] incipiente"
  - "Irregularidades corticales focales de aspecto inespecífico"
  - "Cambios degenerativos incipientes a correlacionar con clínica"
  - Siempre concluye con: "Correlacionar con examen físico y proyecciones adicionales"

Uso:
    from app.ai.attention_booster import AttentionBooster, BoostResult

    booster = AttentionBooster()
    if booster.should_activate(body_part):
        boosted_report, was_boosted = await booster.run(
            draft_report=report_text,
            dicom_images=dicom_images,
            gemini_client=client,
            body_part=body_part,
            proyecciones_txt=proyecciones_txt,
        )
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from google import genai

from app.ai.clinical_audit_engine import BodyPart

logger = logging.getLogger("imagiax.attention_booster")

# ── Regions where Booster is active ───────────────────────────────────────────
_BOOSTER_ELIGIBLE_REGIONS: frozenset[BodyPart] = frozenset({
    BodyPart.SKELETAL,
    # NOTE: BodyPart.PELVIS was here until v17.
    # Removed after CARRASCO benchmark regression (v6→v7: score 2→1).
    # Root cause: Booster Phase 2 adds findings that diverge from radiologist's
    # report format, hurting concordance. The two-pass pipeline + REGLA DE
    # PRESUNCIÓN DE GRADO BAJO in pelvis_hip_xray.yaml now handle pelvis adequately.
})

# ── Hard blacklist — Booster NEVER activates for these regions ─────────────────
# Reason: tórax/chest carries high risk of false positives (pneumonia, nodules).
# The ClinicalAuditEngine already handles chest findings. The Booster adds no
# value here and its activation was confirmed empirically as a safety regression
# (AGUILERA benchmark, 2026-04-16).
# Pelvis removed from eligible (v17): CARRASCO benchmark regression confirmed.
_BOOSTER_BLACKLIST_REGIONS: frozenset[BodyPart] = frozenset({
    BodyPart.CHEST,
    BodyPart.PELVIS,  # v17: regression confirmed, moved to blacklist
})

# Template IDs that should also hard-block the Booster regardless of BodyPart.
# Add more prefixes here as new templates are introduced.
_BOOSTER_BLACKLIST_TEMPLATE_PREFIXES: tuple[str, ...] = (
    "chest_xray",
    "thorax",
    "torax",
    "chest",
    "pelvis_hip_xray",  # v17: Booster regression confirmed in CARRASCO benchmark
    "pelvis",           # v17: catch any future pelvis variants
)

# ── Gemini model for Booster phases (Flash — fast, cheap, sufficient) ─────────
_BOOSTER_MODEL = "gemini-2.5-flash"

# ── Timeouts ──────────────────────────────────────────────────────────────────
_PHASE2_TIMEOUT = 45.0  # seconds
_PHASE3_TIMEOUT = 30.0  # seconds


# ============================================================
# Result dataclasses
# ============================================================


@dataclass
class BoostFinding:
    """A single finding discovered by the Attention Booster (Phase 2)."""

    description: str
    location: str        # spatial coordinates (e.g. "interfaz articular L4-L5")
    confidence: str      # "alta" | "media" | "baja"
    raw_text: str = ""   # original wording from Gemini Phase 2


@dataclass
class BoostResult:
    """Output of Phase 2 — Attention Boost."""

    findings: list[BoostFinding] = field(default_factory=list)
    raw_response: str = ""
    error: str = ""

    @property
    def has_new_findings(self) -> bool:
        return len(self.findings) > 0


@dataclass
class FilteredFindings:
    """Output of Phase 3 — Persistence Filter."""

    confirmed_findings: list[BoostFinding] = field(default_factory=list)
    raw_response: str = ""
    error: str = ""

    @property
    def has_confirmed(self) -> bool:
        return len(self.confirmed_findings) > 0


# ============================================================
# Prompt templates
# ============================================================

_PHASE2_SYSTEM = (
    "Sos un médico radiólogo subespecialista en músculo-esquelético con 20 años de experiencia. "
    "Tu especialidad es detectar cambios degenerativos incipientes y hallazgos sutiles "
    "en interfases articulares, entesis, y superficies corticales. "
    "Sos meticuloso, detallista, y no suprimís señales débiles cuando son clínicamente relevantes. "
    "Respondés ÚNICAMENTE con hallazgos nuevos no mencionados en el borrador — sin repetir "
    "lo que ya está reportado. Si no encontrás nada nuevo, respondés exactamente: SIN_HALLAZGOS_NUEVOS."
)

_PHASE2_PROMPT_TEMPLATE = """\
Se te proporciona un borrador de informe radiológico MSK/Pelvis y las imágenes del estudio.

BORRADOR ACTUAL:
{draft_report}

══════════════════════════════════════════════════════════════
INSTRUCCIÓN DE ZOOM COGNITIVO — FASE 2 ATTENTION BOOSTER
══════════════════════════════════════════════════════════════

Realizá un zoom cognitivo focalizado en las interfases articulares y entesis.
Buscá evidencia de cambios óseos: esclerosis subcondral, osteofitos incipientes,
irregularidades corticales sutiles, o pinzamiento articular que el borrador NO reportó.

RESTRICCIÓN CRÍTICA DE FOCO (obligatoria — no negociable):
Si el área evaluada es parénquima pulmonar o silueta cardíaca, NO emitas hallazgos
adicionales sobre esas estructuras. Limitá tu atención exclusivamente a estructuras
esqueléticas y tejidos blandos periféricos.

ANCLA DE NEGACIÓN (obligatoria):
Si la cortical ósea es lisa y continua, y el espacio articular es simétrico y conservado,
ratificá la normalidad del informe previo respondiendo exactamente: SIN_HALLAZGOS_NUEVOS
No inventés patología. La ausencia de hallazgos es un resultado clínicamente válido.

REGLAS ESTRICTAS:
1. Solo reportá hallazgos NUEVOS en estructuras óseas o tejidos blandos periféricos.
2. NO reportés hallazgos sobre pulmones, pleura, corazón ni mediastino.
3. Si no hay nada nuevo en estructuras óseas, respondé exactamente: SIN_HALLAZGOS_NUEVOS
4. Para cada hallazgo nuevo, indicá:
   - LOCALIZACIÓN: (ej. "interfaz articular L4-L5", "polo superior acetabular derecho")
   - DESCRIPCIÓN: (qué ves morfológicamente)
   - CONFIANZA: alta / media / baja
5. Usá lenguaje morfológico objetivo, no diagnóstico definitivo.
6. Máximo 3 hallazgos nuevos para evitar sobre-diagnóstico.

Formato de respuesta (si hay hallazgos nuevos):
HALLAZGO_1:
  LOCALIZACIÓN: [...]
  DESCRIPCIÓN: [...]
  CONFIANZA: [alta|media|baja]

HALLAZGO_2: (si aplica)
  ...
"""

_PHASE3_SYSTEM = (
    "Sos un árbitro clínico. Tu rol es validar si un hallazgo radiológico sutil "
    "es robusto y clínicamente relevante, o si es ruido visual / artefacto técnico. "
    "Aplicás criterios estrictos: solo confirmás hallazgos que son reproducibles "
    "y que tienen impacto clínico real. Respondés con JSON estructurado únicamente."
)

_PHASE3_PROMPT_TEMPLATE = """\
Se te proporciona un hallazgo radiológico sutil detectado por análisis secundario.
Tu tarea: validar si este hallazgo persiste bajo un segundo análisis independiente.

HALLAZGO A VALIDAR:
  Localización: {location}
  Descripción: {description}
  Confianza inicial: {confidence}

IMÁGENES: adjuntas (las mismas del estudio original).

INSTRUCCIÓN:
Mirá las imágenes con atención específica en la zona indicada.
¿El hallazgo descrito es verificable visualmente de forma independiente?

Respondé EXCLUSIVAMENTE con este JSON (sin markdown, sin texto adicional):
{{
  "confirmed": true | false,
  "reason": "breve justificación (máx. 100 caracteres)",
  "hedging_text": "texto en español con hedging preventivo para incluir en el informe (si confirmed=true)"
}}

Si confirmed=true, el hedging_text DEBE usar lenguaje calibrado como:
  "Hallazgos sutiles sugestivos de [X] incipiente, a correlacionar con clínica"
  "Irregularidades corticales focales de aspecto inespecífico en [localización]"
  "Cambios degenerativos incipientes a nivel [localización], correlacionar con examen físico"
"""


# ============================================================
# Booster implementation
# ============================================================


class AttentionBooster:
    """Pipeline PCCR — Attention Booster for MSK/Pelvis studies.

    Activates only for BodyPart.SKELETAL (MSK, spine, extremities).
    BodyPart.PELVIS is blacklisted since v17 (CARRASCO benchmark regression).
    Runs two additional Gemini calls after the draft report is generated:
    - Phase 2: Focused re-analysis of articular interfaces and entheses.
    - Phase 3: Persistence filter — validates each new finding independently.

    The final report is the draft + confirmed new findings in hedging language.
    """

    def should_activate(self, body_part: BodyPart, template_id: str = "") -> bool:
        """Return True only for MSK and Pelvis regions.

        Hard blacklist (evaluated first, overrides everything):
        - BodyPart.CHEST / THORAX → always False.
        - template_id starting with chest_xray, thorax, torax, chest → always False.

        This dual check exists because BodyPart is inferred from DICOM metadata and
        can be missing or incorrect; template_id is assigned by the application and
        is more reliable.  Both gates must pass.
        """
        # ── Gate 1: template_id blacklist (most reliable signal) ──────────────
        tid_lower = (template_id or "").lower().strip()
        if tid_lower and any(tid_lower.startswith(p) for p in _BOOSTER_BLACKLIST_TEMPLATE_PREFIXES):
            logger.debug(
                "[BOOSTER] Blocked by template_id blacklist: template_id=%r", template_id
            )
            return False

        # ── Gate 2: BodyPart hard blacklist ────────────────────────────────────
        if body_part in _BOOSTER_BLACKLIST_REGIONS:
            logger.debug(
                "[BOOSTER] Blocked by BodyPart blacklist: body_part=%s", body_part.value
            )
            return False

        # ── Gate 3: Eligible whitelist ─────────────────────────────────────────
        return body_part in _BOOSTER_ELIGIBLE_REGIONS

    async def _phase2_boost(
        self,
        *,
        draft_report: str,
        image_parts: list[Any],
        gemini_client: genai.Client,
    ) -> BoostResult:
        """Phase 2 — Focused re-analysis of articular interfaces.

        Sends the draft report + images to Gemini with a focused 'zoom cognitivo'
        instruction. Returns new findings not present in the draft.
        """
        prompt = _PHASE2_PROMPT_TEMPLATE.format(draft_report=draft_report)
        contents: list[Any] = image_parts + [prompt] if image_parts else [prompt]

        try:
            response = await asyncio.wait_for(
                gemini_client.aio.models.generate_content(
                    model=_BOOSTER_MODEL,
                    contents=contents,
                    config=genai.types.GenerateContentConfig(
                        system_instruction=_PHASE2_SYSTEM,
                        temperature=0.1,
                        max_output_tokens=1024,
                    ),
                ),
                timeout=_PHASE2_TIMEOUT,
            )
            raw_text = (response.text or "").strip()
            logger.info("[BOOSTER][PHASE2] Raw response (%d chars): %s", len(raw_text), raw_text[:300])

            if "SIN_HALLAZGOS_NUEVOS" in raw_text:
                return BoostResult(findings=[], raw_response=raw_text)

            findings = _parse_phase2_findings(raw_text)
            return BoostResult(findings=findings, raw_response=raw_text)

        except asyncio.TimeoutError:
            logger.warning("[BOOSTER][PHASE2] Timeout after %.0fs", _PHASE2_TIMEOUT)
            return BoostResult(error="timeout")
        except Exception as exc:
            logger.warning("[BOOSTER][PHASE2] Error: %s", exc)
            return BoostResult(error=str(exc))

    async def _phase3_persistence_filter(
        self,
        *,
        finding: BoostFinding,
        image_parts: list[Any],
        gemini_client: genai.Client,
    ) -> tuple[bool, str]:
        """Phase 3 — Validate that a single finding persists under independent analysis.

        Returns (confirmed: bool, hedging_text: str).
        On error or timeout, returns (False, "") — fail-safe.
        """
        prompt = _PHASE3_PROMPT_TEMPLATE.format(
            location=finding.location,
            description=finding.description,
            confidence=finding.confidence,
        )
        contents: list[Any] = image_parts + [prompt] if image_parts else [prompt]

        try:
            response = await asyncio.wait_for(
                gemini_client.aio.models.generate_content(
                    model=_BOOSTER_MODEL,
                    contents=contents,
                    config=genai.types.GenerateContentConfig(
                        system_instruction=_PHASE3_SYSTEM,
                        temperature=0.0,
                        max_output_tokens=256,
                    ),
                ),
                timeout=_PHASE3_TIMEOUT,
            )
            raw_text = (response.text or "").strip()
            logger.info("[BOOSTER][PHASE3] Finding '%s': %s", finding.location[:40], raw_text[:200])

            import json
            # Strip optional markdown fences
            clean = raw_text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            parsed = json.loads(clean)
            confirmed = bool(parsed.get("confirmed", False))
            hedging_text = parsed.get("hedging_text", "") if confirmed else ""
            return confirmed, hedging_text

        except asyncio.TimeoutError:
            logger.warning("[BOOSTER][PHASE3] Timeout for finding: %s", finding.location)
            return False, ""
        except Exception as exc:
            logger.warning("[BOOSTER][PHASE3] Error for finding '%s': %s", finding.location, exc)
            return False, ""

    async def run(
        self,
        *,
        draft_report: str,
        dicom_images: list[bytes] | None,
        gemini_client: genai.Client,
        body_part: BodyPart,
        proyecciones_txt: str = "estándar",
    ) -> tuple[str, bool]:
        """Run the full PCCR pipeline on top of the draft report.

        Args:
            draft_report: The Phase 1 report already generated by llm_service.
            dicom_images: Raw DICOM bytes; converted to PNG for Gemini Vision.
            gemini_client: Shared Gemini client.
            body_part: Must be SKELETAL (caller should check should_activate first).
            proyecciones_txt: Projection label (for logging only).

        Returns:
            (final_report, was_boosted)
            final_report — draft if no new confirmed findings, otherwise draft + booster section.
            was_boosted  — True if at least one new finding was added.
        """
        if not self.should_activate(body_part):
            logger.debug("[BOOSTER] Inactive for body_part=%s", body_part.value)
            return draft_report, False

        logger.info("[BOOSTER] Activating for body_part=%s, proyecciones=%s", body_part.value, proyecciones_txt)

        # ── Convert DICOM → PNG for both phases ───────────────────────────────
        image_parts: list[Any] = []
        if dicom_images:
            from app.ai.dicom_service import dicom_to_png_bytes
            loop = asyncio.get_running_loop()
            for raw_bytes in dicom_images:
                try:
                    png_bytes = await loop.run_in_executor(None, dicom_to_png_bytes, raw_bytes)
                    image_parts.append(
                        genai.types.Part.from_bytes(data=png_bytes, mime_type="image/png")
                    )
                except Exception as _err:
                    logger.warning("[BOOSTER] DICOM→PNG failed: %s", _err)

        # ── Phase 2: Attention Boost ───────────────────────────────────────────
        boost_result = await self._phase2_boost(
            draft_report=draft_report,
            image_parts=image_parts,
            gemini_client=gemini_client,
        )

        if boost_result.error:
            logger.warning("[BOOSTER] Phase 2 error — returning draft unchanged: %s", boost_result.error)
            return draft_report, False

        if not boost_result.has_new_findings:
            logger.info("[BOOSTER] Phase 2: no new findings. Draft unchanged.")
            return draft_report, False

        logger.info("[BOOSTER] Phase 2: %d potential new finding(s). Running Phase 3 filter.", len(boost_result.findings))

        # ── Phase 3: Persistence Filter (run all validations concurrently) ────
        phase3_tasks = [
            self._phase3_persistence_filter(
                finding=f,
                image_parts=image_parts,
                gemini_client=gemini_client,
            )
            for f in boost_result.findings
        ]
        phase3_results = await asyncio.gather(*phase3_tasks, return_exceptions=True)

        confirmed_hedging: list[str] = []
        for finding, result in zip(boost_result.findings, phase3_results):
            if isinstance(result, Exception):
                logger.warning("[BOOSTER][PHASE3] Exception for '%s': %s", finding.location, result)
                continue
            confirmed, hedging_text = result
            if confirmed and hedging_text:
                confirmed_hedging.append(hedging_text)
                logger.info("[BOOSTER] Confirmed finding: %s → '%s'", finding.location, hedging_text[:80])
            else:
                logger.info("[BOOSTER] Rejected finding (did not persist): %s", finding.location)

        if not confirmed_hedging:
            logger.info("[BOOSTER] Phase 3: all findings rejected. Draft unchanged.")
            return draft_report, False

        # ── Append confirmed booster findings to the draft ────────────────────
        boosted_report = _append_booster_section(draft_report, confirmed_hedging)
        logger.info("[BOOSTER] %d finding(s) confirmed and appended to report.", len(confirmed_hedging))
        return boosted_report, True


# ============================================================
# Parsing helpers
# ============================================================


def _parse_phase2_findings(raw_text: str) -> list[BoostFinding]:
    """Parse Phase 2 free-text response into a list of BoostFinding objects."""
    findings: list[BoostFinding] = []
    current: dict[str, str] = {}

    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("HALLAZGO_"):
            if current:
                findings.append(_build_finding(current))
                current = {}
        elif line.startswith("LOCALIZACIÓN:") or line.startswith("LOCALIZACION:"):
            current["location"] = line.split(":", 1)[1].strip()
        elif line.startswith("DESCRIPCIÓN:") or line.startswith("DESCRIPCION:"):
            current["description"] = line.split(":", 1)[1].strip()
        elif line.startswith("CONFIANZA:"):
            current["confidence"] = line.split(":", 1)[1].strip().lower()

    if current:
        findings.append(_build_finding(current))

    return findings


def _build_finding(data: dict[str, str]) -> BoostFinding:
    return BoostFinding(
        description=data.get("description", ""),
        location=data.get("location", ""),
        confidence=data.get("confidence", "media"),
    )


def _append_booster_section(draft_report: str, hedging_texts: list[str]) -> str:
    """Append Booster-confirmed findings to the existing RECOMENDACIONES section.

    Strategy:
    1. If RECOMENDACIONES exists → insert booster block BEFORE it.
    2. Otherwise → append at the end.

    The booster block always ends with the mandatory clinical correlation note.
    """
    booster_lines = "\n".join(f"- {t}" for t in hedging_texts)
    booster_block = (
        "\nHALLAZGOS ADICIONALES — ANÁLISIS SECUNDARIO\n"
        f"{booster_lines}\n"
        "- Correlacionar con examen físico y proyecciones adicionales según criterio clínico.\n"
    )

    # Try to insert before RECOMENDACIONES
    marker = "RECOMENDACIONES"
    idx = draft_report.find(marker)
    if idx != -1:
        return draft_report[:idx] + booster_block + "\n" + draft_report[idx:]

    # Fallback: append at end
    return draft_report.rstrip() + "\n" + booster_block
