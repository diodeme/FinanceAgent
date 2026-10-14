"""Offline runtime regression tests against the installed AgentScope 2.x APIs.

Run: python -m unittest tests.test_finance_runtime -v
No database, network provider, credentials or real model are used.
"""

import asyncio
import json
import time
import unittest
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agentscope.agent import Agent, ContextConfig
from agentscope.app.middleware._tool_offload_middleware import ToolOffloadMiddleware
from agentscope.message import TextBlock, ToolCallBlock, ToolResultState
from agentscope.model import ChatResponse, ChatUsage, DeepSeekChatModel, FinishedReason
from agentscope.permission import PermissionBehavior
from agentscope.state import AgentState
from agentscope.tool import FunctionTool, ToolChoice, ToolChunk, Toolkit, ToolResponse

from finagent import analysis, runsteps
from finagent.analysis import render_report, citation_url, narrative_title, prepare_evidence, validate_citations
from finagent.sec_client import CONTACT_WARNING
from finagent.warnings import CROSS_COMPANY_WARNING, CROSS_COMPANY_QUARTERS_ALIAS, ORIGINAL_DOCUMENT_WARNING
from finagent.contracts import (
    AnalyzeRequest, Artifact, Cell, Evidence, FinanceError, FinancialRow, TableData,
)
from finagent.finance_middleware import FinanceMiddleware, finance_factory
from finagent.runsteps import RunStepsMiddleware, run_id_var
from finagent.tools import REQUESTS, tool_context_var


def evidence_table():
    evidence = Evidence(
        source_id="source", company="AAPL", concept="us-gaap:Revenues", metric="revenue",
        period_start=date(2024, 1, 1), period_end=date(2024, 3, 31), value="100", unit="USD",
        accession="000000-24-000001", form="10-Q", filed_at=date(2024, 5, 1),
        retrieved_at=datetime(2024, 5, 2, tzinfo=timezone.utc),
        api_url="https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json",
        filing_url="https://www.sec.gov/Archives/edgar/data/320193/filing.htm",
    )
    return TableData(rows=[FinancialRow(
        company="AAPL", period_start=date(2024, 1, 1), period_end=date(2024, 3, 31),
        period_label="2024 Q2", cells=[Cell(cell_id="AAPL/revenue/2024-03-31", metric="revenue",
                                          label="Revenue", value="100", unit="USD", source_ids=["source"])],
    )], sources={"source": evidence}, specification={"requested_metrics": ["revenue"]})


def artifact():
    return Artifact(artifact_id="input", run_id="prior", kind="dataset", title="Input",
                    created_at=datetime.now(timezone.utc), data=evidence_table())


class FakeRepo:
    def __init__(self):
        self.saved = []
        self.finished = []
        self.starts = []
        self.items = {"input": artifact()}
        self.replay = None

    async def start_operation(self, *args):
        self.starts.append(args)
        return self.replay or {"run_id": "run", "status": "running", "artifact_ids": [], "created": True}

    async def finish_operation(self, run_id, status, error=None):
        self.finished.append((run_id, status, error))

    async def touch_operation(self, run_id):
        pass

    async def get_artifact(self, user_id, artifact_id):
        if user_id != "u" or artifact_id not in self.items:
            raise FinanceError("not_found", "Saved result not found for this owner.")
        return self.items[artifact_id]

    async def find_artifacts(self, user_id, session_id, request):
        return list(self.items.values())

    async def save_artifact(self, user_id, run_id, kind, title, **kwargs):
        result = Artifact(artifact_id=f"saved-{len(self.saved)}", run_id=run_id, kind=kind, title=title,
                          created_at=datetime.now(timezone.utc), **kwargs)
        self.saved.append(result)
        self.items[result.artifact_id] = result
        return result


class FakeModel:
    model = "fake-session-model"

    def __init__(self, text=None, *, interrupted=False, fail_start=False, fail_stream=False, incomplete=False):
        self.text = text or f"Revenue was 100 USD [revenue]({citation_url('input', 'AAPL/revenue/2024-03-31')})."
        self.interrupted = interrupted
        self.fail_start = fail_start
        self.fail_stream = fail_stream
        self.incomplete = incomplete
        self.calls = []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail_start:
            raise RuntimeError("fake provider startup failure")

        async def stream():
            yield ChatResponse(content=[TextBlock(text=self.text[:10])], is_last=False)
            if self.fail_stream:
                raise RuntimeError("fake stream failure")
            if not self.incomplete:
                yield ChatResponse(content=[TextBlock(text=self.text)], is_last=True,
                                   finished_reason=FinishedReason.INTERRUPTED if self.interrupted else FinishedReason.COMPLETED,
                                   usage=ChatUsage(input_tokens=50, output_tokens=20, time=0.01))
        return stream()


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.logs = AsyncMock()
        # This suite exercises the outer persistence/framing/report boundary.
        # The native research loop is covered without this stub in test_analyst.
        self.patchers = [patch("finagent.analysis.research_analysis", AsyncMock(return_value="Verified findings.")),
                         patch("finagent.analysis.log_step", self.logs),
                         patch("finagent.runsteps.log_step", self.logs),
                         patch("finagent.finance_middleware.log_step", self.logs)]
        for p in self.patchers:
            p.start()
            self.addCleanup(p.stop)
        self.repo = FakeRepo()
        self.model = FakeModel()

        async def fetch(request, progress):
            await progress("Fetching official numeric facts.\n")
            return evidence_table()

        self.financials = SimpleNamespace(fetch_data=fetch, lookup_company=AsyncMock(return_value=[]))
        self.calculations = SimpleNamespace(calculate_data=lambda inputs, request: evidence_table())
        self.reports = SimpleNamespace(generate_report=AsyncMock())
        self.middleware = FinanceMiddleware("u", "a", "s", repo=self.repo, financials=self.financials,
                                            calculations=self.calculations, reports=self.reports,
                                            registry=SimpleNamespace(metric_prompt=lambda: "Registered metric definitions."))
        self.state = AgentState(session_id="s")
        self.agent = SimpleNamespace(state=self.state, model=self.model, toolkit=Toolkit(), context_config=ContextConfig())
        self.middleware.runtime.model = self.model
        self.agent.toolkit = Toolkit(tools=await self.middleware.list_tools())
        self.steps = RunStepsMiddleware("a", "s")

    def call(self, name, inputs, call_id="call"):
        return ToolCallBlock(id=call_id, name=name, input=json.dumps(inputs))

    def operation_stream(self, name="data_fetch", inputs=None, offload=None, call_id="call"):
        call = self.call(name, inputs or {"companies": ["AAPL"], "metrics": ["revenue"]}, call_id)

        def native(**kwargs):
            return self.agent.toolkit.call_tool(kwargs["tool_call"], self.state)

        def logged(**kwargs):
            return self.steps.on_acting(self.agent, kwargs, native)

        def finance(**kwargs):
            return self.middleware.on_acting(self.agent, kwargs, logged)

        return finance(tool_call=call) if offload is None else offload.on_acting(self.agent, {"tool_call": call}, finance)

    async def collect(self, **kwargs):
        return [event async for event in self.operation_stream(**kwargs)]

    async def test_six_native_tools_and_business_only_schemas(self):
        tools = await self.middleware.list_tools()
        self.assertEqual({t.name for t in tools}, set(REQUESTS))
        for tool in tools:
            self.assertIsInstance(tool, FunctionTool)
            self.assertTrue(tool.is_state_injected)
            self.assertFalse(tool.is_concurrency_safe)
            serialized = json.dumps(tool.input_schema)
            self.assertNotIn("_agent_state", serialized)
            self.assertNotIn("user_id", serialized)
            self.assertNotIn("session_id", serialized)
            self.assertFalse(tool.input_schema.get("additionalProperties", True))

    async def test_finance_only_toolkit_preserves_prompt_and_binds_selected_model(self):
        async def bash():
            return ToolChunk(content=[])
        self.agent.toolkit = Toolkit(tools=[FunctionTool(bash, name="Bash", description="forbidden")])
        self.agent.context_config.compression_tool_enabled = True
        async def reply(**kwargs):
            yield "reply"
        result = [e async for e in self.middleware.on_reply(self.agent, {}, reply)]
        self.assertEqual(result, ["reply"])
        self.assertIs(self.middleware.runtime.model, self.model)
        self.assertIsNone(await self.agent.toolkit.get_tool("Bash"))
        self.assertFalse(self.agent.context_config.compression_tool_enabled)
        prompt = await self.middleware.on_system_prompt(self.agent, "My saved custom prompt")
        self.assertTrue(prompt.startswith("My saved custom prompt"))
        self.assertIn("prevail", prompt)
        self.assertIn("Registered metric definitions.", prompt)
        self.assertIn("No general web search", prompt)
        self.assertIn("displays its requested_metrics", prompt)
        self.assertIn('metrics=["revenue", "gross_margin"]', prompt)
        self.assertIn("include all relevant\nsaved tables", prompt)

    async def test_permissions_are_instance_scoped_not_name_based(self):
        tool = (await self.middleware.list_tools())[0]
        args = {"tool": tool, "tool_call": self.call(tool.name, {"query": "AAPL"})}
        decision = await self.middleware.on_check_permission(self.agent, args, AsyncMock())
        self.assertEqual(decision.behavior, PermissionBehavior.ALLOW)
        args["tool"] = SimpleNamespace(name=tool.name)
        decision = await self.middleware.on_check_permission(self.agent, args, AsyncMock())
        self.assertEqual(decision.behavior, PermissionBehavior.DENY)

    async def test_native_stream_final_metadata_and_run_context(self):
        events = await self.collect()
        self.assertTrue(any(isinstance(e, ToolChunk) and not e.is_last for e in events))
        for event in events:
            if isinstance(event, ToolChunk) and not event.is_last:
                self.assertFalse(event.metadata)
        self.assertIsInstance(events[-1], ToolResponse)
        self.assertEqual(events[-1].state, ToolResultState.SUCCESS)
        self.assertEqual(events[-1].metadata["finance"]["artifacts"][0]["artifact_id"], "saved-0")
        self.assertEqual(self.repo.finished[0][1], "succeeded")
        self.assertEqual(len(self.repo.starts), 1)
        self.assertIsNone(run_id_var.get())
        self.assertIsNone(tool_context_var.get())

    async def test_streaming_past_native_default_ten_second_offload_threshold(self):
        async def fetch(request, progress):
            await progress("first progress\n")
            await asyncio.sleep(10.05)
            await progress("past threshold\n")
            return evidence_table()
        self.financials.fetch_data = fetch
        bg = SimpleNamespace(register_task=AsyncMock())
        offload = ToolOffloadMiddleware(bg, SimpleNamespace(), "u", "a")
        started = time.monotonic()
        arrivals = []
        async for event in self.operation_stream(offload=offload):
            arrivals.append((time.monotonic() - started, event))
        self.assertLess(arrivals[0][0], 1)
        self.assertGreaterEqual(arrivals[-1][0], 10)
        self.assertEqual(arrivals[-1][1].state, ToolResultState.SUCCESS)
        bg.register_task.assert_not_awaited()
        self.assertTrue(any("past threshold" in b.text for _, e in arrivals for b in e.content))

    async def test_native_cancel_cleans_worker_and_never_saves_success(self):
        running = asyncio.Event()
        stopped = asyncio.Event()
        async def fetch(request, progress):
            try:
                running.set()
                await asyncio.Event().wait()
            finally:
                stopped.set()
        self.financials.fetch_data = fetch
        bg = SimpleNamespace(register_task=AsyncMock())
        offload = ToolOffloadMiddleware(bg, SimpleNamespace(), "u", "a", timeout_secs=0.01)
        task = asyncio.create_task(self.collect(offload=offload))
        await asyncio.wait_for(running.wait(), timeout=1)
        task.cancel()
        try:
            events = await task
            self.assertEqual(events[-1].state, ToolResultState.INTERRUPTED)
        except asyncio.CancelledError:
            pass
        self.assertTrue(stopped.is_set())
        self.assertFalse(self.repo.saved)
        self.assertEqual(self.repo.finished[-1][1], "interrupted")
        bg.register_task.assert_not_awaited()
        self.assertFalse([t for t in asyncio.all_tasks() if t.get_name().startswith("finance:") and not t.done()])

    async def test_domain_error_has_no_success_terminal(self):
        async def fail(request, progress):
            raise FinanceError("no_data", "No data available.")
        self.financials.fetch_data = fail
        events = await self.collect()
        self.assertEqual(events[-1].state, ToolResultState.ERROR)
        self.assertEqual(events[-1].metadata["finance"]["error_code"], "no_data")
        self.assertEqual(self.repo.finished[-1][1], "failed")
        self.assertFalse(self.repo.saved)
        self.assertEqual(self.logs.await_args.kwargs["status"], "error")

    async def test_partial_data_stays_partial_not_success(self):
        table = evidence_table()
        table.specification["coverage_missing"] = ["Expected four periods, found one."]
        async def fetch(request, progress):
            return table
        self.financials.fetch_data = fetch
        await self.collect()
        self.assertEqual(self.repo.finished[-1][1], "partial")

    async def test_saved_replay_does_not_create_or_fetch_again(self):
        self.repo.replay = {"run_id": "run", "status": "succeeded", "artifact_ids": ["input"], "created": False}
        self.financials.fetch_data = AsyncMock(side_effect=AssertionError("must not fetch replay"))
        events = await self.collect()
        self.assertEqual(events[-1].metadata["finance"]["artifacts"][0]["artifact_id"], "input")
        self.assertFalse(self.repo.saved)
        self.assertFalse(self.repo.finished)

    async def test_analysis_replay_keeps_framing_compact_response_and_saved_metadata(self):
        await self.collect(name="data_analyze", inputs={"data_ids": ["input"], "question": "Analyze."})
        saved = self.repo.saved[0]
        before = saved.model_dump(mode="json")
        self.repo.replay = {"run_id": "run", "status": "succeeded", "artifact_ids": [saved.artifact_id], "created": False}
        self.repo.finished.clear()
        self.middleware.runtime.model = AsyncMock(side_effect=AssertionError("must not rerun model"))
        events = await self.collect(name="data_analyze", inputs={"data_ids": ["input"], "question": "Analyze."})
        records = [json.loads(line) for line in "".join(b.text for e in events if isinstance(e, ToolChunk) for b in e.content).splitlines()]
        self.assertEqual([r["type"] for r in records], ["start", "summary"])
        self.assertEqual(records[0]["title"], saved.title)
        self.assertNotIn("finance_stream", events[-1].content[0].text)
        self.assertEqual(events[-1].metadata["finance"]["artifacts"][0], before)
        self.assertEqual(saved.model_dump(mode="json"), before)
        self.assertFalse(self.repo.finished)
        self.assertEqual(len(self.repo.saved), 1)

    async def test_running_replay_is_busy_without_work(self):
        self.repo.replay = {"run_id": "run", "status": "running", "artifact_ids": [], "created": False}
        events = await self.collect()
        self.assertEqual(events[-1].metadata["finance"]["error_code"], "operation_busy")
        self.assertFalse(self.repo.saved)
        self.assertFalse(self.repo.finished)

    async def test_hidden_state_is_required(self):
        tool = next(t for t in self.middleware.runtime.tools if t.name == "lookup_company")
        stream = await tool(query="AAPL", _agent_state=self.state)
        chunks = [chunk async for chunk in stream]
        self.assertEqual(chunks[-1].metadata["finance"]["error_code"], "invalid_scope")
        self.financials.lookup_company.assert_not_awaited()

    async def test_analysis_final_snapshot_is_not_duplicated(self):
        events = await self.collect(name="data_analyze", inputs={"data_ids": ["input"], "question": "Compare revenue."})
        self.assertEqual(events[-1].state, ToolResultState.SUCCESS)
        self.assertEqual(self.repo.saved[0].text, self.model.text)
        incremental = "".join(b.text for e in events if isinstance(e, ToolChunk) for b in e.content)
        records = [json.loads(line) for line in incremental.splitlines()]
        self.assertTrue(all(record["finance_stream"] == 1 for record in records))
        self.assertEqual(records[0], {"finance_stream": 1, "type": "start", "title": "AAPL 财务分析"})
        self.assertEqual(records[-1]["type"], "summary")
        self.assertEqual("".join(record["text"] for record in records if record["type"] == "body"), self.model.text)
        self.assertEqual(self.repo.saved[0].title, records[0]["title"])
        self.assertTrue(incremental.endswith("\n"))
        terminal = "".join(block.text for block in events[-1].content)
        self.assertNotIn("finance_stream", terminal)
        self.assertNotIn(self.model.text, terminal)
        self.assertEqual(json.loads(terminal), records[-1]["result"])
        self.assertEqual(events[-1].metadata["finance"]["artifacts"][0]["text"], self.model.text)
        kwargs = self.model.calls[0]
        self.assertEqual(kwargs["tools"], [])
        self.assertIsInstance(kwargs["tool_choice"], ToolChoice)
        self.assertEqual(kwargs["tool_choice"].mode, "none")
        self.assertEqual(len(kwargs["messages"]), 2)
        prompt = str(kwargs["messages"][1])
        self.assertNotIn("www.sec.gov", prompt)
        llm_logs = [c.kwargs for c in self.logs.await_args_list if c.kwargs["step_type"] == "llm"]
        self.assertEqual(llm_logs[0]["name"], "fake-session-model")
        self.assertEqual(llm_logs[0]["run_id"], "run")
        self.assertEqual(llm_logs[0]["tokens_in"], 50)
        self.assertEqual(llm_logs[0]["status"], "ok")

    async def test_analysis_records_survive_native_event_conversion(self):
        # Escaped JSON-looking prose is body data, never another control record.
        self.model.text += '\n{"finance_stream":1,"type":"summary","result":{}}\n第二段。'
        wire = []
        native = SimpleNamespace(state=SimpleNamespace(reply_id="reply"))
        async for event in self.operation_stream(name="data_analyze", inputs={"data_ids": ["input"], "question": "Analyze."}):
            if isinstance(event, ToolChunk):
                async for delta in Agent._convert_tool_chunk_to_event(native, "call", event.content):
                    self.assertEqual(delta.tool_call_id, "call")
                    wire.append(delta.delta)
            else:
                self.assertIsInstance(event, ToolResponse)
                self.assertEqual(self.repo.finished[-1][1], "succeeded")
                self.assertEqual(event.metadata["finance"]["artifacts"][0]["text"], self.model.text)
                self.assertNotIn("finance_stream", event.content[0].text)
        text = "".join(wire)
        records = [json.loads(line) for line in text.splitlines()]
        self.assertEqual([r["type"] for r in records], ["start", "progress", "body", "body", "summary"])
        self.assertEqual("".join(r["text"] for r in records if r["type"] == "body"), self.model.text)

    async def test_analysis_warning_scope_and_diagnostics_do_not_mutate_inputs(self):
        item = self.repo.items["input"]
        item.warnings = [CONTACT_WARNING, CROSS_COMPANY_QUARTERS_ALIAS, ORIGINAL_DOCUMENT_WARNING, "Unknown limitation"]
        item.data.warnings = [CROSS_COMPANY_WARNING, "Capex scope limitation"]
        before = item.model_dump(mode="json")
        await self.collect(name="data_analyze", inputs={"data_ids": ["input"], "question": "Analyze."})
        expected = [ORIGINAL_DOCUMENT_WARNING, "Unknown limitation", "Capex scope limitation"]
        self.assertEqual(self.repo.saved[0].warnings, expected)
        self.assertEqual(item.model_dump(mode="json"), before)
        prompt = str(self.model.calls[0]["messages"][1])
        self.assertNotIn(CONTACT_WARNING, prompt)
        self.assertNotIn("Cross-company", prompt)
        other = artifact().model_copy(deep=True)
        other.artifact_id = "other"
        other.data.rows[0].company = "MSFT"
        _, _, warnings = prepare_evidence([item, other])
        self.assertEqual(warnings.count(CROSS_COMPANY_WARNING), 1)

    async def test_narrative_titles_use_unique_authorized_companies_not_question(self):
        first = artifact()
        other = artifact().model_copy(deep=True)
        other.data.rows[0].company = "MSFT"
        self.assertEqual(narrative_title([first, first]), "AAPL 财务分析")
        self.assertEqual(narrative_title([other, first]), "AAPL / MSFT 财务对比")
        self.assertEqual(narrative_title([first, other]), narrative_title([other, first]))
        fixtures = []
        for i in range(15):
            item = artifact().model_copy(deep=True)
            item.data.rows[0].company = str(i) + "x" * 200
            fixtures.append(item)
        self.assertLess(len(narrative_title(fixtures)), 120)
        self.assertNotIn("Q", narrative_title([first]))

    async def test_analysis_persistence_failure_never_publishes_success(self):
        async def fail_finish(run_id, status, error=None):
            if status == "succeeded":
                raise RuntimeError("fake persistence failure")
        self.repo.finish_operation = fail_finish
        events = []
        with self.assertRaises(RuntimeError):
            async for event in self.operation_stream(name="data_analyze", inputs={"data_ids": ["input"], "question": "Analyze."}):
                events.append(event)
        self.assertTrue(events)
        self.assertFalse(any(e.state == ToolResultState.SUCCESS for e in events))
        wire = "".join(b.text for e in events for b in e.content)
        self.assertNotIn('"type": "summary"', wire)

    async def test_analysis_cancel_after_start_cleans_model_and_never_saves(self):
        entered = asyncio.Event()
        stopped = asyncio.Event()
        async def model(**kwargs):
            async def stream():
                try:
                    entered.set()
                    await asyncio.Event().wait()
                    yield ChatResponse(content=[], is_last=True)
                finally:
                    stopped.set()
            return stream()
        self.middleware.runtime.model = model
        events = []
        async def collect():
            async for event in self.operation_stream(name="data_analyze", inputs={"data_ids": ["input"], "question": "Analyze."}):
                events.append(event)
        task = asyncio.create_task(collect())
        await asyncio.wait_for(entered.wait(), timeout=1)
        # Native processing may need one loop turn to publish the queued start.
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self.assertTrue(stopped.is_set())
        self.assertFalse(self.repo.saved)
        self.assertEqual(self.repo.finished[-1][1], "interrupted")
        self.assertFalse(any(e.state == ToolResultState.SUCCESS for e in events))
        self.assertFalse([t for t in asyncio.all_tasks() if t.get_name().startswith("finance:") and not t.done()])

    async def test_analysis_large_delta_preserves_record_across_queue_chunks(self):
        self.model.text = "数据\n" * 1000 + self.model.text
        events = await self.collect(name="data_analyze", inputs={"data_ids": ["input"], "question": "Analyze."})
        chunks = [b.text for e in events if isinstance(e, ToolChunk) for b in e.content]
        self.assertTrue(any(not chunk.endswith("\n") for chunk in chunks))
        records = [json.loads(line) for line in "".join(chunks).splitlines()]
        self.assertEqual("".join(r["text"] for r in records if r["type"] == "body"), self.model.text)
        self.assertEqual(events[-1].metadata["finance"]["artifacts"][0]["text"], self.model.text)

    async def test_analysis_failure_after_start_is_framed_without_saved_artifact(self):
        self.model.text = "Uncited draft."
        events = await self.collect(name="data_analyze", inputs={"data_ids": ["input"], "question": "Analyze."})
        records = [json.loads(line) for line in "".join(b.text for e in events if isinstance(e, ToolChunk) for b in e.content).splitlines()]
        self.assertEqual(records[0]["type"], "start")
        self.assertEqual(records[-1]["type"], "summary")
        self.assertEqual(records[-1]["result"]["error_code"], "invalid_citations")
        self.assertEqual(records[-1]["result"]["artifacts"], [])
        self.assertFalse(self.repo.saved)
        self.assertEqual(events[-1].state, ToolResultState.ERROR)

    async def test_analysis_uses_native_deepseek_tool_choice_formatting(self):
        class NativeFormattingFake(FakeModel):
            async def __call__(self, **kwargs):
                # Exercise the real provider adapter boundary, but neither build
                # a provider client nor invoke its HTTP/model API.
                self.formatted = DeepSeekChatModel._format_tools(
                    self, kwargs["tools"], kwargs["tool_choice"],
                )
                return await super().__call__(**kwargs)

        model = NativeFormattingFake()
        self.middleware.runtime.model = model
        events = await self.collect(name="data_analyze", inputs={"data_ids": ["input"], "question": "Analyze revenue."})
        self.assertEqual(events[-1].state, ToolResultState.SUCCESS)
        self.assertEqual(model.formatted, ([], "none"))
        self.assertEqual(self.repo.saved[0].text, model.text)
        # Demonstrate that this adapter-level regression detects the former
        # string input that an unconstrained fake model accepted.
        with self.assertRaises(AttributeError):
            DeepSeekChatModel._format_tools(model, [], "none")

    async def test_infrastructure_retry_guard_counts_tool_not_rephrased_question(self):
        self.model.fail_start = True
        errors = []
        for index in range(3):
            if index < 2:
                with self.assertLogs("finagent.tools", level="ERROR"):
                    events = await self.collect(name="data_analyze", call_id=f"call-{index}",
                                                inputs={"data_ids": ["input"], "question": f"Different question {index}."})
            else:
                events = await self.collect(name="data_analyze", call_id=f"call-{index}",
                                            inputs={"data_ids": ["input"], "question": f"Different question {index}."})
            errors.append(events[-1].metadata["finance"]["error_code"])
        self.assertEqual(errors, ["operation_failed", "operation_failed", "infrastructure_retry_exhausted"])
        self.assertEqual(len(self.model.calls), 2)
        self.assertEqual(len(self.repo.starts), 2)
        self.assertEqual(self.middleware._infrastructure_failures["data_analyze"], 2)
        self.assertFalse(self.repo.saved)
        # Independent tools remain available; the guard does not force a loop
        # or prevent recovery of already completed results.
        events = await self.collect(name="lookup_company", call_id="lookup", inputs={"query": "AAPL"})
        self.assertEqual(events[-1].state, ToolResultState.SUCCESS)
        async def reply(**kwargs):
            yield "new turn"
        _ = [event async for event in self.middleware.on_reply(self.agent, {}, reply)]
        self.assertFalse(self.middleware._infrastructure_failures)

    async def test_analysis_missing_coverage_never_calls_model(self):
        self.repo.items["input"].data.rows[0].cells[0].value = None
        self.repo.items["input"].data.rows[0].cells[0].status = "missing"
        events = await self.collect(name="data_analyze", inputs={"data_ids": ["input"], "question": "Analyze."})
        self.assertEqual(events[-1].metadata["finance"]["error_code"], "missing_coverage")
        self.assertFalse(self.model.calls)
        self.assertFalse(self.repo.saved)

    async def test_analysis_period_coverage_never_calls_model(self):
        self.repo.items["input"].data.specification["coverage_missing"] = ["Only one of four periods available."]
        await self.collect(name="data_analyze", inputs={"data_ids": ["input"], "question": "Analyze."})
        self.assertFalse(self.model.calls)
        self.assertFalse(self.repo.saved)

    async def test_optional_missing_dependency_does_not_block_requested_complete_metric(self):
        self.repo.items["input"].data.rows[0].cells.append(Cell(cell_id="optional", metric="cash", label="Cash",
                                                              value=None, unit="USD", status="missing"))
        await self.collect(name="data_analyze", inputs={"data_ids": ["input"], "question": "Analyze."})
        self.assertEqual(len(self.repo.saved), 1)

    async def test_unowned_analysis_input_fails_before_model(self):
        events = await self.collect(name="data_analyze", inputs={"data_ids": ["other-user"], "question": "Analyze."})
        self.assertEqual(events[-1].state, ToolResultState.ERROR)
        self.assertFalse(self.model.calls)
        self.assertFalse(self.repo.saved)

    async def test_interrupted_model_response_is_not_saved(self):
        self.model.interrupted = True
        events = await self.collect(name="data_analyze", inputs={"data_ids": ["input"], "question": "Analyze."})
        self.assertEqual(events[-1].state, ToolResultState.ERROR)
        self.assertFalse(self.repo.saved)
        self.assertEqual(self.repo.finished[-1][1], "interrupted")

    async def test_bad_citations_are_not_saved(self):
        self.model.text = "Revenue [source](https://untrusted.example/filing)."
        events = await self.collect(name="data_analyze", inputs={"data_ids": ["input"], "question": "Analyze."})
        self.assertEqual(events[-1].metadata["finance"]["error_code"], "invalid_citations")
        self.assertFalse(self.repo.saved)

    async def test_citation_validation_encodes_ids_and_rejects_forged_links(self):
        _, allowed, _ = prepare_evidence([artifact()])
        good = self.model.text
        self.assertIn("%2F", next(iter(allowed)))
        self.assertEqual(len(validate_citations(good, allowed)), 1)
        bad_values = [good + " [fake](#finance/other/cell)", good + " https://evil.example",
                      good + " [source][ref]\n[ref]: https://evil.example", good + " <a href='evil'>source</a>",
                      good + " [broken](#finance/input/not-known)"]
        for bad in bad_values:
            with self.subTest(text=bad), self.assertRaises(FinanceError):
                validate_citations(bad, allowed)

    async def test_model_startup_and_stream_errors_are_logged(self):
        for kwargs in ({"fail_start": True}, {"fail_stream": True}, {"incomplete": True}):
            with self.subTest(kwargs=kwargs):
                self.logs.reset_mock()
                model = FakeModel(**kwargs)
                with self.assertRaises((RuntimeError, FinanceError)):
                    await render_report([artifact()], AnalyzeRequest(data_ids=["input"], question="Analyze."),
                                       model=model, progress=AsyncMock(), actor="a", session_id="s", run_id="run")
                self.assertEqual(self.logs.await_args.kwargs["status"], "error")

    async def test_native_model_logging_startup_interrupt_and_cancel(self):
        async def fail(**kwargs):
            raise RuntimeError("startup failure")
        with self.assertRaises(RuntimeError):
            await self.steps.on_model_call(self.agent, {"current_model": self.model}, fail)
        self.assertEqual(self.logs.await_args.kwargs["status"], "error")
        self.assertEqual(self.logs.await_args.kwargs["name"], "fake-session-model")
        async def interrupted(**kwargs):
            return ChatResponse(content=[], is_last=True, finished_reason=FinishedReason.INTERRUPTED)
        await self.steps.on_model_call(self.agent, {"current_model": self.model}, interrupted)
        self.assertEqual(self.logs.await_args.kwargs["detail"], {"interrupted": True})
        async def cancel(**kwargs):
            raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.steps.on_model_call(self.agent, {"current_model": self.model}, cancel)
        self.assertEqual(self.logs.await_args.kwargs["status"], "error")

    async def test_only_provisioned_identity_enables_finance_factory(self):
        with patch("finagent.repository.is_finance_agent", AsyncMock(return_value=False)) as check:
            middlewares = await finance_factory("u", "ordinary-agent", "s")
        check.assert_awaited_once_with("u", "ordinary-agent")
        self.assertEqual(len(middlewares), 1)
        self.assertIsInstance(middlewares[0], RunStepsMiddleware)
        with (patch("finagent.repository.is_finance_agent", AsyncMock(return_value=True)),
              patch("finagent.financials", self.financials, create=True),
              patch("finagent.calculations", self.calculations, create=True),
              patch("finagent.reports", self.reports, create=True),
              patch("finagent.registry", self.middleware.registry, create=True)):
            middlewares = await finance_factory("u", "a", "s")
        self.assertIsInstance(middlewares[0], FinanceMiddleware)
        self.assertIsInstance(middlewares[1], RunStepsMiddleware)

    async def test_turn_budget_fails_before_starting_an_operation(self):
        self.middleware._tool_calls = 12
        events = await self.collect()
        self.assertEqual(events[-1].metadata["finance"]["error_code"], "turn_budget_exceeded")
        self.assertFalse(self.repo.starts)
        self.assertFalse(self.repo.saved)

    async def test_operation_timeout_cleans_worker_without_publishing(self):
        stopped = asyncio.Event()
        async def fetch(request, progress):
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        self.financials.fetch_data = fetch
        with patch("finagent.tools.TOOL_TIMEOUT_SECONDS", 0.01):
            events = await self.collect()
        self.assertEqual(events[-1].metadata["finance"]["error_code"], "operation_timeout")
        self.assertTrue(stopped.is_set())
        self.assertFalse(self.repo.saved)
        self.assertEqual(self.repo.finished[-1][1], "failed")

    async def test_terminal_success_is_published_only_after_operation_finishes(self):
        async for event in self.operation_stream():
            if event.state == ToolResultState.SUCCESS:
                self.assertEqual(self.repo.finished[-1][1], "succeeded")
        self.assertEqual(len(self.repo.finished), 1)

    async def test_final_text_preview_is_bounded_and_metadata_remains_complete(self):
        table = evidence_table()
        table.rows = [table.rows[0].model_copy(deep=True) for _ in range(20)]
        async def fetch(request, progress):
            return table
        self.financials.fetch_data = fetch
        events = await self.collect()
        final = next(e for e in events if isinstance(e, ToolChunk) and e.is_last)
        preview = json.loads(final.content[0].text)["artifacts"][0]
        self.assertEqual(len(preview["rows"]), 16)
        self.assertEqual(preview["row_count"], 20)
        self.assertTrue(preview["preview_truncated"])
        self.assertNotIn("inputs", preview["rows"][0]["cells"][0])
        self.assertNotIn("source_ids", preview["rows"][0]["cells"][0])
        self.assertEqual(len(final.metadata["finance"]["artifacts"][0]["data"]["rows"]), 20)

    async def test_narrative_length_guard_prevents_publication(self):
        self.model.text = "x" * 12001
        events = await self.collect(name="data_analyze", inputs={"data_ids": ["input"], "question": "Analyze."})
        self.assertEqual(events[-1].metadata["finance"]["error_code"], "analysis_too_long")
        self.assertFalse(self.repo.saved)

    async def test_log_sequence_is_allocated_by_database(self):
        connection = SimpleNamespace(execute=AsyncMock())
        acquired = AsyncMock()
        acquired.__aenter__.return_value = connection
        pool = SimpleNamespace(acquire=lambda: acquired)
        # Test the original function, despite other test log mocks.
        original = self.patchers[1].temp_original
        with patch("finagent.runsteps.get_pool", AsyncMock(return_value=pool)):
            await original(actor="a", session_id="s", step_type="tool", name="data_fetch")
        sql = connection.execute.await_args.args[0]
        self.assertIn("nextval(pg_get_serial_sequence('fin.run_steps', 'step_id'))", sql)
        self.assertIn("SELECT id, $1, id", sql)


if __name__ == "__main__":
    unittest.main()
