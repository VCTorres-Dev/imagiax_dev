from __future__ import annotations

import asyncio
import os
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

os.environ.setdefault("POSTGRES_URL", "postgresql://postgres:postgres@localhost:5432/imagiax")
os.environ.setdefault("JWT_SECRET", "0123456789abcdef0123456789abcdef")


asyncpg_mod = sys.modules.get("asyncpg")
if asyncpg_mod is None:
    asyncpg_mod = types.ModuleType("asyncpg")
    sys.modules["asyncpg"] = asyncpg_mod


class _Pool:
    pass


async def _fake_create_pool(*_args, **_kwargs):
    return _Pool()


setattr(asyncpg_mod, "Pool", _Pool)
setattr(asyncpg_mod, "create_pool", _fake_create_pool)

config_mod = types.ModuleType("app.core.config")


class _Secret:
    def get_secret_value(self) -> str:
        return ""


class _Settings:
    POSTGRES_URL = "postgresql://postgres:postgres@localhost:5432/imagiax"
    POSTGRES_CONNECT_RETRIES = 2
    POSTGRES_CONNECT_DELAY_SEC = 0.0
    POSTGRES_CONNECT_BACKOFF_MULTIPLIER = 1.0
    POSTGRES_CONNECT_MAX_DELAY_SEC = 0.0
    POSTGRES_BACKGROUND_RETRY_SEC = 8.0
    IMAGIAX_REQUIRE_POSTGRES = False
    IMAGIAX_HTTP_TIMEOUT = 10.0
    ORTHANC_AUTH_HEADER = ""
    ORTHANC_USER = ""
    ORTHANC_PASSWORD = _Secret()
    ORTHANC_BASE_URL = "http://localhost:8042"
    is_production = False


config_mod.settings = _Settings()
sys.modules["app.core.config"] = config_mod

middleware_mod = types.ModuleType("app.core.middleware")
middleware_mod.setup_middleware = lambda app: None
sys.modules["app.core.middleware"] = middleware_mod

from app.core.database import create_pool_with_retries
import api.main as api_main


class StartupResilienceParityTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_pool_with_retries_failures_then_success(self):
        attempts = 0
        fake_pool = object()

        async def _fake_create_pool(*_args, **_kwargs):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise RuntimeError("postgres unavailable")
            return fake_pool

        with patch("app.core.database.create_pool", side_effect=_fake_create_pool):
            pool = await create_pool_with_retries(
                "postgresql://postgres:postgres@localhost:5432/imagiax",
                retries=5,
                initial_delay_sec=0,
                backoff_multiplier=1.0,
                max_delay_sec=0,
            )

        self.assertIs(pool, fake_pool)
        self.assertEqual(attempts, 3)

    async def test_background_reconnect_recovers_and_runs_deferred_bootstrap(self):
        app = FastAPI()
        app.state.pool = None
        fake_pool = SimpleNamespace(close=AsyncMock())

        async def _fast_sleep(seconds: float):
            if seconds >= 3600:
                await asyncio.Future()
            return None

        with (
            patch.object(api_main, "create_pool_with_retries", AsyncMock(return_value=fake_pool)) as mocked_create,
            patch.object(api_main, "_bootstrap_pool_dependent_services", AsyncMock()) as mocked_bootstrap,
            patch("api.main.asyncio.sleep", side_effect=_fast_sleep),
        ):
            await asyncio.wait_for(api_main._background_reconnect_until_ready(app), timeout=1.0)

        self.assertIs(app.state.pool, fake_pool)
        mocked_create.assert_awaited()
        mocked_bootstrap.assert_awaited_once_with(app)

        cleanup_task = getattr(app.state, "cleanup_task", None)
        self.assertIsNotNone(cleanup_task)
        cleanup_task.cancel()
        await cleanup_task
        self.assertTrue(cleanup_task.done())

    async def test_lifespan_cancels_background_reconnect_on_shutdown(self):
        app = FastAPI()
        cancelled = {"value": False}

        async def _never_ready(_app: FastAPI):
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled["value"] = True
                raise

        with (
            patch.object(api_main, "create_pool_with_retries", side_effect=RuntimeError("db down")),
            patch.object(api_main, "_bootstrap_pool_dependent_services", AsyncMock()) as mocked_bootstrap,
            patch.object(api_main, "_background_reconnect_until_ready", new=_never_ready),
            patch.object(api_main.settings, "IMAGIAX_REQUIRE_POSTGRES", False),
            patch.object(api_main.settings, "POSTGRES_BACKGROUND_RETRY_SEC", 8.0),
        ):
            lifespan_cm = api_main._lifespan(app)
            await lifespan_cm.__aenter__()
            await asyncio.sleep(0)
            await lifespan_cm.__aexit__(None, None, None)

        mocked_bootstrap.assert_not_awaited()
        self.assertTrue(cancelled["value"])


if __name__ == "__main__":
    unittest.main()
