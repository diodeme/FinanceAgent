"""Best-effort native and direct-model trajectory logging, without prompt bodies."""

import asyncio
import contextvars
import json
import logging
import time
from contextlib import aclosing
from typing import Any

from agentscope.middleware import MiddlewareBase
from agentscope.model import ChatResponse

from .db import get_pool

logger = logging.getLogger(__name__)
run_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("fin_run_id", default=None)


def state_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "").lower()


async def log_step(
    *, actor: str, session_id: str, step_type: str, name: str,
    msg_id: str | None = None, tokens_in: int | None = None,
    tokens_out: int | None = None, latency_ms: int = 0,
    detail: dict | None = None, status: str = "ok", run_id: str | None = None,
) -> None:
    """Use the database sequence for both identity and cross-worker ordering."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                WITH allocated AS (
                    SELECT nextval(pg_get_serial_sequence('fin.run_steps', 'step_id')) AS id
                )
                INSERT INTO fin.run_steps
                  (step_id, run_id, seq, actor, session_id, msg_id, step_type, name,
                   tokens_in, tokens_out, latency_ms, detail, status)
                SELECT id, $1, id, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11
                FROM allocated
                """,
                run_id if run_id is not None else run_id_var.get(), actor,
                session_id, msg_id, step_type, name, tokens_in, tokens_out,
                latency_ms, json.dumps(detail, ensure_ascii=False, default=str) if detail else None,
                status,
            )
    except Exception:
        logger.warning("run_steps write failed", exc_info=True)


class RunStepsMiddleware(MiddlewareBase):
    def __init__(self, actor: str, session_id: str, *, detail: dict | None = None) -> None:
        self.actor = actor
        self.session_id = session_id
        self.detail = dict(detail or {})

    async def on_model_call(self, agent, input_kwargs, next_handler):
        model = input_kwargs.get("current_model")
        name = getattr(model, "model", None) or type(model).__name__
        started = time.monotonic()
        run_id = run_id_var.get()

        async def finalize(usage=None, msg_id=None, status="ok", detail=None):
            await log_step(
                actor=self.actor, session_id=self.session_id, step_type="llm",
                name=name, msg_id=msg_id, tokens_in=getattr(usage, "input_tokens", None),
                tokens_out=getattr(usage, "output_tokens", None),
                latency_ms=int((time.monotonic() - started) * 1000),
                status=status, detail=(self.detail | (detail or {})) or None, run_id=run_id,
            )

        try:
            result = await next_handler(**input_kwargs)
        except asyncio.CancelledError:
            await finalize(status="error", detail={"interrupted": True})
            raise
        except Exception as exc:
            await finalize(status="error", detail={"error_type": type(exc).__name__})
            raise
        if isinstance(result, ChatResponse):
            interrupted = state_value(result.finished_reason) == "interrupted"
            await finalize(result.usage, result.id, "error" if interrupted else "ok",
                           {"interrupted": True} if interrupted else None)
            return result

        async def wrapped():
            usage = None
            msg_id = None
            complete = False
            detail = None
            try:
                async with aclosing(result):
                    async for chunk in result:
                        msg_id = getattr(chunk, "id", None) or msg_id
                        usage = getattr(chunk, "usage", None) or usage
                        if state_value(getattr(chunk, "finished_reason", None)) == "interrupted":
                            detail = {"interrupted": True}
                        complete = complete or bool(getattr(chunk, "is_last", False))
                        yield chunk
            except (asyncio.CancelledError, GeneratorExit):
                detail = {"interrupted": True}
                raise
            except Exception as exc:
                detail = {"error_type": type(exc).__name__}
                raise
            finally:
                if not complete and detail is None:
                    detail = {"incomplete": True}
                await finalize(usage, msg_id, "error" if detail else "ok", detail)

        return wrapped()

    async def on_acting(self, agent, input_kwargs, next_handler):
        call = input_kwargs.get("tool_call")
        started = time.monotonic()
        complete = False
        detail = {**self.detail, "arguments": getattr(call, "input", None)}
        failed = False
        try:
            async with aclosing(next_handler(**input_kwargs)) as stream:
                async for event in stream:
                    state = state_value(getattr(event, "state", None))
                    if state in {"error", "denied", "interrupted"}:
                        failed = True
                        detail["tool_state"] = state
                    if state == "success":
                        complete = True
                    yield event
        except (asyncio.CancelledError, GeneratorExit):
            failed = True
            detail["interrupted"] = True
            raise
        except Exception as exc:
            failed = True
            detail["error_type"] = type(exc).__name__
            raise
        finally:
            if not complete and not failed:
                failed = True
                detail["incomplete"] = True
            await log_step(
                actor=self.actor, session_id=self.session_id, step_type="tool",
                name=getattr(call, "name", None) or "unknown_tool",
                msg_id=getattr(call, "id", None),
                latency_ms=int((time.monotonic() - started) * 1000),
                detail=detail, status="error" if failed else "ok",
            )


async def runsteps_factory(user_id: str, agent_id: str, session_id: str) -> list[MiddlewareBase]:
    return [RunStepsMiddleware(actor=agent_id, session_id=session_id)]
