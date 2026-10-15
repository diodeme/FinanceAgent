"""Read-only, session-wide usage, including nested FinanceAgent analysis calls.

Run inside the backend with: python -I - SESSION_ID < collect-usage.py
No credentials, prompts, or raw run_steps.detail are exported. Missing usage is
null, never silently reported as zero. Totals are observed (possibly partial).
"""
import asyncio
import json
import os
import sys

import asyncpg


async def collect(session_id):
    dsn = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)
    connection = await asyncpg.connect(dsn)
    try:
        async with connection.transaction(readonly=True):
            totals = dict(await connection.fetchrow("""
                SELECT count(*) AS logged_model_calls,
                    count(*) FILTER (WHERE tokens_in IS NOT NULL AND tokens_out IS NOT NULL) AS calls_with_usage,
                    count(*) FILTER (WHERE tokens_in IS NOT NULL) AS calls_with_input_usage,
                    count(*) FILTER (WHERE tokens_out IS NOT NULL) AS calls_with_output_usage,
                    count(*) FILTER (WHERE status='error') AS error_records,
                    sum(tokens_in) AS input_tokens,
                    sum(tokens_out) AS output_tokens
                FROM fin.run_steps WHERE session_id=$1 AND step_type='llm'
            """, session_id))
            steps = await connection.fetch("""
                SELECT step_id, step_type, name, status, tokens_in, tokens_out, latency_ms,
                    detail->>'phase' AS phase, detail->'skill' AS skill
                FROM fin.run_steps WHERE session_id=$1 ORDER BY step_id
            """, session_id)
            runs = await connection.fetch("""
                SELECT operation, status, error FROM fin.task_runs
                WHERE session_id=$1 ORDER BY created_at
            """, session_id)
        totals["usage_complete"] = bool(totals["logged_model_calls"]) and (
            totals["logged_model_calls"] == totals["calls_with_usage"]
        )
        totals["missing_usage_calls"] = totals["logged_model_calls"] - totals["calls_with_usage"]
        return {
            "session_id": session_id,
            "scope": "all fin.run_steps for this session, including internal analysis",
            "totals_are_observed_not_estimated": True,
            "usage": totals,
            "steps": [dict(row) for row in steps],
            "task_runs": [dict(row) for row in runs],
        }
    finally:
        await connection.close()


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2:
            raise ValueError("Exactly one session ID is required")
        print(json.dumps(asyncio.run(collect(sys.argv[1])), ensure_ascii=False, default=str))
    except Exception as exc:
        # Exception text/tracebacks can contain connection URLs; emit only class.
        print(json.dumps({"error": "usage_collection_failed", "error_type": type(exc).__name__}))
        sys.exit(1)
