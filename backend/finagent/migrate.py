# -*- coding: utf-8 -*-
"""Apply fin schema (idempotent) before the API starts.

Usage: python -m finagent.migrate
Runs as the first step of the backend container CMD. asyncpg's no-arg
execute() uses the simple query protocol, which accepts multi-statement
scripts, so schema.sql can be sent as one payload.
"""

import asyncio
import pathlib

import asyncpg

from .db import to_asyncpg_dsn

import os

SCHEMA = (pathlib.Path(__file__).parent / "schema.sql").read_text(
    encoding="utf-8",
)


async def main() -> None:
    conn = await asyncpg.connect(
        to_asyncpg_dsn(os.environ["DATABASE_URL"]),
    )
    try:
        await conn.execute(SCHEMA)
        tables = await conn.fetchval(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'fin'",
        )
        print(f"fin schema ensured ({tables} tables)")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
