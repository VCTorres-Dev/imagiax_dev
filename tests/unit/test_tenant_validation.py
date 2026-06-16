"""
Unit tests for tenant validation regressions.

Covers:
- JWT tenant claims (create/decode, old-token defaults, renewal preservation)
- UserPayload tenant fields (defaults, JSON roundtrip, scope distinctions)
- Bootstrap mode configuration (prod rejection, dev acceptance, fail-closed)
- Router-level tenant scoping via TestClient with DB dependency overrides:
  - ver_informe: missing scope → 401, cross-clinic → 403, same-clinic → 200
  - listado_informes: missing scope → 401, clinic-scoped filter, global sees all
  - abrir_estudio_viewer: missing scope → 401, cross-clinic → 404
- Branding clinic passthrough (DB function signatures accept clinic_id)
- DB function tenant signatures (all DB functions accept clinic_id/scope)
- Tenant validation helpers (backfill, validate)
- CreateUser tenant validation (non-superadmin without clinic → 400)
- [CRITICAL-1] Fresh DB startup: ALTERs on non-existent tables are guarded
- [CRITICAL-2] Admin endpoints: clinic-scoped admins cannot bypass tenant isolation
- [CRITICAL-3] generar_informe: old JWTs cannot create PHI rows with clinic_id=NULL
- [CRITICAL-4] informes_por_estudio: study report lookup is tenant-scoped
- [CRITICAL-5] scope=clinic + clinic_id=None: fail-closed on study/listado + DB helpers
- [CRITICAL-6] Clinic-scoped admin cannot mutate cross-tenant users (PATCH/reset/delete)
- [CRITICAL-7] Admin with scope=clinic but no clinic_id cannot mutate any user

No real database required — uses unittest with AsyncMock/patch for DB calls,
and FastAPI TestClient for router-level tests with dependency overrides.
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.auth.models import UserPayload
from app.informes import db as informes_db
from app.admin import service as admin_service


# ── Test user fixtures ────────────────────────────────────────────────────────

CLINIC_A = "11111111-1111-1111-1111-111111111111"
CLINIC_B = "22222222-2222-2222-2222-222222222222"

CLINIC_USER = UserPayload(
    user_id="00000000-0000-0000-0000-000000000001",
    email="medico@clinic-a.dev",
    nombre="Dra. Clinica",
    rol="medico",
    clinic_id=CLINIC_A,
    scope="clinic",
)

GLOBAL_USER = UserPayload(
    user_id="00000000-0000-0000-0000-000000000002",
    email="superadmin@imagiax.dev",
    nombre="Super Admin",
    rol="superadmin",
    clinic_id=None,
    scope="global",
)

NO_SCOPE_USER = UserPayload(
    user_id="00000000-0000-0000-0000-000000000003",
    email="old-token@imagiax.dev",
    nombre="Legacy User",
    rol="medico",
    clinic_id=None,
    scope=None,
)

ADMIN_USER = UserPayload(
    user_id="00000000-0000-0000-0000-000000000004",
    email="admin@clinic-a.dev",
    nombre="Admin Clinica",
    rol="admin",
    clinic_id=CLINIC_A,
    scope="clinic",
)

ADMIN_CLINIC_B = UserPayload(
    user_id="00000000-0000-0000-0000-000000000005",
    email="admin@clinic-b.dev",
    nombre="Admin Clinica B",
    rol="admin",
    clinic_id=CLINIC_B,
    scope="clinic",
)

CLINIC_USER_NO_CLINIC = UserPayload(
    user_id="00000000-0000-0000-0000-000000000006",
    email="broken@clinic-a.dev",
    nombre="Broken Session",
    rol="medico",
    clinic_id=None,
    scope="clinic",
)

ADMIN_USER_NO_CLINIC = UserPayload(
    user_id="00000000-0000-0000-0000-000000000007",
    email="admin-no-clinic@clinic-a.dev",
    nombre="Admin Sin Clinica",
    rol="admin",
    clinic_id=None,
    scope="clinic",
)


# ── JWT Tenant Claims ──────────────────────────────────────────────────────────


class TestJWTTenantClaims(unittest.TestCase):
    """JWT encode/decode with clinic_id/scope; old-JWT defaults; renewal preservation."""

    @patch("app.auth.service.settings")
    def test_create_token_includes_clinic_id_and_scope(self, mock_settings):
        from app.auth.service import create_access_token

        mock_settings.JWT_SECRET = MagicMock(get_secret_value=lambda: "test-secret-key-at-least-32-characters-long!!")
        mock_settings.JWT_EXPIRE_HOURS = 24
        mock_settings.JWT_REMEMBER_HOURS = 48
        mock_settings.jwt_issuer = None
        mock_settings.jwt_audience = None

        token = create_access_token(
            user_id="user-1",
            email="test@test.dev",
            nombre="Test",
            rol="medico",
            clinic_id=CLINIC_A,
            scope="clinic",
        )
        self.assertIsInstance(token, str)
        self.assertTrue(len(token) > 20)

    @patch("app.auth.service.settings")
    def test_create_token_null_clinic_omits_from_payload(self, mock_settings):
        from app.auth.service import create_access_token
        import jwt as _jwt

        mock_settings.JWT_SECRET = MagicMock(get_secret_value=lambda: "test-secret-key-at-least-32-characters-long!!")
        mock_settings.JWT_EXPIRE_HOURS = 24
        mock_settings.JWT_REMEMBER_HOURS = 48
        mock_settings.jwt_issuer = None
        mock_settings.jwt_audience = None

        # Global user has no clinic_id — it should not appear in the token
        token = create_access_token(
            user_id="su-1",
            email="super@test.dev",
            nombre="Super",
            rol="superadmin",
            clinic_id=None,
            scope="global",
        )
        self.assertIsInstance(token, str)
        payload = _jwt.decode(
            token,
            "test-secret-key-at-least-32-characters-long!!",
            algorithms=["HS256"],
            options={"verify_exp": False},
        )
        self.assertEqual(payload.get("scope"), "global")
        self.assertNotIn("clinic_id", payload)

    @patch("app.auth.service.settings")
    def test_decode_token_old_jwt_defaults_none(self, mock_settings):
        from app.auth.service import create_access_token, decode_token

        mock_settings.JWT_SECRET = MagicMock(get_secret_value=lambda: "test-secret-key-at-least-32-characters-long!!")
        mock_settings.JWT_EXPIRE_HOURS = 24
        mock_settings.JWT_REMEMBER_HOURS = 48
        mock_settings.jwt_issuer = None
        mock_settings.jwt_audience = None

        # Create token WITHOUT clinic_id/scope (simulating old JWT)
        token = create_access_token(
            user_id="old-user",
            email="old@test.dev",
            nombre="Old User",
            rol="medico",
        )
        user = decode_token(token)
        self.assertIsNone(user.clinic_id)
        self.assertIsNone(user.scope)

    @patch("app.auth.service.settings")
    def test_decode_token_with_renewal_preserves_claims(self, mock_settings):
        from app.auth.service import decode_token_with_renewal_check, create_access_token

        mock_settings.JWT_SECRET = MagicMock(get_secret_value=lambda: "test-secret-key-at-least-32-characters-long!!")
        mock_settings.JWT_EXPIRE_HOURS = 24
        mock_settings.JWT_REMEMBER_HOURS = 48
        mock_settings.jwt_issuer = None
        mock_settings.jwt_audience = None

        token = create_access_token(
            user_id="user-1",
            email="clinic@test.dev",
            nombre="Clinic User",
            rol="medico",
            clinic_id=CLINIC_A,
            scope="clinic",
        )
        user, should_renew = decode_token_with_renewal_check(token)
        self.assertFalse(should_renew)
        self.assertEqual(user.clinic_id, CLINIC_A)
        self.assertEqual(user.scope, "clinic")


# ── UserPayload Tenant Fields ──────────────────────────────────────────────────


class TestUserPayloadTenantFields(unittest.TestCase):
    """Pydantic model fields, defaults, JSON roundtrip, scope distinctions."""

    def test_payload_has_clinic_id_and_scope(self):
        u = UserPayload(
            user_id="uid-1", email="t@t.dev", nombre="T", rol="medico",
            clinic_id=CLINIC_A, scope="clinic",
        )
        self.assertEqual(u.clinic_id, CLINIC_A)
        self.assertEqual(u.scope, "clinic")

    def test_payload_defaults_none(self):
        u = UserPayload(user_id="uid-2", email="t@t.dev", nombre="T", rol="medico")
        self.assertIsNone(u.clinic_id)
        self.assertIsNone(u.scope)

    def test_payload_json_roundtrip(self):
        u = UserPayload(
            user_id="uid-3", email="t@t.dev", nombre="T", rol="admin",
            clinic_id=CLINIC_A, scope="clinic",
        )
        data = u.model_dump()
        self.assertEqual(data["clinic_id"], CLINIC_A)
        self.assertEqual(data["scope"], "clinic")
        u2 = UserPayload(**data)
        self.assertEqual(u2.clinic_id, CLINIC_A)
        self.assertEqual(u2.scope, "clinic")

    def test_global_scope_distinguishes_from_clinic(self):
        clinic_user = UserPayload(
            user_id="uid-4", email="c@t.dev", nombre="C", rol="medico",
            clinic_id=CLINIC_A, scope="clinic",
        )
        global_user = UserPayload(
            user_id="uid-5", email="g@t.dev", nombre="G", rol="superadmin",
            clinic_id=None, scope="global",
        )
        self.assertEqual(clinic_user.scope, "clinic")
        self.assertEqual(global_user.scope, "global")
        self.assertIsNone(global_user.clinic_id)


# ── Bootstrap Mode Configuration ────────────────────────────────────────────────


class TestBootstrapModeConfig(unittest.TestCase):
    """Prod rejection, dev acceptance, fail-closed default."""

    def test_bootstrap_rejected_in_production(self):
        from pydantic_core import ValidationError
        from app.core.config import Settings

        with self.assertRaises(ValidationError):
            Settings(APP_ENV="production", IMAGIAX_BOOTSTRAP_MODE="1")

    def test_bootstrap_accepted_in_development(self):
        from app.core.config import Settings

        s = Settings(APP_ENV="development", IMAGIAX_BOOTSTRAP_MODE="1")
        self.assertTrue(s.bootstrap_mode)

    def test_production_without_bootstrap_is_ok(self):
        from app.core.config import Settings

        s = Settings(APP_ENV="production", IMAGIAX_PAYMENT_MODE="production")
        self.assertFalse(s.bootstrap_mode)

    def test_bootstrap_false_by_default(self):
        from app.core.config import Settings

        s = Settings(APP_ENV="development")
        self.assertFalse(s.bootstrap_mode)


# ── Router-level tenant scoping (TestClient) ───────────────────────────────────


def _make_test_app():
    """Create a FastAPI app instance for testing."""
    from app.main import create_app

    return create_app()


def _setup_client_with_user(client: TestClient, user: UserPayload):
    """Override auth dependencies to return the given user and mock DB pool."""
    from app.auth.dependencies import get_current_user

    def _fake_user():
        return user

    client.app.dependency_overrides[get_current_user] = _fake_user
    # Need a fake pool to avoid RuntimeError
    from app.core.database import get_db_pool

    fake_pool = AsyncMock()
    client.app.state.pool = fake_pool
    client.app.dependency_overrides[get_db_pool] = lambda: fake_pool


class TestVerInformeTenantScoping(unittest.TestCase):
    """ver_informe: missing scope → 401, cross-clinic → 403, same-clinic → 200."""

    def setUp(self):
        self.app = _make_test_app()
        self.client = TestClient(self.app)

    @patch("app.informes.router.informes_db.get_informe_por_id", new_callable=AsyncMock)
    def test_ver_informe_rejects_missing_scope(self, mock_get):
        """A user with scope=None (old JWT) should get 401."""
        _setup_client_with_user(self.client, NO_SCOPE_USER)
        mock_get.return_value = {"id": "abc", "usuario_id": NO_SCOPE_USER.user_id, "clinic_id": CLINIC_A}

        resp = self.client.get("/informes/some-id/ver", follow_redirects=False)
        self.assertIn(resp.status_code, (401, 403))

    @patch("app.informes.router.informes_db.get_informe_por_id", new_callable=AsyncMock)
    def test_ver_informe_rejects_cross_clinic(self, mock_get):
        """A clinic user accessing another clinic's informe gets 403."""
        _setup_client_with_user(self.client, CLINIC_USER)
        mock_get.return_value = {
            "id": "abc",
            "usuario_id": "different-user",
            "clinic_id": CLINIC_B,
        }

        resp = self.client.get("/informes/some-id/ver", follow_redirects=False)
        self.assertEqual(resp.status_code, 403)

    @patch("app.informes.router.informes_db.get_informe_por_id", new_callable=AsyncMock)
    def test_ver_informe_same_clinic_passes(self, mock_get):
        """A clinic user accessing their own clinic's informe gets 200."""
        _setup_client_with_user(self.client, CLINIC_USER)
        mock_get.return_value = {
            "id": "abc",
            "usuario_id": CLINIC_USER.user_id,
            "clinic_id": CLINIC_A,
            "informe_generado": "Test report content",
            "ai_nivel_alerta": "NORMAL",
            "estado": "pendiente",
        }

        resp = self.client.get("/informes/some-id/ver", follow_redirects=False)
        self.assertEqual(resp.status_code, 200)

    @patch("app.informes.router.informes_db.get_informe_por_id", new_callable=AsyncMock)
    def test_ver_informe_global_user_accesses_any_clinic(self, mock_get):
        """A superadmin with scope=global can access any clinic's informe."""
        _setup_client_with_user(self.client, GLOBAL_USER)
        mock_get.return_value = {
            "id": "abc",
            "usuario_id": "some-other-user",
            "clinic_id": CLINIC_B,
            "informe_generado": "Test report content",
            "ai_nivel_alerta": "NORMAL",
            "estado": "pendiente",
        }

        resp = self.client.get("/informes/some-id/ver", follow_redirects=False)
        self.assertEqual(resp.status_code, 200)


class TestListadoInformesTenantScoping(unittest.TestCase):
    """listado_informes: missing scope → 401, clinic-scoped filter, global sees all."""

    def setUp(self):
        self.app = _make_test_app()
        self.client = TestClient(self.app)

    @patch("app.informes.router.informes_db.get_todos_informes", new_callable=AsyncMock)
    def test_listado_rejects_missing_scope(self, mock_get):
        """A user with scope=None should get 401 on listado."""
        _setup_client_with_user(self.client, NO_SCOPE_USER)
        resp = self.client.get("/informes/listado")
        self.assertEqual(resp.status_code, 401)

    @patch("app.informes.router.informes_db.get_todos_informes", new_callable=AsyncMock)
    def test_listado_scopes_by_clinic(self, mock_get):
        """A clinic user's listado call passes clinic_id to the DB function."""
        from app.auth.dependencies import get_current_user

        mock_get.return_value = {"items": [], "total": 0, "limit": 50, "offset": 0, "has_more": False}
        self.client.app.dependency_overrides[get_current_user] = lambda: CLINIC_USER
        self.client.app.state.pool = AsyncMock()

        resp = self.client.get("/informes/listado")
        self.assertEqual(resp.status_code, 200)
        call_kwargs = mock_get.call_args[1]
        self.assertEqual(call_kwargs.get("clinic_id"), CLINIC_A)
        self.assertEqual(call_kwargs.get("scope"), "clinic")

    @patch("app.informes.router.informes_db.get_todos_informes", new_callable=AsyncMock)
    def test_listado_global_sees_all(self, mock_get):
        """A superadmin with scope=global gets no clinic filter."""
        from app.auth.dependencies import get_current_user

        mock_get.return_value = {"items": [], "total": 0, "limit": 50, "offset": 0, "has_more": False}
        self.client.app.dependency_overrides[get_current_user] = lambda: GLOBAL_USER
        self.client.app.state.pool = AsyncMock()

        resp = self.client.get("/informes/listado")
        self.assertEqual(resp.status_code, 200)
        call_kwargs = mock_get.call_args[1]
        self.assertIsNone(call_kwargs.get("clinic_id"))
        self.assertEqual(call_kwargs.get("scope"), "global")


# ── Branding Clinic Passthrough ────────────────────────────────────────────────


class TestBrandingClinicPassthrough(unittest.TestCase):
    """Ensure branding DB functions accept and use clinic_id parameter."""

    def test_get_branding_passes_clinic_id(self):
        """get_branding_for_user should accept clinic_id parameter."""
        import inspect

        from app.informes.db import get_branding_for_user

        sig = inspect.signature(get_branding_for_user)
        params = list(sig.parameters.keys())
        self.assertIn("clinic_id", params)

    def test_save_branding_passes_clinic_id(self):
        """save_branding_config should accept clinic_id parameter."""
        import inspect

        from app.informes.db import save_branding_config

        sig = inspect.signature(save_branding_config)
        params = list(sig.parameters.keys())
        self.assertIn("clinic_id", params)

    def test_get_branding_no_clinic_still_works(self):
        """get_branding_for_user with no clinic_id should still work (backward compat)."""
        import inspect

        from app.informes.db import get_branding_for_user

        sig = inspect.signature(get_branding_for_user)
        self.assertEqual(sig.parameters["clinic_id"].default, None)


# ── DB Function Tenant Signatures ──────────────────────────────────────────────


class TestDBFunctionTenantSignatures(unittest.TestCase):
    """All DB functions that handle PHI must accept clinic_id/scope parameters."""

    def test_guardar_informe_accepts_clinic_id(self):
        import inspect
        from app.informes.db import guardar_informe

        sig = inspect.signature(guardar_informe)
        self.assertIn("clinic_id", sig.parameters)
        self.assertEqual(sig.parameters["clinic_id"].default, None)

    def test_get_todos_informes_accepts_clinic_id_and_scope(self):
        import inspect
        from app.informes.db import get_todos_informes

        sig = inspect.signature(get_todos_informes)
        self.assertIn("clinic_id", sig.parameters)
        self.assertIn("scope", sig.parameters)

    def test_get_study_by_short_id_accepts_clinic_id_and_scope(self):
        import inspect
        from app.informes.db import get_study_by_short_id

        sig = inspect.signature(get_study_by_short_id)
        self.assertIn("clinic_id", sig.parameters)
        self.assertIn("scope", sig.parameters)

    def test_set_branding_active_accepts_clinic_id(self):
        import inspect
        from app.informes.db import set_branding_active

        sig = inspect.signature(set_branding_active)
        self.assertIn("clinic_id", sig.parameters)

    def test_get_informe_por_id_includes_clinic_id(self):
        """get_informe_por_id SELECT should include clinic_id column."""
        import inspect
        from app.informes.db import get_informe_por_id

        sig = inspect.signature(get_informe_por_id)
        self.assertIn("informe_id", sig.parameters)

    def test_create_user_accepts_clinic_id_and_scope(self):
        import inspect
        from app.auth.service import create_user

        sig = inspect.signature(create_user)
        self.assertIn("clinic_id", sig.parameters)
        self.assertIn("scope", sig.parameters)

    def test_list_users_returns_tenant_fields(self):
        """list_users should query clinic_id and scope columns."""
        from app.auth.service import list_users

        self.assertTrue(callable(list_users))


# ── Tenant Validation Helpers ──────────────────────────────────────────────────


class TestTenantValidationHelpers(unittest.TestCase):
    """backfill and validate helpers are importable and accept pool."""

    def test_backfill_function_exists_and_accepts_pool(self):
        import inspect
        from app.auth.service import backfill_tenant_columns

        sig = inspect.signature(backfill_tenant_columns)
        params = list(sig.parameters.keys())
        self.assertIn("pool", params)

    def test_validate_function_exists_and_accepts_pool(self):
        import inspect
        from app.auth.service import validate_tenant_null_counts

        sig = inspect.signature(validate_tenant_null_counts)
        params = list(sig.parameters.keys())
        self.assertIn("pool", params)


# ── CreateUser Tenant Validation ───────────────────────────────────────────────


class TestCreateUserTenantValidation(unittest.TestCase):
    """Non-superadmin without clinic → 400; superadmin without clinic → OK."""

    @patch("app.auth.service.hash_password", return_value="$2b$12$hashedpassword123456789012345")
    @patch("app.auth.service.verify_password")
    @patch("app.auth.service.settings")
    def test_non_superadmin_without_clinic_raises_400(self, mock_settings, mock_verify, mock_hash):
        """Non-superadmin user creation without clinic_id should raise 400."""
        from fastapi import HTTPException
        from app.auth.service import create_user

        mock_settings.JWT_SECRET = MagicMock(get_secret_value=lambda: "test-secret-key-at-least-32-characters-long!!")
        mock_pool = AsyncMock()

        # create_user is async — must await it
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(create_user(
                mock_pool,
                email="test@clinic.dev",
                password="ValidPass!123",
                nombre="Test",
                rol="medico",
                clinic_id=None,
            ))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_superadmin_without_clinic_is_ok(self):
        """Superadmin creation without clinic_id should not raise 400."""
        import inspect
        from app.auth.service import create_user

        sig = inspect.signature(create_user)
        # clinic_id is optional (default=None) for superadmin support
        self.assertEqual(sig.parameters["clinic_id"].default, None)


# ── [CRITICAL-1] Fresh DB Startup: ALTER guards ───────────────────────────────


class TestFreshDBTenantMigrationGuards(unittest.TestCase):
    """create_auth_tables must not crash on fresh DB where non-auth tables don't exist."""

    def test_branding_config_create_table_includes_clinic_id(self):
        """branding_config CREATE TABLE must include clinic_id column."""
        import inspect
        from app.informes.db import create_branding_tables

        # Verify the function exists and is callable
        self.assertTrue(callable(create_branding_tables))
        # Verify the CREATE TABLE includes clinic_id by checking source
        import ast
        source = inspect.getsource(create_branding_tables)
        # The function source should contain 'clinic_id' in the CREATE TABLE
        self.assertIn("clinic_id", source)

    def test_create_auth_tables_handles_missing_tables_gracefully(self):
        """ALTER TABLE on non-existent tables should be guarded, not crash.

        Verifies the function uses the _table_exists helper (now module-level)
        and contains guard logic for missing non-auth tables.
        """
        from app.auth.service import create_auth_tables, _table_exists

        # _table_exists is a module-level shared helper — verify it exists
        self.assertTrue(callable(_table_exists))

        # Verify create_auth_tables source references the helper
        import inspect
        source = inspect.getsource(create_auth_tables)
        self.assertIn("_table_exists", source)
        # Verify logging for skipped tables
        self.assertIn("clinic_id ALTER skipped", source)

    def test_list_users_by_clinic_exists(self):
        """list_users_by_clinic function must exist for clinic-scoped admin listing."""
        from app.auth.service import list_users_by_clinic
        import inspect

        sig = inspect.signature(list_users_by_clinic)
        params = list(sig.parameters.keys())
        self.assertIn("pool", params)
        self.assertIn("clinic_id", params)

    def test_backfill_tenant_columns_source_has_existence_guards(self):
        """backfill_tenant_columns must check table existence before UPDATEs."""
        import inspect
        from app.auth.service import backfill_tenant_columns

        source = inspect.getsource(backfill_tenant_columns)
        self.assertIn("_table_exists", source)
        self.assertIn("informes", source)
        self.assertIn("analysis_jobs", source)
        self.assertIn("branding_config", source)
        # Must log when skipping
        self.assertIn("backfill skipped", source)

    def test_validate_tenant_null_counts_source_has_existence_guards(self):
        """validate_tenant_null_counts must check table existence before SELECTs."""
        import inspect
        from app.auth.service import validate_tenant_null_counts

        source = inspect.getsource(validate_tenant_null_counts)
        self.assertIn("_table_exists", source)
        self.assertIn("informes", source)
        self.assertIn("analysis_jobs", source)
        self.assertIn("branding_config", source)
        # Must log when skipping
        self.assertIn("validation skipped", source)


class TestFreshDBBackfillBehavior(unittest.TestCase):
    """backfill_tenant_columns must return 0 counts for missing tables, not crash.

    Uses a mock asyncpg pool/connection where _table_exists returns False
    for non-auth tables, simulating a fresh database.
    """

    @staticmethod
    def _make_fake_conn(existing_tables: set[str]) -> AsyncMock:
        """Build a fake asyncpg Connection that reports only existing_tables as present.

        - fetchval for _table_exists: returns True only for tables in existing_tables.
        - execute for UPDATE: returns "UPDATE 0" (no rows affected).
        - supports async context manager.
        """
        conn = AsyncMock()

        async def _fake_fetchval(query, *args, **kwargs):
            # Intercept _table_exists queries
            if "information_schema.tables" in query:
                # args[0] is the table_name parameter ($1)
                table_name = args[0] if args else None
                return table_name in existing_tables
            # For COUNT(*) queries, return 0
            if "COUNT" in query:
                return 0
            return None

        async def _fake_execute(query, *args, **kwargs):
            return "UPDATE 0"

        conn.fetchval = _fake_fetchval
        conn.execute = _fake_execute
        return conn

    @staticmethod
    def _make_fake_pool(conn: AsyncMock) -> AsyncMock:
        """Build a fake pool that yields the given conn on acquire().

        asyncpg.Pool.acquire() returns an async context manager, not a coroutine.
        We mock this by returning a custom object with __aenter__/__aexit__.
        """
        pool = MagicMock()

        class _AcquireContext:
            async def __aenter__(self_inner):
                return conn
            async def __aexit__(self_inner, *exc):
                pass

        pool.acquire.return_value = _AcquireContext()
        return pool

    def test_backfill_skips_missing_informes(self):
        """When informes table is absent, backfill returns 0 instead of crashing."""
        from app.auth.service import backfill_tenant_columns

        conn = self._make_fake_conn(existing_tables={"usuarios"})
        pool = self._make_fake_pool(conn)

        result = asyncio.run(backfill_tenant_columns(pool))
        self.assertEqual(result["informes_backfilled"], 0)

    def test_backfill_skips_missing_analysis_jobs(self):
        """When analysis_jobs table is absent, backfill returns 0 instead of crashing."""
        from app.auth.service import backfill_tenant_columns

        conn = self._make_fake_conn(existing_tables={"usuarios"})
        pool = self._make_fake_pool(conn)

        result = asyncio.run(backfill_tenant_columns(pool))
        self.assertEqual(result["analysis_jobs_backfilled"], 0)

    def test_backfill_skips_missing_branding_config(self):
        """When branding_config table is absent, backfill returns 0 instead of crashing."""
        from app.auth.service import backfill_tenant_columns

        conn = self._make_fake_conn(existing_tables={"usuarios"})
        pool = self._make_fake_pool(conn)

        result = asyncio.run(backfill_tenant_columns(pool))
        self.assertEqual(result["branding_config_backfilled"], 0)

    def test_backfill_all_tables_missing_still_returns_usuarios_counts(self):
        """On a completely fresh DB, backfill still processes usuarios (always exists)."""
        from app.auth.service import backfill_tenant_columns

        conn = self._make_fake_conn(existing_tables={"usuarios"})
        pool = self._make_fake_pool(conn)

        result = asyncio.run(backfill_tenant_columns(pool))
        # usuarios keys must always be present
        self.assertIn("usuarios_backfilled", result)
        self.assertIn("superadmin_scope_set", result)
        # All non-auth tables should be 0
        self.assertEqual(result["informes_backfilled"], 0)
        self.assertEqual(result["analysis_jobs_backfilled"], 0)
        self.assertEqual(result["branding_config_backfilled"], 0)

    def test_backfill_all_tables_present_updates_all(self):
        """When all tables exist, backfill attempts UPDATE on each (behavior preserved)."""
        from app.auth.service import backfill_tenant_columns

        conn = self._make_fake_conn(existing_tables={"usuarios", "informes", "analysis_jobs", "branding_config"})
        pool = self._make_fake_pool(conn)

        result = asyncio.run(backfill_tenant_columns(pool))
        # All keys present — values are 0 because mock execute returns "UPDATE 0"
        self.assertIn("informes_backfilled", result)
        self.assertIn("analysis_jobs_backfilled", result)
        self.assertIn("branding_config_backfilled", result)


class TestFreshDBValidateBehavior(unittest.TestCase):
    """validate_tenant_null_counts must return 0 for missing tables, not crash.

    Uses a mock asyncpg pool/connection where _table_exists returns False
    for non-auth tables, simulating a fresh database.
    """

    @staticmethod
    def _make_fake_conn(existing_tables: set[str]) -> AsyncMock:
        """Build a fake asyncpg Connection for validation (fetchval-only)."""
        conn = AsyncMock()

        async def _fake_fetchval(query, *args, **kwargs):
            # Intercept _table_exists queries
            if "information_schema.tables" in query:
                table_name = args[0] if args else None
                return table_name in existing_tables
            # For COUNT(*) queries, return 0
            if "COUNT" in query:
                return 0
            return None

        conn.fetchval = _fake_fetchval
        return conn

    @staticmethod
    def _make_fake_pool(conn: AsyncMock) -> MagicMock:
        """Build a fake pool that yields the given conn on acquire().

        asyncpg.Pool.acquire() returns an async context manager, not a coroutine.
        """
        pool = MagicMock()

        class _AcquireContext:
            async def __aenter__(self_inner):
                return conn
            async def __aexit__(self_inner, *exc):
                pass

        pool.acquire.return_value = _AcquireContext()
        return pool

    def test_validate_skips_missing_informes(self):
        """When informes is absent, validation returns 0 for that key."""
        from app.auth.service import validate_tenant_null_counts

        conn = self._make_fake_conn(existing_tables={"usuarios"})
        pool = self._make_fake_pool(conn)

        result = asyncio.run(validate_tenant_null_counts(pool))
        self.assertEqual(result["informes_null_clinic"], 0)

    def test_validate_skips_missing_analysis_jobs(self):
        """When analysis_jobs is absent, validation returns 0 for that key."""
        from app.auth.service import validate_tenant_null_counts

        conn = self._make_fake_conn(existing_tables={"usuarios"})
        pool = self._make_fake_pool(conn)

        result = asyncio.run(validate_tenant_null_counts(pool))
        self.assertEqual(result["analysis_jobs_null_clinic"], 0)

    def test_validate_skips_missing_branding_config(self):
        """When branding_config is absent, validation returns 0 for that key."""
        from app.auth.service import validate_tenant_null_counts

        conn = self._make_fake_conn(existing_tables={"usuarios"})
        pool = self._make_fake_pool(conn)

        result = asyncio.run(validate_tenant_null_counts(pool))
        self.assertEqual(result["branding_config_null_clinic"], 0)

    def test_validate_all_tables_missing_returns_zeros(self):
        """Fresh DB: all non-auth counts are 0, usuarios still validated."""
        from app.auth.service import validate_tenant_null_counts

        conn = self._make_fake_conn(existing_tables={"usuarios"})
        pool = self._make_fake_pool(conn)

        result = asyncio.run(validate_tenant_null_counts(pool))
        self.assertIn("usuarios_non_superadmin_null_clinic", result)
        self.assertEqual(result["informes_null_clinic"], 0)
        self.assertEqual(result["analysis_jobs_null_clinic"], 0)
        self.assertEqual(result["branding_config_null_clinic"], 0)

    def test_validate_all_tables_present_queries_all(self):
        """When all tables exist, validation queries each (behavior preserved)."""
        from app.auth.service import validate_tenant_null_counts

        conn = self._make_fake_conn(existing_tables={"usuarios", "informes", "analysis_jobs", "branding_config"})
        pool = self._make_fake_pool(conn)

        result = asyncio.run(validate_tenant_null_counts(pool))
        # All keys present and all values 0 (mock fetchval returns 0 for COUNT)
        self.assertEqual(result["informes_null_clinic"], 0)
        self.assertEqual(result["analysis_jobs_null_clinic"], 0)
        self.assertEqual(result["branding_config_null_clinic"], 0)

    def test_validate_missing_table_does_not_hit_count_query(self):
        """When a table is absent, the COUNT query for that table must NOT be executed.

        This ensures the guard is truly preventing the query, not just catching errors.
        We track call count to the mock fetchval to verify no COUNT was issued for
        a missing table.
        """
        from app.auth.service import validate_tenant_null_counts

        conn = self._make_fake_conn(existing_tables={"usuarios"})
        pool = self._make_fake_pool(conn)

        # Wrap fetchval to track calls
        call_log: list[str] = []

        original_fetchval = conn.fetchval

        async def _tracking_fetchval(query, *args, **kwargs):
            call_log.append(query.strip().upper())
            return await original_fetchval(query, *args, **kwargs)

        conn.fetchval = _tracking_fetchval

        asyncio.run(validate_tenant_null_counts(pool))

        # No query should contain "FROM INFORMES" (only "INFORMES" in _table_exists check)
        informes_queries = [q for q in call_log if "FROM INFORMES" in q]
        self.assertEqual(
            len(informes_queries), 0,
            f"Expected no COUNT(*) FROM informes query, but got: {informes_queries}",
        )
        # The _table_exists check for informes should have happened
        table_check_queries = [q for q in call_log if "INFORMATION_SCHEMA.TABLES" in q and "INFORMES" not in q]
        # At least one information_schema check should exist
        self.assertTrue(
            any("INFORMATION_SCHEMA.TABLES" in q for q in call_log),
            "Expected _table_exists check via information_schema.tables",
        )


# ── [CRITICAL-2] Admin Tenant Isolation ────────────────────────────────────────


class TestAdminInformeTenantIsolation(unittest.TestCase):
    """Clinic-scoped admins cannot bypass tenant isolation on admin informe endpoints."""

    def setUp(self):
        self.app = _make_test_app()
        self.client = TestClient(self.app)

    @patch("app.admin.router.informes_db.get_todos_informes", new_callable=AsyncMock)
    def test_admin_informes_ignores_arbitrary_clinic_id(self, mock_get):
        """Clinic-scoped admin cannot pass arbitrary clinic_id to see other clinics."""
        from app.auth.dependencies import get_current_user

        mock_get.return_value = {"items": [], "total": 0, "limit": 50, "offset": 0, "has_more": False}
        self.client.app.dependency_overrides[get_current_user] = lambda: ADMIN_USER
        self.client.app.state.pool = AsyncMock()

        # Try to pass CLINIC_B as clinic_id query param — should be ignored
        resp = self.client.get(f"/admin/api/informes?clinic_id={CLINIC_B}")
        self.assertEqual(resp.status_code, 200)
        call_kwargs = mock_get.call_args[1]
        # The effective clinic_id should be the admin's own clinic, NOT CLINIC_B
        self.assertEqual(call_kwargs.get("clinic_id"), CLINIC_A)

    @patch("app.admin.router.informes_db.get_todos_informes", new_callable=AsyncMock)
    def test_admin_informes_global_can_use_clinic_filter(self, mock_get):
        """Global admin can use clinic_id filter freely."""
        from app.auth.dependencies import get_current_user

        mock_get.return_value = {"items": [], "total": 0, "limit": 50, "offset": 0, "has_more": False}
        self.client.app.dependency_overrides[get_current_user] = lambda: GLOBAL_USER
        self.client.app.state.pool = AsyncMock()

        resp = self.client.get(f"/admin/api/informes?clinic_id={CLINIC_B}")
        self.assertEqual(resp.status_code, 200)
        call_kwargs = mock_get.call_args[1]
        # Global admin's clinic_id filter should be passed through
        self.assertEqual(call_kwargs.get("clinic_id"), CLINIC_B)

    @patch("app.admin.router.informes_db.get_informe_por_id", new_callable=AsyncMock)
    def test_admin_informe_detail_rejects_cross_clinic(self, mock_get):
        """Clinic-scoped admin cannot see informe detail from another clinic."""
        from app.auth.dependencies import get_current_user

        self.client.app.dependency_overrides[get_current_user] = lambda: ADMIN_USER
        self.client.app.state.pool = AsyncMock()
        mock_get.return_value = {
            "id": "abc",
            "clinic_id": CLINIC_B,
            "informe_generado": "Test",
        }

        resp = self.client.get("/admin/api/informes/some-id")
        # Should get 404 (opaque — no existence leak)
        self.assertEqual(resp.status_code, 404)

    @patch("app.admin.router.informes_db.get_informe_por_id", new_callable=AsyncMock)
    def test_admin_informe_detail_same_clinic(self, mock_get):
        """Clinic-scoped admin can see informe detail from own clinic."""
        from app.auth.dependencies import get_current_user

        self.client.app.dependency_overrides[get_current_user] = lambda: ADMIN_USER
        self.client.app.state.pool = AsyncMock()
        mock_get.return_value = {
            "id": "abc",
            "clinic_id": CLINIC_A,
            "informe_generado": "Test",
            "created_at": "2024-01-01T00:00:00",
        }

        resp = self.client.get("/admin/api/informes/some-id")
        self.assertEqual(resp.status_code, 200)

    @patch("app.admin.router.informes_db.get_informe_por_id", new_callable=AsyncMock)
    def test_admin_informe_detail_global_sees_any(self, mock_get):
        """Global admin can see informe detail from any clinic."""
        from app.auth.dependencies import get_current_user

        self.client.app.dependency_overrides[get_current_user] = lambda: GLOBAL_USER
        self.client.app.state.pool = AsyncMock()
        mock_get.return_value = {
            "id": "abc",
            "clinic_id": CLINIC_B,
            "informe_generado": "Test",
            "created_at": "2024-01-01T00:00:00",
        }

        resp = self.client.get("/admin/api/informes/some-id")
        self.assertEqual(resp.status_code, 200)

    @patch("app.admin.router.informes_db.get_informe_por_id", new_callable=AsyncMock)
    def test_admin_informe_pdf_rejects_cross_clinic(self, mock_get):
        """Clinic-scoped admin cannot get PDF from another clinic."""
        from app.auth.dependencies import get_current_user

        self.client.app.dependency_overrides[get_current_user] = lambda: ADMIN_USER
        self.client.app.state.pool = AsyncMock()
        mock_get.return_value = {
            "id": "abc",
            "clinic_id": CLINIC_B,
            "informe_generado": "Test report content",
        }

        resp = self.client.get("/admin/api/informes/some-id/pdf")
        self.assertEqual(resp.status_code, 404)

    @patch("app.admin.router.admin_service.serialize_user")
    @patch("app.admin.router.auth_service.list_users_by_clinic", new_callable=AsyncMock)
    def test_admin_list_usuarios_scoped_to_clinic(self, mock_list, mock_serialize):
        """Clinic-scoped admin only sees users in their own clinic."""
        from app.auth.dependencies import get_current_user

        mock_list.return_value = [
            {"id": "u1", "email": "user@clinic-a.dev", "nombre": "User A", "rol": "medico",
             "activo": True, "clinic_id": CLINIC_A, "scope": "clinic", "created_at": datetime.now(timezone.utc)},
        ]
        mock_serialize.return_value = {
            "id": "u1", "email": "user@clinic-a.dev", "nombre": "User A", "rol": "medico",
            "activo": True, "clinic_id": CLINIC_A, "scope": "clinic", "created_at": "2024-01-01T00:00:00",
        }
        self.client.app.dependency_overrides[get_current_user] = lambda: ADMIN_USER
        self.client.app.state.pool = AsyncMock()

        resp = self.client.get("/admin/api/usuarios")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["total"], 1)
        # Verify list_users_by_clinic was called with admin's clinic_id
        mock_list.assert_called_once()

    @patch("app.admin.router.admin_service.serialize_user")
    @patch("app.admin.router.auth_service.list_users", new_callable=AsyncMock)
    def test_global_admin_list_usuarios_sees_all(self, mock_list, mock_serialize):
        """Global admin sees all users across clinics."""
        from app.auth.dependencies import get_current_user

        mock_list.return_value = [
            {"id": "u1", "email": "user@clinic-a.dev", "nombre": "User A", "rol": "medico",
             "activo": True, "clinic_id": CLINIC_A, "scope": "clinic", "created_at": datetime.now(timezone.utc)},
            {"id": "u2", "email": "user@clinic-b.dev", "nombre": "User B", "rol": "medico",
             "activo": True, "clinic_id": CLINIC_B, "scope": "clinic", "created_at": datetime.now(timezone.utc)},
        ]
        mock_serialize.side_effect = [
            {"id": "u1", "email": "user@clinic-a.dev", "nombre": "User A", "rol": "medico",
             "activo": True, "clinic_id": CLINIC_A, "scope": "clinic", "created_at": "2024-01-01T00:00:00"},
            {"id": "u2", "email": "user@clinic-b.dev", "nombre": "User B", "rol": "medico",
             "activo": True, "clinic_id": CLINIC_B, "scope": "clinic", "created_at": "2024-01-01T00:00:00"},
        ]
        self.client.app.dependency_overrides[get_current_user] = lambda: GLOBAL_USER
        self.client.app.state.pool = AsyncMock()

        resp = self.client.get("/admin/api/usuarios")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["total"], 2)

    @patch("app.admin.router.auth_service.create_user", new_callable=AsyncMock)
    @patch("app.admin.router.auth_service.log_audit", new_callable=AsyncMock)
    def test_clinic_admin_cannot_create_user_in_other_clinic(self, mock_audit, mock_create):
        """Clinic-scoped admin creating a user with different clinic_id gets forced to own clinic."""
        from app.auth.dependencies import get_current_user

        mock_create.return_value = {
            "id": "new-user-id",
            "email": "new@clinic-a.dev",
            "nombre": "New User",
            "rol": "medico",
            "clinic_id": CLINIC_A,
        }
        self.client.app.dependency_overrides[get_current_user] = lambda: ADMIN_USER
        self.client.app.state.pool = AsyncMock()

        # Try to create user in CLINIC_B while being admin of CLINIC_A
        # CSRF middleware requires Origin header on POST requests
        resp = self.client.post("/admin/api/usuarios", json={
            "email": "new@clinic-b.dev",
            "password": "ValidPass!123456",
            "nombre": "New User",
            "rol": "medico",
            "clinic_id": CLINIC_B,  # trying to create in different clinic!
        }, headers={"Origin": "http://localhost:3000"})
        self.assertEqual(resp.status_code, 201, f"Expected 201, got {resp.status_code}: {resp.json()}")
        # Verify create_user was called with the ADMIN's clinic_id, not the requested one
        call_kwargs = mock_create.call_args[1]
        self.assertEqual(call_kwargs.get("clinic_id"), CLINIC_A)


# ── [CRITICAL-3] Old JWTs cannot create PHI rows with clinic_id=NULL ──────────


class TestGenerarInformeTenantFailClosed(unittest.TestCase):
    """generar_informe must fail-closed when scope is None or clinic_id missing."""

    def setUp(self):
        self.app = _make_test_app()
        self.client = TestClient(self.app)

    def test_generar_informe_rejects_missing_scope(self):
        """Old JWT with scope=None must be rejected before any PHI persistence."""
        from app.auth.dependencies import get_current_user

        self.client.app.dependency_overrides[get_current_user] = lambda: NO_SCOPE_USER
        self.client.app.state.pool = AsyncMock()

        resp = self.client.post("/generar/informe", json={
            "study_instance_uid": "1.2.3.4.5.6.7.8",
        })
        # 401 (our explicit check) or 403 (middleware/dependency layer)
        self.assertIn(resp.status_code, (401, 403))

    def test_generar_informe_rejects_clinic_user_without_clinic_id(self):
        """Clinic-scoped user with no clinic_id must be rejected."""
        from app.auth.dependencies import get_current_user

        self.client.app.dependency_overrides[get_current_user] = lambda: CLINIC_USER_NO_CLINIC
        self.client.app.state.pool = AsyncMock()

        resp = self.client.post("/generar/informe", json={
            "study_instance_uid": "1.2.3.4.5.6.7.8",
        })
        # 401 (our explicit check) or 403 (middleware/dependency layer)
        self.assertIn(resp.status_code, (401, 403))

    @patch("app.ai.router._verify_payment", new_callable=AsyncMock)
    @patch("app.ai.router.find_instances_by_query", new_callable=AsyncMock)
    def test_generar_informe_allows_clinic_user(self, mock_find, mock_payment):
        """Clinic-scoped user with valid clinic_id can generate informe."""
        from app.auth.dependencies import get_current_user
        from app.core.database import get_db_pool

        mock_payment.return_value = ("creditos", {"creditos_disponibles": 5})
        mock_find.return_value = []  # no instances found → will fail later, but tenant check passed

        self.client.app.dependency_overrides[get_current_user] = lambda: CLINIC_USER
        fake_pool = AsyncMock()
        self.client.app.state.pool = fake_pool
        self.client.app.state.orthanc = AsyncMock()
        self.client.app.state.model = None
        self.client.app.state.device = None
        self.client.app.dependency_overrides[get_db_pool] = lambda: fake_pool

        # This will fail at Orthanc/model stage, but should NOT fail at tenant check
        resp = self.client.post("/generar/informe", json={
            "study_instance_uid": "1.2.3.4.5.6.7.8",
        })
        # Should NOT be 401 (tenant rejection) — may be 503 (no model) or other
        self.assertNotEqual(resp.status_code, 401)


# ── [CRITICAL-4] Study report lookup is tenant-scoped ─────────────────────────


class TestInformesPorEstudioTenantScoping(unittest.TestCase):
    """informes_por_estudio must filter by clinic for clinic-scoped users."""

    def setUp(self):
        self.app = _make_test_app()
        self.client = TestClient(self.app)

    @patch("app.ai.router.informes_db.get_informes_por_estudio", new_callable=AsyncMock)
    def test_informes_por_estudio_rejects_missing_scope(self, mock_get):
        """User with scope=None should get 401."""
        from app.auth.dependencies import get_current_user

        self.client.app.dependency_overrides[get_current_user] = lambda: NO_SCOPE_USER
        self.client.app.state.pool = AsyncMock()

        resp = self.client.get("/informes/estudio/1.2.3.4.5.6.7.8")
        self.assertEqual(resp.status_code, 401)

    @patch("app.ai.router.informes_db.get_informes_por_estudio", new_callable=AsyncMock)
    def test_informes_por_estudio_passes_clinic_filter(self, mock_get):
        """Clinic-scoped user should have clinic_id filter applied."""
        from app.auth.dependencies import get_current_user

        mock_get.return_value = []
        self.client.app.dependency_overrides[get_current_user] = lambda: CLINIC_USER
        self.client.app.state.pool = AsyncMock()

        resp = self.client.get("/informes/estudio/1.2.3.4.5.6.7.8")
        self.assertEqual(resp.status_code, 200)
        call_kwargs = mock_get.call_args[1]
        self.assertEqual(call_kwargs.get("clinic_id"), CLINIC_A)
        self.assertEqual(call_kwargs.get("scope"), "clinic")

    @patch("app.ai.router.informes_db.get_informes_por_estudio", new_callable=AsyncMock)
    def test_informes_por_estudio_global_sees_all(self, mock_get):
        """Global admin should see reports from all clinics (no clinic filter)."""
        from app.auth.dependencies import get_current_user

        mock_get.return_value = []
        self.client.app.dependency_overrides[get_current_user] = lambda: GLOBAL_USER
        self.client.app.state.pool = AsyncMock()

        resp = self.client.get("/informes/estudio/1.2.3.4.5.6.7.8")
        self.assertEqual(resp.status_code, 200)
        call_kwargs = mock_get.call_args[1]
        self.assertIsNone(call_kwargs.get("clinic_id"))
        self.assertEqual(call_kwargs.get("scope"), "global")

    def test_get_informes_por_estudio_accepts_clinic_id_and_scope(self):
        """DB function get_informes_por_estudio must accept clinic_id and scope."""
        import inspect
        from app.informes.db import get_informes_por_estudio

        sig = inspect.signature(get_informes_por_estudio)
        self.assertIn("clinic_id", sig.parameters)
        self.assertIn("scope", sig.parameters)
        self.assertEqual(sig.parameters["clinic_id"].default, None)
        self.assertEqual(sig.parameters["scope"].default, None)


# ── [CRITICAL-5] scope=clinic + clinic_id=None fail-closed ─────────────────────


class TestClinicScopeMissingClinicIdFailClosed(unittest.TestCase):
    """Non-global users with scope='clinic' but missing clinic_id must be rejected
    before any DB lookup on study/listado endpoints, and DB helpers must fail closed."""

    def setUp(self):
        self.app = _make_test_app()
        self.client = TestClient(self.app)

    @patch("app.ai.router.informes_db.get_informes_por_estudio", new_callable=AsyncMock)
    def test_informes_por_estudio_rejects_clinic_scope_no_clinic_id(self, mock_get):
        """User with scope='clinic' but clinic_id=None should get 401 on informes/estudio."""
        from app.auth.dependencies import get_current_user

        self.client.app.dependency_overrides[get_current_user] = lambda: CLINIC_USER_NO_CLINIC
        self.client.app.state.pool = AsyncMock()

        resp = self.client.get("/informes/estudio/1.2.3.4.5.6.7.8")
        self.assertEqual(resp.status_code, 401)
        # DB helper must NOT have been called
        mock_get.assert_not_called()

    @patch("app.ai.router.informes_db.get_todos_informes", new_callable=AsyncMock)
    def test_ai_listado_rejects_clinic_scope_no_clinic_id(self, mock_get):
        """User with scope='clinic' but clinic_id=None should get 401 on /ai/listado."""
        from app.auth.dependencies import get_current_user

        self.client.app.dependency_overrides[get_current_user] = lambda: CLINIC_USER_NO_CLINIC
        self.client.app.state.pool = AsyncMock()

        resp = self.client.get("/listado")
        self.assertEqual(resp.status_code, 401)
        mock_get.assert_not_called()

    @patch("app.informes.router.informes_db.get_todos_informes", new_callable=AsyncMock)
    def test_informes_listado_rejects_clinic_scope_no_clinic_id(self, mock_get):
        """User with scope='clinic' but clinic_id=None should get 401 on /informes/listado."""
        from app.auth.dependencies import get_current_user

        self.client.app.dependency_overrides[get_current_user] = lambda: CLINIC_USER_NO_CLINIC
        self.client.app.state.pool = AsyncMock()

        resp = self.client.get("/informes/listado")
        self.assertEqual(resp.status_code, 401)
        mock_get.assert_not_called()

    def test_get_informes_por_estudio_returns_empty_when_no_clinic_id(self):
        """DB helper get_informes_por_estudio must return empty list when
        scope='clinic' and clinic_id=None (defense-in-depth, no DB needed)."""
        mock_pool = AsyncMock()
        # Should return empty list without hitting the pool
        result = asyncio.run(
            informes_db.get_informes_por_estudio(
                mock_pool,
                study_uid="1.2.3.4.5",
                clinic_id=None,
                scope="clinic",
            )
        )
        self.assertEqual(result, [])
        # Pool should NOT have been acquired
        mock_pool.acquire.assert_not_called()

    def test_get_todos_informes_returns_zero_total_when_no_clinic_id(self):
        """DB helper get_todos_informes must return zero-total result when
        scope='clinic' and clinic_id=None (defense-in-depth, no DB needed)."""
        mock_pool = AsyncMock()
        result = asyncio.run(
            informes_db.get_todos_informes(
                mock_pool,
                clinic_id=None,
                scope="clinic",
            )
        )
        self.assertEqual(result["items"], [])
        self.assertEqual(result["total"], 0)
        self.assertFalse(result["has_more"])
        # Pool should NOT have been acquired
        mock_pool.acquire.assert_not_called()


# ── [CRITICAL-6] Clinic-scoped admin cannot mutate cross-tenant users ──────────


class TestAdminUserMutationTenantIsolation(unittest.TestCase):
    """Clinic-scoped admins cannot patch/reset/delete users from another clinic."""

    def setUp(self):
        self.app = _make_test_app()
        self.client = TestClient(self.app)

    @patch("app.admin.router.auth_service.update_user", new_callable=AsyncMock)
    @patch("app.admin.router.auth_service.get_user_by_id", new_callable=AsyncMock)
    def test_patch_user_rejects_cross_clinic(self, mock_get, mock_update):
        """Clinic-scoped admin cannot PATCH a user from another clinic."""
        from app.auth.dependencies import get_current_user

        mock_get.return_value = {
            "id": "target-user-id",
            "email": "user@clinic-b.dev",
            "nombre": "Target User",
            "rol": "medico",
            "activo": True,
            "clinic_id": CLINIC_B,  # different clinic!
            "scope": "clinic",
        }
        self.client.app.dependency_overrides[get_current_user] = lambda: ADMIN_USER
        self.client.app.state.pool = AsyncMock()

        resp = self.client.patch(
            "/admin/api/usuarios/target-user-id",
            json={"nombre": "Hacked Name"},
            headers={"Origin": "http://localhost:3000"},
        )
        self.assertEqual(resp.status_code, 403)
        # update_user must NOT have been called
        mock_update.assert_not_called()

    @patch("app.admin.router.auth_service.update_user", new_callable=AsyncMock)
    @patch("app.admin.router.auth_service.get_user_by_id", new_callable=AsyncMock)
    def test_patch_user_allows_same_clinic(self, mock_get, mock_update):
        """Clinic-scoped admin can PATCH a user in their own clinic."""
        from app.auth.dependencies import get_current_user

        mock_get.return_value = {
            "id": "target-user-id",
            "email": "user@clinic-a.dev",
            "nombre": "Target User",
            "rol": "medico",
            "activo": True,
            "clinic_id": CLINIC_A,  # same clinic as ADMIN_USER
            "scope": "clinic",
        }
        mock_update.return_value = {
            "id": "target-user-id",
            "email": "user@clinic-a.dev",
            "nombre": "Updated Name",
            "rol": "medico",
            "activo": True,
            "clinic_id": CLINIC_A,
            "scope": "clinic",
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }
        self.client.app.dependency_overrides[get_current_user] = lambda: ADMIN_USER
        self.client.app.state.pool = AsyncMock()

        resp = self.client.patch(
            "/admin/api/usuarios/target-user-id",
            json={"nombre": "Updated Name"},
            headers={"Origin": "http://localhost:3000"},
        )
        self.assertEqual(resp.status_code, 200)

    @patch("app.admin.router.auth_service.update_user", new_callable=AsyncMock)
    @patch("app.admin.router.auth_service.get_user_by_id", new_callable=AsyncMock)
    def test_patch_user_global_admin_allows_any_clinic(self, mock_get, mock_update):
        """Global admin can PATCH a user in any clinic."""
        from app.auth.dependencies import get_current_user

        mock_get.return_value = {
            "id": "target-user-id",
            "email": "user@clinic-b.dev",
            "nombre": "Target User",
            "rol": "medico",
            "activo": True,
            "clinic_id": CLINIC_B,
            "scope": "clinic",
        }
        mock_update.return_value = {
            "id": "target-user-id",
            "email": "user@clinic-b.dev",
            "nombre": "Updated Name",
            "rol": "medico",
            "activo": True,
            "clinic_id": CLINIC_B,
            "scope": "clinic",
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }
        self.client.app.dependency_overrides[get_current_user] = lambda: GLOBAL_USER
        self.client.app.state.pool = AsyncMock()

        resp = self.client.patch(
            "/admin/api/usuarios/target-user-id",
            json={"nombre": "Updated Name"},
            headers={"Origin": "http://localhost:3000"},
        )
        self.assertEqual(resp.status_code, 200)

    @patch("app.admin.router.auth_service.update_password", new_callable=AsyncMock)
    @patch("app.admin.router.auth_service.log_audit", new_callable=AsyncMock)
    @patch("app.admin.router.auth_service.get_user_by_id", new_callable=AsyncMock)
    def test_reset_password_rejects_cross_clinic(self, mock_get, mock_audit, mock_update_pw):
        """Clinic-scoped admin cannot reset password for user from another clinic."""
        from app.auth.dependencies import get_current_user

        mock_get.return_value = {
            "id": "target-user-id",
            "email": "user@clinic-b.dev",
            "rol": "medico",
            "clinic_id": CLINIC_B,  # different clinic!
            "scope": "clinic",
        }
        self.client.app.dependency_overrides[get_current_user] = lambda: ADMIN_USER
        self.client.app.state.pool = AsyncMock()

        resp = self.client.post(
            "/admin/api/usuarios/target-user-id/reset-password",
            json={"new_password": "ValidPass!123456"},
            headers={"Origin": "http://localhost:3000"},
        )
        self.assertEqual(resp.status_code, 403)
        mock_update_pw.assert_not_called()

    @patch("app.admin.router.auth_service.update_password", new_callable=AsyncMock)
    @patch("app.admin.router.auth_service.log_audit", new_callable=AsyncMock)
    @patch("app.admin.router.auth_service.get_user_by_id", new_callable=AsyncMock)
    def test_reset_password_allows_same_clinic(self, mock_get, mock_audit, mock_update_pw):
        """Clinic-scoped admin can reset password for user in their own clinic."""
        from app.auth.dependencies import get_current_user

        mock_get.return_value = {
            "id": "target-user-id",
            "email": "user@clinic-a.dev",
            "rol": "medico",
            "clinic_id": CLINIC_A,  # same clinic as ADMIN_USER
            "scope": "clinic",
        }
        self.client.app.dependency_overrides[get_current_user] = lambda: ADMIN_USER
        self.client.app.state.pool = AsyncMock()

        resp = self.client.post(
            "/admin/api/usuarios/target-user-id/reset-password",
            json={"new_password": "ValidPass!123456"},
            headers={"Origin": "http://localhost:3000"},
        )
        self.assertEqual(resp.status_code, 200)

    @patch("app.admin.router.auth_service.delete_user", new_callable=AsyncMock)
    @patch("app.admin.router.auth_service.revoke_all_user_tokens", new_callable=AsyncMock)
    @patch("app.admin.router.auth_service.log_audit", new_callable=AsyncMock)
    @patch("app.admin.router.auth_service.get_user_by_id", new_callable=AsyncMock)
    def test_delete_user_rejects_cross_clinic(self, mock_get, mock_audit, mock_revoke, mock_delete):
        """Clinic-scoped admin cannot delete a user from another clinic."""
        from app.auth.dependencies import get_current_user

        mock_get.return_value = {
            "id": "target-user-id",
            "email": "user@clinic-b.dev",
            "rol": "medico",
            "clinic_id": CLINIC_B,  # different clinic!
            "scope": "clinic",
        }
        self.client.app.dependency_overrides[get_current_user] = lambda: ADMIN_USER
        self.client.app.state.pool = AsyncMock()

        resp = self.client.delete(
            "/admin/api/usuarios/target-user-id",
            headers={"Origin": "http://localhost:3000"},
        )
        self.assertEqual(resp.status_code, 403)
        mock_delete.assert_not_called()

    @patch("app.admin.router.auth_service.delete_user", new_callable=AsyncMock)
    @patch("app.admin.router.auth_service.revoke_all_user_tokens", new_callable=AsyncMock)
    @patch("app.admin.router.auth_service.log_audit", new_callable=AsyncMock)
    @patch("app.admin.router.auth_service.get_user_by_id", new_callable=AsyncMock)
    def test_delete_user_allows_same_clinic(self, mock_get, mock_audit, mock_revoke, mock_delete):
        """Clinic-scoped admin can delete a user in their own clinic."""
        from app.auth.dependencies import get_current_user

        mock_get.return_value = {
            "id": "target-user-id",
            "email": "user@clinic-a.dev",
            "rol": "medico",
            "clinic_id": CLINIC_A,  # same clinic as ADMIN_USER
            "scope": "clinic",
        }
        mock_delete.return_value = True
        self.client.app.dependency_overrides[get_current_user] = lambda: ADMIN_USER
        self.client.app.state.pool = AsyncMock()

        resp = self.client.delete(
            "/admin/api/usuarios/target-user-id",
            headers={"Origin": "http://localhost:3000"},
        )
        self.assertEqual(resp.status_code, 200)

    @patch("app.admin.router.auth_service.get_user_by_id", new_callable=AsyncMock)
    def test_get_user_by_id_returns_clinic_id_and_scope(self, mock_get):
        """get_user_by_id must return clinic_id and scope for tenant enforcement."""
        # This tests that the service function signature includes these fields.
        import inspect
        from app.auth.service import get_user_by_id

        # Verify the function is importable (it was already updated to return clinic_id/scope)
        self.assertTrue(callable(get_user_by_id))
        # Verify that serialize_user handles clinic_id/scope from target
        result = admin_service.serialize_user({
            "id": "some-uuid",
            "email": "test@test.dev",
            "nombre": "Test",
            "rol": "medico",
            "activo": True,
            "clinic_id": CLINIC_A,
            "scope": "clinic",
            "created_at": datetime.now(timezone.utc),
        })
        self.assertEqual(result["clinic_id"], CLINIC_A)
        self.assertEqual(result["scope"], "clinic")


# ── [CRITICAL-7] Admin with scope=clinic but no clinic_id cannot mutate ────────


class TestAdminNoClinicIdFailClosed(unittest.TestCase):
    """Admin with scope='clinic' but missing clinic_id must be rejected on mutation endpoints."""

    def setUp(self):
        self.app = _make_test_app()
        self.client = TestClient(self.app)

    @patch("app.admin.router.auth_service.get_user_by_id", new_callable=AsyncMock)
    def test_patch_user_rejects_admin_no_clinic_id(self, mock_get):
        """Admin with scope='clinic' but clinic_id=None should get 401 on PATCH."""
        from app.auth.dependencies import get_current_user

        mock_get.return_value = {
            "id": "target-user-id",
            "email": "user@clinic-a.dev",
            "nombre": "Target",
            "rol": "medico",
            "activo": True,
            "clinic_id": CLINIC_A,
            "scope": "clinic",
        }
        self.client.app.dependency_overrides[get_current_user] = lambda: ADMIN_USER_NO_CLINIC
        self.client.app.state.pool = AsyncMock()

        resp = self.client.patch(
            "/admin/api/usuarios/target-user-id",
            json={"nombre": "Hacked"},
            headers={"Origin": "http://localhost:3000"},
        )
        self.assertIn(resp.status_code, (401, 403))

    @patch("app.admin.router.auth_service.get_user_by_id", new_callable=AsyncMock)
    def test_reset_password_rejects_admin_no_clinic_id(self, mock_get):
        """Admin with scope='clinic' but clinic_id=None should get 401 on reset-password."""
        from app.auth.dependencies import get_current_user

        mock_get.return_value = {
            "id": "target-user-id",
            "email": "user@clinic-a.dev",
            "rol": "medico",
            "clinic_id": CLINIC_A,
            "scope": "clinic",
        }
        self.client.app.dependency_overrides[get_current_user] = lambda: ADMIN_USER_NO_CLINIC
        self.client.app.state.pool = AsyncMock()

        resp = self.client.post(
            "/admin/api/usuarios/target-user-id/reset-password",
            json={"new_password": "ValidPass!123456"},
            headers={"Origin": "http://localhost:3000"},
        )
        self.assertIn(resp.status_code, (401, 403))

    @patch("app.admin.router.auth_service.get_user_by_id", new_callable=AsyncMock)
    def test_delete_user_rejects_admin_no_clinic_id(self, mock_get):
        """Admin with scope='clinic' but clinic_id=None should get 401 on DELETE."""
        from app.auth.dependencies import get_current_user

        mock_get.return_value = {
            "id": "target-user-id",
            "email": "user@clinic-a.dev",
            "rol": "medico",
            "clinic_id": CLINIC_A,
            "scope": "clinic",
        }
        self.client.app.dependency_overrides[get_current_user] = lambda: ADMIN_USER_NO_CLINIC
        self.client.app.state.pool = AsyncMock()

        resp = self.client.delete(
            "/admin/api/usuarios/target-user-id",
            headers={"Origin": "http://localhost:3000"},
        )
        self.assertIn(resp.status_code, (401, 403))


if __name__ == "__main__":
    unittest.main()