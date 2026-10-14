"""Native analyst permissions, bounded terminal observation and trajectory logging."""

from contextlib import aclosing

from agentscope.event import ReplyEndEvent, TextBlockDeltaEvent
from agentscope.model import ChatResponse
from agentscope.permission import PermissionBehavior, PermissionDecision

from .contracts import FinanceError
from .runsteps import RunStepsMiddleware, state_value

WORKSPACE_TOOL_NAMES = {"Read", "Grep", "Glob", "Bash", "Write", "Edit"}
MAX_RESEARCH_OUTPUT_CHARS = 24000
MAX_RESEARCH_OUTPUT_TOKENS = 12000


class AnalystMiddleware(RunStepsMiddleware):
    def __init__(self, *, tools, progress, actor, session_id, skill_tool=None, skill_info=None):
        super().__init__(actor, session_id, detail={"phase": "analysis_research", "skill": skill_info})
        self.tools = tuple(tools)
        self.skill_tool = skill_tool
        self.skill_loaded = False
        self.progress = progress
        self.finished_reason = None
        self.output_chars = 0
        self.output_tokens = 0

    async def on_check_permission(self, agent, input_kwargs, next_handler):
        tool = input_kwargs.get("tool")
        call = input_kwargs.get("tool_call")
        valid = (any(tool is known for known in self.tools)
                 and getattr(call, "name", None) in WORKSPACE_TOOL_NAMES
                 and getattr(tool, "name", None) == getattr(call, "name", None))
        valid = valid or (self.skill_tool is not None and tool is self.skill_tool
                          and getattr(call, "name", None) == "Skill")
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW if valid else PermissionBehavior.DENY,
            message="This run's native workspace tool." if valid else "Tool is outside this analysis workspace.",
        )

    async def on_reply(self, agent, input_kwargs, next_handler):
        async with aclosing(next_handler(**input_kwargs)) as stream:
            async for event in stream:
                if isinstance(event, TextBlockDeltaEvent):
                    # Research prose stays private to the analyst, never report body.
                    self.output_chars += len(event.delta)
                    if self.output_chars > MAX_RESEARCH_OUTPUT_CHARS:
                        raise FinanceError("analysis_too_long", "Research exceeded its output budget.")
                if isinstance(event, ReplyEndEvent):
                    self.finished_reason = state_value(event.finished_reason)
                yield event

    async def on_model_call(self, agent, input_kwargs, next_handler):
        # Reuse native logging exactly once per call; do not also count event usage.
        result = await super().on_model_call(agent, input_kwargs, next_handler)

        def check_terminal(chunk):
            if state_value(chunk.finished_reason) == "interrupted":
                raise FinanceError("interrupted", "Research was interrupted; no result was saved.")
            if chunk.is_last:
                self.output_tokens += getattr(chunk.usage, "output_tokens", 0) or 0
                if self.output_tokens > MAX_RESEARCH_OUTPUT_TOKENS:
                    raise FinanceError("analysis_too_long", "Research exceeded its token budget.")

        if isinstance(result, ChatResponse):
            check_terminal(result)
            if not result.is_last:
                raise FinanceError("incomplete_analysis", "Research model returned no terminal response.")
            return result

        async def checked():
            complete = False
            async with aclosing(result):
                async for chunk in result:
                    check_terminal(chunk)
                    complete = complete or chunk.is_last
                    yield chunk
            if not complete:
                raise FinanceError("incomplete_analysis", "Research model stream ended without a terminal response.")
        return checked()

    async def on_acting(self, agent, input_kwargs, next_handler):
        call = input_kwargs["tool_call"]
        await self.progress(f"Research tool: {call.name}")
        async with aclosing(super().on_acting(agent, input_kwargs, next_handler)) as stream:
            async for event in stream:
                if call.name == "Skill" and state_value(getattr(event, "state", None)) == "success":
                    self.skill_loaded = True
                yield event
