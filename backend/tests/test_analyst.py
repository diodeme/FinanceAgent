"""Offline tests exercise public native Agent.reply, not a hand-written ReAct loop."""

import asyncio
import hashlib
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agentscope.formatter import DeepSeekChatFormatter
from agentscope.message import TextBlock, ToolCallBlock
from agentscope.model import ChatModelBase, ChatResponse, ChatUsage, FinishedReason
from agentscope.permission import PermissionBehavior
from agentscope.skill import Skill
from agentscope.tool import FunctionTool, ToolChunk, Toolkit

from finagent.analysis import analyze_data, document_citation_url, prepare_evidence, research_analysis, skill_identity, validate_citations
from finagent.analysis_workspace import AnalysisWorkspaces
from finagent.analyst_middleware import AnalystMiddleware, WORKSPACE_TOOL_NAMES
from finagent.contracts import AnalyzeRequest, Artifact, DocumentCitation, DocumentInfo, FinanceError
from finagent.tools import tool_context_var
from tests.test_finance_runtime import artifact

RAW = b'<html><body>Demand increased in the Americas.</body></html>'


def document():
    now = datetime.now(timezone.utc)
    return Artifact(artifact_id="doc/owned", run_id="prior", kind="document", title="AAPL filing", created_at=now,
                    document=DocumentInfo(source_document_id="source", company="AAPL", cik="0000320193",
                                          form="10-Q", period_end=date(2024, 3, 31), accession="0000320193-24-000001",
                                          primary_document="report.htm", source_url="https://www.sec.gov/Archives/edgar/data/320193/1/report.htm",
                                          sha256=hashlib.sha256(RAW).hexdigest(), byte_size=len(RAW), fetched_at=now))


def response(text="", tool=None, *, interrupted=False):
    blocks = [tool] if tool else [TextBlock(text=text)]
    return ChatResponse(content=blocks, is_last=True,
                        finished_reason=FinishedReason.INTERRUPTED if interrupted else FinishedReason.COMPLETED,
                        usage=ChatUsage(input_tokens=10, output_tokens=5, time=0.01))


class ScriptModel(ChatModelBase):
    def __init__(self, script):
        super().__init__(credential=None, model="offline-script", parameters=self.Parameters(), max_retries=0)
        self.formatter = DeepSeekChatFormatter()
        self.script = list(script)
        self.calls = []
        self.authorities = []

    async def __call__(self, **kwargs):
        self.calls.append({**kwargs, "messages": deepcopy(kwargs.get("messages", []))})
        self.authorities.append(tool_context_var.get())
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return await item()
        return item


class FakeWorkspaces:
    def __init__(self):
        self.files = {}
        self.tool_calls = []
        self.closed = 0
        self.opened = []
        self.block = None

    async def write_file(self, path, data):
        self.files[path] = data

    @asynccontextmanager
    async def open(self, **scope):
        self.opened.append(scope)
        try:
            yield self
        finally:
            self.closed += 1

    def get_backend(self):
        return self

    async def list_tools(self):
        tools = []
        for name in sorted(WORKSPACE_TOOL_NAMES):
            def make(current):
                async def invoke(path: str = ""):
                    self.tool_calls.append((current, path, tool_context_var.get()))
                    if self.block:
                        await self.block()
                    return ToolChunk(content=[TextBlock(text="Demand increased in the Americas.")])
                return FunctionTool(invoke, name=current, description=f"Offline {current}")
            tools.append(make(name))
        return tools


class AnalystTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.logs = AsyncMock()
        for target in ("finagent.runsteps.log_step", "finagent.analysis.log_step"):
            patcher = patch(target, self.logs)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.workspace = FakeWorkspaces()
        self.reader = AsyncMock(return_value=RAW)
        self.progress = AsyncMock()
        self.body = AsyncMock()
        self.link = document_citation_url("doc/owned")

    async def analyze(self, model, inputs=None, request=None, skill=None):
        return await analyze_data(inputs or [document()], request or AnalyzeRequest(document_ids=["doc/owned"], question="Explain demand."),
                                  model=model, progress=self.body, research_progress=self.progress,
                                  actor="agent", session_id="session", run_id="run", user_id="owner",
                                  read_document=self.reader, workspaces=self.workspace, skill=skill)

    async def test_native_multi_tool_research_then_one_report_call(self):
        script = [response(tool=ToolCallBlock(id=str(i), name=name, input=json.dumps({"path": "/workspace/inputs/document-0.html"})))
                  for i, name in enumerate(("Read", "Grep", "Bash"))]
        script += [response(f"Private research finding [filing]({self.link})."), response(f"Final report [filing]({self.link}).")]
        model = ScriptModel(script)
        sentinel = object()
        token = tool_context_var.set(sentinel)
        try:
            result = await self.analyze(model)
            self.assertIs(tool_context_var.get(), sentinel)
        finally:
            tool_context_var.reset(token)
        self.assertEqual(result["text"], f"Final report [filing]({self.link}).")
        self.assertEqual([call[0] for call in self.workspace.tool_calls], ["Read", "Grep", "Bash"])
        self.assertTrue(all(call[2] is None for call in self.workspace.tool_calls))
        self.assertTrue(all(authority is None for authority in model.authorities[:-1]))
        self.assertEqual(self.workspace.closed, 1)
        self.assertEqual(self.workspace.files["/workspace/inputs/document-0.html"], RAW)
        self.reader.assert_awaited_once_with("owner", "doc/owned")
        self.assertEqual(self.workspace.files["/workspace/inputs/numeric-evidence.json"], b"[]")
        self.assertEqual(len(model.calls), 5)
        self.assertEqual(model.calls[-1]["tools"], [])
        self.assertEqual(model.calls[-1]["tool_choice"].mode, "none")
        for call in model.calls[:-1]:
            self.assertEqual({t["function"]["name"] for t in call["tools"]}, WORKSPACE_TOOL_NAMES)
        body = "".join(call.args[0] for call in self.body.await_args_list)
        self.assertNotIn("Private research", body)
        self.assertEqual(body, result["text"])
        llm_logs = [c.kwargs for c in self.logs.await_args_list if c.kwargs["step_type"] == "llm"]
        tool_logs = [c.kwargs for c in self.logs.await_args_list if c.kwargs["step_type"] == "tool"]
        self.assertEqual(len(llm_logs), 5)
        self.assertEqual(len(tool_logs), 3)
        self.assertEqual(sum(c["tokens_out"] or 0 for c in llm_logs), 25)
        self.assertTrue(all(c.get("run_id", "run") == "run" for c in llm_logs))
        self.assertEqual(llm_logs[-1]["detail"]["phase"], "report_renderer")
        self.assertIsInstance(result["citations"][0], DocumentCitation)

    async def test_native_skill_snapshot_is_shared_with_renderer_and_logged_once(self):
        skill = Skill("financial-analysis", "Financial methodology", "/private/owner/skill", "Method-v1: explain cash quality.", 1.0)
        expected = skill_identity(skill)
        async def findings():
            skill.markdown = "Method-v2: changed during this run."
            return response("Cash quality research.")
        model = ScriptModel([response(tool=ToolCallBlock(id="skill", name="Skill", input='{"skill":"financial-analysis"}')),
                             findings, response(f"Cash quality report [filing]({self.link}).")])
        await self.analyze(model, skill=skill)
        self.assertNotIn("Method-v1", str(model.calls[0]["messages"]))
        self.assertIn("financial-analysis", str(model.calls[0]["messages"]))
        self.assertIn("Method-v1", str(model.calls[1]["messages"]))
        self.assertIn("Method-v1", str(model.calls[-1]["messages"]))
        self.assertNotIn("Method-v2", str(model.calls[-1]["messages"]))
        self.assertNotIn("/private/owner", str(model.calls))
        self.assertEqual(self.workspace.files["/workspace/skills/financial-analysis/SKILL.md"], b"Method-v1: explain cash quality.")
        self.assertEqual({t["function"]["name"] for t in model.calls[0]["tools"]}, WORKSPACE_TOOL_NAMES | {"Skill"})
        logs = [call.kwargs for call in self.logs.await_args_list]
        self.assertEqual(len([log for log in logs if log["step_type"] == "llm"]), 3)
        self.assertTrue(all(log["detail"]["skill"] == expected for log in logs))
        self.assertEqual([log["name"] for log in logs if log["step_type"] == "tool"], ["Skill"])

    async def test_attached_skill_must_be_loaded_before_report(self):
        skill = Skill("financial-analysis", "Method", "/source", "Read this method.", 1.0)
        model = ScriptModel([response("Skipped the skill.")])
        with self.assertRaises(FinanceError) as error:
            await self.analyze(model, skill=skill)
        self.assertEqual(error.exception.code, "skill_not_loaded")
        self.body.assert_not_awaited()
        self.assertEqual(self.workspace.closed, 1)

    async def test_skill_permission_is_the_exact_toolkit_viewer(self):
        tools = await self.workspace.list_tools()
        skill = Skill("financial-analysis", "Method", "/source", "Method.", 1.0)
        toolkit = Toolkit(tools=tools, skills_or_loaders=[skill])
        viewer = await toolkit.get_tool("Skill")
        other_viewer = await Toolkit(skills_or_loaders=[skill]).get_tool("Skill")
        middleware = AnalystMiddleware(tools=tools, skill_tool=viewer, progress=self.progress, actor="a", session_id="s")
        call = ToolCallBlock(id="s", name="Skill", input='{"skill":"financial-analysis"}')
        for tool, expected in ((viewer, PermissionBehavior.ALLOW), (other_viewer, PermissionBehavior.DENY),
                               (SimpleNamespace(name="Skill"), PermissionBehavior.DENY)):
            decision = await middleware.on_check_permission(None, {"tool": tool, "tool_call": call}, None)
            self.assertEqual(decision.behavior, expected)

    async def test_mixed_report_preserves_both_citations_and_user_presentation(self):
        inputs = [artifact(), document()]
        rows, _, _ = prepare_evidence(inputs)
        text = f"Numeric result {rows[0]['citation']}; filing explanation [filing]({self.link})."
        model = ScriptModel([response("Combined findings."), response(text)])
        result = await self.analyze(model, inputs, AnalyzeRequest(data_ids=[inputs[0].artifact_id],
                                   document_ids=["doc/owned"], question="300字以内，讨论收入和现金流。"))
        self.assertEqual(len(result["citations"]), 2)
        self.assertEqual(sum(isinstance(item, DocumentCitation) for item in result["citations"]), 1)
        self.assertIn("300字以内", str(model.calls[-1]["messages"]))
        self.assertIn("honor their explicit", str(model.calls[-1]["messages"]))
        self.assertNotIn("numeric_cells", result["text"])
        self.assertIn("not itself a data gap", str(model.calls[-1]["messages"]))

    async def test_numeric_files_are_multiline_lossless_and_original_html_unchanged(self):
        from finagent.analysis import RESEARCH_MAX_ITERS
        self.assertEqual(RESEARCH_MAX_ITERS, 50)
        data = artifact()
        row = data.data.rows[0]
        data.data.rows = [row.model_copy(update={"cells": [row.cells[0].model_copy(update={"cell_id": f"cell-{i}"})]})
                          for i in range(20)]
        before = data.model_dump(mode="json")
        expected, _, _ = prepare_evidence([data, document()])
        self.assertGreater(len(json.dumps(expected)), 2000)
        model = ScriptModel([response("Findings."), response(f"Report [filing]({self.link}).")])
        await self.analyze(model, [data, document()], AnalyzeRequest(data_ids=[data.artifact_id],
                           document_ids=["doc/owned"], question="Summarize the supplied evidence."))
        paths = ("/workspace/inputs/numeric-evidence.json", "/workspace/inputs/table-0.json",
                 "/workspace/inputs/manifest.json")
        for path in paths:
            text = self.workspace.files[path].decode()
            self.assertGreater(len(text.splitlines()), 1)
            self.assertLess(max(map(len, text.splitlines())), 2000)
        self.assertEqual(json.loads(self.workspace.files[paths[0]]), expected)
        self.assertEqual(json.loads(self.workspace.files[paths[1]]), before)
        self.assertEqual(data.model_dump(mode="json"), before)
        self.assertEqual(self.workspace.files["/workspace/inputs/document-1.html"], RAW)
        self.assertTrue(all(all(key in item for key in ("value", "unit", "period_end", "citation")) for item in expected))

    async def test_each_analysis_has_fresh_native_state(self):
        model = ScriptModel([response(f"First finding [f]({self.link})."), response(f"First report [f]({self.link})."),
                             response(f"Second finding [f]({self.link})."), response(f"Second report [f]({self.link}).")])
        await self.analyze(model)
        await self.analyze(model)
        second_initial = str(model.calls[2]["messages"])
        self.assertNotIn("First finding", second_initial)
        self.assertNotIn("First report", second_initial)
        self.assertEqual(self.workspace.closed, 2)

    async def test_max_iterations_rejects_report_and_closes_workspace(self):
        model = ScriptModel([response(tool=ToolCallBlock(id="repeat", name="Read", input='{"path":"x"}'))] * 4)
        with patch("finagent.analysis.RESEARCH_MAX_ITERS", 2), self.assertRaises(FinanceError):
            await self.analyze(model)
        self.assertEqual(self.workspace.closed, 1)
        self.body.assert_not_awaited()
        self.assertTrue(all(call["tools"] for call in model.calls))

    async def test_interruption_error_and_missing_model_terminal_are_rejected(self):
        async def incomplete():
            async def chunks():
                yield ChatResponse(content=[TextBlock(text="draft")], is_last=False)
            return chunks()
        for script in ([response("interrupted", interrupted=True)], [RuntimeError("provider failed")], [incomplete]):
            with self.subTest(script=script):
                self.workspace = FakeWorkspaces()
                with self.assertRaises((FinanceError, RuntimeError, asyncio.CancelledError)):
                    await self.analyze(ScriptModel(script))
                self.assertEqual(self.workspace.closed, 1)
                self.body.assert_not_awaited()

    async def test_cancel_waits_for_child_and_closes_workspace(self):
        entered, stopped = asyncio.Event(), asyncio.Event()
        async def block():
            try:
                entered.set()
                await asyncio.Event().wait()
            finally:
                stopped.set()
        self.workspace.block = block
        model = ScriptModel([response(tool=ToolCallBlock(id="long", name="Bash", input='{}'))])
        task = asyncio.create_task(self.analyze(model))
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(stopped.is_set())
        self.assertEqual(self.workspace.closed, 1)
        self.body.assert_not_awaited()
        self.assertFalse([t for t in asyncio.all_tasks() if t.get_name().startswith("finance-analyst:") and not t.done()])

    async def test_permission_is_instance_scoped(self):
        tools = await self.workspace.list_tools()
        middleware = AnalystMiddleware(tools=tools, progress=self.progress, actor="a", session_id="s")
        call = ToolCallBlock(id="x", name=tools[0].name, input="{}")
        decision = await middleware.on_check_permission(None, {"tool": tools[0], "tool_call": call}, None)
        self.assertEqual(decision.behavior, PermissionBehavior.ALLOW)
        decision = await middleware.on_check_permission(None, {"tool": SimpleNamespace(name=tools[0].name), "tool_call": call}, None)
        self.assertEqual(decision.behavior, PermissionBehavior.DENY)

    async def test_document_and_cell_whitelist_and_numeric_coverage(self):
        rows, allowed, _ = prepare_evidence([artifact(), document()])
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(allowed), 2)
        self.assertIsInstance(validate_citations(f"[f]({self.link})", allowed)[0], DocumentCitation)
        for invalid in ("#filing/someone-else", "#finance/doc%2Fowned/fake", self.link.replace("%2F", "/")):
            with self.assertRaises(FinanceError):
                validate_citations(f"[f]({invalid})", allowed)
        bad = artifact()
        bad.data.rows[0].cells[0].status = "missing"
        model = ScriptModel([])
        with self.assertRaises(FinanceError):
            await self.analyze(model, [bad, document()], AnalyzeRequest(data_ids=["input"], document_ids=["doc/owned"], question="Compare."))
        self.assertFalse(model.calls)
        self.assertFalse(self.workspace.opened)

    async def test_owned_document_failure_closes_without_model(self):
        self.reader.side_effect = FinanceError("not_found", "Not owned")
        model = ScriptModel([])
        with self.assertRaises(FinanceError):
            await self.analyze(model)
        self.assertEqual(self.workspace.closed, 1)
        self.assertFalse(model.calls)

    async def test_setup_failure_and_cancellation_remove_reserved_directory(self):
        for failure in (RuntimeError("startup failed"), asyncio.CancelledError()):
            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as directory:
                manager = SimpleNamespace(get_workspace=AsyncMock(side_effect=failure), close=AsyncMock())
                workspaces = AnalysisWorkspaces(manager, directory)
                with self.assertRaises(type(failure)):
                    async with workspaces.open(user_id="u", agent_id="a", session_id="s"):
                        self.fail("Failed initialization must not yield a workspace")
                workspace_id = manager.get_workspace.await_args.kwargs["workspace_id"]
                manager.close.assert_awaited_once_with(workspace_id)
                self.assertEqual(list(Path(directory).iterdir()), [])

    async def test_native_manager_unique_directory_cleanup(self):
        manager = SimpleNamespace(get_workspace=AsyncMock(return_value=SimpleNamespace()), close=AsyncMock())
        with tempfile.TemporaryDirectory() as directory:
            sibling = Path(directory) / "unrelated"
            sibling.mkdir()
            (sibling / "keep").write_text("keep")
            workspaces = AnalysisWorkspaces(manager, directory)
            for _ in range(2):
                async with workspaces.open(user_id="u", agent_id="a", session_id="s"):
                    pass
            ids = [c.kwargs["workspace_id"] for c in manager.get_workspace.await_args_list]
            self.assertEqual(len(set(ids)), 2)
            self.assertEqual([c.args[0] for c in manager.close.await_args_list], ids)
            self.assertEqual(list(Path(directory).iterdir()), [sibling])
            self.assertTrue((sibling / "keep").exists())


if __name__ == "__main__":
    unittest.main()
