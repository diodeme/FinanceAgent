"""Opt-in PostgreSQL migration integration tests (unittest; no app import).

Run ONLY against a dedicated, disposable PostgreSQL 16/pgvector server. The
explicit MIGRATION_TEST_ADMIN_URL must name an administrative database and a
role permitted to CREATE/DROP DATABASE and CREATE EXTENSION vector. Each test
creates UUID-named databases FROM template0 and drops only those databases.
Nothing ever migrates the administrative database or the ordinary DATABASE_URL.
Without MIGRATION_TEST_ADMIN_URL, the entire integration suite is skipped.

From the repository root, with backend dependencies already installed:

    # PowerShell; use credentials for your isolated disposable test server.
    $env:MIGRATION_TEST_ADMIN_URL = 'postgresql://USER:PASSWORD@HOST:PORT/postgres'
    $env:PYTHONPATH = (Resolve-Path backend).Path
    python -m unittest discover -s backend/tests -t backend -v

Equivalent from backend: python -m unittest tests.test_migrations -v
Do not point the opt-in variable at a development or production server. Tests
never start containers, install packages, or import finagent.app. Temporary
successor revisions are written only into copies of the migration directory.
"""

import asyncio
from contextlib import asynccontextmanager, redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import quote, quote_plus
from uuid import uuid4

import asyncpg
from alembic import command
from sqlalchemy.engine import make_url

from tests.schema_snapshot import schema_snapshot


BACKEND = Path(__file__).resolve().parents[1]
MIGRATIONS = BACKEND / "finagent" / "migrations"
LEGACY_DDL = Path(__file__).parent / "fixtures" / "legacy_schema.sql"
BASELINE = "0001_baseline"
RUNTIME = "0002_finance_runtime"
HEAD = "0003_document_artifacts"
BASELINE_TABLES = {
    "company_registry", "metric_registry", "source_registry", "source_documents",
    "fact_financials", "doc_chunks", "task_runs", "run_coverage", "run_steps",
    "task_artifacts", "agent_memory",
}
ADMIN_ENV = "MIGRATION_TEST_ADMIN_URL"

PUBLIC_SENTINELS = """
    CREATE TABLE public.sessions (id text PRIMARY KEY, state jsonb NOT NULL);
    INSERT INTO public.sessions VALUES ('keep-session', '{"owner":"agentscope"}');
    CREATE TABLE public.messages (id text PRIMARY KEY, content text NOT NULL);
    INSERT INTO public.messages VALUES ('keep-message', 'do not touch');
    CREATE TABLE public.alembic_version (version_num varchar(64) PRIMARY KEY);
    INSERT INTO public.alembic_version VALUES ('agentscope_sentinel_revision');
"""

DOMAIN_SENTINELS = """
    INSERT INTO fin.company_registry
        (company, cik, name_official, fy_end_month, sector)
    VALUES ('TEST', '0000000001', 'Migration sentinel', 12, 'test');
    INSERT INTO fin.fact_financials
        (company, concept, period_end, value, unit, source_type, source_ref,
         retrieved_at, confidence)
    VALUES ('TEST', 'test:Revenue', DATE '2025-12-31', 123.4567, 'USD',
            'manual', 'migration-test', TIMESTAMPTZ '2026-01-01 00:00:00+00',
            'verified');
    INSERT INTO fin.run_steps
        (seq, actor, session_id, step_type, name, status)
    VALUES (1, 'migration-test', 'keep-session', 'tool', 'sentinel', 'ok');
    -- Non-default sequence counters catch accidental recreation or reseeding.
    SELECT setval('fin.fact_financials_fact_id_seq', 730, true);
    SELECT setval('fin.run_steps_step_id_seq', 920, false);
"""


@unittest.skipUnless(os.environ.get(ADMIN_ENV), f"Set {ADMIN_ENV} to opt in")
class DedicatedPostgresTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Parse only the explicit test URL. DATABASE_URL is never a fallback.
        try:
            self.admin_url = make_url(os.environ[ADMIN_ENV])
        except Exception:
            raise AssertionError(
                f"{ADMIN_ENV} must be a valid PostgreSQL URL (value withheld)"
            ) from None
        if self.admin_url.get_backend_name() not in ("postgres", "postgresql"):
            self.fail(f"{ADMIN_ENV} must use PostgreSQL")
        if not self.admin_url.database:
            self.fail(f"{ADMIN_ENV} must explicitly name an administrative database")
        self.databases = []
        self.admin = await asyncpg.connect(self._dsn(self.admin_url), timeout=15)
        self.addAsyncCleanup(self._cleanup_databases)
        version = int(await self.admin.fetchval("SHOW server_version_num"))
        self.assertEqual(version // 10000, 16, "Use a dedicated PostgreSQL 16 server")
        available = await self.admin.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'vector')"
        )
        self.assertTrue(available, "The disposable server must provide pgvector")
        self.url = await self._new_database()

    @staticmethod
    def _dsn(url):
        return url.set(drivername="postgresql").render_as_string(hide_password=False)

    @staticmethod
    def _sqlalchemy_url(url):
        return url.set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)

    @asynccontextmanager
    async def _connection(self, url=None):
        conn = await asyncpg.connect(self._dsn(url or self.url), timeout=15)
        try:
            yield conn
        finally:
            await conn.close()

    async def _new_database(self, *, vector=True):
        # No caller-supplied identifier is interpolated into CREATE/DROP DATABASE.
        name = "finagent_migration_test_" + uuid4().hex
        await self.admin.execute(f'CREATE DATABASE "{name}" TEMPLATE template0')
        self.databases.append(name)
        url = self.admin_url.set(database=name)
        async with self._connection(url) as conn:
            if vector:
                await conn.execute("CREATE EXTENSION vector")
            await conn.execute(PUBLIC_SENTINELS)
        return url

    async def _cleanup_databases(self):
        errors = []
        try:
            for name in reversed(self.databases):
                # Drop only names generated and recorded by this test instance.
                try:
                    await self.admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
                except Exception as exc:
                    errors.append(f"{name}: {type(exc).__name__}")
        finally:
            await self.admin.close()
        if errors:
            self.fail("Could not clean disposable test databases: " + "; ".join(errors))

    async def asyncTearDown(self):
        # Public AgentScope-style rows and its independent version marker must
        # survive every successful and failed migration operation.
        for name in self.databases:
            async with self._connection(self.admin_url.set(database=name)) as conn:
                sessions = await conn.fetch("SELECT id, state::text FROM public.sessions ORDER BY id")
                messages = await conn.fetch("SELECT id, content FROM public.messages ORDER BY id")
                versions = await conn.fetch("SELECT version_num FROM public.alembic_version")
                self.assertEqual([tuple(r) for r in sessions], [
                    ("keep-session", '{"owner": "agentscope"}'),
                ])
                self.assertEqual([tuple(r) for r in messages], [("keep-message", "do not touch")])
                self.assertEqual([tuple(r) for r in versions], [("agentscope_sentinel_revision",)])

    def _check_credentials(self, text, url):
        secrets = {self._dsn(url), self._sqlalchemy_url(url)}
        if url.password:
            secrets.update({url.password, quote(url.password, safe=""), quote_plus(url.password)})
        for secret in secrets:
            # Never include the offending secret or raw output in failure text.
            # Token boundaries avoid mistaking a short test password such as
            # 'test' or 'postgres' for part of an identifier/module name.
            exposed = re.search(r"(?<!\w)" + re.escape(secret) + r"(?!\w)", text)
            self.assertFalse(bool(exposed), "Migration output exposed database credentials")

    def _redact(self, text, url):
        secrets = [self._dsn(url), self._sqlalchemy_url(url)]
        if url.password:
            secrets += [url.password, quote(url.password, safe=""), quote_plus(url.password)]
        for secret in sorted(set(secrets), key=len, reverse=True):
            text = text.replace(secret, "[REDACTED]")
        return text

    async def _alembic(self, operation, revision=None, *, scripts=None, url=None, fails=False):
        url = url or self.url

        def invoke():
            # Import the migration API, never finagent.app. Alembic's async env
            # owns asyncio.run(), so invoke it outside this test's event loop.
            from finagent.migrate import make_config

            output = io.StringIO()
            error = None
            with patch.dict(os.environ, {"DATABASE_URL": self._sqlalchemy_url(url)}):
                with redirect_stdout(output), redirect_stderr(output):
                    try:
                        config = make_config(script_location=scripts)
                        config.attributes["operation"] = operation
                        fn = getattr(command, operation)
                        if revision is None:
                            fn(config)
                        else:
                            fn(config, revision)
                    except Exception as exc:
                        error = exc
            return output.getvalue(), error

        output, error = await asyncio.to_thread(invoke)
        text = output + (str(error) if error else "")
        self._check_credentials(text, url)
        if fails:
            self.assertIsNotNone(error, f"Expected {operation} to refuse this database state")
        elif error:
            self.fail(f"{operation} failed: {type(error).__name__}: {self._redact(text, url)}")
        return text

    async def _cli(self, *args, url=None, succeeds=True):
        url = url or self.url
        env = dict(os.environ)
        env["DATABASE_URL"] = self._sqlalchemy_url(url)
        env["PYTHONPATH"] = str(BACKEND) + os.pathsep + env.get("PYTHONPATH", "")
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "finagent.migrate", *args,
            cwd=str(BACKEND), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        text = (stdout + stderr).decode("utf-8", errors="replace")
        self._check_credentials(text, url)
        if succeeds:
            self.assertEqual(process.returncode, 0, self._redact(text, url))
        else:
            self.assertNotEqual(process.returncode, 0, "Command unexpectedly succeeded")
        return text

    async def _snapshot(self, url=None):
        async with self._connection(url) as conn:
            return await schema_snapshot(conn)

    async def _versions(self, url=None):
        async with self._connection(url) as conn:
            if not await conn.fetchval("SELECT to_regclass('fin.alembic_version')"):
                return []
            return list(await conn.fetch("SELECT version_num FROM fin.alembic_version ORDER BY version_num"))

    async def _assert_baseline(self, url=None):
        self.assertEqual([r["version_num"] for r in await self._versions(url)], [BASELINE])

    async def _assert_head(self, url=None):
        self.assertEqual([r["version_num"] for r in await self._versions(url)], [HEAD])

    async def _load_legacy(self, url=None):
        async with self._connection(url) as conn:
            await conn.execute("CREATE SCHEMA fin")
            # Frozen independent fixture, never the new migration's own SQL.
            await conn.execute(LEGACY_DDL.read_text(encoding="utf-8"))

    async def _seed_domain(self):
        async with self._connection() as conn:
            await conn.execute(DOMAIN_SENTINELS)

    async def _domain_state(self, *, legacy_columns=False):
        added_columns = {
            "task_runs": ["agent_id", "reply_id", "tool_call_id", "operation", "idempotency_key"],
            "task_artifacts": ["title", "input_ids", "filename", "mime_type"],
            "source_documents": ["fetch_status", "fetch_error"],
        }
        async with self._connection() as conn:
            rows = {}
            for table in sorted(BASELINE_TABLES):
                rows[table] = await conn.fetchval(
                    f"SELECT coalesce(jsonb_agg(to_jsonb(t) - $1::text[] ORDER BY to_jsonb(t)::text), '[]'::jsonb)::text "
                    f"FROM fin.{table} t",
                    added_columns.get(table, []) if legacy_columns else [],
                )
            sequences = {}
            for name in ("fact_financials_fact_id_seq", "run_steps_step_id_seq"):
                sequences[name] = tuple(await conn.fetchrow(f"SELECT last_value, is_called FROM fin.{name}"))
            return {"rows": rows, "sequences": sequences}

    @asynccontextmanager
    async def _test_revision(self, *, broken=False):
        with tempfile.TemporaryDirectory(prefix="finagent-migration-revisions-") as directory:
            scripts = Path(directory) / "migrations"
            shutil.copytree(MIGRATIONS, scripts)
            # Keep generated revisions out of the production migrations tree.
            body = '''"""Disposable integration-test successor."""
from alembic import op
revision = "test_successor"
down_revision = "{parent}"
branch_labels = None
depends_on = None

def upgrade():
    op.execute("CREATE TABLE fin.migration_probe (id integer PRIMARY KEY)")
    op.execute("INSERT INTO fin.migration_probe VALUES (1)")
    {failure}

def downgrade():
    op.execute("DROP TABLE fin.migration_probe")
'''.replace("{parent}", HEAD).replace("{failure}", 'op.execute("SELECT 1 / 0")' if broken else "pass")
            (scripts / "versions" / "9999_test_successor.py").write_text(body, encoding="utf-8")
            yield scripts


class MigrationIntegrationTests(DedicatedPostgresTestCase):
    async def test_fresh_schema_matches_frozen_legacy(self):
        legacy = await self._new_database()
        await self._load_legacy(legacy)
        # Baseline equivalence is intentionally independent of later migrations.
        await self._alembic("upgrade", BASELINE)
        actual = await self._snapshot()
        expected = await self._snapshot(legacy)
        self.assertEqual(actual, expected)
        self.assertEqual({t["name"] for t in actual["tables"]}, BASELINE_TABLES)
        self.assertTrue(any(c["name"] == "uq_fact" and c["nulls_not_distinct"]
                            and "NULLS NOT DISTINCT" in c["definition"]
                            for c in actual["constraints"]))
        self.assertTrue(any(i["name"] == "ix_doc_chunks_vec" and i["access_method"] == "hnsw"
                            and "vector_cosine_ops" in i["definition"]
                            for i in actual["indexes"]))
        self.assertEqual({(s["owner_table"], s["owner_column"]) for s in actual["sequences"]},
                         {("fact_financials", "fact_id"), ("run_steps", "step_id")})
        await self._assert_baseline()

    async def test_schema_snapshot_is_read_only_and_restores_search_path(self):
        await self._load_legacy()
        await self._seed_domain()
        before = await self._domain_state()
        async with self._connection() as conn:
            await conn.execute("SET search_path = fin, public")
            await conn.execute("SET default_transaction_read_only = on")
            path = await conn.fetchval("SHOW search_path")
            snapshot = await schema_snapshot(conn)
            self.assertEqual(await conn.fetchval("SHOW search_path"), path)
            self.assertEqual(await conn.fetchval("SHOW default_transaction_read_only"), "on")
            self.assertEqual({t["name"] for t in snapshot["tables"]}, BASELINE_TABLES)
        self.assertEqual(await self._domain_state(), before)

    async def test_repeated_upgrade_is_idempotent_and_cli_defaults_to_head(self):
        await self._cli()
        await self._seed_domain()
        before = await self._snapshot(), await self._domain_state()
        await self._cli("upgrade")
        await self._alembic("upgrade", "head")
        self.assertEqual((await self._snapshot(), await self._domain_state()), before)
        await self._assert_head()

    async def test_unversioned_legacy_upgrade_refused_without_mutation(self):
        await self._load_legacy()
        await self._seed_domain()
        before = await self._snapshot(), await self._domain_state()
        await self._alembic("upgrade", "head", fails=True)
        await self._cli("upgrade", succeeds=False)
        self.assertEqual((await self._snapshot(), await self._domain_state()), before)
        self.assertEqual(await self._versions(), [])

    async def test_explicit_baseline_adoption_preserves_rows_sequences_and_schema(self):
        await self._load_legacy()
        await self._seed_domain()
        async with self._connection() as conn:
            await conn.execute("""
                INSERT INTO fin.source_documents
                  (doc_id,company,doc_type,url,fetched_at,local_path,trust_level)
                VALUES ('legacy-doc','TEST','10-Q','https://example.invalid/filing',
                        '2026-01-01 00:00:00+00','/legacy/retained.html','verified');
                INSERT INTO fin.task_runs
                  (run_id,session_id,user_id,user_query,status,phase,data_request)
                VALUES ('legacy-run','test-session','test-user','preserve me',
                        'succeeded','done','{"legacy":true}');
                INSERT INTO fin.task_artifacts
                  (artifact_id,run_id,kind,format,content,meta)
                VALUES ('legacy-table','legacy-run','table','json','retained content',
                        '{"value":"123.4567"}');
            """)
        before = await self._snapshot(), await self._domain_state()
        # Exercise the public CLI, not guessed internal adoption attributes.
        await self._cli("stamp-baseline", succeeds=False)
        self.assertEqual(await self._versions(), [])
        self.assertEqual((await self._snapshot(), await self._domain_state()), before)
        await self._cli("stamp-baseline", "--verified")
        await self._assert_baseline()
        self.assertEqual((await self._snapshot(), await self._domain_state()), before)
        await self._cli("upgrade", "head")
        await self._assert_head()
        self.assertNotEqual(await self._snapshot(), before[0])
        self.assertEqual(await self._domain_state(legacy_columns=True), before[1])
        after = await self._snapshot(), await self._domain_state()
        # A stamp is an adoption of unversioned tables, not an overwrite tool.
        await self._cli("stamp-baseline", "--verified", succeeds=False)
        await self._assert_head()
        self.assertEqual((await self._snapshot(), await self._domain_state()), after)
        async with self._connection() as conn:
            self.assertEqual(await conn.fetchval("SELECT fetch_status FROM fin.source_documents WHERE doc_id='legacy-doc'"), "fetched")
            self.assertEqual(await conn.fetchval("SELECT input_ids FROM fin.task_artifacts WHERE artifact_id='legacy-table'"), [])
            self.assertEqual(await conn.fetchval("SELECT nextval('fin.fact_financials_fact_id_seq')"), 731)
            self.assertEqual(await conn.fetchval("SELECT nextval('fin.run_steps_step_id_seq')"), 920)

    async def test_partial_schema_refuses_upgrade_and_verified_adoption(self):
        await self._load_legacy()
        async with self._connection() as conn:
            await conn.execute("DROP TABLE fin.agent_memory")
            # Still eleven tables: adoption must check names, not just count.
            await conn.execute("CREATE TABLE fin.not_a_baseline_table (id integer)")
        before = await self._snapshot()
        self.assertEqual(len(before["tables"]), 11)
        await self._alembic("upgrade", "head", fails=True)
        await self._cli("stamp-baseline", "--verified", succeeds=False)
        self.assertEqual(await self._snapshot(), before)
        self.assertEqual(await self._versions(), [])

    async def test_empty_database_cannot_be_stamped_as_baseline(self):
        before = await self._snapshot()
        await self._cli("stamp-baseline", "--verified", succeeds=False)
        self.assertEqual(await self._snapshot(), before)
        self.assertEqual(await self._versions(), [])

    async def test_missing_vector_fails_without_partial_domain_schema(self):
        without_vector = await self._new_database(vector=False)
        before = await self._snapshot(without_vector)
        await self._alembic("upgrade", "head", url=without_vector, fails=True)
        await self._cli("upgrade", url=without_vector, succeeds=False)
        self.assertEqual(await self._snapshot(without_vector), before)
        self.assertEqual(await self._versions(without_vector), [])
        async with self._connection(without_vector) as conn:
            self.assertFalse(await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')"
            ))

    async def test_unknown_requested_revision_fails_without_mutation(self):
        before = await self._snapshot()
        await self._alembic("upgrade", "unknown_test_revision", fails=True)
        await self._cli("upgrade", "unknown_test_revision", succeeds=False)
        self.assertEqual(await self._snapshot(), before)
        self.assertEqual(await self._versions(), [])

    async def test_unknown_stored_revision_cannot_be_overwritten(self):
        await self._load_legacy()
        async with self._connection() as conn:
            await conn.execute("""
                CREATE TABLE fin.alembic_version (version_num varchar(32) PRIMARY KEY);
                INSERT INTO fin.alembic_version VALUES ('unknown_stored_revision');
            """)
        before = await self._snapshot(), await self._versions()
        await self._alembic("upgrade", "head", fails=True)
        await self._cli("stamp-baseline", "--verified", succeeds=False)
        self.assertEqual((await self._snapshot(), await self._versions()), before)

    async def test_baseline_downgrade_is_refused_and_data_retained(self):
        await self._alembic("upgrade", BASELINE)
        await self._seed_domain()
        before = await self._snapshot(), await self._domain_state()
        await self._alembic("downgrade", "base", fails=True)
        await self._cli("downgrade", "base", succeeds=False)
        self.assertEqual((await self._snapshot(), await self._domain_state()), before)
        await self._assert_baseline()

    async def test_head_schema_supports_runtime_and_metadata_only_documents(self):
        await self._cli()
        await self._assert_head()
        snapshot = await self._snapshot()
        self.assertEqual({t["name"] for t in snapshot["tables"]}, BASELINE_TABLES | {"builtin_agents"})
        async with self._connection() as conn:
            columns = await conn.fetch("""
                SELECT table_name,column_name,is_nullable FROM information_schema.columns
                WHERE table_schema='fin'
            """)
            names = {(r["table_name"], r["column_name"]) for r in columns}
            self.assertTrue({("task_runs", name) for name in
                             ("agent_id", "reply_id", "tool_call_id", "operation", "idempotency_key")} <= names)
            self.assertTrue({("task_artifacts", name) for name in
                             ("title", "input_ids", "filename", "mime_type")} <= names)
            self.assertTrue({("source_documents", name) for name in ("fetch_status", "fetch_error")} <= names)
            nullable = {r["column_name"]: r["is_nullable"] for r in columns if r["table_name"] == "source_documents"}
            self.assertEqual(nullable["fetched_at"], "YES")
            self.assertEqual(nullable["local_path"], "YES")
            indexes = await conn.fetchval("SELECT indexdef FROM pg_indexes WHERE schemaname='fin' AND indexname='uq_fin_operation_call'")
            self.assertIn("UNIQUE", indexes)
            self.assertIn("idempotency_key IS NOT NULL", indexes)
            self.assertFalse(await conn.fetchval("SELECT EXISTS(SELECT 1 FROM pg_constraint WHERE conrelid='fin.task_artifacts'::regclass AND conname='task_artifacts_run_id_kind_key')"))
        self.assertIn(HEAD, await self._cli("current"))
        history = await self._cli("history")
        self.assertIn(BASELINE, history)
        self.assertIn(HEAD, history)

    async def test_document_upgrade_preserves_runtime_rows_and_pins_file_metadata(self):
        await self._alembic("upgrade", RUNTIME)
        await self._seed_domain()
        before = await self._domain_state()
        await self._alembic("upgrade", "head")
        self.assertEqual(await self._domain_state(), before)
        await self._assert_head()
        async with self._connection() as conn:
            await conn.execute("""
                INSERT INTO fin.task_runs
                  (run_id,session_id,user_id,user_query,status,phase)
                VALUES ('document-run','test-session','test-user','filing','succeeded','done');
                INSERT INTO fin.task_artifacts (artifact_id,run_id,kind,format,path,meta,title)
                VALUES ('document-test','document-run','document','html','/fixture/hash.html',
                        '{"document":{"sha256":"fixed-hash","source_url":"https://www.sec.gov/fixture.htm"}}',
                        'Original filing');
            """)
            saved = await conn.fetchrow("SELECT kind,path,meta::text FROM fin.task_artifacts WHERE artifact_id='document-test'")
            self.assertEqual(saved["kind"], "document")
            self.assertEqual(saved["path"], "/fixture/hash.html")
            self.assertIn("fixed-hash", saved["meta"])
        snapshot = await self._snapshot(), await self._domain_state()
        await self._alembic("upgrade", "head")
        self.assertEqual((await self._snapshot(), await self._domain_state()), snapshot)
        await self._alembic("downgrade", RUNTIME, fails=True)
        self.assertEqual((await self._snapshot(), await self._domain_state()), snapshot)
        await self._assert_head()

    async def test_document_downgrade_without_documents_preserves_data(self):
        await self._alembic("upgrade", "head")
        await self._seed_domain()
        before = await self._domain_state()
        await self._alembic("downgrade", RUNTIME)
        self.assertEqual(await self._domain_state(), before)
        self.assertEqual([row["version_num"] for row in await self._versions()], [RUNTIME])
        await self._alembic("upgrade", "head")
        await self._assert_head()
        self.assertEqual(await self._domain_state(), before)

    async def test_runtime_downgrade_is_refused_without_data_loss(self):
        await self._alembic("upgrade", "head")
        await self._seed_domain()
        async with self._connection() as conn:
            await conn.execute("""
                INSERT INTO fin.task_runs
                  (run_id,session_id,user_id,user_query,status,phase,agent_id)
                VALUES ('runtime-run','test-session','test-user','test','running','exporting','test-agent');
                INSERT INTO fin.task_artifacts (artifact_id,run_id,kind,format,title)
                VALUES ('table-one','runtime-run','table','json','First'),
                       ('table-two','runtime-run','table','json','Second'),
                       ('export-one','runtime-run','export','xlsx','Export');
            """)
        before = await self._snapshot(), await self._domain_state()
        await self._alembic("downgrade", BASELINE, fails=True)
        await self._cli("downgrade", BASELINE, succeeds=False)
        self.assertEqual((await self._snapshot(), await self._domain_state()), before)
        await self._assert_head()

    async def test_temporary_successor_upgrade_and_downgrade_to_head(self):
        await self._alembic("upgrade", "head")
        await self._seed_domain()
        before = await self._snapshot(), await self._domain_state()
        async with self._test_revision() as scripts:
            await self._alembic("upgrade", "head", scripts=scripts)
            self.assertEqual([r["version_num"] for r in await self._versions()], ["test_successor"])
            async with self._connection() as conn:
                self.assertEqual(await conn.fetchval("SELECT id FROM fin.migration_probe"), 1)
            self.assertEqual(await self._domain_state(), before[1])
            await self._alembic("downgrade", HEAD, scripts=scripts)
        self.assertEqual((await self._snapshot(), await self._domain_state()), before)
        await self._assert_head()

    async def test_failed_successor_ddl_is_transactionally_rolled_back(self):
        await self._alembic("upgrade", "head")
        await self._seed_domain()
        before = await self._snapshot(), await self._domain_state()
        async with self._test_revision(broken=True) as scripts:
            await self._alembic("upgrade", "head", scripts=scripts, fails=True)
        self.assertEqual((await self._snapshot(), await self._domain_state()), before)
        await self._assert_head()
        async with self._connection() as conn:
            self.assertIsNone(await conn.fetchval("SELECT to_regclass('fin.migration_probe')"))

    async def test_current_is_read_only_on_fresh_and_legacy_databases(self):
        async with self._connection() as conn:
            self.assertFalse(await conn.fetchval("SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'fin')"))
        await self._alembic("current")
        await self._cli("current")
        async with self._connection() as conn:
            self.assertFalse(await conn.fetchval("SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'fin')"))
        await self._load_legacy()
        before = await self._snapshot()
        await self._alembic("current")
        await self._cli("current")
        self.assertEqual(await self._snapshot(), before)
        async with self._connection() as conn:
            self.assertIsNone(await conn.fetchval("SELECT to_regclass('fin.alembic_version')"))

    async def test_cli_connection_error_does_not_expose_credentials(self):
        # A nonexistent role fails even on a disposable server using trust auth.
        # Punctuation exercises both raw and URL-encoded password redaction.
        bad_url = self.url.set(
            username="missing_migration_role_" + uuid4().hex,
            password="migration-secret-" + uuid4().hex + ":/?#@%",
        )
        await self._cli("current", url=bad_url, succeeds=False)
        self.assertEqual(await self._versions(), [])

    async def test_cli_current_and_history_report_baseline(self):
        await self._cli("upgrade", BASELINE)
        self.assertIn(BASELINE, await self._cli("current"))
        self.assertIn(BASELINE, await self._cli("history"))
        await self._assert_baseline()


if __name__ == "__main__":
    unittest.main()
