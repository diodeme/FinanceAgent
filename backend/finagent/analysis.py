"""Coverage-gated narrative generation using the current session's native model."""

import asyncio
import hashlib
import inspect
import json
import re
import time
from contextlib import aclosing
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from agentscope.agent import Agent, ContextConfig, ReActConfig
from agentscope.message import SystemMsg, TextBlock, UserMsg
from agentscope.model import ChatResponse
from agentscope.skill import Skill
from agentscope.tool import ToolChoice, Toolkit

from .analyst_middleware import AnalystMiddleware, WORKSPACE_TOOL_NAMES
from .analysis_workspace import get_analysis_workspaces
from .contracts import AnalyzeRequest, Artifact, Citation, DocumentCitation, FinanceError
from .prompts import ANALYSIS_PROMPT, ANALYST_PROMPT
from .runsteps import log_step, run_id_var, state_value
from .warnings import ORIGINAL_DOCUMENT_WARNING, artifact_companies, normalize_warnings

RESEARCH_SECONDS = 180
REPORT_SECONDS = 120
RESEARCH_MAX_ITERS = 50
MAX_RESEARCH_CHARS = 20000  # Below the analyst's separate 24000-character total output budget.


def narrative_title(artifacts: list[Artifact]) -> str:
    """A bounded deterministic label from authorized evidence, never the question."""
    companies = sorted(artifact_companies(artifacts))
    labels = [re.sub(r"[\r\n\t]", " ", company)[:24] for company in companies[:3]]
    scope = " / ".join(labels)
    if len(companies) > 3:
        scope += f" 等{len(companies)}家公司"
    return (scope + (" 财务对比" if len(companies) > 1 else " 财务分析")).strip()


def citation_url(artifact_id: str, cell_id: str) -> str:
    return f"#finance/{quote(artifact_id, safe='')}/{quote(cell_id, safe='')}"


def document_citation_url(artifact_id: str) -> str:
    return f"#filing/{quote(artifact_id, safe='')}"


def prepare_evidence(artifacts: list[Artifact]) -> tuple[list[dict], dict[str, Citation | DocumentCitation], list[str]]:
    """Require the entire requested coverage before spending any model tokens."""
    rows = []
    allowed = {}
    warnings = []
    gaps = []
    for artifact in artifacts:
        if artifact.kind == "document" and artifact.document is not None:
            doc = artifact.document
            label = re.sub(r"[\[\]()\r\n]", " ", f"{doc.company} {doc.form} {doc.period_end}")
            allowed[document_citation_url(artifact.artifact_id)] = DocumentCitation(
                document_id=artifact.artifact_id, label=label,
            )
            warnings.extend(artifact.warnings)
            continue
        table = artifact.data
        if artifact.kind not in {"dataset", "table"} or table is None or not table.rows:
            raise FinanceError("missing_coverage", f"{artifact.artifact_id}: a nonempty numeric table is required.")
        warnings.extend(artifact.warnings)
        warnings.extend(table.warnings)
        gaps.extend(str(gap) for gap in table.specification.get("coverage_missing", []))
        requested = table.specification.get("requested_metrics")
        if not requested:
            requested = sorted({cell.metric for row in table.rows for cell in row.cells})
        if not requested:
            raise FinanceError("missing_coverage", "The table has no requested numeric metrics.")
        for row in table.rows:
            cells = {cell.metric: cell for cell in row.cells}
            for metric in requested:
                cell = cells.get(metric)
                if cell is None or cell.status != "available" or cell.value is None:
                    gaps.append(f"{row.company} {row.period_label} {metric}: {getattr(cell, 'reason', None) or 'missing'}")
                    continue
                try:
                    numeric = Decimal(cell.value).is_finite()
                except (InvalidOperation, ValueError):
                    numeric = False
                if not numeric or not cell.source_ids or any(s not in table.sources for s in cell.source_ids):
                    gaps.append(f"{row.company} {row.period_label} {metric}: missing numeric evidence")
                    continue
                url = citation_url(artifact.artifact_id, cell.cell_id)
                if url in allowed:
                    raise FinanceError("invalid_evidence", "Duplicate cell identity in the supplied evidence.")
                label = re.sub(r"[\[\]()\r\n]", " ", f"{row.company} {row.period_label} {cell.label}")
                allowed[url] = Citation(artifact_id=artifact.artifact_id, cell_id=cell.cell_id, label=label)
                rows.append({
                    "artifact_id": artifact.artifact_id, "cell_id": cell.cell_id,
                    "company": row.company, "period_start": str(row.period_start) if row.period_start else None,
                    "period_end": str(row.period_end), "period_label": row.period_label,
                    "metric": cell.metric, "value": cell.value, "unit": cell.unit,
                    "formula": cell.formula, "citation": f"[{label}]({url})",
                })
    if gaps:
        raise FinanceError("missing_coverage", "Analysis was not started. Required coverage is incomplete: " + "; ".join(gaps[:30]))
    if any(a.document is not None for a in artifacts) and ORIGINAL_DOCUMENT_WARNING in warnings:
        warnings = [w for w in warnings if w != ORIGINAL_DOCUMENT_WARNING]
        warnings.append("Numeric tables retain their original structured-API evidence; only the explicitly supplied filing documents are available for this analysis. Availability does not imply full-document review.")
    return rows, allowed, normalize_warnings(warnings, companies=artifact_companies(artifacts), include_diagnostics=False)


def normalize_citations(text: str, allowed: dict[str, Citation | DocumentCitation]) -> str:
    """Expand only standalone shorthand with an exact authorized target.

    Standard links are preserved, including invalid ones for validation to reject.
    Unknown or malformed shorthand is never repaired or removed.
    """
    pattern = (r"\[[^\[\]\n]+\]\([^\s()]+\)"
               r"|(?<![!\\\[])\[(?P<target>#(?:finance|filing)/[^\s\[\]()]+)\](?!\s*[\[\]()])")

    def expand(match):
        target = match.group("target")
        if target not in allowed:
            return match.group(0)
        # Do not turn a nested/malformed label into an apparently valid link.
        if text.rfind("[", 0, match.start()) > text.rfind("]", 0, match.start()):
            return match.group(0)
        return f"[{allowed[target].label}]({target})"

    return re.sub(pattern, expand, text)


def validate_citations(text: str, allowed: dict[str, Citation | DocumentCitation]) -> list[Citation | DocumentCitation]:
    """Accept only canonical, explicitly supplied artifact/cell links."""
    links = list(re.finditer(r"\[([^\[\]\n]+)\]\(([^\s()]+)\)", text))
    if not links:
        raise FinanceError(
            "invalid_citations", "Analysis has no standard Markdown evidence links. Use [label](target) "
            "with an exact supplied #finance/... or #filing/... target; bare [#...] is not the citation protocol. "
            "This is a missing-link or syntax error, not proof that sources are absent. "
            "No result was saved; do not present the streamed draft as a completed report.",
        )
    citations = []
    seen = set()
    for match in links:
        target = match.group(2)
        if target not in allowed or (match.start() and text[match.start() - 1] == "!"):
            raise FinanceError(
                "invalid_citations", "Analysis contains a target outside the supplied citation whitelist "
                "or an unsupported image citation. Copy the exact supplied [label](target) link; do not guess or repair IDs. "
                "No result was saved; do not present the streamed draft as a completed report.",
            )
        if target not in seen:
            citations.append(allowed[target].model_copy(update={"label": match.group(1)}))
            seen.add(target)
    remainder = re.sub(r"\[([^\[\]\n]+)\]\(([^\s()]+)\)", "", text)
    if re.search(r"#finance|#filing|(?:https?|ftp|file|javascript|data):|www\.|\]\s*[\[(]|<[^>]+>|^\s*\[[^\]]+\]:", remainder, re.I | re.M):
        raise FinanceError(
            "invalid_citations", "Analysis contains unsupported links or citation syntax. "
            "Use only complete [label](target) links copied from supplied evidence; "
            "malformed or unknown [#...] references are not accepted. "
            "No result was saved; do not present the streamed draft as a completed report.",
        )
    return citations


def skill_identity(skill: Skill | None) -> dict | None:
    if skill is None:
        return None
    return {"name": skill.name, "sha256": hashlib.sha256(skill.markdown.encode()).hexdigest()}


async def research_analysis(
    artifacts, request, *, rows, warnings, model, progress, actor, session_id,
    run_id, user_id, read_document, workspaces=None, skill: Skill | None = None,
) -> str:
    """Stage owned originals, then let a fresh native Agent choose its own tools."""
    await progress("Preparing native Docker analysis workspace (first image build may take several minutes).")
    service = workspaces or get_analysis_workspaces()
    async with service.open(user_id=user_id, agent_id=actor, session_id=session_id) as workspace:
        backend = workspace.get_backend()
        manifest = {"question": request.question, "numeric_evidence": "/workspace/inputs/numeric-evidence.json",
                    "artifacts": [], "warnings": warnings, "skill": skill_identity(skill)}
        run_skill = None
        if skill is not None:
            run_skill = replace(skill, dir="/workspace/skills/financial-analysis")
            await backend.write_file(run_skill.dir + "/SKILL.md", run_skill.markdown.encode())
            await progress(f"Using analysis skill: {skill.name}")
        else:
            await progress("No financial-analysis skill is attached; using baseline evidence rules.")
        await backend.write_file(manifest["numeric_evidence"], json.dumps(rows, ensure_ascii=False, indent=2).encode())
        for index, artifact in enumerate(artifacts):
            item = {"artifact_id": artifact.artifact_id, "kind": artifact.kind, "title": artifact.title}
            if artifact.document is not None:
                if read_document is None:
                    raise FinanceError("document_unavailable", "Owned document storage is unavailable.")
                # Repository validates ownership, pinned path and SHA-256 against the canonical original.
                raw = await read_document(user_id, artifact.artifact_id)
                item.update(path=f"/workspace/inputs/document-{index}.html",
                            document=artifact.document.model_dump(mode="json"),
                            citation=f"[Original filing]({document_citation_url(artifact.artifact_id)})")
                await backend.write_file(item["path"], raw)
            else:
                item["path"] = f"/workspace/inputs/table-{index}.json"
                await backend.write_file(item["path"], artifact.model_dump_json(indent=2).encode())
            manifest["artifacts"].append(item)
        await backend.write_file("/workspace/inputs/manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode())
        tools = await workspace.list_tools()
        if len(tools) != 6 or {tool.name for tool in tools} != WORKSPACE_TOOL_NAMES:
            raise FinanceError("invalid_workspace", "The analyst requires exactly the six native workspace tools.")
        toolkit = Toolkit(tools=tools, skills_or_loaders=[run_skill] if run_skill else [])
        skill_tool = await toolkit.get_tool("Skill") if run_skill else None
        if run_skill is not None and skill_tool is None:
            raise FinanceError("skill_unavailable", "The native financial-analysis Skill tool is unavailable.")
        observer = AnalystMiddleware(tools=tools, progress=progress, actor=actor, session_id=session_id,
                                     skill_tool=skill_tool, skill_info=skill_identity(skill))
        child = Agent(
            name="finance_analyst", system_prompt=ANALYST_PROMPT, model=model,
            toolkit=toolkit, middlewares=[observer],
            context_config=ContextConfig(compression_tool_enabled=False),
            react_config=ReActConfig(max_iters=RESEARCH_MAX_ITERS, interruption_raise_cancelled_error=True),
        )
        # No outer FinanceMiddleware, state, toolkit, default MCPs or authority.
        from .tools import tool_context_var
        authority = tool_context_var.set(None)
        run_token = run_id_var.set(run_id)
        child_task = None
        try:
            await progress("Researching the supplied evidence with native workspace tools.")
            async with asyncio.timeout(RESEARCH_SECONDS):
                child_task = asyncio.create_task(child.reply(UserMsg(
                    name="analysis_request", content=json.dumps({
                        "question": request.question, "manifest": "/workspace/inputs/manifest.json",
                    }, ensure_ascii=False),
                )), name=f"finance-analyst:{run_id}")
                final = await child_task
            if observer.finished_reason != "completed":
                code = "interrupted" if observer.finished_reason == "interrupted" else "incomplete_analysis"
                raise FinanceError(code, f"Research did not complete ({observer.finished_reason or 'no terminal event'}).")
            if run_skill is not None and not observer.skill_loaded:
                raise FinanceError("skill_not_loaded", "The analyst did not load the attached financial-analysis skill; no report was saved.")
            findings = "".join(block.text for block in final.content if isinstance(block, TextBlock))
            if not findings.strip():
                raise FinanceError("empty_research", "Research completed without findings; no report was saved.")
            if len(findings) > MAX_RESEARCH_CHARS:
                raise FinanceError("research_too_long", "Completed research exceeded the handoff resource limit; no report was saved. Return concise findings without repeating numeric tables.")
            return findings
        finally:
            if child_task is not None:
                if not child_task.done():
                    child_task.cancel()
                await asyncio.gather(child_task, return_exceptions=True)
            tool_context_var.reset(authority)
            run_id_var.reset(run_token)


async def analyze_data(
    artifacts: list[Artifact], request: AnalyzeRequest, *, model, progress,
    actor: str, session_id: str, run_id: str, user_id: str,
    research_progress, read_document=None, workspaces=None, skill: Skill | None = None,
) -> dict:
    skill = replace(skill) if skill is not None else None
    rows, allowed, warnings = prepare_evidence(artifacts)
    if model is None:
        raise FinanceError("model_unavailable", "Select a session model before running analysis.")
    findings = await research_analysis(
        artifacts, request, rows=rows, warnings=warnings, model=model, progress=research_progress,
        actor=actor, session_id=session_id, run_id=run_id, user_id=user_id,
        read_document=read_document, workspaces=workspaces, skill=skill,
    )
    await research_progress("Organizing findings into the final report (one additional model call).")
    async with asyncio.timeout(REPORT_SECONDS):
        return await render_report(artifacts, request, model=model, progress=progress,
                                   actor=actor, session_id=session_id, run_id=run_id, findings=findings, skill=skill)


async def render_report(
    artifacts: list[Artifact], request: AnalyzeRequest, *, model, progress,
    actor: str, session_id: str, run_id: str, findings: str = "", skill: Skill | None = None,
) -> dict:
    rows, allowed, warnings = prepare_evidence(artifacts)
    if model is None:
        raise FinanceError("model_unavailable", "Select a session model before running analysis.")
    methodology = ("Attached financial-analysis methodology (same snapshot used in research):\n"
                   + skill.markdown + "\n\n") if skill is not None else ""
    messages = [
        SystemMsg(name="finance_analysis", content=methodology + ANALYSIS_PROMPT),
        UserMsg(name="finance_evidence", content=json.dumps({
            "question": request.question, "numeric_cells": rows, "warnings": warnings,
            "research_findings": findings,
            "documents": [{"document": a.document.model_dump(mode="json"),
                           "citation": f"[{allowed[document_citation_url(a.artifact_id)].label}]({document_citation_url(a.artifact_id)})"}
                          for a in artifacts if a.document is not None],
        }, ensure_ascii=False)),
    ]
    started = time.monotonic()
    usage = None
    msg_id = None
    text = ""
    complete = False
    status = "error"
    detail = {"prompt_template": "finance-report-v3", "phase": "report_renderer",
              "input_ids": request.data_ids + request.document_ids, "skill": skill_identity(skill)}

    async def consume(chunk):
        nonlocal usage, msg_id, text, complete
        usage = getattr(chunk, "usage", None) or usage
        msg_id = getattr(chunk, "id", None) or msg_id
        if state_value(getattr(chunk, "finished_reason", None)) == "interrupted":
            raise FinanceError("interrupted", "Analysis was interrupted; no result was saved.")
        if any(getattr(block, "type", None) == "tool_call" for block in chunk.content):
            raise FinanceError("invalid_analysis", "The analysis model attempted an unavailable tool.")
        delta = "".join(block.text for block in chunk.content if isinstance(block, TextBlock))
        if len(delta) + (0 if chunk.is_last else len(text)) > 12000:
            raise FinanceError("analysis_too_long", "Analysis exceeded the bounded narrative length; no result was saved.")
        if chunk.is_last:
            # Native terminal ChatResponse contains the full response, not another delta.
            if not delta.startswith(text):
                raise FinanceError("invalid_analysis", "The final model snapshot differs from the streamed text.")
            suffix = delta[len(text):]
            text = delta
            complete = True
            if suffix:
                await progress(suffix)
        else:
            text += delta
            if delta:
                await progress(delta)

    try:
        result = model(messages=messages, tools=[], tool_choice=ToolChoice(mode="none"))
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, ChatResponse):
            await consume(result)
        else:
            async with aclosing(result):
                async for chunk in result:
                    await consume(chunk)
        if not complete or not text.strip():
            raise FinanceError("incomplete_analysis", "The model stream ended without a complete analysis; no result was saved.")
        # The card's terminal artifact is authoritative; do not append the report
        # again to the native stream when repairing an exact shorthand citation.
        text = normalize_citations(text, allowed)
        citations = validate_citations(text, allowed)
        status = "ok"
        return {"text": text, "citations": citations, "warnings": warnings}
    except (asyncio.CancelledError, GeneratorExit):
        detail["interrupted"] = True
        raise
    except FinanceError as exc:
        detail["error_code"] = exc.code
        if exc.code == "interrupted":
            detail["interrupted"] = True
        raise
    except Exception as exc:
        detail["error_type"] = type(exc).__name__
        raise
    finally:
        await log_step(
            actor=actor, session_id=session_id, step_type="llm",
            name=getattr(model, "model", None) or type(model).__name__,
            msg_id=msg_id, tokens_in=getattr(usage, "input_tokens", None),
            tokens_out=getattr(usage, "output_tokens", None),
            latency_ms=int((time.monotonic() - started) * 1000),
            detail=detail, status=status, run_id=run_id,
        )
