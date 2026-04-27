"""
app.core.database -- Pool management and FastAPI dependency.

The pool is created in the lifespan and stored in app.state.pool.
No global _pool variable -- every DB access receives the pool explicitly.

Usage (lifespan):
    from app.core.database import create_pool

    async def lifespan(app: FastAPI):
        app.state.pool = await create_pool(settings.POSTGRES_URL)
        yield
        await app.state.pool.close()

Usage (dependency injection):
    from app.core.database import get_db_pool

    @router.get("/")
    async def my_endpoint(pool: asyncpg.Pool = Depends(get_db_pool)):
        async with pool.acquire() as conn:
            ...
"""

from __future__ import annotations

import logging

import asyncpg
from fastapi import Request

logger = logging.getLogger("imagiax.db")


async def create_pool(
    dsn: str,
    min_size: int = 2,
    max_size: int = 20,
) -> asyncpg.Pool:
    """Create and return a new asyncpg connection pool.

    The pool is NOT stored globally -- callers must store it in
    app.state.pool (or equivalent) and pass it around explicitly.

    Args:
        dsn: PostgreSQL connection string (e.g. postgresql://...).
        min_size: Minimum number of connections to keep open.
        max_size: Maximum number of simultaneous connections.
            # Pool sizing: AI analysis (long-running) + dashboard + webhooks + admin queries
            # 20 connections handles ~4 concurrent AI analyses + normal traffic

    Returns:
        An initialised asyncpg.Pool.
    """
    pool = await asyncpg.create_pool(dsn=dsn, min_size=min_size, max_size=max_size)
    logger.info("IMAGIAX DB: PostgreSQL connection pool created (min=%d, max=%d).", min_size, max_size)
    return pool


def get_db_pool(request: Request) -> asyncpg.Pool:
    """FastAPI dependency that returns the pool stored in app.state.pool.

    Raises:
        RuntimeError: If the pool has not been initialised in the lifespan.
    """
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise RuntimeError(
            "Database pool is not initialised. "
            "Ensure the lifespan creates the pool and stores it in app.state.pool."
        )
    return pool
