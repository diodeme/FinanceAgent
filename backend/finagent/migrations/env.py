# -*- coding: utf-8 -*-
"""Async Alembic environment for fin only; never change the search_path."""

import asyncio
import os

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

BASELINE_TABLES = frozenset({
    "company_registry", "metric_registry", "source_registry",
    "source_documents", "fact_financials", "doc_chunks", "task_runs",
    "run_coverage", "run_steps", "task_artifacts", "agent_memory",
})


def run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=None,
        version_table="alembic_version",
        version_table_schema="fin",
    )
    operation = context.config.attributes.get("operation", "upgrade")
    if operation in {"upgrade", "downgrade", "stamp-baseline"}:
        heads = context.get_context().get_current_heads()
        tables = set(connection.execute(text(
            "SELECT c.relname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'fin' AND c.relkind IN ('r', 'p', 'v', 'm', 'f') "
            "AND c.relname <> 'alembic_version'"
        )).scalars())
        if operation == "stamp-baseline":
            if heads:
                raise RuntimeError("fin already has a revision; use upgrade, not stamp-baseline.")
            if tables != BASELINE_TABLES:
                raise RuntimeError(
                    "Legacy baseline requires exactly the 11 original tables; "
                    f"missing={sorted(BASELINE_TABLES - tables)}, "
                    f"unexpected={sorted(tables - BASELINE_TABLES)}. "
                    "Do not stamp an incomplete or different schema."
                )
            # The CLI requires operator acknowledgement of a full comparison.
            # Table names alone are NOT proof that a legacy schema matches.
        elif not heads and tables:
            raise RuntimeError(
                "Unversioned fin tables exist. Back up and compare the complete "
                "schema with a fresh 0001_baseline database, then run "
                "python -m finagent.migrate stamp-baseline --verified. "
                "See README; startup will not automatically adopt legacy tables."
            )
        if not connection.execute(text(
            "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')"
        )).scalar_one():
            raise RuntimeError("The vector extension is missing; provision it using db/init.sql first.")
        connection.execute(text("CREATE SCHEMA IF NOT EXISTS fin"))

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is required for database migration commands.")
    engine = create_async_engine(url, poolclass=pool.NullPool)
    try:
        # One outer transaction includes bootstrap, DDL and version updates.
        async with engine.begin() as connection:
            await connection.run_sync(run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("Offline mode is not supported; schema adoption checks require a database.")
asyncio.run(run_async_migrations())
