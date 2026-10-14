"""Seven state-injected finance tools and an owned, bounded streaming bridge."""

import asyncio
import contextvars
import json
import logging
from dataclasses import dataclass
from contextlib import aclosing

from agentscope.message import TextBlock, ToolResultState
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import FunctionTool, ToolChunk

from .analysis import analyze_data, citation_url, document_citation_url, narrative_title, RESEARCH_SECONDS, REPORT_SECONDS
from .analysis_workspace import WORKSPACE_SETUP_SECONDS
from .warnings import artifact_companies, normalize_warnings
from .contracts import (
    AnalyzeRequest, CalculateRequest, ExportRequest, FetchRequest, FinanceEnvelope,
    FinanceError, FilingRequest, FindRequest, LookupRequest,
)

logger = logging.getLogger(__name__)
TOOL_TIMEOUT_SECONDS = 120
REQUESTS = {
    "lookup_company": LookupRequest, "data_fetch": FetchRequest, "fetch_filings": FilingRequest,
    "calculate_metrics": CalculateRequest, "data_analyze": AnalyzeRequest,
    "find_results": FindRequest, "export_report": ExportRequest,
}
DESCRIPTIONS = {
    "lookup_company": "Validate a company name/ticker and discover supported coverage; never invent an identity.",
    "data_fetch": "Fetch immutable consolidated SEC numeric data. Streams progress; missing facts remain explicit gaps.",
    "calculate_metrics": "Compute registered or restricted arithmetic metrics and growth from owned saved data_ids.",
    "fetch_filings": "Download original SEC 10-K/10-Q HTML documents as owned immutable artifacts; dates select report periods. Count is per company, at most eight total.",
    "data_analyze": "Research owned numeric data_ids and/or original filing document_ids with native Docker file tools, then save a cited report. Numeric coverage must be complete; document-only is allowed.",
    "find_results": "Find or read your saved immutable results, normally in this session, for reuse across turns.",
    "export_report": "Export explicitly selected saved data and optional narrative IDs to an Excel artifact; no new analysis needed.",
}


@dataclass(frozen=True)
class ToolContext:
    user_id: str
    agent_id: str
    session_id: str
    reply_id: str
    tool_call_id: str
    operation: str
    run_id: str


tool_context_var: contextvars.ContextVar[ToolContext | None] = contextvars.ContextVar("finance_tool_context", default=None)


def reply_id(state) -> str | None:
    return getattr(getattr(state, "reply_context", None), "reply_id", None)


def analysis_record(record_type: str, **fields) -> str:
    """Newline-delimited transport only; body prose cannot inject control records."""
    return json.dumps({"finance_stream": 1, "type": record_type, **fields}, ensure_ascii=False) + "\n"


def compact_summary(envelope: FinanceEnvelope, *, include_text: bool = False) -> dict:
    """Model evidence excludes collection diagnostics, without rewriting metadata."""
    summaries = []
    for artifact in envelope.artifacts:
        item = {"artifact_id": artifact.artifact_id, "kind": artifact.kind, "title": artifact.title,
                "warnings": normalize_warnings(artifact.warnings, include_diagnostics=False), "input_ids": artifact.input_ids}
        if artifact.data:
            item["specification"] = artifact.data.specification
            item["warnings"] = normalize_warnings(artifact.warnings + artifact.data.warnings,
                                                   companies=artifact_companies([artifact]), include_diagnostics=False)
            requested = artifact.data.specification.get("requested_metrics")
            item["row_count"] = len(artifact.data.rows)
            item["preview_truncated"] = len(artifact.data.rows) > 16
            item["rows"] = [
                {"company": row.company, "period_start": str(row.period_start) if row.period_start else None,
                 "period_end": str(row.period_end), "period_label": row.period_label,
                 "cells": [{"metric": cell.metric, "value": cell.value, "unit": cell.unit,
                            "status": cell.status, "reason": cell.reason,
                            "citation": citation_url(artifact.artifact_id, cell.cell_id)}
                           for cell in row.cells if not requested or cell.metric in requested]}
                for row in artifact.data.rows[:16]
            ]
            if item["preview_truncated"]:
                item["preview_note"] = "Preview only. data_analyze and export_report read the full saved table by artifact_id."
        if artifact.document is not None:
            item["document"] = artifact.document.model_dump(mode="json")
            item["citation"] = document_citation_url(artifact.artifact_id)
            item["evidence_state"] = "downloaded; not necessarily read or cited"
        if include_text and artifact.text:
            item["text"] = artifact.text
        summaries.append(item)
    return {"message": envelope.message, "artifacts": summaries,
            "companies": [c.model_dump(mode="json", exclude={"warning"} if c.warning and not normalize_warnings([c.warning], include_diagnostics=False) else set())
                          for c in envelope.companies],
            "warnings": normalize_warnings(envelope.warnings,
                                           companies=artifact_companies(envelope.artifacts) if any(a.data for a in envelope.artifacts) else None,
                                           include_diagnostics=False),
            "error_code": envelope.error_code, "partial": envelope.partial}


def final_chunk(envelope: FinanceEnvelope, *, failed: bool = False, include_text: bool = False,
                framed: bool = False) -> ToolChunk:
    """Metadata is authoritative; only analysis transport uses versioned records."""
    summary = compact_summary(envelope, include_text=include_text)
    content = analysis_record("summary", result=summary) if framed else "\n" + json.dumps(summary, ensure_ascii=False)
    return ToolChunk(content=[TextBlock(text=content)],
                     state=ToolResultState.ERROR if failed else ToolResultState.SUCCESS,
                     is_last=True, metadata={"finance": envelope.model_dump(mode="json")})


def has_gaps(table) -> bool:
    if not table.rows or table.specification.get("coverage_missing"):
        return True
    metrics = table.specification.get("requested_metrics")
    for row in table.rows:
        cells = {cell.metric: cell for cell in row.cells}
        requested = metrics or list(cells)
        if not requested or any(m not in cells or cells[m].status != "available" or cells[m].value is None for m in requested):
            return True
    return False


class FinanceTools:
    def __init__(self, user_id: str, agent_id: str, session_id: str, *, repo, financials, calculations, reports,
                 filings=None, workspaces=None, skill_source=None):
        self.user_id = user_id
        self.agent_id = agent_id
        self.session_id = session_id
        self.repo = repo
        self.financials = financials
        self.calculations = calculations
        self.reports = reports
        self.filings = filings
        self.workspaces = workspaces
        # Production injects the lifecycle-bound source; standalone offline tests
        # can omit it or supply their own source without a native app lifecycle.
        self.skill_source = skill_source
        self.model = None
        self.tools = [self._make_tool(name, request) for name, request in REQUESTS.items()]

    def _make_tool(self, name, request_type):
        async def invoke(_agent_state=None, **kwargs):
            async with aclosing(self.stream(name, request_type, kwargs, _agent_state)) as stream:
                async for chunk in stream:
                    yield chunk

        return FunctionTool(
            invoke, name=name, description=DESCRIPTIONS[name], input_schema=request_type,
            is_state_injected=True, is_concurrency_safe=False,
            permission=PermissionDecision(behavior=PermissionBehavior.ALLOW,
                                          message="Server-scoped finance operation."),
        )

    async def _owned_inputs(self, ids):
        return [await self.repo.get_artifact(self.user_id, artifact_id) for artifact_id in dict.fromkeys(ids)]

    async def execute(self, name, request, context: ToolContext, progress) -> FinanceEnvelope:
        if name == "lookup_company":
            companies = await self.financials.lookup_company(request.query)
            return FinanceEnvelope(message="Company identity and coverage checked.", companies=companies)
        if name == "find_results":
            artifacts = await self.repo.find_artifacts(self.user_id, self.session_id, request)
            return FinanceEnvelope(message=f"Found {len(artifacts)} saved result(s).", artifacts=artifacts)
        if name == "fetch_filings":
            if self.filings is None:
                raise FinanceError("operation_failed", "The filing service is not available.")
            return await self.filings.fetch_filings(self.user_id, context.run_id, request, progress)
        if name == "data_fetch":
            table = await self.financials.fetch_data(request, progress)
            artifact = await self.repo.save_artifact(
                self.user_id, context.run_id, "dataset", "SEC financial data",
                data=table, warnings=table.warnings,
            )
            return FinanceEnvelope(message="Saved data with explicit coverage gaps." if has_gaps(table) else "Saved financial data.",
                                   artifacts=[artifact], warnings=table.warnings)
        if name == "calculate_metrics":
            inputs = await self._owned_inputs(request.data_ids)
            table = self.calculations.calculate_data(inputs, request)
            artifact = await self.repo.save_artifact(
                self.user_id, context.run_id, "table", "Calculated financial metrics",
                data=table, warnings=table.warnings, input_ids=request.data_ids,
            )
            return FinanceEnvelope(message="Saved calculation table.", artifacts=[artifact], warnings=table.warnings)
        if name == "data_analyze":
            tables = await self._owned_inputs(request.data_ids)
            documents = await self._owned_inputs(request.document_ids)
            if any(a.kind not in {"dataset", "table"} for a in tables) or any(a.kind != "document" for a in documents):
                raise FinanceError("invalid_input", "data_ids must reference numeric tables and document_ids must reference filings.")
            inputs = tables + documents
            title = narrative_title(inputs)
            await progress(analysis_record("start", title=title))

            async def body_delta(text):
                await progress(analysis_record("body", text=text))

            async def research_progress(text):
                await progress(analysis_record("progress", text=text))

            skill = (await self.skill_source.load_analysis_skill(
                user_id=self.user_id, agent_id=self.agent_id, session_id=self.session_id,
            ) if self.skill_source is not None else None)
            result = await analyze_data(
                inputs, request, model=self.model, progress=body_delta, research_progress=research_progress,
                actor=self.agent_id, session_id=self.session_id, run_id=context.run_id,
                user_id=self.user_id, read_document=getattr(self.repo, "read_document", None),
                workspaces=self.workspaces, skill=skill,
            )
            artifact = await self.repo.save_artifact(
                self.user_id, context.run_id, "narrative", title,
                input_ids=list(dict.fromkeys(request.data_ids + request.document_ids)), **result,
            )
            return FinanceEnvelope(message="Saved complete, citation-validated analysis.", artifacts=[artifact],
                                   warnings=result["warnings"])
        if name == "export_report":
            # The report service repeats ownership/type checks when resolving IDs.
            await self._owned_inputs(request.data_ids + request.analysis_ids)
            await progress("Generating Excel from saved results…\n")
            artifact = await self.reports.generate_report(self.user_id, context.run_id, request)
            return FinanceEnvelope(message="Excel report saved.", artifacts=[artifact], warnings=artifact.warnings)
        raise FinanceError("unknown_tool", "This tool is not available to the finance agent.")

    async def stream(self, name, request_type, kwargs, state):
        context = tool_context_var.get()
        try:
            if (context is None or context.operation != name or
                (context.user_id, context.agent_id, context.session_id) != (self.user_id, self.agent_id, self.session_id) or
                getattr(state, "session_id", None) != self.session_id or reply_id(state) != context.reply_id):
                raise FinanceError("invalid_scope", "The tool requires its server-injected session and operation context.")
            request = request_type.model_validate(kwargs)
        except FinanceError as exc:
            yield final_chunk(FinanceEnvelope(message=str(exc), error_code=exc.code), failed=True)
            return
        queue = asyncio.Queue(maxsize=16)
        analysis_started = False

        async def progress(text):
            nonlocal analysis_started
            # Analysis execute emits records; other tools emit ordinary progress.
            # Track protocol activation, never inspect or split prose semantically.
            if name == "data_analyze":
                analysis_started = True
            # Split without dropping bytes; all content is appended by native Toolkit.
            text = str(text)
            if name != "data_analyze" and text and not text.endswith("\n"):
                text += "\n"
            for offset in range(0, len(text), 1200):
                await queue.put(text[offset:offset + 1200])

        async def work():
            try:
                budget = (WORKSPACE_SETUP_SECONDS + RESEARCH_SECONDS + REPORT_SECONDS + 60
                          if name == "data_analyze" else TOOL_TIMEOUT_SECONDS)
                async with asyncio.timeout(budget):
                    return await self.execute(name, request, context, progress)
            except TimeoutError as exc:
                raise FinanceError("operation_timeout", "The operation exceeded its bounded execution budget; no complete result was produced.") from exc

        task = asyncio.create_task(work(), name=f"finance:{name}")
        get_task = None
        try:
            if name != "data_analyze":
                yield ToolChunk(content=[TextBlock(text=f"{name}: started.\n")],
                                state=ToolResultState.RUNNING, is_last=False)
            while not task.done() or not queue.empty():
                get_task = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait({task, get_task}, timeout=15, return_when=asyncio.FIRST_COMPLETED)
                if get_task in done:
                    yield ToolChunk(content=[TextBlock(text=get_task.result())],
                                    state=ToolResultState.RUNNING, is_last=False)
                else:
                    get_task.cancel()
                    await asyncio.gather(get_task, return_exceptions=True)
                get_task = None
                if not done:
                    await self.repo.touch_operation(context.run_id)
            envelope = await task
            yield final_chunk(envelope, failed=bool(envelope.error_code),
                              include_text=name == "find_results", framed=analysis_started)
        except FinanceError as exc:
            yield final_chunk(FinanceEnvelope(message=str(exc), error_code=exc.code), failed=True, framed=analysis_started)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Finance tool failed: %s", name)
            yield final_chunk(FinanceEnvelope(message="The finance service failed. Rephrasing the question cannot fix infrastructure errors. Retry at most once only if the failure appears transient; otherwise report the limitation.",
                                             error_code="operation_failed"), failed=True, framed=analysis_started)
        finally:
            if get_task is not None:
                get_task.cancel()
                await asyncio.gather(get_task, return_exceptions=True)
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
