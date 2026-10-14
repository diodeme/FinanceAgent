"""Read-only PostgreSQL 16 metadata snapshot for migration regression checks.

``await schema_snapshot(conn)`` also supports a caller-owned connection to a
legacy database. It neither creates objects nor reads application rows or
sequence counters. The result is independent of database names and object OIDs.
The Alembic bookkeeping table is deliberately excluded.
"""

from typing import Any

import asyncpg


async def schema_snapshot(conn: asyncpg.Connection) -> dict[str, list[dict[str, Any]]]:
    """Return deterministic ``fin`` metadata without modifying the database.

    Use an idle connection (not one already inside a transaction). A read-only,
    repeatable-read transaction provides one consistent catalog snapshot. The
    local search path makes PostgreSQL's rendered definitions comparable across
    connections while leaving the caller's search path unchanged on completion.
    Sequence *configuration and ownership* are included; last_value/is_called
    are intentionally not, since they are data rather than schema.
    """
    queries = {
        "tables": """
            SELECT c.relname AS name, c.relkind::text AS kind,
                   c.relpersistence::text AS persistence,
                   c.relreplident::text AS replica_identity, c.reloptions AS options
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'fin' AND c.relkind IN ('r', 'p')
              AND c.relname <> 'alembic_version'
            ORDER BY c.relname
        """,
        "columns": """
            SELECT c.relname AS table_name, a.attnum AS position,
                   a.attname AS name, format_type(a.atttypid, a.atttypmod) AS type,
                   a.attnotnull AS not_null,
                   pg_get_expr(d.adbin, d.adrelid) AS default_expression,
                   a.attidentity::text AS identity,
                   a.attgenerated::text AS generated,
                   CASE WHEN a.attcollation <> 0
                        THEN cn.nspname || '.' || co.collname END AS collation
            FROM pg_attribute a
            JOIN pg_class c ON c.oid = a.attrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
            LEFT JOIN pg_collation co ON co.oid = a.attcollation
            LEFT JOIN pg_namespace cn ON cn.oid = co.collnamespace
            WHERE n.nspname = 'fin' AND c.relkind IN ('r', 'p')
              AND c.relname <> 'alembic_version'
              AND a.attnum > 0 AND NOT a.attisdropped
            ORDER BY c.relname, a.attnum
        """,
        "constraints": """
            SELECT c.relname AS table_name, con.conname AS name,
                   con.contype::text AS type,
                   pg_get_constraintdef(con.oid, false) AS definition,
                   con.condeferrable AS deferrable,
                   con.condeferred AS initially_deferred,
                   con.convalidated AS validated,
                   CASE WHEN con.contype IN ('p', 'u')
                        THEN i.indnullsnotdistinct END AS nulls_not_distinct
            FROM pg_constraint con
            JOIN pg_class c ON c.oid = con.conrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            LEFT JOIN pg_index i ON i.indexrelid = con.conindid
            WHERE n.nspname = 'fin' AND c.relname <> 'alembic_version'
            ORDER BY c.relname, con.conname
        """,
        "indexes": """
            SELECT t.relname AS table_name, c.relname AS name,
                   am.amname AS access_method,
                   pg_get_indexdef(i.indexrelid, 0, false) AS definition,
                   i.indisunique AS is_unique, i.indisprimary AS is_primary,
                   i.indnullsnotdistinct AS nulls_not_distinct,
                   i.indisvalid AS is_valid, i.indisready AS is_ready,
                   c.reloptions AS options
            FROM pg_index i
            JOIN pg_class t ON t.oid = i.indrelid
            JOIN pg_class c ON c.oid = i.indexrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            JOIN pg_am am ON am.oid = c.relam
            WHERE n.nspname = 'fin' AND t.relname <> 'alembic_version'
            ORDER BY t.relname, c.relname
        """,
        "sequences": """
            SELECT c.relname AS name,
                   format_type(s.seqtypid, NULL) AS type,
                   s.seqstart AS start, s.seqincrement AS increment,
                   s.seqmin AS minimum, s.seqmax AS maximum,
                   s.seqcache AS cache, s.seqcycle AS cycle,
                   own_ns.nspname AS owner_schema,
                   own.relname AS owner_table, a.attname AS owner_column,
                   dep.deptype::text AS dependency_type
            FROM pg_sequence s
            JOIN pg_class c ON c.oid = s.seqrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            LEFT JOIN pg_depend dep
              ON dep.classid = 'pg_class'::regclass AND dep.objid = c.oid
             AND dep.refclassid = 'pg_class'::regclass
             AND dep.deptype IN ('a', 'i') AND dep.refobjsubid > 0
            LEFT JOIN pg_class own ON own.oid = dep.refobjid
            LEFT JOIN pg_namespace own_ns ON own_ns.oid = own.relnamespace
            LEFT JOIN pg_attribute a
              ON a.attrelid = dep.refobjid AND a.attnum = dep.refobjsubid
            WHERE n.nspname = 'fin'
              AND NOT (coalesce(own_ns.nspname, '') = 'fin'
                       AND coalesce(own.relname, '') = 'alembic_version')
            ORDER BY c.relname, own_ns.nspname, own.relname, a.attname
        """,
    }
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        await conn.execute("SET LOCAL search_path = pg_catalog, public")
        return {
            section: [dict(row) for row in await conn.fetch(sql)]
            for section, sql in queries.items()
        }
