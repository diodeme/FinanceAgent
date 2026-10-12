# -*- coding: utf-8 -*-
"""Shared asyncpg access for fin-domain code (middleware, routes, tools).

The app's DATABASE_URL is a SQLAlchemy async URL
(`postgresql+asyncpg://...`); asyncpg wants a plain DSN, so we strip the
driver suffix. One lazily-created process-wide pool; AgentScope's own
storage manages its engine independently and is not touched here.
"""

import asyncio
import os

import asyncpg

_pool: asyncpg.Pool | None = None
_lock = asyncio.Lock()


def to_asyncpg_dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        async with _lock:
            if _pool is None:
                _pool = await asyncpg.create_pool(
                    to_asyncpg_dsn(os.environ["DATABASE_URL"]),
                    min_size=1,
                    max_size=5,
                )
    return _pool
