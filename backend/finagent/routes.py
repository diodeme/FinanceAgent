"""Finance bootstrap, owned result/provenance access and binary downloads."""

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from agentscope.app.deps import get_current_user_id, get_storage
from agentscope.app.storage import StorageBase

from .bootstrap import capabilities, ensure_finance_agent
from .contracts import FinanceError, FindRequest
from .db import get_pool
from .repository import artifact_file, cell_provenance, document_provenance, find_artifacts, get_artifact

fin_router = APIRouter(prefix="/fin", tags=["fin"])


def _error(exc: FinanceError) -> HTTPException:
    return HTTPException(status_code=404 if exc.code in {"not_found", "file_unavailable"} else 400,
                         detail={"code": exc.code, "message": str(exc)})


@fin_router.get("/healthz")
async def fin_healthz() -> dict:
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            tables = await conn.fetch(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='fin' ORDER BY table_name",
            )
            counts = {}
            for row in tables:
                name = row["table_name"]
                counts[name] = await conn.fetchval(f'SELECT count(*) FROM fin."{name}"')
        return {"status": "ok", "tables": counts}
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Finance database unavailable") from exc


@fin_router.post("/bootstrap")
async def bootstrap(
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
) -> dict:
    return await ensure_finance_agent(user_id, storage)


@fin_router.get("/capabilities")
async def finance_capabilities(user_id: str = Depends(get_current_user_id)) -> dict:
    return capabilities()


@fin_router.get("/results")
async def results(
    session_id: str, limit: int = Query(default=10, ge=1, le=30),
    user_id: str = Depends(get_current_user_id),
) -> dict:
    artifacts = await find_artifacts(user_id, session_id, FindRequest(limit=min(limit, 10), kind=None, current_session_only=True))
    return {"artifacts": [a.model_dump(mode="json") for a in artifacts]}


@fin_router.get("/results/{artifact_id}")
async def result(artifact_id: str, user_id: str = Depends(get_current_user_id)) -> dict:
    try:
        return (await get_artifact(user_id, artifact_id)).model_dump(mode="json")
    except FinanceError as exc:
        raise _error(exc) from exc


@fin_router.get("/results/{artifact_id}/cells/{cell_id}")
async def provenance(artifact_id: str, cell_id: str, user_id: str = Depends(get_current_user_id)) -> dict:
    try:
        return await cell_provenance(user_id, artifact_id, cell_id)
    except FinanceError as exc:
        raise _error(exc) from exc


@fin_router.get("/results/{artifact_id}/document")
async def document(artifact_id: str, user_id: str = Depends(get_current_user_id)) -> dict:
    try:
        return await document_provenance(user_id, artifact_id)
    except FinanceError as exc:
        raise _error(exc) from exc


@fin_router.get("/artifacts/{artifact_id}/download")
async def download(artifact_id: str, user_id: str = Depends(get_current_user_id)) -> FileResponse:
    try:
        path, filename, media_type = await artifact_file(user_id, artifact_id)
        return FileResponse(path, filename=filename, media_type=media_type,
                            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})
    except FinanceError as exc:
        raise _error(exc) from exc
