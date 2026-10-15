"""Provisioned-agent-only tools, prompt, permission and operation boundaries."""

import asyncio
import json
import logging
import time
from contextlib import aclosing

from agentscope.middleware import MiddlewareBase
from agentscope.message import TextBlock, ToolResultBlock, ToolResultState
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import ToolChunk, Toolkit, ToolResponse

from .contracts import FinanceEnvelope, FinanceError
from .prompts import FINANCE_PROMPT
from .runsteps import RunStepsMiddleware, log_step, run_id_var, state_value
from .tools import FinanceTools, REQUESTS, ToolContext, analysis_record, final_chunk, has_gaps, includes_result_text, reply_id, tool_context_var

logger = logging.getLogger(__name__)
MAX_TOOL_CALLS = 12
# Accommodate native cold image setup separately from bounded research/report calls.
TURN_TIMEOUT_SECONDS = 1500
INFRASTRUCTURE_ERRORS = {"operation_failed", "operation_timeout", "operation_start_failed"}
MAX_INFRASTRUCTURE_FAILURES = 2  # Initial attempt plus at most one retry per tool/turn.


class FinanceMiddleware(MiddlewareBase):
    """Instantiate only after verifying the provisioned owner/agent binding."""

    def __init__(self, user_id: str, agent_id: str, session_id: str, *, repo,
                 financials, calculations, reports, registry, filings=None, workspaces=None, skill_source=None):
        self.user_id = user_id
        self.agent_id = agent_id
        self.session_id = session_id
        self.repo = repo
        self.registry = registry
        self._tool_calls = 0
        self._turn_started = time.monotonic()
        self._infrastructure_failures: dict[str, int] = {}
        self.runtime = FinanceTools(user_id, agent_id, session_id, repo=repo,
                                    financials=financials, calculations=calculations, reports=reports,
                                    filings=filings, workspaces=workspaces, skill_source=skill_source)

    async def list_tools(self):
        return self.runtime.tools

    async def on_reply(self, agent, input_kwargs, next_handler):
        if getattr(agent.state, "session_id", None) != self.session_id:
            raise FinanceError("invalid_scope", "Finance middleware session does not match the agent session.")
        if self.runtime.skill_source is not None:
            await self.runtime.skill_source.ensure_analysis_skill(
                user_id=self.user_id, agent_id=self.agent_id, session_id=self.session_id,
            )
        self.runtime.model = agent.model
        self._tool_calls = 0
        self._turn_started = time.monotonic()
        self._infrastructure_failures.clear()
        if getattr(agent, "react_config", None) is not None:
            agent.react_config.max_iters = min(agent.react_config.max_iters, MAX_TOOL_CALLS)
        # A new toolkit discards inherited tools, MCPs, skills, groups and schedules.
        agent.toolkit = Toolkit(tools=self.runtime.tools)
        # Retain native automatic compression, but don't advertise its dynamically
        # registered tool alongside the strict business-tool surface.
        if getattr(agent, "context_config", None) is not None:
            agent.context_config.compression_tool_enabled = False
        async with asyncio.timeout(TURN_TIMEOUT_SECONDS):
            async with aclosing(next_handler(**input_kwargs)) as stream:
                async for event in stream:
                    yield event

    async def on_system_prompt(self, agent, current_prompt):
        return f"{current_prompt}\n\n{FINANCE_PROMPT}\n\n{self.registry.metric_prompt()}"

    async def on_model_call(self, agent, input_kwargs, next_handler):
        # Success is already compacted by on_acting. Failed native responses can
        # still contain streamed drafts; project only their error into the model
        # request, leaving persisted history, UI chunks and metadata untouched.
        messages = []
        for message in input_kwargs["messages"]:
            content = []
            for block in message.content:
                if (isinstance(block, ToolResultBlock) and block.name == "data_analyze"
                        and state_value(block.state) in {"error", "denied", "interrupted"}):
                    envelope = block.metadata.get("finance") or {}
                    error = {
                        "message": envelope.get("message") or "Analysis did not complete; no saved report is confirmed.",
                        "error_code": envelope.get("error_code") or state_value(block.state),
                    }
                    block = block.model_copy(update={
                        "output": [TextBlock(text=json.dumps(error, ensure_ascii=False))],
                        "metadata": {},
                    })
                content.append(block)
            messages.append(message.model_copy(update={"content": content}))
        return await next_handler(**{**input_kwargs, "messages": messages})

    async def on_check_permission(self, agent, input_kwargs, next_handler):
        tool = input_kwargs.get("tool")
        call = input_kwargs.get("tool_call")
        valid = (any(tool is item for item in self.runtime.tools)
                 and getattr(call, "name", None) in REQUESTS
                 and getattr(tool, "name", None) == getattr(call, "name", None)
                 and getattr(agent.state, "session_id", None) == self.session_id)
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW if valid else PermissionBehavior.DENY,
            message="Server-scoped finance tool." if valid else "Only server-scoped finance tools are allowed.",
        )

    async def on_acting(self, agent, input_kwargs, next_handler):
        call = input_kwargs["tool_call"]
        started = time.monotonic()
        operation = None
        try:
            self._tool_calls += 1
            if self._tool_calls > MAX_TOOL_CALLS or time.monotonic() - self._turn_started >= TURN_TIMEOUT_SECONDS:
                raise FinanceError("turn_budget_exceeded", "This turn's finance tool budget is exhausted. Summarize completed results and ask for a new turn.")
            if self._infrastructure_failures.get(call.name, 0) >= MAX_INFRASTRUCTURE_FAILURES:
                raise FinanceError("infrastructure_retry_exhausted", "This tool already failed twice in this turn. Do not retry or rephrase its arguments; explain the service failure and retain completed results.")
            if call.name not in REQUESTS or getattr(agent.state, "session_id", None) != self.session_id:
                raise FinanceError("invalid_scope", "This tool is outside the finance session scope.")
            request = REQUESTS[call.name].model_validate(json.loads(call.input))
            current_reply = reply_id(agent.state)
            if not current_reply:
                raise FinanceError("invalid_scope", "The native reply identifier is missing.")
            saved_request = request.model_dump(mode="json")
            compatibility = {}
            if call.name == "find_results":
                legacy_request = dict(saved_request)
                for field, default in (("kind", None), ("current_session_only", True)):
                    if field not in request.model_fields_set:
                        legacy_request[field] = default
                compatibility["legacy_request"] = legacy_request
                saved_request["_retrieval_version"] = 2
            operation = await self.repo.start_operation(
                self.user_id, self.agent_id, self.session_id, current_reply, call.id,
                call.name, saved_request, **compatibility,
            )
            if operation.get("legacy_find"):
                request = REQUESTS[call.name].model_validate(legacy_request)
        except Exception as exc:
            code = exc.code if isinstance(exc, FinanceError) else "operation_start_failed"
            message = str(exc) if isinstance(exc, FinanceError) else "The finance operation could not be started."
            if code in INFRASTRUCTURE_ERRORS:
                self._infrastructure_failures[call.name] = self._infrastructure_failures.get(call.name, 0) + 1
            await log_step(actor=self.agent_id, session_id=self.session_id, step_type="tool", name=call.name,
                           msg_id=call.id, status="error", detail={"error_code": code},
                           latency_ms=int((time.monotonic() - started) * 1000))
            chunk = final_chunk(FinanceEnvelope(message=message, error_code=code), failed=True)
            yield chunk
            yield ToolResponse(id=call.id).append_chunk(chunk)
            return
        except asyncio.CancelledError:
            await log_step(actor=self.agent_id, session_id=self.session_id, step_type="tool", name=call.name,
                           msg_id=call.id, status="error", detail={"interrupted": True})
            raise

        run_id = str(operation["run_id"])
        context = ToolContext(self.user_id, self.agent_id, self.session_id, current_reply, call.id, call.name, run_id)
        run_token = run_id_var.set(run_id)
        tool_token = tool_context_var.set(context)
        created = operation.get("created", True)
        terminal = "interrupted"
        error = None
        finalized = False
        infrastructure_failed = False
        analysis_started = False
        try:
            if not created:
                old_status = operation["status"]
                if old_status not in {"succeeded", "partial"}:
                    raise FinanceError("operation_busy" if old_status == "running" else "operation_not_replayable",
                                       "This call is already running or did not finish. Make a new explicit tool call to retry.")
                artifacts = [await self.repo.get_artifact(self.user_id, str(i)) for i in operation.get("artifact_ids", [])]
                if artifacts:
                    envelope = FinanceEnvelope(message="Reused the saved result of this tool call.", artifacts=artifacts,
                                               partial=old_status == "partial")
                elif operation.get("legacy_find"):
                    artifacts = await self.repo.find_artifacts(self.user_id, self.session_id, request)
                    envelope = FinanceEnvelope(message=f"Found {len(artifacts)} saved result(s).", artifacts=artifacts)
                elif call.name in {"lookup_company", "find_results", "get_result"}:
                    async def ignore_progress(text):
                        pass
                    envelope = await self.runtime.execute(call.name, request, context, ignore_progress)
                else:
                    raise FinanceError("result_unavailable", "The earlier call has no saved result; make a new tool call.")
                is_analysis = call.name == "data_analyze"
                chunk = final_chunk(envelope, include_text=bool(operation.get("legacy_find")) or includes_result_text(call.name, request), framed=is_analysis)
                await log_step(actor=self.agent_id, session_id=self.session_id, step_type="tool", name=call.name,
                               msg_id=call.id, detail={"replayed": True}, run_id=run_id)
                if is_analysis:
                    # Replays are already persisted; their immutable metadata is
                    # authoritative even when no live model body is generated.
                    yield ToolChunk(content=[TextBlock(text=analysis_record("start", title=artifacts[0].title))],
                                    state=ToolResultState.RUNNING, is_last=False)
                yield chunk
                yield ToolResponse(id=call.id).append_chunk(final_chunk(envelope) if is_analysis else chunk)
                return
            failed = False
            final_events = []
            async with aclosing(next_handler(**input_kwargs)) as stream:
                async for event in stream:
                    state = state_value(getattr(event, "state", None))
                    metadata = getattr(event, "metadata", {}) or {}
                    envelope = metadata.get("finance") or {}
                    if state in {"error", "denied", "interrupted"}:
                        failed = True
                        interrupted = state == "interrupted" or envelope.get("error_code") == "interrupted"
                        terminal = "interrupted" if interrupted else "failed"
                        error = envelope.get("message") or state
                        infrastructure_failed |= envelope.get("error_code") in INFRASTRUCTURE_ERRORS
                    elif state == "success" and not failed:
                        terminal = "succeeded"
                        if envelope:
                            parsed = FinanceEnvelope.model_validate(envelope)
                            if parsed.partial or (call.name in {"data_fetch", "calculate_metrics"}
                                                  and any(a.data is not None and has_gaps(a.data) for a in parsed.artifacts)):
                                terminal = "partial"
                    if state in {"success", "error", "denied", "interrupted"}:
                        final_events.append(event)
                    else:
                        if call.name == "data_analyze":
                            analysis_started = True
                        yield event
            # Only the two terminal native events are held back; progress remains
            # live. Persist the operation outcome before publishing success.
            await self.repo.finish_operation(run_id, terminal, error=error)
            finalized = True
            for event in final_events:
                if (call.name == "data_analyze" and terminal == "succeeded"
                        and isinstance(event, ToolResponse) and state_value(event.state) == "success"):
                    # The native response has accumulated every transport record.
                    # Keep live chunks intact, but give subsequent model turns only
                    # compact evidence, after the existing persistence gate succeeds.
                    envelope = FinanceEnvelope.model_validate(event.metadata["finance"])
                    event.content = final_chunk(envelope).content
                yield event
        except FinanceError as exc:
            terminal = "interrupted" if exc.code == "interrupted" else "failed"
            error = str(exc)
            await log_step(actor=self.agent_id, session_id=self.session_id, step_type="tool", name=call.name,
                           msg_id=call.id, status="error", detail={"error_code": exc.code}, run_id=run_id)
            chunk = final_chunk(FinanceEnvelope(message=str(exc), error_code=exc.code), failed=True, framed=analysis_started)
            yield chunk
            yield ToolResponse(id=call.id).append_chunk(chunk)
        except (asyncio.CancelledError, GeneratorExit):
            terminal = "interrupted"
            error = "Operation interrupted."
            raise
        except Exception:
            terminal = "failed"
            error = "Unexpected tool execution failure."
            infrastructure_failed = True
            raise
        finally:
            if infrastructure_failed:
                self._infrastructure_failures[call.name] = self._infrastructure_failures.get(call.name, 0) + 1
            try:
                if created and not finalized:
                    await self.repo.finish_operation(run_id, terminal, error=error)
            finally:
                tool_context_var.reset(tool_token)
                run_id_var.reset(run_token)


async def finance_factory(user_id: str, agent_id: str, session_id: str) -> list[MiddlewareBase]:
    from . import repository

    steps = RunStepsMiddleware(actor=agent_id, session_id=session_id)
    if not await repository.is_finance_agent(user_id, agent_id):
        return [steps]
    from . import analysis_skills, calculations, filings, financials, registry, reports

    return [FinanceMiddleware(user_id, agent_id, session_id, repo=repository, financials=financials,
                              calculations=calculations, reports=reports, registry=registry, filings=filings,
                              skill_source=analysis_skills), steps]
