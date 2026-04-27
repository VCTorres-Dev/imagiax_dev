from __future__ import annotations

import sys
import types
import unittest
import os
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import MagicMock


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


def _ensure_stub(name: str) -> None:
    if name in sys.modules:
        return
    mod = types.ModuleType(name)
    mod.__dict__.update({
        "__all__": [],
        "__path__": [],
        "__file__": f"<stub:{name}>",
    })
    sys.modules[name] = MagicMock(spec=mod, __path__=[], __all__=[])


for module_name in (
    "asyncpg",
    "bcrypt",
    "google",
    "google.genai",
    "google.genai.types",
    "jose",
    "jose.jwt",
    "torch",
    "torchvision",
    "torchxrayvision",
    "torchxrayvision.models",
    "PIL",
    "PIL.Image",
    "cv2",
    "numpy",
    "pydicom",
    "yaml",
    "mercadopago",
    "transbank",
    "transbank.webpay",
    "transbank.webpay.webpay_plus",
):
    _ensure_stub(module_name)


os.environ.setdefault("POSTGRES_URL", "postgresql://postgres:postgres@localhost:5432/imagiax")
os.environ.setdefault("JWT_SECRET", "0123456789abcdef0123456789abcdef")


from fastapi.testclient import TestClient
from fastapi import APIRouter


def _stub_router_module(module_name: str) -> None:
    mod = types.ModuleType(module_name)
    mod.router = APIRouter()
    sys.modules[module_name] = mod


for router_module in (
    "api.auth.router",
    "app.admin.router",
    "infrastructure.pagos.tbk.router",
    "infrastructure.pagos.mp.router",
    "api.ai.router",
    "app.informes.router",
):
    _stub_router_module(router_module)


deps_mod = types.ModuleType("api.auth.dependencies")
deps_mod.get_current_user = lambda: {"sub": "test"}


def _require_role(_role: str):
    def _dep():
        return {"role": _role}

    return _dep


deps_mod.require_role = _require_role
sys.modules["api.auth.dependencies"] = deps_mod

models_mod = types.ModuleType("api.auth.models")


class _UserPayload(dict):
    pass


models_mod.UserPayload = _UserPayload
sys.modules["api.auth.models"] = models_mod

middleware_mod = types.ModuleType("app.core.middleware")
middleware_mod.setup_middleware = lambda app: None
sys.modules["app.core.middleware"] = middleware_mod

config_mod = types.ModuleType("app.core.config")


class _Settings:
    is_production = False
    POSTGRES_URL = "postgresql://postgres:postgres@localhost:5432/imagiax"
    IMAGIAX_BASE_URL = "http://localhost:8010"
    IMAGIAX_VIEWER_URL = "http://localhost:3000"
    IMAGIAX_DOCTOR_NAME = ""
    IMAGIAX_DOCTOR_SPECIALTY = ""
    IMAGIAX_DOCTOR_LICENSE = ""
    IMAGIAX_BRAND_NAME = ""
    IMAGIAX_BRAND_SUBTITLE = ""
    IMAGIAX_PRECIO_SCAN = 990


config_mod.settings = _Settings()
sys.modules["app.core.config"] = config_mod

import api.main as api_main


@asynccontextmanager
async def _noop_lifespan(app):
    yield


api_main._lifespan = _noop_lifespan
create_app = api_main.create_app


class _FakeAcquireCtx:
    def __init__(self, conn: object):
        self._conn = conn

    async def __aenter__(self) -> object:
        return self._conn

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class _FakeConn:
    async def fetchval(self, query: str) -> int:
        if query == "SELECT 1":
            return 1
        return 0


class _FakePool:
    def acquire(self) -> _FakeAcquireCtx:
        return _FakeAcquireCtx(_FakeConn())

    async def close(self) -> None:
        return None


class RuntimeContractParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = create_app()
        cls.app.state.pool = _FakePool()

    def setUp(self) -> None:
        self.client = TestClient(self.app)

    def test_ping_status_and_shape(self):
        response = self.client.get("/ping")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("content-type"), "application/json")
        self.assertEqual(response.json(), {"status": "ok"})

    def test_health_db_status_shape_and_content_type(self):
        response = self.client.get("/health/db")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("content-type"), "application/json")
        self.assertEqual(response.json(), {"ok": True, "database": "up"})

    def test_robots_txt_status_content_type_and_body_shape(self):
        response = self.client.get("/robots.txt")
        self.assertEqual(response.status_code, 200)
        content_type = response.headers.get("content-type", "")
        self.assertIn("text/plain", content_type)
        body = response.text
        self.assertIn("User-agent: *", body)
        self.assertIn("Disallow: /api/", body)
        self.assertIn("Sitemap:", body)

    def test_sitemap_xml_status_content_type_and_body_shape(self):
        response = self.client.get("/sitemap.xml")
        self.assertEqual(response.status_code, 200)
        content_type = response.headers.get("content-type", "")
        self.assertIn("application/xml", content_type)
        body = response.text
        self.assertIn("<?xml version=\"1.0\" encoding=\"UTF-8\"?>", body)
        self.assertIn("<urlset", body)
        self.assertIn("<loc>", body)

    def test_salud_delta_is_explicitly_non_parity_contract(self):
        response = self.client.get("/salud")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        self.assertNotIn("estado", response.json())


if __name__ == "__main__":
    unittest.main()
