"""Owned operations/results; only public SEC facts are shared between users."""

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import os
from uuid import uuid4

from .contracts import Artifact, DocumentInfo, Evidence, FinanceError, FindRequest, TableData
from .sec_client import MAX_RESPONSE_BYTES, filing_url
from .db import get_pool


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


def _dump(value):
    return json.dumps(value, ensure_ascii=False, default=str)


def report_root() -> Path:
    return Path(os.getenv("FIN_DATA_DIR", "/data/workspaces/.finance")).resolve() / "reports"


def filing_root() -> Path:
    return Path(os.getenv("FIN_DATA_DIR", "/data/workspaces/.finance")).resolve() / "filings"


def source_document_id(company: str, accession: str) -> str:
    return "sec_" + hashlib.sha256(f"{company}:{accession}".encode()).hexdigest()[:32]


def _read_document_file(path: str | None, document: DocumentInfo) -> bytes:
    expected = filing_root() / f"{document.sha256}.html"
    if not path or Path(path).resolve() != expected or not expected.resolve().is_relative_to(filing_root()):
        raise FinanceError("file_unavailable", "原始申报文件路径不可用。")
    try:
        if expected.stat().st_size != document.byte_size or document.byte_size > MAX_RESPONSE_BYTES:
            raise FinanceError("document_integrity", "原始申报文件大小与固定版本不符。")
        with expected.open("rb") as handle:
            raw = handle.read(MAX_RESPONSE_BYTES + 1)
    except OSError as exc:
        raise FinanceError("file_unavailable", "原始申报文件不可用，请重新获取。") from exc
    if len(raw) != document.byte_size or hashlib.sha256(raw).hexdigest() != document.sha256:
        raise FinanceError("document_integrity", "原始申报文件校验失败，未返回替代版本。")
    return raw


def _artifact(row) -> Artifact:
    meta = _json(row["meta"]) or {}
    return Artifact(
        artifact_id=row["artifact_id"], run_id=row["run_id"], kind=row["kind"],
        title=row["title"], created_at=row["created_at"], input_ids=row["input_ids"],
        data=meta.get("data"), document=meta.get("document"), text=row["content"], citations=meta.get("citations", []),
        warnings=meta.get("warnings", []), filename=row["filename"], mime_type=row["mime_type"],
    )


async def is_finance_agent(user_id: str, agent_id: str) -> bool:
    pool = await get_pool()
    return bool(await pool.fetchval(
        "SELECT 1 FROM fin.builtin_agents WHERE user_id=$1 AND agent_id=$2", user_id, agent_id,
    ))


async def start_operation(
    user_id: str, agent_id: str, session_id: str, reply_id: str,
    tool_call_id: str, operation: str, request: dict,
) -> dict:
    pool = await get_pool()
    key = hashlib.sha256(f"{reply_id}:{tool_call_id or uuid4().hex}:{operation}".encode()).hexdigest()
    phase = {
        "data_fetch": "collecting", "fetch_filings": "collecting", "calculate_metrics": "tabulating",
        "data_analyze": "analyzing", "export_report": "exporting",
    }.get(operation, "reading")
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow("""
            INSERT INTO fin.task_runs
              (run_id,session_id,user_id,user_query,status,phase,data_request,
               heartbeat_at,agent_id,reply_id,tool_call_id,operation,idempotency_key)
            VALUES ($1,$2,$3,$4,'running',$5,$6::jsonb,now(),$7,$8,$9,$10,$11)
            ON CONFLICT (user_id,session_id,idempotency_key)
              WHERE idempotency_key IS NOT NULL DO NOTHING
            RETURNING run_id,status
        """, "run_" + uuid4().hex, session_id, user_id, _dump(request)[:2000],
            phase, _dump(request), agent_id, reply_id, tool_call_id, operation, key)
        created = row is not None
        if row is None:
            row = await conn.fetchrow("""
                SELECT run_id,status,data_request FROM fin.task_runs
                WHERE user_id=$1 AND session_id=$2 AND idempotency_key=$3 FOR UPDATE
            """, user_id, session_id, key)
            if _json(row["data_request"]) != _json(_dump(request)):
                raise FinanceError("idempotency_conflict", "同一工具调用标识对应不同参数，不能重放。")
        ids = await conn.fetch("SELECT artifact_id FROM fin.task_artifacts WHERE run_id=$1 ORDER BY created_at", row["run_id"])
        return {"run_id": row["run_id"], "status": row["status"], "created": created,
                "artifact_ids": [r["artifact_id"] for r in ids]}


async def touch_operation(run_id: str) -> None:
    pool = await get_pool()
    await pool.execute("UPDATE fin.task_runs SET heartbeat_at=now() WHERE run_id=$1 AND status='running'", run_id)


async def finish_operation(run_id: str, status: str, error: str | None = None) -> None:
    if status not in {"succeeded", "partial", "failed", "interrupted"}:
        raise ValueError("Invalid terminal operation status")
    pool = await get_pool()
    await pool.execute("""
        UPDATE fin.task_runs SET status=$2,error=$3,finished_at=now(),heartbeat_at=now(),
          phase=CASE WHEN $2 IN ('succeeded','partial') THEN 'done' ELSE phase END
        WHERE run_id=$1 AND status='running'
    """, run_id, status, error[:2000] if error else None)


async def recover_stale_operations() -> int:
    pool = await get_pool()
    # The single-backend deployment heartbeats foreground tools. This is not
    # permission to steal a live operation; expiry must exceed heartbeat gaps.
    rows = await pool.fetch("""
        UPDATE fin.task_runs SET status='interrupted',finished_at=now(),
          error='Worker heartbeat expired; retry can reuse saved official facts.'
        WHERE status='running' AND coalesce(heartbeat_at,created_at) < now()-interval '10 minutes'
        RETURNING run_id
    """)
    return len(rows)


async def _save_evidence(conn, evidence: Evidence) -> int:
    value = Decimal(evidence.value)
    if not value.is_finite():
        raise FinanceError("invalid_fact", "官方事实包含非有限数值。")
    doc_id = source_document_id(evidence.company, evidence.accession)
    await conn.execute("""
        INSERT INTO fin.source_documents
          (doc_id,company,doc_type,period_end,url,accession,fetched_at,local_path,trust_level,fetch_status)
        VALUES ($1,$2,$3,$4,$5,$6,NULL,NULL,'verified','metadata_only')
        ON CONFLICT (doc_id) DO NOTHING
    """, doc_id, evidence.company, evidence.form, evidence.period_end,
        evidence.filing_url, evidence.accession)
    concept = evidence.concept if ":" in evidence.concept else "us-gaap:" + evidence.concept
    fact_id = await conn.fetchval("""
        INSERT INTO fin.fact_financials
          (company,concept,metric,period_start,period_end,value,unit,form,source_type,
           source_ref,doc_id,filed_at,retrieved_at,confidence)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'xbrl_api',$9,$10,$11,$12,'verified')
        ON CONFLICT ON CONSTRAINT uq_fact DO NOTHING RETURNING fact_id
    """, evidence.company, concept, evidence.metric, evidence.period_start, evidence.period_end,
        value, evidence.unit, evidence.form, evidence.accession, doc_id,
        evidence.filed_at, evidence.retrieved_at)
    if fact_id is not None:
        return fact_id
    existing = await conn.fetchrow("""
        SELECT fact_id,value,unit FROM fin.fact_financials
        WHERE company=$1 AND concept=$2 AND period_start IS NOT DISTINCT FROM $3
          AND period_end=$4 AND source_ref=$5
    """, evidence.company, concept, evidence.period_start, evidence.period_end, evidence.accession)
    if existing is None or existing["value"] != value or existing["unit"] != evidence.unit:
        raise FinanceError("source_conflict", "同一来源事实与已存值不一致；为保留历史结果，未覆盖原记录。")
    return existing["fact_id"]


async def save_artifact(
    user_id: str, run_id: str, kind: str, title: str, *, data: TableData | None = None,
    text: str | None = None, citations=None, warnings=None, input_ids=None,
    filename: str | None = None, mime_type: str | None = None, path: str | None = None,
    document: DocumentInfo | None = None,
) -> Artifact:
    from .registry import seed_registry

    input_ids = list(dict.fromkeys(input_ids or []))
    if (kind == "document") != (document is not None):
        raise FinanceError("invalid_document", "文档产物必须且仅能包含文档描述。")
    if document:
        document = document.model_copy(deep=True)
        from .registry import COMPANIES
        company = COMPANIES.get(document.company)
        if (not company or company.cik != document.cik
                or document.source_document_id != source_document_id(document.company, document.accession)
                or document.source_url != filing_url(document.cik, document.accession, document.primary_document)):
            raise FinanceError("invalid_document", "申报文档来源描述不符合已核验公司与 SEC 路径。")
        await asyncio.to_thread(_read_document_file, path, document)
    elif path and not Path(path).resolve().is_relative_to(report_root()):
        # Keep export path checks independent from the original-document root.
        raise FinanceError("invalid_path", "文件路径不属于财务导出目录。")
    if data is not None:
        data = data.model_copy(deep=True)
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        run = await conn.fetchrow("SELECT user_id,status FROM fin.task_runs WHERE run_id=$1 FOR UPDATE", run_id)
        if not run or run["user_id"] != user_id:
            raise FinanceError("not_found", "任务不存在或无权访问。")
        if run["status"] != "running":
            raise FinanceError("operation_finished", "任务已结束或中断，不能继续发布产物。")
        if input_ids:
            count = await conn.fetchval("""
                SELECT count(*) FROM fin.task_artifacts a JOIN fin.task_runs r USING(run_id)
                WHERE a.artifact_id=ANY($1::text[]) AND r.user_id=$2
                  AND r.status IN ('succeeded','partial')
            """, input_ids, user_id)
            if count != len(input_ids):
                raise FinanceError("not_found", "输入结果不存在或无权访问。")
        if document:
            await seed_registry(conn)
            # Shared current metadata can move forward. Published artifacts below
            # retain their own descriptor and content-addressed path permanently.
            await conn.execute("""
                INSERT INTO fin.source_documents
                  (doc_id,company,doc_type,period_end,url,accession,fetched_at,local_path,checksum,trust_level,fetch_status)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,'verified','fetched')
                ON CONFLICT (doc_id) DO UPDATE SET
                  doc_type=excluded.doc_type,period_end=excluded.period_end,url=excluded.url,
                  fetched_at=excluded.fetched_at,local_path=excluded.local_path,
                  checksum=excluded.checksum,fetch_status='fetched',fetch_error=NULL
                WHERE fin.source_documents.fetched_at IS NULL
                   OR fin.source_documents.fetched_at <= excluded.fetched_at
            """, document.source_document_id, document.company, document.form, document.period_end,
                document.source_url, document.accession, document.fetched_at, path, document.sha256)
        if data:
            await seed_registry(conn)
            for evidence in data.sources.values():
                evidence.fact_id = await _save_evidence(conn, evidence)
        artifact = Artifact(
            artifact_id=kind + "_" + uuid4().hex, run_id=run_id, kind=kind,
            title=title, created_at=datetime.now(timezone.utc), input_ids=input_ids,
            data=data, document=document, text=text, citations=citations or [], warnings=warnings or [],
            filename=filename, mime_type=mime_type,
        )
        meta = {"schema_version": 1, "data": data.model_dump(mode="json") if data else None,
                "document": document.model_dump(mode="json") if document else None,
                "citations": [c.model_dump(mode="json") for c in artifact.citations],
                "warnings": artifact.warnings}
        await conn.execute("""
            INSERT INTO fin.task_artifacts
              (artifact_id,run_id,kind,format,path,content,meta,created_at,title,input_ids,filename,mime_type)
            VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8,$9,$10,$11,$12)
        """, artifact.artifact_id, run_id, kind, "xlsx" if kind == "export" else "html" if kind == "document" else "md" if kind == "narrative" else "json",
            path, text, _dump(meta), artifact.created_at, title, input_ids, filename, mime_type)
        return artifact


async def get_artifact(user_id: str, artifact_id: str) -> Artifact:
    pool = await get_pool()
    row = await pool.fetchrow("""
        SELECT a.* FROM fin.task_artifacts a JOIN fin.task_runs r USING(run_id)
        WHERE a.artifact_id=$1 AND r.user_id=$2 AND r.status IN ('succeeded','partial')
    """, artifact_id, user_id)
    if row is None:
        raise FinanceError("not_found", "结果不存在或无权访问。")
    return _artifact(row)


async def find_artifacts(user_id: str, session_id: str, request: FindRequest) -> list[Artifact]:
    if request.artifact_id:
        return [await get_artifact(user_id, request.artifact_id)]
    pool = await get_pool()
    # Search only owned result descriptions, not credentials or global messages.
    query = request.query or ""
    pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    rows = await pool.fetch("""
        SELECT a.* FROM fin.task_artifacts a JOIN fin.task_runs r USING(run_id)
        WHERE r.user_id=$1 AND r.status IN ('succeeded','partial')
          AND ($2::text IS NULL OR r.session_id=$2)
          AND ($3::text IS NULL OR a.kind=$3)
          AND (a.title ILIKE $4 OR a.meta::text ILIKE $4)
        ORDER BY a.created_at DESC, a.artifact_id DESC LIMIT $5
    """, user_id, session_id if request.current_session_only else None, request.kind, pattern, request.limit)
    return [_artifact(row) for row in rows]


async def artifact_file(user_id: str, artifact_id: str) -> tuple[Path, str, str]:
    pool = await get_pool()
    row = await pool.fetchrow("""
        SELECT a.path,a.filename,a.mime_type,a.kind,a.meta FROM fin.task_artifacts a
        JOIN fin.task_runs r USING(run_id)
        WHERE a.artifact_id=$1 AND r.user_id=$2 AND a.kind IN ('export','document')
          AND r.status IN ('succeeded','partial')
    """, artifact_id, user_id)
    if not row or not row["path"]:
        raise FinanceError("not_found", "导出文件不存在或无权访问。")
    path = Path(row["path"]).resolve()
    if row["kind"] == "document":
        document = DocumentInfo.model_validate((_json(row["meta"]) or {}).get("document"))
        await asyncio.to_thread(_read_document_file, row["path"], document)
        return path, document.primary_document, "application/octet-stream"
    if not path.is_relative_to(report_root()) or not path.is_file():
        raise FinanceError("file_unavailable", "导出文件不可用，请重新生成。")
    return path, row["filename"] or "finance-report.xlsx", row["mime_type"] or "application/octet-stream"


async def read_document(user_id: str, artifact_id: str) -> bytes:
    pool = await get_pool()
    row = await pool.fetchrow("""
        SELECT a.path,a.meta FROM fin.task_artifacts a JOIN fin.task_runs r USING(run_id)
        WHERE a.artifact_id=$1 AND r.user_id=$2 AND a.kind='document'
          AND r.status IN ('succeeded','partial')
    """, artifact_id, user_id)
    if row is None:
        raise FinanceError("not_found", "文档不存在或无权访问。")
    document = DocumentInfo.model_validate((_json(row["meta"]) or {}).get("document"))
    return await asyncio.to_thread(_read_document_file, row["path"], document)


async def document_provenance(user_id: str, artifact_id: str) -> dict:
    artifact = await get_artifact(user_id, artifact_id)
    if artifact.kind != "document" or artifact.document is None:
        raise FinanceError("not_found", "该结果不是原始申报文档。")
    return {"artifact_id": artifact.artifact_id, "title": artifact.title,
            "document": artifact.document.model_dump(mode="json"), "warnings": artifact.warnings}


async def cell_provenance(user_id: str, artifact_id: str, cell_id: str) -> dict:
    artifact = await get_artifact(user_id, artifact_id)
    if artifact.data:
        for row in artifact.data.rows + artifact.data.history_rows:
            for cell in row.cells:
                if cell.cell_id == cell_id:
                    return {
                        "artifact_id": artifact_id, "title": artifact.title,
                        "row": row.model_dump(mode="json", exclude={"cells"}),
                        "cell": cell.model_dump(mode="json"),
                        "sources": [artifact.data.sources[s].model_dump(mode="json") for s in cell.source_ids if s in artifact.data.sources],
                        "warnings": list(dict.fromkeys(artifact.warnings + artifact.data.warnings)),
                    }
    raise FinanceError("not_found", "该结果没有对应的数据单元格。")
