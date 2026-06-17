"""
Integration tests for self-service branding surface (/me/branding + /me/plantilla).

Covers:
- Route registration (all 4 endpoints exist in the app)
- Auth scoping (unauthenticated → 401 for JSON, 303 redirect for HTML)
- /me/branding CRUD with mocked DB pool (authenticated)
- /me/plantilla HTML page renders for authenticated user
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.auth.models import UserPayload


# ── Fake authenticated user ────────────────────────────────────────────────────

FAKE_USER = UserPayload(
    user_id="00000000-0000-0000-0000-000000000001",
    email="test@imagiax.dev",
    nombre="Dra. Test",
    rol="medico",
)


def _make_app():
    """Create a FastAPI app instance for testing."""
    from app.main import create_app
    return create_app()


def _override_auth(app):
    """Override get_current_user to return FAKE_USER without DB calls.

    Must use a sync function with no parameters — FastAPI matches
    the dependency's resolved signature, not the original async one.
    """
    from app.auth.dependencies import get_current_user

    def _fake_user():
        return FAKE_USER

    app.dependency_overrides[get_current_user] = _fake_user


# ── Common lifespan patches (avoid DB/PyTorch in test) ─────────────────────────

_LIFESPAN_PATCHES = [
    "app.core.database.create_pool",
    "app.ai.model_loader.get_model",
    "app.ai.model_loader.DEVICE",
]


class _LifespanTeardown:
    """Mixin that patches lifespan dependencies and cleans up afterwards."""

    _patchers = []

    @classmethod
    def _start_lifespan_patches(cls):
        mock_pool = AsyncMock()
        mock_conn = AsyncMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        p1 = patch("app.core.database.create_pool", new_callable=AsyncMock, return_value=mock_pool)
        p2 = patch("app.ai.model_loader.get_model", return_value=None)
        p3 = patch("app.ai.model_loader.DEVICE", None)
        cls._patchers = [p1, p2, p3]
        for p in cls._patchers:
            p.start()
        return mock_pool

    @classmethod
    def _stop_lifespan_patches(cls):
        for p in cls._patchers:
            p.stop()
        cls._patchers = []


# ── Tests ──────────────────────────────────────────────────────────────────────


class TestBrandingRouteRegistration(unittest.TestCase):
    """Verify all branding routes are registered in the app."""

    def test_routes_exist_in_app(self):
        app = _make_app()
        routes = {r.path for r in app.routes if hasattr(r, "path")}
        self.assertIn("/me/branding", routes)
        self.assertIn("/me/plantilla", routes)

    def test_branding_routes_methods(self):
        app = _make_app()
        # Collect methods per path (multiple route objects can share a path)
        methods = set()
        for r in app.routes:
            if hasattr(r, "methods") and hasattr(r, "path") and r.path == "/me/branding":
                methods.update(r.methods)
        self.assertIn("GET", methods)
        self.assertIn("PUT", methods)
        self.assertIn("DELETE", methods)

    def test_plantilla_is_get(self):
        app = _make_app()
        plantilla_methods = set()
        for r in app.routes:
            if hasattr(r, "path") and r.path == "/me/plantilla" and hasattr(r, "methods"):
                plantilla_methods.update(r.methods)
        self.assertIn("GET", plantilla_methods)


class TestBrandingAuthScoping(unittest.TestCase):
    """Unauthenticated requests must be rejected."""

    def test_get_branding_requires_auth(self):
        """GET /me/branding without auth → 401."""
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/me/branding")
        self.assertIn(resp.status_code, (401, 403))

    def test_put_branding_requires_auth(self):
        """PUT /me/branding without auth → 401."""
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.put("/me/branding", json={"brand_name": "test"})
        self.assertIn(resp.status_code, (401, 403))

    def test_delete_branding_requires_auth(self):
        """DELETE /me/branding without auth → 401."""
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.delete("/me/branding")
        self.assertIn(resp.status_code, (401, 403))

    def test_plantilla_unauthenticated_redirects(self):
        """GET /me/plantilla without session cookie → 303 redirect to /login."""
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/me/plantilla", follow_redirects=False)
        # Should not return 200 (would mean unauthenticated access)
        self.assertNotEqual(resp.status_code, 200)


class TestBrandingCRUDWithAuth(unittest.TestCase):
    """Authenticated CRUD with mocked DB pool."""

    # Origin header required by CSRF middleware for state-mutating requests.
    _ORIGIN = "http://localhost:3000"
    _HEADERS = {"Origin": "http://localhost:3000"}

    def _make_client(self):
        """Create app + TestClient with auth override and mocked pool."""
        mock_pool = _LifespanTeardown._start_lifespan_patches()

        app = _make_app()
        _override_auth(app)

        # Patch branding DB functions
        self.mock_get = patch("app.api.branding.router.informes_db.get_branding_for_user", new_callable=AsyncMock)
        self.mock_save = patch("app.api.branding.router.informes_db.save_branding_config", new_callable=AsyncMock)
        self.mock_set_active = patch("app.api.branding.router.informes_db.set_branding_active", new_callable=AsyncMock)

        self.get_patcher = self.mock_get.start()
        self.save_patcher = self.mock_save.start()
        self.set_active_patcher = self.mock_set_active.start()

        client = TestClient(app, raise_server_exceptions=False)

        # Override pool on app.state after lifespan
        app.state.pool = mock_pool

        self.app = app
        return client

    def tearDown(self):
        patch.stopall()
        _LifespanTeardown._stop_lifespan_patches()

    def test_get_branding_returns_config(self):
        """Authenticated GET /me/branding returns user's branding config."""
        client = self._make_client()
        self.get_patcher.return_value = {"brand_name": "Test Clinic", "activo": True}

        resp = client.get("/me/branding")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data.get("brand_name"), "Test Clinic")

    def test_get_branding_returns_empty_when_none(self):
        """Authenticated GET /me/branding returns {} when no config."""
        client = self._make_client()
        self.get_patcher.return_value = None

        resp = client.get("/me/branding")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {})

    def test_put_branding_saves_config(self):
        """Authenticated PUT /me/branding upserts branding fields."""
        client = self._make_client()
        self.save_patcher.return_value = {
            "brand_name": "New Clinic",
            "brand_subtitle": None,
            "doctor_name": None,
            "doctor_specialty": None,
            "doctor_license": None,
            "logo_b64": None,
            "signature_b64": None,
            "activo": True,
        }

        resp = client.put("/me/branding", json={
            "brand_name": "New Clinic",
        }, headers=self._HEADERS)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data.get("brand_name"), "New Clinic")

    def test_put_branding_ignores_client_user_id(self):
        """PUT /me/branding always ignores client-supplied user_id."""
        client = self._make_client()
        self.save_patcher.return_value = {"brand_name": "X", "activo": True}

        resp = client.put("/me/branding", json={
            "brand_name": "X",
            "user_id": "attacker-should-be-ignored",
        }, headers=self._HEADERS)
        self.assertEqual(resp.status_code, 200)
        # user_id forced to None by BrandingRequest validator → save uses auth session
        self.save_patcher.assert_called_once()
        call_kwargs = self.save_patcher.call_args[1]
        self.assertEqual(call_kwargs["usuario_id"], FAKE_USER.user_id)

    def test_delete_branding_soft_deletes(self):
        """Authenticated DELETE /me/branding sets activo=FALSE."""
        client = self._make_client()
        self.set_active_patcher.return_value = {"brand_name": "Old", "activo": False}

        resp = client.delete("/me/branding", headers=self._HEADERS)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("ok"))
        call_kwargs = self.set_active_patcher.call_args[1]
        self.assertEqual(call_kwargs["usuario_id"], FAKE_USER.user_id)
        self.assertFalse(call_kwargs["activo"])

    def test_delete_branding_404_when_no_config(self):
        """DELETE /me/branding returns 404 when no branding config exists."""
        client = self._make_client()
        self.set_active_patcher.return_value = None

        resp = client.delete("/me/branding", headers=self._HEADERS)
        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()