# -*- coding: utf-8 -*-
"""/fin routes: our own API surface mounted onto the AgentScope app.

This verifies the "include custom routes on the returned app" pattern
(待核对 item in docs/数据结构.md) and gives the web UI a place to fetch
provenance data later (/fin/artifacts/...). Read-only for now.
"""

from fastapi import APIRouter, HTTPException

from .db import get_pool

fin_router = APIRouter(prefix="/fin", tags=["fin"])


@fin_router.get("/healthz")
async def fin_healthz() -> dict:
    """Liveness of the fin schema: DB reachable + table inventory."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            tables = await conn.fetch(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'fin' ORDER BY table_name",
            )
            counts = {}
            for row in tables:
                name = row["table_name"]
                counts[name] = await conn.fetchval(
                    f'SELECT count(*) FROM fin."{name}"',
                )
        return {"status": "ok", "tables": counts}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=str(exc)) from exc
