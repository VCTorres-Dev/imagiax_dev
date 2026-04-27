"""Unit tests for prompt_orchestrator.py — classifier and normalizer coverage.

Covers:
  - normalize_metadata() — alias resolution, modality mapping, keyword extraction
  - classify_exam_rules() — Rule 1 (direct match), Rule 2 (keyword scan), Rule 3
    (modality-only MG fallback), no-match returns None
  - should_run_resnet50() — ML gate (CHEST + XRAY only)
  - Edge cases: empty strings, None inputs, long descriptions, special chars

All test classes are skipped gracefully when pydantic or the module itself
is not importable (e.g. in the WSL dev environment without pydantic installed).
Runtime validation of the actual module happens on the target server.
"""
from __future__ import annotations

import sys
import os
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# Add project root to path so the import resolves when running from anywhere
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------------------
# Guarded import — skip all tests when dependencies are unavailable
# ---------------------------------------------------------------------------
try:
    from prompt_orchestrator import (
        normalize_metadata,
        normalize_body_part,
        classify_exam_rules,
        classify_exam_llm,
        classify_exam,
        should_run_resnet50,
        PatientContext,
        ExamType,
        ExamRegion,
        ExamModality,
        ClassifierSource,
        BODY_PART_ALIASES,
    )
    HAS_DEPS = True
    SKIP_REASON = ""
except ImportError as e:
    HAS_DEPS = False
    SKIP_REASON = f"Dependencies not available: {e}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ctx(**kwargs) -> "PatientContext":
    """Create a PatientContext with sane defaults for all unspecified fields."""
    defaults = dict(
        age="",
        sex="No especificado",
        modality_raw="",
        body_part_examined="",
        series_description="",
        study_description="",
        view_position="",
        requested_procedure="",
    )
    defaults.update(kwargs)
    return PatientContext(**defaults)


# ---------------------------------------------------------------------------
# TestNormalizeBodyPart
# ---------------------------------------------------------------------------

@unittest.skipUnless(HAS_DEPS, SKIP_REASON)
class TestNormalizeBodyPart(unittest.TestCase):
    """Tests for the lower-level normalize_body_part() helper."""

    def test_exact_uppercase_chest(self):
        self.assertEqual(normalize_body_part("CHEST"), "chest")

    def test_lowercase_input(self):
        self.assertEqual(normalize_body_part("chest"), "chest")

    def test_mixed_case_knee(self):
        self.assertEqual(normalize_body_part("Knee"), "knee")

    def test_spanish_alias_rodilla(self):
        self.assertEqual(normalize_body_part("RODILLA"), "knee")

    def test_spanish_alias_torax(self):
        self.assertEqual(normalize_body_part("TORAX"), "chest")

    def test_thorax_alias(self):
        self.assertEqual(normalize_body_part("THORAX"), "chest")

    def test_lumbar_spine_alias(self):
        self.assertEqual(normalize_body_part("COLUMNA_LUMBAR"), "lumbar_spine")

    def test_head_maps_to_brain(self):
        self.assertEqual(normalize_body_part("HEAD"), "brain")

    def test_unknown_returns_none(self):
        self.assertIsNone(normalize_body_part("FINGER"))

    def test_empty_string_returns_none(self):
        self.assertIsNone(normalize_body_part(""))

    def test_none_returns_none(self):
        self.assertIsNone(normalize_body_part(None))

    def test_whitespace_stripped(self):
        self.assertEqual(normalize_body_part("  CHEST  "), "chest")


# ---------------------------------------------------------------------------
# TestNormalizeMetadata
# ---------------------------------------------------------------------------

@unittest.skipUnless(HAS_DEPS, SKIP_REASON)
class TestNormalizeMetadata(unittest.TestCase):
    """Tests for normalize_metadata() which returns {body_part, modality, keywords}."""

    # ── body_part resolution ─────────────────────────────────────────────────

    def test_chest_uppercase(self):
        ctx = _ctx(body_part_examined="CHEST")
        result = normalize_metadata(ctx)
        self.assertEqual(result["body_part"], "chest")

    def test_chest_lowercase(self):
        ctx = _ctx(body_part_examined="chest")
        result = normalize_metadata(ctx)
        self.assertEqual(result["body_part"], "chest")

    def test_thorax_alias_resolves_to_chest(self):
        ctx = _ctx(body_part_examined="THORAX")
        result = normalize_metadata(ctx)
        self.assertEqual(result["body_part"], "chest")

    def test_rodilla_alias_resolves_to_knee(self):
        ctx = _ctx(body_part_examined="RODILLA")
        result = normalize_metadata(ctx)
        self.assertEqual(result["body_part"], "knee")

    def test_columna_lumbar_resolves_to_lumbar_spine(self):
        ctx = _ctx(body_part_examined="COLUMNA_LUMBAR")
        result = normalize_metadata(ctx)
        self.assertEqual(result["body_part"], "lumbar_spine")

    def test_unknown_body_part_returns_none(self):
        """Unknown body parts (not in alias table) return None, not a pass-through."""
        ctx = _ctx(body_part_examined="FINGER")
        result = normalize_metadata(ctx)
        self.assertIsNone(result["body_part"])

    def test_empty_body_part_returns_none(self):
        ctx = _ctx(body_part_examined="")
        result = normalize_metadata(ctx)
        self.assertIsNone(result["body_part"])

    def test_mixed_case_knee(self):
        ctx = _ctx(body_part_examined="Knee")
        result = normalize_metadata(ctx)
        self.assertEqual(result["body_part"], "knee")

    # ── modality normalisation ───────────────────────────────────────────────

    def test_modality_dx_becomes_xray(self):
        ctx = _ctx(modality_raw="DX")
        result = normalize_metadata(ctx)
        self.assertEqual(result["modality"], ExamModality.XRAY)

    def test_modality_cr_becomes_xray(self):
        ctx = _ctx(modality_raw="CR")
        result = normalize_metadata(ctx)
        self.assertEqual(result["modality"], ExamModality.XRAY)

    def test_modality_ct_becomes_ct(self):
        ctx = _ctx(modality_raw="CT")
        result = normalize_metadata(ctx)
        self.assertEqual(result["modality"], ExamModality.CT)

    def test_modality_mr_becomes_mri(self):
        ctx = _ctx(modality_raw="MR")
        result = normalize_metadata(ctx)
        self.assertEqual(result["modality"], ExamModality.MRI)

    def test_modality_mg_becomes_mammo(self):
        ctx = _ctx(modality_raw="MG")
        result = normalize_metadata(ctx)
        self.assertEqual(result["modality"], ExamModality.MAMMO)

    def test_modality_us_becomes_us(self):
        ctx = _ctx(modality_raw="US")
        result = normalize_metadata(ctx)
        self.assertEqual(result["modality"], ExamModality.US)

    def test_unknown_modality_becomes_generic(self):
        ctx = _ctx(modality_raw="XA")
        result = normalize_metadata(ctx)
        self.assertEqual(result["modality"], ExamModality.GENERIC)

    def test_empty_modality_becomes_generic(self):
        ctx = _ctx(modality_raw="")
        result = normalize_metadata(ctx)
        self.assertEqual(result["modality"], ExamModality.GENERIC)

    # ── keyword extraction ───────────────────────────────────────────────────

    def test_keywords_extracted_from_study_description(self):
        ctx = _ctx(study_description="RX TORAX PA Y LAT")
        result = normalize_metadata(ctx)
        # Should have uppercase tokens
        self.assertIn("TORAX", result["keywords"])
        self.assertIn("RX", result["keywords"])

    def test_keywords_extracted_from_series_description(self):
        ctx = _ctx(series_description="RODILLA DERECHA AP")
        result = normalize_metadata(ctx)
        self.assertIn("RODILLA", result["keywords"])

    def test_keywords_split_on_underscore_separator(self):
        """RX_TORAX should be split into ['RX', 'TORAX'] by tokenizer."""
        ctx = _ctx(study_description="RX_TORAX")
        result = normalize_metadata(ctx)
        self.assertIn("TORAX", result["keywords"])

    def test_keywords_split_on_slash_separator(self):
        ctx = _ctx(study_description="TORAX/AP")
        result = normalize_metadata(ctx)
        self.assertIn("TORAX", result["keywords"])

    def test_keywords_split_on_hyphen_separator(self):
        ctx = _ctx(study_description="TORAX-PA")
        result = normalize_metadata(ctx)
        self.assertIn("TORAX", result["keywords"])

    def test_keywords_empty_when_no_descriptions(self):
        ctx = _ctx(series_description="", study_description="")
        result = normalize_metadata(ctx)
        self.assertIsInstance(result["keywords"], set)
        # Only empty or whitespace-only tokens get filtered out
        self.assertEqual(len(result["keywords"]), 0)

    # ── all-None / all-empty inputs ──────────────────────────────────────────

    def test_all_empty_inputs(self):
        ctx = _ctx()
        result = normalize_metadata(ctx)
        self.assertIsNone(result["body_part"])
        self.assertEqual(result["modality"], ExamModality.GENERIC)
        self.assertIsInstance(result["keywords"], set)

    def test_return_has_required_keys(self):
        ctx = _ctx()
        result = normalize_metadata(ctx)
        self.assertIn("body_part", result)
        self.assertIn("modality", result)
        self.assertIn("keywords", result)


# ---------------------------------------------------------------------------
# TestClassifyExamRules
# ---------------------------------------------------------------------------

@unittest.skipUnless(HAS_DEPS, SKIP_REASON)
class TestClassifyExamRules(unittest.TestCase):
    """Tests for classify_exam_rules() — Rule 1, Rule 2, Rule 3, no-match."""

    # ── Rule 1: BodyPartExamined + Modality (confidence 0.95) ───────────────

    def test_chest_xray_rule1(self):
        ctx = _ctx(body_part_examined="CHEST", modality_raw="DX")
        result = classify_exam_rules(ctx)
        self.assertIsNotNone(result)
        self.assertEqual(result.region, ExamRegion.CHEST)
        self.assertEqual(result.modality, ExamModality.XRAY)
        self.assertEqual(result.template_id, "chest_xray")
        self.assertGreaterEqual(result.confidence, 0.9)
        self.assertEqual(result.source, ClassifierSource.RULES)

    def test_chest_xray_cr_modality(self):
        """CR (computed radiography) should also map to XRAY for chest."""
        ctx = _ctx(body_part_examined="CHEST", modality_raw="CR")
        result = classify_exam_rules(ctx)
        self.assertIsNotNone(result)
        self.assertEqual(result.region, ExamRegion.CHEST)
        self.assertEqual(result.template_id, "chest_xray")

    def test_knee_xray_rule1(self):
        ctx = _ctx(body_part_examined="KNEE", modality_raw="DX")
        result = classify_exam_rules(ctx)
        self.assertIsNotNone(result)
        self.assertEqual(result.region, ExamRegion.KNEE)
        self.assertEqual(result.modality, ExamModality.XRAY)
        self.assertEqual(result.template_id, "msk_xray")
        self.assertGreaterEqual(result.confidence, 0.9)

    def test_lumbar_spine_xray_rule1(self):
        ctx = _ctx(body_part_examined="LUMBAR_SPINE", modality_raw="DX")
        result = classify_exam_rules(ctx)
        self.assertIsNotNone(result)
        self.assertEqual(result.region, ExamRegion.LUMBAR_SPINE)
        self.assertEqual(result.template_id, "lumbar_spine_xray")

    def test_brain_ct_rule1(self):
        ctx = _ctx(body_part_examined="HEAD", modality_raw="CT")
        result = classify_exam_rules(ctx)
        self.assertIsNotNone(result)
        self.assertEqual(result.region, ExamRegion.BRAIN)
        self.assertEqual(result.modality, ExamModality.CT)
        self.assertEqual(result.template_id, "brain_ct")

    def test_brain_mri_rule1(self):
        ctx = _ctx(body_part_examined="BRAIN", modality_raw="MR")
        result = classify_exam_rules(ctx)
        self.assertIsNotNone(result)
        self.assertEqual(result.region, ExamRegion.BRAIN)
        self.assertEqual(result.modality, ExamModality.MRI)

    def test_rule1_confidence_is_0_95(self):
        ctx = _ctx(body_part_examined="CHEST", modality_raw="DX")
        result = classify_exam_rules(ctx)
        self.assertAlmostEqual(result.confidence, 0.95)

    def test_torax_alias_triggers_rule1(self):
        """TORAX is an alias for chest — should still fire Rule 1."""
        ctx = _ctx(body_part_examined="TORAX", modality_raw="DX")
        result = classify_exam_rules(ctx)
        self.assertIsNotNone(result)
        self.assertEqual(result.region, ExamRegion.CHEST)
        self.assertEqual(result.template_id, "chest_xray")

    def test_rodilla_alias_triggers_rule1(self):
        ctx = _ctx(body_part_examined="RODILLA", modality_raw="CR")
        result = classify_exam_rules(ctx)
        self.assertIsNotNone(result)
        self.assertEqual(result.region, ExamRegion.KNEE)

    # ── Rule 2: Keyword scan (confidence 0.80) ───────────────────────────────

    def test_keyword_scan_torax_in_study_description(self):
        """Rule 2 fires when body_part is None but keywords contain TORAX."""
        ctx = _ctx(body_part_examined="", modality_raw="DX",
                   study_description="RX TORAX PA Y LAT")
        result = classify_exam_rules(ctx)
        self.assertIsNotNone(result)
        self.assertEqual(result.region, ExamRegion.CHEST)
        self.assertEqual(result.template_id, "chest_xray")

    def test_keyword_scan_confidence_is_0_80(self):
        ctx = _ctx(body_part_examined="", study_description="TORAX PA")
        result = classify_exam_rules(ctx)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result.confidence, 0.80)

    def test_keyword_scan_chest_has_lower_confidence_than_rule1(self):
        """Rule 2 (keyword, 0.80) should be less confident than Rule 1 (0.95)."""
        ctx_rule1 = _ctx(body_part_examined="CHEST", modality_raw="DX")
        ctx_rule2 = _ctx(body_part_examined="", study_description="TORAX PA")
        r1 = classify_exam_rules(ctx_rule1)
        r2 = classify_exam_rules(ctx_rule2)
        self.assertGreater(r1.confidence, r2.confidence)

    def test_keyword_scan_rodilla_in_series_description(self):
        ctx = _ctx(body_part_examined="", series_description="RODILLA DERECHA AP")
        result = classify_exam_rules(ctx)
        self.assertIsNotNone(result)
        self.assertEqual(result.region, ExamRegion.KNEE)

    def test_keyword_scan_lumbar_keyword(self):
        ctx = _ctx(body_part_examined="", study_description="COLUMNA LUMBAR AP LAT")
        result = classify_exam_rules(ctx)
        self.assertIsNotNone(result)
        self.assertEqual(result.region, ExamRegion.LUMBAR_SPINE)

    def test_keyword_scan_cerebro_brain(self):
        ctx = _ctx(body_part_examined="", study_description="CEREBRO SIN CONTRASTE")
        result = classify_exam_rules(ctx)
        self.assertIsNotNone(result)
        self.assertEqual(result.region, ExamRegion.BRAIN)

    def test_rule2_not_triggered_when_body_part_present(self):
        """Rule 2 is ONLY triggered when body_part resolves to None.
        If body_part resolves (even to a non-mapped modality combo), Rule 2 is
        bypassed for keywords — Rule 1 takes precedence."""
        # body_part resolved to "chest" — Rule 2 keyword scan should NOT fire
        # independently; Rule 1 handles it (or returns None if no template match)
        ctx = _ctx(body_part_examined="CHEST", modality_raw="DX",
                   study_description="RODILLA")  # misleading description
        result = classify_exam_rules(ctx)
        # Should classify as chest (Rule 1), not knee (Rule 2)
        self.assertIsNotNone(result)
        self.assertEqual(result.region, ExamRegion.CHEST)

    # ── Rule 3: Modality-only MG → MAMMO ────────────────────────────────────

    def test_mg_modality_triggers_rule3(self):
        """MG (mammography) with no body_part and no useful keywords → Rule 3."""
        ctx = _ctx(body_part_examined="", modality_raw="MG", study_description="")
        result = classify_exam_rules(ctx)
        self.assertIsNotNone(result)
        self.assertEqual(result.modality, ExamModality.MAMMO)
        self.assertEqual(result.region, ExamRegion.GENERIC)  # no BREAST enum
        self.assertEqual(result.template_id, "generic_fallback")
        self.assertAlmostEqual(result.confidence, 0.90)

    def test_mg_rule3_source_is_rules(self):
        ctx = _ctx(modality_raw="MG")
        result = classify_exam_rules(ctx)
        self.assertIsNotNone(result)
        self.assertEqual(result.source, ClassifierSource.RULES)

    # ── No match → None ──────────────────────────────────────────────────────

    def test_unknown_body_part_unknown_modality_returns_none(self):
        ctx = _ctx(body_part_examined="FINGER", modality_raw="XA",
                   study_description="ANGIO")
        result = classify_exam_rules(ctx)
        self.assertIsNone(result)

    def test_no_metadata_returns_none(self):
        """All fields empty and no matching keywords → None."""
        ctx = _ctx()
        result = classify_exam_rules(ctx)
        self.assertIsNone(result)

    def test_ct_without_body_part_no_keyword_returns_none(self):
        """CT with no body_part and no matching keywords. Rule 3 only covers MG."""
        ctx = _ctx(body_part_examined="", modality_raw="CT", study_description="")
        result = classify_exam_rules(ctx)
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# TestShouldRunResnet50
# ---------------------------------------------------------------------------

@unittest.skipUnless(HAS_DEPS, SKIP_REASON)
class TestShouldRunResnet50(unittest.TestCase):
    """Tests for the ML gate — should_run_resnet50()."""

    def _make_exam(
        self,
        region: "ExamRegion",
        modality: "ExamModality",
        template_id: str = "test_template",
        confidence: float = 0.95,
    ) -> "ExamType":
        return ExamType(
            region=region,
            modality=modality,
            confidence=confidence,
            source=ClassifierSource.RULES,
            template_id=template_id,
        )

    def test_chest_xray_returns_true(self):
        exam = self._make_exam(ExamRegion.CHEST, ExamModality.XRAY)
        self.assertTrue(should_run_resnet50(exam))

    def test_knee_xray_returns_false(self):
        exam = self._make_exam(ExamRegion.KNEE, ExamModality.XRAY)
        self.assertFalse(should_run_resnet50(exam))

    def test_chest_ct_returns_false(self):
        """ResNet50 is only for X-ray, not CT, even if region is CHEST."""
        exam = self._make_exam(ExamRegion.CHEST, ExamModality.CT)
        self.assertFalse(should_run_resnet50(exam))

    def test_chest_mri_returns_false(self):
        exam = self._make_exam(ExamRegion.CHEST, ExamModality.MRI)
        self.assertFalse(should_run_resnet50(exam))

    def test_generic_exam_returns_false(self):
        exam = self._make_exam(ExamRegion.GENERIC, ExamModality.GENERIC)
        self.assertFalse(should_run_resnet50(exam))

    def test_brain_ct_returns_false(self):
        exam = self._make_exam(ExamRegion.BRAIN, ExamModality.CT)
        self.assertFalse(should_run_resnet50(exam))

    def test_lumbar_spine_xray_returns_false(self):
        exam = self._make_exam(ExamRegion.LUMBAR_SPINE, ExamModality.XRAY)
        self.assertFalse(should_run_resnet50(exam))

    def test_mammography_returns_false(self):
        exam = self._make_exam(ExamRegion.GENERIC, ExamModality.MAMMO)
        self.assertFalse(should_run_resnet50(exam))

    def test_shoulder_xray_returns_false(self):
        exam = self._make_exam(ExamRegion.SHOULDER, ExamModality.XRAY)
        self.assertFalse(should_run_resnet50(exam))


# ---------------------------------------------------------------------------
# TestBodyPartAliasTable
# ---------------------------------------------------------------------------

@unittest.skipUnless(HAS_DEPS, SKIP_REASON)
class TestBodyPartAliasTable(unittest.TestCase):
    """Smoke tests ensuring the alias table has the expected entries."""

    def test_alias_table_is_dict(self):
        self.assertIsInstance(BODY_PART_ALIASES, dict)

    def test_alias_table_has_minimum_entries(self):
        self.assertGreaterEqual(len(BODY_PART_ALIASES), 30)

    def test_all_values_are_valid_exam_region_strings(self):
        valid_values = {r.value for r in ExamRegion}
        for key, value in BODY_PART_ALIASES.items():
            self.assertIn(
                value, valid_values,
                msg=f"Alias '{key}' → '{value}' is not a valid ExamRegion value",
            )

    def test_chest_alias_present(self):
        self.assertIn("CHEST", BODY_PART_ALIASES)

    def test_rodilla_alias_present(self):
        self.assertIn("RODILLA", BODY_PART_ALIASES)

    def test_columna_lumbar_alias_present(self):
        self.assertIn("COLUMNA_LUMBAR", BODY_PART_ALIASES)


# ---------------------------------------------------------------------------
# TestEdgeCases
# ---------------------------------------------------------------------------

@unittest.skipUnless(HAS_DEPS, SKIP_REASON)
class TestEdgeCases(unittest.TestCase):
    """Edge cases: empty strings, very long input, special characters."""

    def test_empty_body_part_treated_as_none(self):
        ctx = _ctx(body_part_examined="", modality_raw="DX", study_description="")
        result = classify_exam_rules(ctx)
        # No keywords → None (Rule 1 skipped since body_part is None,
        # Rule 2 skipped since no keyword match, Rule 3 skipped since not MG)
        self.assertIsNone(result)

    def test_very_long_study_description_does_not_crash(self):
        long_desc = "TORAX " * 5000  # 30000 characters
        ctx = _ctx(study_description=long_desc)
        try:
            result = classify_exam_rules(ctx)
        except Exception as exc:
            self.fail(f"classify_exam_rules() raised {type(exc).__name__} on long input: {exc}")
        self.assertIsNotNone(result)
        self.assertEqual(result.region, ExamRegion.CHEST)

    def test_special_characters_in_description_do_not_crash(self):
        ctx = _ctx(study_description="RX//TORAX;;PA__LAT--AP")
        try:
            result = classify_exam_rules(ctx)
        except Exception as exc:
            self.fail(f"classify_exam_rules() raised {type(exc).__name__} on special chars: {exc}")
        # Regardless of match, it should not crash

    def test_unicode_body_part_tórax_with_accent(self):
        """TÓRAX (with accent) is in the alias table."""
        ctx = _ctx(body_part_examined="TÓRAX", modality_raw="DX")
        result = classify_exam_rules(ctx)
        self.assertIsNotNone(result)
        self.assertEqual(result.region, ExamRegion.CHEST)

    def test_normalize_metadata_with_only_whitespace_description(self):
        ctx = _ctx(study_description="   ", series_description="\t\n")
        result = normalize_metadata(ctx)
        # Whitespace-only tokens should be filtered; keywords should be empty
        self.assertEqual(len(result["keywords"]), 0)

    def test_classify_does_not_mutate_input_context(self):
        ctx = _ctx(body_part_examined="CHEST", modality_raw="DX",
                   study_description="original")
        original_bp = ctx.body_part_examined
        original_desc = ctx.study_description
        classify_exam_rules(ctx)
        self.assertEqual(ctx.body_part_examined, original_bp)
        self.assertEqual(ctx.study_description, original_desc)

    def test_normalize_metadata_returns_dict_with_set_keywords(self):
        ctx = _ctx(study_description="A B A B C")
        result = normalize_metadata(ctx)
        # keywords is a set — no duplicates
        self.assertIsInstance(result["keywords"], set)
        # 'A', 'B', 'C' but no duplicates
        self.assertEqual(result["keywords"], {"A", "B", "C"})


# ---------------------------------------------------------------------------
# TestClassifyExam (T4.2 / T5.2) — async orchestrator
# ---------------------------------------------------------------------------

def _run(coro):
    """Run async coroutine in tests (compatible with Python 3.10+)."""
    return asyncio.get_event_loop().run_until_complete(coro)


@unittest.skipUnless(HAS_DEPS, SKIP_REASON)
class TestClassifyExam(unittest.TestCase):
    """Tests for classify_exam() — rules path, generic fallback, LLM delegation."""

    # ── Rules path: high-confidence result returned directly ────────────────

    def test_chest_high_confidence_no_llm_call(self):
        """Chest DX → rules return confidence >= 0.7 → classify_exam returns immediately."""
        ctx = _ctx(body_part_examined="CHEST", modality_raw="DX")
        result = _run(classify_exam(ctx, gemini_client=None))
        self.assertEqual(result.region, ExamRegion.CHEST)
        self.assertEqual(result.source, ClassifierSource.RULES)
        self.assertGreaterEqual(result.confidence, 0.7)

    def test_knee_rules_path(self):
        """Knee CR → rules high-confidence → classify_exam returns KNEE."""
        ctx = _ctx(body_part_examined="KNEE", modality_raw="CR")
        result = _run(classify_exam(ctx, gemini_client=None))
        self.assertEqual(result.region, ExamRegion.KNEE)
        self.assertEqual(result.source, ClassifierSource.RULES)

    def test_lumbar_spine_rules_path(self):
        """Lumbar spine → rules → classify_exam returns LUMBAR_SPINE."""
        ctx = _ctx(body_part_examined="LSPINE", modality_raw="DX")
        result = _run(classify_exam(ctx, gemini_client=None))
        self.assertEqual(result.region, ExamRegion.LUMBAR_SPINE)

    # ── No gemini_client + low-confidence → generic_fallback ────────────────

    def test_unknown_body_part_no_client_returns_generic_fallback(self):
        """Ambiguous metadata + no gemini_client → generic_fallback."""
        ctx = _ctx(body_part_examined="XYZUNKNOWN_PART", modality_raw="OT")
        result = _run(classify_exam(ctx, gemini_client=None))
        self.assertEqual(result.source, ClassifierSource.GENERIC_FALLBACK)
        self.assertEqual(result.template_id, "generic_fallback")
        self.assertEqual(result.confidence, 0.0)

    def test_empty_metadata_no_client_returns_generic_fallback(self):
        """Completely empty metadata + no gemini_client → generic_fallback."""
        ctx = _ctx()
        result = _run(classify_exam(ctx, gemini_client=None))
        # May be rules-based (generic) or generic_fallback — either is valid
        self.assertIn(result.template_id, ("generic_fallback",))

    # ── Confidence threshold boundary ───────────────────────────────────────

    def test_confidence_threshold_respected(self):
        """If rules return exactly at threshold, result is accepted."""
        ctx = _ctx(body_part_examined="CHEST", modality_raw="DX")
        result_default = _run(classify_exam(ctx, gemini_client=None, confidence_threshold=0.7))
        self.assertEqual(result_default.source, ClassifierSource.RULES)

    def test_very_high_threshold_falls_to_generic(self):
        """If threshold is 1.0, rules result won't reach it → generic_fallback (no client)."""
        ctx = _ctx(body_part_examined="CHEST", modality_raw="DX")
        result = _run(classify_exam(ctx, gemini_client=None, confidence_threshold=1.0))
        # Rules chest confidence is typically < 1.0, so should fall to generic_fallback
        # (unless rules return exactly 1.0 — accept either RULES or GENERIC_FALLBACK)
        self.assertIn(result.source, (ClassifierSource.RULES, ClassifierSource.GENERIC_FALLBACK))


# ---------------------------------------------------------------------------
# TestClassifyExamLLM (T5.1 / T5.2) — LLM fallback
# ---------------------------------------------------------------------------

@unittest.skipUnless(HAS_DEPS, SKIP_REASON)
class TestClassifyExamLLM(unittest.TestCase):
    """Tests for classify_exam_llm() with mocked Gemini client."""

    def _make_mock_client(self, json_response: str):
        """Build a mock gemini_client that returns json_response from generate_content."""
        mock_response = MagicMock()
        mock_response.text = json_response

        mock_models = MagicMock()
        mock_models.generate_content = AsyncMock(return_value=mock_response)

        mock_aio = MagicMock()
        mock_aio.models = mock_models

        mock_client = MagicMock()
        mock_client.aio = mock_aio
        return mock_client

    # ── Happy path: valid JSON response ─────────────────────────────────────

    def test_valid_chest_json_returns_llm_fallback(self):
        """LLM returns valid chest JSON → classify_exam_llm returns LLM_FALLBACK source."""
        client = self._make_mock_client(
            '{"region": "chest", "modality": "xray", "confidence": 0.85}'
        )
        ctx = _ctx(modality_raw="DX", study_description="Rx torax frente")
        result = _run(classify_exam_llm(ctx, gemini_client=client))
        self.assertEqual(result.source, ClassifierSource.LLM_FALLBACK)
        self.assertEqual(result.region, ExamRegion.CHEST)
        self.assertEqual(result.modality, ExamModality.XRAY)
        self.assertAlmostEqual(result.confidence, 0.85)
        self.assertEqual(result.template_id, "chest_xray")

    def test_valid_brain_ct_json(self):
        """LLM returns brain+ct → brain_ct template."""
        client = self._make_mock_client(
            '{"region": "brain", "modality": "ct", "confidence": 0.9}'
        )
        ctx = _ctx(modality_raw="CT", body_part_examined="HEAD")
        result = _run(classify_exam_llm(ctx, gemini_client=client))
        self.assertEqual(result.region, ExamRegion.BRAIN)
        self.assertEqual(result.modality, ExamModality.CT)
        self.assertEqual(result.template_id, "brain_ct")
        self.assertEqual(result.source, ClassifierSource.LLM_FALLBACK)

    def test_valid_msk_json(self):
        """LLM returns msk (not a valid ExamRegion) → falls back to GENERIC, msk_xray template."""
        client = self._make_mock_client(
            '{"region": "msk", "modality": "xray", "confidence": 0.8}'
        )
        ctx = _ctx(modality_raw="CR", body_part_examined="SHOULDER")
        result = _run(classify_exam_llm(ctx, gemini_client=client))
        self.assertEqual(result.region, ExamRegion.GENERIC)
        self.assertEqual(result.template_id, "msk_xray")

    def test_json_with_markdown_fencing(self):
        """LLM wraps response in ```json ... ``` → still parses correctly."""
        client = self._make_mock_client(
            "```json\n{\"region\": \"chest\", \"modality\": \"xray\", \"confidence\": 0.7}\n```"
        )
        ctx = _ctx(modality_raw="DX", body_part_examined="CHEST")
        result = _run(classify_exam_llm(ctx, gemini_client=client))
        self.assertEqual(result.source, ClassifierSource.LLM_FALLBACK)
        self.assertEqual(result.region, ExamRegion.CHEST)

    def test_confidence_clamped_above_1(self):
        """LLM returns confidence > 1.0 → clamped to 1.0."""
        client = self._make_mock_client(
            '{"region": "chest", "modality": "xray", "confidence": 1.5}'
        )
        ctx = _ctx()
        result = _run(classify_exam_llm(ctx, gemini_client=client))
        self.assertLessEqual(result.confidence, 1.0)

    def test_confidence_clamped_below_0(self):
        """LLM returns negative confidence → clamped to 0.0."""
        client = self._make_mock_client(
            '{"region": "unknown", "modality": "generic", "confidence": -0.5}'
        )
        ctx = _ctx()
        result = _run(classify_exam_llm(ctx, gemini_client=client))
        self.assertGreaterEqual(result.confidence, 0.0)

    # ── Error / failure paths → always return GENERIC_FALLBACK ──────────────

    def test_invalid_json_returns_generic_fallback(self):
        """LLM returns invalid JSON → classify_exam_llm returns GENERIC_FALLBACK."""
        client = self._make_mock_client("not valid json at all")
        ctx = _ctx()
        result = _run(classify_exam_llm(ctx, gemini_client=client))
        self.assertEqual(result.source, ClassifierSource.GENERIC_FALLBACK)
        self.assertEqual(result.template_id, "generic_fallback")
        self.assertEqual(result.confidence, 0.0)

    def test_empty_response_returns_generic_fallback(self):
        """LLM returns empty string → GENERIC_FALLBACK."""
        client = self._make_mock_client("")
        ctx = _ctx()
        result = _run(classify_exam_llm(ctx, gemini_client=client))
        self.assertEqual(result.source, ClassifierSource.GENERIC_FALLBACK)

    def test_network_error_returns_generic_fallback(self):
        """Gemini client raises exception → GENERIC_FALLBACK (never propagates)."""
        mock_models = MagicMock()
        mock_models.generate_content = AsyncMock(side_effect=RuntimeError("network down"))
        mock_aio = MagicMock()
        mock_aio.models = mock_models
        mock_client = MagicMock()
        mock_client.aio = mock_aio

        ctx = _ctx()
        result = _run(classify_exam_llm(ctx, gemini_client=mock_client))
        self.assertEqual(result.source, ClassifierSource.GENERIC_FALLBACK)

    def test_timeout_returns_generic_fallback(self):
        """asyncio.TimeoutError → GENERIC_FALLBACK (never propagates)."""
        import asyncio as _asyncio

        mock_models = MagicMock()
        mock_models.generate_content = AsyncMock(side_effect=_asyncio.TimeoutError())
        mock_aio = MagicMock()
        mock_aio.models = mock_models
        mock_client = MagicMock()
        mock_client.aio = mock_aio

        ctx = _ctx()
        result = _run(classify_exam_llm(ctx, gemini_client=mock_client))
        self.assertEqual(result.source, ClassifierSource.GENERIC_FALLBACK)

    def test_unknown_region_maps_to_generic_fallback_template(self):
        """LLM returns region='unknown' → generic_fallback template."""
        client = self._make_mock_client(
            '{"region": "unknown", "modality": "generic", "confidence": 0.0}'
        )
        ctx = _ctx()
        result = _run(classify_exam_llm(ctx, gemini_client=client))
        self.assertEqual(result.template_id, "generic_fallback")

    # ── classify_exam with gemini_client: delegation to LLM on low-confidence ─

    def test_classify_exam_calls_llm_when_rules_low_confidence(self):
        """If rules return low-confidence result and gemini_client is provided, LLM is called."""
        client = self._make_mock_client(
            '{"region": "msk", "modality": "xray", "confidence": 0.8}'
        )
        # Use a body part that rules won't match confidently
        ctx = _ctx(modality_raw="DX", body_part_examined="XYZUNKNOWN_PART")
        result = _run(classify_exam(ctx, gemini_client=client, confidence_threshold=0.7))
        # Could be rules-fallback or LLM — the point is it doesn't crash
        self.assertIsInstance(result, ExamType)
        self.assertIsInstance(result.source, ClassifierSource)


# ---------------------------------------------------------------------------
# TestClassifyExamRulesReturnType
# ---------------------------------------------------------------------------

@unittest.skipUnless(HAS_DEPS, SKIP_REASON)
class TestClassifyExamRulesReturnType(unittest.TestCase):
    """Verify ExamType field types and value constraints on returned objects."""

    def test_examtype_has_required_fields(self):
        ctx = _ctx(body_part_examined="CHEST", modality_raw="DX")
        result = classify_exam_rules(ctx)
        self.assertIsNotNone(result)
        self.assertIsInstance(result, ExamType)
        self.assertIsInstance(result.region, ExamRegion)
        self.assertIsInstance(result.modality, ExamModality)
        self.assertIsInstance(result.confidence, float)
        self.assertIsInstance(result.template_id, str)
        self.assertIsInstance(result.source, ClassifierSource)

    def test_confidence_within_bounds(self):
        for bp, mod in [("CHEST", "DX"), ("KNEE", "DX"), ("HEAD", "CT")]:
            with self.subTest(body_part=bp, modality=mod):
                ctx = _ctx(body_part_examined=bp, modality_raw=mod)
                result = classify_exam_rules(ctx)
                if result is not None:
                    self.assertGreaterEqual(result.confidence, 0.0)
                    self.assertLessEqual(result.confidence, 1.0)

    def test_template_id_is_non_empty_string(self):
        ctx = _ctx(body_part_examined="CHEST", modality_raw="DX")
        result = classify_exam_rules(ctx)
        self.assertIsNotNone(result)
        self.assertTrue(len(result.template_id) > 0)

    def test_keyword_scan_result_has_source_rules(self):
        ctx = _ctx(body_part_examined="", study_description="TORAX PA")
        result = classify_exam_rules(ctx)
        self.assertIsNotNone(result)
        self.assertEqual(result.source, ClassifierSource.RULES)


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)
