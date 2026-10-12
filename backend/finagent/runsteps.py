# -*- coding: utf-8 -*-
"""RunStepsMiddleware: write every LLM / tool call into fin.run_steps.

Wiring: create_app(extra_agent_middlewares=runsteps_factory) — the service
builds one middleware instance per agent assembly and hands us
(user_id, agent_id, session_id).

run_id comes from the `fin_run_id` contextvar; the orchestrator (agent
business logic, later) sets it when a task starts. Ad-hoc UI chats have no
task row yet → run_id NULL, which run_steps allows.

Never let a logging failure break a chat: all writes are best-effort with
a warning log.
"""

import asyncio
import contextvars
import itertools
import json
import logging
import time
from typing import TYPE_CHECKING, Any, AsyncGenerator, Awaitable, Callable

from agentscope.message import ToolCallBlock
from agentscope.middleware import MiddlewareBase
from agentscope.model import ChatResponse

from .db import get_pool

if TYPE_CHECKING:  # pragma: no cover
    from agentscope.agent import Agent

logger = logging.getLogger(__name__)

# The orchestrator sets this (token = run_id) at task start.
run_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "fin_run_id", default=None,
)

# Per-process step counter; seq is "global order of arrival", good enough
# for trajectory replay. Uses a lock because pool writes happen on the
# event loop but interleaving across agents must stay monotonic per run.
_seq_lock = asyncio.Lock()
_seq = itertools.count(1)

_DETAIL_MAX = 2000


def _trim(data: Any) -> str:
    s = json.dumps(data, ensure_ascii=False, default=str)
    return s[:_DETAIL_MAX]


async def _next_seq() -> int:
    async with _seq_lock:
        return next(_seq)


async def _insert_step(
    actor: str,
    session_id: str,
    step_type: str,
    name: str,
    msg_id: str | None,
    tokens_in: int | None,
    tokens_out: int | None,
    latency_ms: int,
    detail: dict | None,
    status: str,
) -> None:
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO fin.run_steps
                  (run_id, seq, actor, session_id, msg_id, step_type, name,
                   tokens_in, tokens_out, latency_ms, detail, status)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                        $11::jsonb, $12)
                """,
                run_id_var.get(),
                await _next_seq(),
                actor,
                session_id,
                msg_id,
                step_type,
                name,
                tokens_in,
                tokens_out,
                latency_ms,
                json.dumps(detail, ensure_ascii=False, default=str)
                if detail
                else None,
                status,
            )
    except Exception:  # noqa: BLE001 — logging must never break the chat
        logger.warning("run_steps write failed", exc_info=True)


class RunStepsMiddleware(MiddlewareBase):
    """Log llm/tool calls of one agent assembly into fin.run_steps."""

    def __init__(self, actor: str, session_id: str) -> None:
        self.actor = actor
        self.session_id = session_id

    # ------------------------------------------------------------ llm calls

    async def on_model_call(
        self,
        agent: "Agent",
        input_kwargs: dict,
        next_handler: Callable[
            ..., Awaitable["ChatResponse | AsyncGenerator[ChatResponse, None]"],
        ],
    ) -> "ChatResponse | AsyncGenerator[ChatResponse, None]":
        model = input_kwargs.get("current_model")
        name = getattr(model, "model_name", None) or type(model).__name__
        t0 = time.monotonic()

        async def finalize(
            usage: Any, msg_id: str | None, status: str,
        ) -> None:
            await _insert_step(
                actor=self.actor,
                session_id=self.session_id,
                step_type="llm",
                name=name,
                msg_id=msg_id,
                tokens_in=getattr(usage, "input_tokens", None),
                tokens_out=getattr(usage, "output_tokens", None),
                latency_ms=int((time.monotonic() - t0) * 1000),
                detail=None,
                status=status,
            )

        result = await next_handler(**input_kwargs)
        if isinstance(result, ChatResponse):
            await finalize(result.usage, result.id, "ok")
            return result

        # Streaming: capture usage from the last chunk that carries one,
        # then hand every chunk through untouched.
        async def wrapped() -> AsyncGenerator[ChatResponse, None]:
            usage = None
            msg_id: str | None = None
            status = "ok"
            try:
                async for chunk in result:
                    if isinstance(chunk, ChatResponse):
                        msg_id = chunk.id or msg_id
                        if chunk.usage is not None:
                            usage = chunk.usage
                    yield chunk
            except Exception:
                status = "error"
                raise
            finally:
                await finalize(usage, msg_id, status)

        return wrapped()

    # ------------------------------------------------------------ tool calls

    async def on_acting(
        self,
        agent: "Agent",
        input_kwargs: dict,
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator:
        call: ToolCallBlock | None = input_kwargs.get("tool_call")
        name = getattr(call, "name", None) or "unknown_tool"
        t0 = time.monotonic()
        status = "ok"
        try:
            async for event in next_handler(**input_kwargs):
                yield event
        except Exception:
            status = "error"
            raise
        finally:
            await _insert_step(
                actor=self.actor,
                session_id=self.session_id,
                step_type="tool",
                name=name,
                msg_id=getattr(call, "id", None),
                tokens_in=None,
                tokens_out=None,
                latency_ms=int((time.monotonic() - t0) * 1000),
                detail={"arguments": getattr(call, "input", None)},
                status=status,
            )


async def runsteps_factory(
    user_id: str,
    agent_id: str,
    session_id: str,
) -> list[MiddlewareBase]:
    """AgentMiddlewareFactory for create_app(extra_agent_middlewares=...)."""
    return [RunStepsMiddleware(actor=agent_id, session_id=session_id)]
