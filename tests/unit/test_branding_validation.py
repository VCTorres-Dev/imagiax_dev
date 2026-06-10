"""
Unit tests for app.informes.branding_validation.

Covers BrandingRequest model validation:
- Extra fields (e.g. user_id) are silently ignored
- base64 size validators for logo_b64 and signature_b64
- Field length limits
- Normal valid payloads pass

No database required — pure Pydantic model tests.
"""

import base64
import unittest

from app.informes.branding_validation import (
    LOGO_MAX_BYTES,
    SIGNATURE_MAX_BYTES,
    BrandingRequest,
    validate_base64_size,
)


class TestBrandingRequestExtraIgnore(unittest.TestCase):
    """Extra fields must be silently ignored (identity from auth session)."""

    def test_user_id_is_ignored(self):
        """Client-supplied user_id must be dropped — identity from session."""
        req = BrandingRequest(user_id="attacker-id", brand_name="Clinic")
        self.assertIsNone(req.user_id)
        self.assertEqual(req.brand_name, "Clinic")

    def test_unknown_extra_fields_ignored(self):
        """Any extra fields not in the model must be silently dropped."""
        req = BrandingRequest(brand_name="OK", random_field="dropped")
        self.assertEqual(req.brand_name, "OK")
        with self.assertRaises(AttributeError):
            _ = req.random_field

    def test_all_optional_fields_default_none(self):
        """Empty request with no fields should have all None values."""
        req = BrandingRequest()
        self.assertIsNone(req.brand_name)
        self.assertIsNone(req.brand_subtitle)
        self.assertIsNone(req.doctor_name)
        self.assertIsNone(req.doctor_specialty)
        self.assertIsNone(req.doctor_license)
        self.assertIsNone(req.logo_b64)
        self.assertIsNone(req.signature_b64)


class TestBrandingRequestFieldLengths(unittest.TestCase):
    """String fields must respect max_length constraints."""

    def test_brand_name_max_length(self):
        with self.assertRaises(Exception):
            BrandingRequest(brand_name="x" * 101)

    def test_brand_subtitle_max_length(self):
        with self.assertRaises(Exception):
            BrandingRequest(brand_subtitle="x" * 201)

    def test_doctor_name_max_length(self):
        with self.assertRaises(Exception):
            BrandingRequest(doctor_name="x" * 201)

    def test_doctor_specialty_max_length(self):
        with self.assertRaises(Exception):
            BrandingRequest(doctor_specialty="x" * 201)

    def test_doctor_license_max_length(self):
        with self.assertRaises(Exception):
            BrandingRequest(doctor_license="x" * 51)

    def test_valid_lengths_pass(self):
        req = BrandingRequest(
            brand_name="x" * 100,
            brand_subtitle="x" * 200,
            doctor_name="x" * 200,
            doctor_specialty="x" * 200,
            doctor_license="x" * 50,
        )
        self.assertEqual(len(req.brand_name), 100)


class TestBrandingRequestB64Validation(unittest.TestCase):
    """logo_b64 and signature_b64 must pass size validation."""

    def _make_b64(self, size_bytes: int) -> str:
        """Create a valid base64 string of roughly ``size_bytes`` decoded length."""
        raw = b"A" * size_bytes
        return base64.b64encode(raw).decode("ascii")

    def test_logo_within_limit(self):
        b64 = self._make_b64(100)
        req = BrandingRequest(logo_b64=b64)
        self.assertEqual(req.logo_b64, b64)

    def test_logo_at_exact_limit(self):
        b64 = self._make_b64(LOGO_MAX_BYTES)
        req = BrandingRequest(logo_b64=b64)
        self.assertEqual(req.logo_b64, b64)

    def test_oversized_logo_rejected(self):
        b64 = self._make_b64(LOGO_MAX_BYTES + 1)
        with self.assertRaises(Exception):
            BrandingRequest(logo_b64=b64)

    def test_signature_within_limit(self):
        b64 = self._make_b64(100)
        req = BrandingRequest(signature_b64=b64)
        self.assertEqual(req.signature_b64, b64)

    def test_signature_at_exact_limit(self):
        b64 = self._make_b64(SIGNATURE_MAX_BYTES)
        req = BrandingRequest(signature_b64=b64)
        self.assertEqual(req.signature_b64, b64)

    def test_oversized_signature_rejected(self):
        b64 = self._make_b64(SIGNATURE_MAX_BYTES + 1)
        with self.assertRaises(Exception):
            BrandingRequest(signature_b64=b64)

    def test_none_logo_and_signature_are_valid(self):
        req = BrandingRequest(logo_b64=None, signature_b64=None)
        self.assertIsNone(req.logo_b64)
        self.assertIsNone(req.signature_b64)

    def test_invalid_base64_rejected(self):
        with self.assertRaises(Exception):
            BrandingRequest(logo_b64="not-valid-base64!!!")


class TestValidateBase64SizeHelper(unittest.TestCase):
    """Direct tests for the validate_base64_size helper function."""

    def test_none_passes(self):
        self.assertIsNone(validate_base64_size(None, 1024, "test_field"))

    def test_valid_small_value(self):
        b64 = base64.b64encode(b"hello").decode()
        result = validate_base64_size(b64, 1024, "test_field")
        self.assertEqual(result, b64)

    def test_oversized_raises(self):
        b64 = base64.b64encode(b"A" * 2048).decode()
        with self.assertRaises(ValueError) as ctx:
            validate_base64_size(b64, 1024, "test_field")
        self.assertIn("test_field", str(ctx.exception))

    def test_invalid_base64_raises(self):
        with self.assertRaises(ValueError) as ctx:
            validate_base64_size("not-base64!!!", 1024, "test_field")
        self.assertIn("test_field", str(ctx.exception))


class TestBrandingRequestFullValidPayload(unittest.TestCase):
    """A complete, valid payload should parse without errors."""

    def test_full_valid_payload(self):
        logo_b64 = base64.b64encode(b"logo-data").decode()
        sig_b64 = base64.b64encode(b"sig-data").decode()
        req = BrandingRequest(
            brand_name="IMAGIAX",
            brand_subtitle="Diagnóstico Inteligente",
            doctor_name="Dr. Ejemplo",
            doctor_specialty="Radiología",
            doctor_license="12345",
            logo_b64=logo_b64,
            signature_b64=sig_b64,
        )
        self.assertEqual(req.brand_name, "IMAGIAX")
        self.assertEqual(req.doctor_specialty, "Radiología")
        self.assertEqual(req.logo_b64, logo_b64)


if __name__ == "__main__":
    unittest.main()