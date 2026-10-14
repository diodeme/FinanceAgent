"""Opt-in owned repository/bootstrap tests; never use the ordinary DATABASE_URL.

Uses the dedicated MIGRATION_TEST_ADMIN_URL server and UUID database lifecycle
from test_migrations. No HTTP, model, provider, real user, or app startup calls.
"""

import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal
import json
import os
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from finagent import db, repository
from finagent.contracts import Cell, Evidence, FinanceError, FinancialRow, FindRequest, TableData
from tests import test_migrations


OWNER = "test-finance-owner"
OTHER = "test-finance-other-owner"
SESSION = "test-finance-session"
AGENT = "test-finance-agent"


def synthetic_table(*, instant=False):
    evidence = Evidence(
        source_id="synthetic-sec-source", company="AAPL",
        concept="Assets" if instant else "Revenues",
        metric="total_assets" if instant else "revenue",
        period_start=None if instant else date(2025, 1, 1),
        period_end=date(2025, 3, 31), value="123456.7890", unit="USD",
        accession="0000320193-25-999999", form="10-Q",
        filed_at=date(2025, 5, 1), retrieved_at=datetime(2025, 5, 2, tzinfo=timezone.utc),
        api_url="https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json",
        filing_url="https://www.sec.gov/Archives/edgar/data/320193/synthetic-test-only.htm",
    )
    return TableData(
        rows=[FinancialRow(
            company=evidence.company, period_start=evidence.period_start,
            period_end=evidence.period_end, period_label="FY2025 Q2",
            cells=[Cell(cell_id="synthetic-cell", metric=evidence.metric, label="Revenue",
                        value=evidence.value, unit=evidence.unit, source_ids=[evidence.source_id])],
        )], sources={evidence.source_id: evidence}, warnings=["Synthetic test evidence only"],
    )


class FinanceDatabaseCase(test_migrations.DedicatedPostgresTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self._alembic("upgrade", "head")
        directory = tempfile.TemporaryDirectory(prefix="finagent-repository-test-")
        self.addCleanup(directory.cleanup)
        environment = patch.dict(os.environ, {
            "DATABASE_URL": self._sqlalchemy_url(self.url), "FIN_DATA_DIR": directory.name,
        })
        environment.start()
        self.addCleanup(environment.stop)
        # get_pool is imported by repository/bootstrap but reads these module
        # globals. A fresh lock and a closed pool prevent cross-loop leakage.
        globals_patch = patch.multiple(db, _pool=None, _lock=asyncio.Lock())
        globals_patch.start()
        self.addCleanup(globals_patch.stop)
        self.addAsyncCleanup(self._close_pool)

    async def _close_pool(self):
        pool, db._pool = db._pool, None
        if pool is not None:
            await pool.close()

    async def start(self, *, owner=OWNER, session=SESSION, call=None, request=None):
        return await repository.start_operation(
            owner, AGENT, session, "test-reply", call or "test-call-" + uuid4().hex,
            "data_fetch", request if request is not None else {"companies": ["AAPL"]},
        )

    async def assert_finance_error(self, code, awaitable):
        with self.assertRaises(FinanceError) as caught:
            await awaitable
        self.assertEqual(caught.exception.code, code)


class FinanceRepositoryTests(FinanceDatabaseCase):
    async def test_round_trip_sources_cells_and_multiple_artifacts_of_same_kind(self):
        operation = await self.start()
        original = synthetic_table()
        first = await repository.save_artifact(
            OWNER, operation["run_id"], "dataset", "Synthetic revenue", data=original,
            warnings=["Artifact warning"],
        )
        second = await repository.save_artifact(
            OWNER, operation["run_id"], "dataset", "Second dataset", data=original,
        )
        self.assertNotEqual(first.artifact_id, second.artifact_id)
        source = first.data.sources["synthetic-sec-source"]
        self.assertIsInstance(source.fact_id, int)
        self.assertEqual(source.fact_id, second.data.sources[source.source_id].fact_id)
        self.assertIsNone(original.sources[source.source_id].fact_id)
        original.rows[0].cells[0].value = "0"
        await repository.finish_operation(operation["run_id"], "succeeded")
        saved = await repository.get_artifact(OWNER, first.artifact_id)
        self.assertEqual(saved.model_dump(mode="json"), first.model_dump(mode="json"))
        self.assertEqual(saved.data.rows[0].cells[0].value, "123456.7890")
        provenance = await repository.cell_provenance(OWNER, first.artifact_id, "synthetic-cell")
        self.assertEqual(provenance["cell"]["value"], "123456.7890")
        self.assertEqual(provenance["sources"][0]["fact_id"], source.fact_id)
        self.assertEqual(provenance["sources"][0]["filing_url"], source.filing_url)
        self.assertEqual(provenance["sources"][0]["document_status"], "metadata_only")
        self.assertIn("Artifact warning", provenance["warnings"])
        async with self._connection() as conn:
            fact = await conn.fetchrow("SELECT * FROM fin.fact_financials WHERE fact_id=$1", source.fact_id)
            self.assertEqual(fact["value"], Decimal("123456.7890"))
            self.assertEqual(fact["concept"], "us-gaap:Revenues")
            self.assertEqual(await conn.fetchval("SELECT count(*) FROM fin.fact_financials"), 1)
            document = await conn.fetchrow("SELECT * FROM fin.source_documents WHERE doc_id=$1", fact["doc_id"])
            self.assertEqual(document["fetch_status"], "metadata_only")
            self.assertIsNone(document["fetched_at"])
            self.assertIsNone(document["local_path"])

    async def test_document_versions_remain_pinned_when_shared_source_refreshes(self):
        from tests.test_filings import RAW, document_fixture
        first_run = await self.start()
        snapshot = synthetic_table()
        snapshot.sources["synthetic-sec-source"].accession = document_fixture().accession
        numeric = await repository.save_artifact(OWNER, first_run["run_id"], "dataset", "Metadata snapshot", data=snapshot)
        document = document_fixture()
        root = repository.filing_root()
        root.mkdir()
        path = root / f"{document.sha256}.html"
        path.write_bytes(RAW)
        first = await repository.save_artifact(OWNER, first_run["run_id"], "document", "Original", document=document, path=str(path))
        for owner in (OWNER, OTHER):
            await self.assert_finance_error("not_found", repository.read_document(owner, first.artifact_id))
        await repository.finish_operation(first_run["run_id"], "partial")
        refreshed_run = await self.start()
        changed = RAW + b"\nrefreshed bytes"
        refreshed = document_fixture(changed)
        refreshed.fetched_at = datetime(2025, 5, 3, tzinfo=timezone.utc)
        new_path = root / f"{refreshed.sha256}.html"
        new_path.write_bytes(changed)
        second = await repository.save_artifact(OWNER, refreshed_run["run_id"], "document", "Refreshed", document=refreshed, path=str(new_path))
        await repository.finish_operation(refreshed_run["run_id"], "succeeded")
        self.assertEqual(await repository.read_document(OWNER, first.artifact_id), RAW)
        self.assertEqual(await repository.read_document(OWNER, second.artifact_id), changed)
        self.assertEqual((await repository.get_artifact(OWNER, first.artifact_id)).document, document)
        self.assertEqual((await repository.get_artifact(OWNER, numeric.artifact_id)).data.sources["synthetic-sec-source"].document_status, "metadata_only")
        self.assertEqual((await repository.artifact_file(OWNER, first.artifact_id)), (path.resolve(), document.primary_document, "application/octet-stream"))
        provenance = await repository.document_provenance(OWNER, first.artifact_id)
        self.assertEqual(provenance["document"]["sha256"], document.sha256)
        self.assertNotIn("path", provenance)
        for request in (
            repository.read_document(OTHER, first.artifact_id),
            repository.document_provenance(OTHER, first.artifact_id),
            repository.artifact_file(OTHER, first.artifact_id),
        ):
            await self.assert_finance_error("not_found", request)
        found = await repository.find_artifacts(OWNER, SESSION, FindRequest(kind="document"))
        self.assertEqual({item.artifact_id for item in found}, {first.artifact_id, second.artifact_id})
        async with self._connection() as conn:
            shared = await conn.fetchrow("SELECT * FROM fin.source_documents WHERE doc_id=$1", document.source_document_id)
            self.assertEqual(shared["checksum"], refreshed.sha256)
            self.assertEqual(shared["local_path"], str(new_path))
            self.assertEqual(shared["fetch_status"], "fetched")
            self.assertEqual(await conn.fetchval("SELECT count(*) FROM fin.source_documents"), 1)
        path.write_bytes(b"x" * len(RAW))
        await self.assert_finance_error("document_integrity", repository.read_document(OWNER, first.artifact_id))
        self.assertEqual(await repository.read_document(OWNER, second.artifact_id), changed)

    async def test_concurrent_idempotency_created_flag_replay_and_argument_conflict(self):
        results = await asyncio.gather(*[self.start(call="same-call") for _ in range(5)])
        self.assertEqual(sum(result["created"] for result in results), 1)
        self.assertEqual(len({result["run_id"] for result in results}), 1)
        run_id = results[0]["run_id"]
        artifact = await repository.save_artifact(OWNER, run_id, "narrative", "Saved result", text="Synthetic")
        await repository.finish_operation(run_id, "succeeded")
        replay = await self.start(call="same-call")
        self.assertFalse(replay["created"])
        self.assertEqual(replay["status"], "succeeded")
        self.assertEqual(replay["artifact_ids"], [artifact.artifact_id])
        await self.assert_finance_error("idempotency_conflict", self.start(
            call="same-call", request={"companies": ["MSFT"]},
        ))
        other_owner = await self.start(owner=OTHER, call="same-call")
        other_session = await self.start(session="test-other-session", call="same-call")
        self.assertTrue(other_owner["created"])
        self.assertTrue(other_session["created"])
        self.assertEqual(len({run_id, other_owner["run_id"], other_session["run_id"]}), 3)

    async def test_save_checks_owner_run_and_input_ownership(self):
        operation = await self.start()
        foreign = await self.start(owner=OTHER)
        foreign_artifact = await repository.save_artifact(OTHER, foreign["run_id"], "narrative", "Private")
        await repository.finish_operation(foreign["run_id"], "succeeded")
        for run_id in (foreign["run_id"], "test-nonexistent-run"):
            await self.assert_finance_error("not_found", repository.save_artifact(
                OWNER, run_id, "narrative", "Cannot publish",
            ))
        for input_id in (foreign_artifact.artifact_id, "test-nonexistent-artifact"):
            await self.assert_finance_error("not_found", repository.save_artifact(
                OWNER, operation["run_id"], "narrative", "Cannot reference", input_ids=[input_id],
            ))
        source_run = await self.start()
        own = await repository.save_artifact(OWNER, source_run["run_id"], "dataset", "Owned", data=synthetic_table())
        await repository.finish_operation(source_run["run_id"], "partial")
        derived = await repository.save_artifact(
            OWNER, operation["run_id"], "narrative", "Derived",
            input_ids=[own.artifact_id, own.artifact_id],
        )
        self.assertEqual(derived.input_ids, [own.artifact_id])
        async with self._connection() as conn:
            self.assertEqual(await conn.fetchval("SELECT count(*) FROM fin.task_artifacts WHERE run_id=$1", operation["run_id"]), 1)

    async def test_terminal_and_stale_operations_cannot_publish_or_be_revived(self):
        for status in ("failed", "interrupted", "succeeded", "partial"):
            with self.subTest(status=status):
                operation = await self.start()
                await repository.finish_operation(operation["run_id"], status, "Synthetic terminal state")
                await self.assert_finance_error("operation_finished", repository.save_artifact(
                    OWNER, operation["run_id"], "dataset", "Too late", data=synthetic_table(),
                ))
                await repository.finish_operation(operation["run_id"], "succeeded")
                async with self._connection() as conn:
                    self.assertEqual(await conn.fetchval("SELECT status FROM fin.task_runs WHERE run_id=$1", operation["run_id"]), status)
        stale, live = await self.start(), await self.start()
        async with self._connection() as conn:
            await conn.execute("UPDATE fin.task_runs SET heartbeat_at=now()-interval '11 minutes' WHERE run_id=$1", stale["run_id"])
        self.assertEqual(await repository.recover_stale_operations(), 1)
        self.assertEqual(await repository.recover_stale_operations(), 0)
        await self.assert_finance_error("operation_finished", repository.save_artifact(
            OWNER, stale["run_id"], "narrative", "Stale worker",
        ))
        await repository.save_artifact(OWNER, live["run_id"], "narrative", "Still live")
        async with self._connection() as conn:
            self.assertEqual(await conn.fetchval("SELECT count(*) FROM fin.fact_financials"), 0)

    async def test_saved_drafts_from_running_failed_interrupted_runs_are_hidden(self):
        root = repository.report_root()
        root.mkdir(parents=True)
        consumer = await self.start()
        for status in ("running", "failed", "interrupted"):
            with self.subTest(status=status):
                operation = await self.start()
                path = root / (status + ".xlsx")
                path.write_bytes(b"synthetic test file")
                draft = await repository.save_artifact(
                    OWNER, operation["run_id"], "export", "Unpublished draft", path=str(path),
                )
                if status != "running":
                    await repository.finish_operation(operation["run_id"], status)
                await self.assert_finance_error("not_found", repository.get_artifact(OWNER, draft.artifact_id))
                await self.assert_finance_error("not_found", repository.artifact_file(OWNER, draft.artifact_id))
                await self.assert_finance_error("not_found", repository.find_artifacts(
                    OWNER, SESSION, FindRequest(artifact_id=draft.artifact_id),
                ))
                self.assertEqual(await repository.find_artifacts(OWNER, SESSION, FindRequest(current_session_only=False, kind=None)), [])
                await self.assert_finance_error("not_found", repository.save_artifact(
                    OWNER, consumer["run_id"], "narrative", "Cannot consume draft", input_ids=[draft.artifact_id],
                ))
        # A successful partial result is intentionally readable and downloadable.
        partial_run = await self.start()
        partial = await repository.save_artifact(
            OWNER, partial_run["run_id"], "export", "Partial result", path=str(path),
        )
        await repository.finish_operation(partial_run["run_id"], "partial")
        self.assertEqual((await repository.get_artifact(OWNER, partial.artifact_id)).artifact_id, partial.artifact_id)
        self.assertEqual((await repository.artifact_file(OWNER, partial.artifact_id))[0], path.resolve())
        self.assertEqual([a.artifact_id for a in await repository.find_artifacts(OWNER, SESSION, FindRequest(kind=None, current_session_only=True))], [partial.artifact_id])

    async def test_same_source_conflicts_never_overwrite_raw_fact_or_saved_result(self):
        # Includes NULL period_start, exercising UNIQUE NULLS NOT DISTINCT.
        for instant in (False, True):
            with self.subTest(instant=instant):
                operation = await self.start()
                table = synthetic_table(instant=instant)
                first = await repository.save_artifact(OWNER, operation["run_id"], "dataset", "Original", data=table)
                repeated = await repository.save_artifact(OWNER, operation["run_id"], "dataset", "Repeat", data=table)
                source = first.data.sources["synthetic-sec-source"]
                self.assertEqual(source.fact_id, repeated.data.sources[source.source_id].fact_id)
                for changes in ({"value": "123457.7890"}, {"unit": "EUR"}):
                    conflicting = table.model_copy(deep=True)
                    for name, value in changes.items():
                        setattr(conflicting.sources[source.source_id], name, value)
                    await self.assert_finance_error("source_conflict", repository.save_artifact(
                        OWNER, operation["run_id"], "dataset", "Conflict", data=conflicting,
                    ))
                async with self._connection() as conn:
                    row = await conn.fetchrow("SELECT value,unit FROM fin.fact_financials WHERE fact_id=$1", source.fact_id)
                    self.assertEqual(tuple(row), (Decimal("123456.7890"), "USD"))
                    self.assertEqual(await conn.fetchval("SELECT count(*) FROM fin.task_artifacts WHERE run_id=$1", operation["run_id"]), 2)
                await repository.finish_operation(operation["run_id"], "succeeded")
                self.assertEqual((await repository.get_artifact(OWNER, first.artifact_id)).data, first.data)

    async def test_cross_owner_read_provenance_find_and_download_are_blocked(self):
        operation = await self.start()
        dataset = await repository.save_artifact(OWNER, operation["run_id"], "dataset", "Private data", data=synthetic_table())
        await repository.finish_operation(operation["run_id"], "succeeded")
        export_run = await self.start()
        root = repository.report_root()
        root.mkdir(parents=True)
        path = root / "synthetic.xlsx"
        path.write_bytes(b"synthetic test download; not a real workbook")
        export = await repository.save_artifact(
            OWNER, export_run["run_id"], "export", "Private report", path=str(path),
            filename="synthetic.xlsx", mime_type="application/test", input_ids=[dataset.artifact_id],
        )
        await repository.finish_operation(export_run["run_id"], "succeeded")
        self.assertEqual(await repository.artifact_file(OWNER, export.artifact_id), (path.resolve(), "synthetic.xlsx", "application/test"))
        for request in (
            repository.get_artifact(OTHER, dataset.artifact_id),
            repository.cell_provenance(OTHER, dataset.artifact_id, "synthetic-cell"),
            repository.find_artifacts(OTHER, SESSION, FindRequest(artifact_id=dataset.artifact_id)),
            repository.artifact_file(OTHER, export.artifact_id),
        ):
            await self.assert_finance_error("not_found", request)
        self.assertEqual(await repository.find_artifacts(OTHER, SESSION, FindRequest(current_session_only=False, kind=None)), [])
        self.assertEqual(await repository.find_artifacts(OWNER, "test-other-session", FindRequest(kind=None, current_session_only=True)), [])
        found = await repository.find_artifacts(OWNER, "test-other-session", FindRequest(current_session_only=False, kind="dataset"))
        self.assertEqual([item.artifact_id for item in found], [dataset.artifact_id])
        await self.assert_finance_error("not_found", repository.cell_provenance(OWNER, dataset.artifact_id, "unknown-cell"))
        await self.assert_finance_error("invalid_path", repository.save_artifact(
            OWNER, operation["run_id"], "export", "Outside", path=str(root.parent / "outside.xlsx"),
        ))
        path.unlink()
        await self.assert_finance_error("file_unavailable", repository.artifact_file(OWNER, export.artifact_id))


class FinanceCatalogTests(FinanceDatabaseCase):
    async def catalog_artifact(
        self, artifact_id, *, kind="narrative", title=None, owner=OWNER,
        session=SESSION, status="succeeded", meta=None, inputs=None,
    ):
        # Insert synthetic snapshots directly so legacy/corrupt dependency graphs
        # can be tested without relaxing the production writer's authorization.
        operation = await self.start(owner=owner, session=session)
        async with self._connection() as conn:
            await conn.execute("""
                INSERT INTO fin.task_artifacts
                  (artifact_id,run_id,kind,format,content,meta,created_at,title,input_ids)
                VALUES ($1,$2,$3,'json',$4,$5::jsonb,$6,$7,$8)
            """, artifact_id, operation["run_id"], kind, "body-only-secret",
                json.dumps(meta or {}), datetime(2025, 6, 1, tzinfo=timezone.utc),
                title or artifact_id, inputs or [])
        if status != "running":
            await repository.finish_operation(operation["run_id"], status)
        return artifact_id

    @staticmethod
    def scope_meta(company="AAPL", start="2025-01-01", end="2025-03-31"):
        return {"data": {"rows": [{"company": company, "period_start": start,
                                    "period_end": end, "cells": [{"private": "cell-secret"}]}],
                         "sources": {"private": "source-secret"}}}

    async def test_default_cross_session_explicit_local_kind_owner_and_status_filters(self):
        await self.catalog_artifact("local-report")
        await self.catalog_artifact("remote-report", session="older-session", status="partial")
        await self.catalog_artifact("foreign-report", owner=OTHER)
        for status in ("running", "failed", "interrupted"):
            await self.catalog_artifact("draft-" + status, status=status)
        for kind in ("dataset", "table", "export", "document"):
            await self.catalog_artifact("kind-" + kind, kind=kind)
        found = await repository.find_result_summaries(OWNER, SESSION, FindRequest())
        self.assertEqual([r.artifact_id for r in found], ["remote-report", "local-report"])
        self.assertEqual([r.run_status for r in found], ["partial", "succeeded"])
        self.assertEqual([r.source_session_id for r in found], ["older-session", SESSION])
        self.assertEqual([r.is_current_session for r in found], [False, True])
        local = await repository.find_result_summaries(OWNER, SESSION, FindRequest(current_session_only=True))
        self.assertEqual([r.artifact_id for r in local], ["local-report"])
        for kind in ("dataset", "table", "export", "document"):
            found = await repository.find_result_summaries(OWNER, SESSION, FindRequest(kind=kind))
            self.assertEqual([r.artifact_id for r in found], ["kind-" + kind])
        all_kinds = await repository.find_result_summaries(OWNER, SESSION, FindRequest(kind=None, limit=10))
        self.assertEqual({r.artifact_id for r in all_kinds}, {
            "local-report", "remote-report", "kind-dataset", "kind-table", "kind-export", "kind-document",
        })

    async def test_visible_table_document_and_derived_scopes_keep_dates_per_company(self):
        await self.catalog_artifact("wide-dataset", kind="dataset", meta=self.scope_meta("NVDA"))
        table = self.scope_meta()
        table["data"]["rows"].append({"company": "MSFT", "period_end": "2024-12-31"})
        table["data"]["history_rows"] = [{"company": "HISTORY", "period_end": "2020-01-01"}]
        await self.catalog_artifact("visible-table", kind="table", meta=table, inputs=["wide-dataset"])
        await self.catalog_artifact("source-document", kind="document", meta={
            "document": {"company": "TSLA", "period_end": "2023-12-31", "source_url": "url-secret"},
        })
        await self.catalog_artifact("analysis-report", inputs=["visible-table"], meta={"citations": [
            {"artifact_id": "visible-table", "cell_id": "cell-secret"},
            {"document_id": "source-document"},
        ]})
        await self.catalog_artifact("export-report", kind="export", inputs=["analysis-report"])
        rows = {r.artifact_id: r for r in await repository.find_result_summaries(
            OWNER, SESSION, FindRequest(kind=None, limit=10),
        )}
        self.assertEqual(rows["visible-table"].companies, ["AAPL", "MSFT"])
        self.assertEqual(rows["wide-dataset"].companies, ["NVDA"])
        self.assertEqual(rows["source-document"].companies, ["TSLA"])
        expected = [
            {"company": "AAPL", "period_start": "2025-01-01", "period_end": "2025-03-31"},
            {"company": "MSFT", "period_start": None, "period_end": "2024-12-31"},
            {"company": "TSLA", "period_start": None, "period_end": "2023-12-31"},
        ]
        for artifact_id in ("analysis-report", "export-report"):
            self.assertEqual(rows[artifact_id].companies, ["AAPL", "MSFT", "TSLA"])
            self.assertEqual(rows[artifact_id].model_dump(mode="json")["periods"], expected)
        self.assertTrue(all(r.scope_complete for r in rows.values()))
        await self.catalog_artifact("undated-document", kind="document", meta={"document": {"company": "AAPL"}})
        undated = await repository.find_result_summaries(OWNER, SESSION, FindRequest(kind="document", query="undated"))
        self.assertEqual(undated[0].companies, ["AAPL"])
        self.assertEqual(undated[0].periods, [])
        self.assertFalse(undated[0].scope_complete)

    async def test_missing_foreign_and_unpublished_dependencies_never_leak_scope(self):
        await self.catalog_artifact("own-input", kind="dataset", meta=self.scope_meta())
        await self.catalog_artifact("foreign-input", kind="dataset", owner=OTHER, meta=self.scope_meta("PRIVATE"))
        await self.catalog_artifact("draft-input", kind="dataset", status="failed", meta=self.scope_meta("DRAFT"))
        await self.catalog_artifact("report", inputs=["own-input", "missing-input", "foreign-input"], meta={
            "citations": [{"artifact_id": "draft-input", "cell_id": "hidden-cell"}],
        })
        result = (await repository.find_result_summaries(OWNER, SESSION, FindRequest()))[0]
        self.assertEqual(result.companies, ["AAPL"])
        self.assertEqual(len(result.periods), 1)
        self.assertFalse(result.scope_complete)
        payload = result.model_dump_json()
        for secret in ("PRIVATE", "DRAFT", "foreign-input", "missing-input", "draft-input", "hidden-cell"):
            self.assertNotIn(secret, payload)
        self.assertEqual(set(result.model_dump()), {
            "artifact_id", "kind", "title", "created_at", "companies", "periods",
            "source_session_id", "is_current_session", "run_status", "scope_complete",
        })

    async def test_newest_stable_sort_limit_and_literal_title_metadata_search(self):
        for index in range(12):
            await self.catalog_artifact(f"sorted-{index:02d}")
        for limit in (1, 5, 10):
            result = await repository.find_result_summaries(OWNER, SESSION, FindRequest(limit=limit))
            self.assertEqual([r.artifact_id for r in result], [f"sorted-{i:02d}" for i in range(11, 11-limit, -1)])
        default = await repository.find_result_summaries(OWNER, SESSION, FindRequest())
        self.assertEqual(len(default), 5)
        # Newer timestamp takes priority over lexicographic artifact IDs.
        async with self._connection() as conn:
            await conn.execute("UPDATE fin.task_artifacts SET created_at=created_at+interval '1 day' WHERE artifact_id='sorted-00'")
        newest = await repository.find_result_summaries(OWNER, SESSION, FindRequest(limit=1))
        self.assertEqual(newest[0].artifact_id, "sorted-00")
        await self.catalog_artifact("literal", title="Literal %_\\ marker", meta={"warnings": ["Metadata needle"]})
        for query in ("%", "_", "\\", "%_\\", "metadata NEEDLE"):
            result = await repository.find_result_summaries(OWNER, SESSION, FindRequest(query=query))
            self.assertEqual([r.artifact_id for r in result], ["literal"])
        self.assertEqual(await repository.find_result_summaries(OWNER, SESSION, FindRequest(query="body-only-secret")), [])

    async def test_projection_excludes_bodies_and_batches_shared_dependencies(self):
        await self.catalog_artifact("shared-table", kind="table", meta=self.scope_meta())
        for index in range(5):
            await self.catalog_artifact(f"report-{index}", inputs=["shared-table"])
        pool = await db.get_pool()
        calls, fetched = [], []

        async def fetch(sql, *args):
            calls.append((sql, args))
            rows = await pool.fetch(sql, *args)
            fetched.extend(dict(row) for row in rows)
            return rows

        with patch.object(repository, "get_pool", return_value=SimpleNamespace(fetch=fetch)), \
                patch.object(repository, "_artifact", side_effect=AssertionError("Full artifact loaded")):
            results = await repository.find_result_summaries(OWNER, SESSION, FindRequest())
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][1], (["shared-table"], OWNER))
        self.assertTrue(all(r.scope_complete and r.companies == ["AAPL"] for r in results))
        allowed = {"artifact_id", "kind", "title", "created_at", "session_id", "status", "scope", "refs"}
        for row in fetched:
            self.assertEqual(set(row), allowed)
        serialized = json.dumps(fetched, default=str)
        for secret in ("body-only-secret", "cell-secret", "source-secret", "cells", "sources", "history_rows"):
            self.assertNotIn(secret, serialized)

    async def test_cycles_depth_and_dependency_count_are_bounded_and_incomplete(self):
        await self.catalog_artifact("cycle-a", inputs=["cycle-b"])
        await self.catalog_artifact("cycle-b", inputs=["cycle-a"])
        cycles = await repository.find_result_summaries(OWNER, SESSION, FindRequest(query="cycle"))
        self.assertEqual(len(cycles), 2)
        self.assertTrue(all(not r.scope_complete for r in cycles))
        await self.catalog_artifact("chain-base", kind="table", meta=self.scope_meta())
        previous = "chain-base"
        for index in range(repository._CATALOG_MAX_DEPTH + 1):
            previous = await self.catalog_artifact(f"chain-{index}", inputs=[previous])
        deep = await repository.find_result_summaries(OWNER, SESSION, FindRequest(query=previous))
        self.assertFalse(deep[0].scope_complete)
        self.assertEqual(deep[0].companies, [])
        # The earliest authorized dependency is retained, but arbitrary fan-out
        # cannot trigger unbounded queries or hide truncation as complete scope.
        refs = ["chain-base"] + [f"missing-{i:03d}" for i in range(140)]
        await self.catalog_artifact("wide-report", inputs=refs)
        pool = await db.get_pool()
        calls = []

        async def fetch(sql, *args):
            calls.append(args)
            return await pool.fetch(sql, *args)

        with patch.object(repository, "get_pool", return_value=SimpleNamespace(fetch=fetch)):
            wide = await repository.find_result_summaries(OWNER, SESSION, FindRequest(query="wide-report"))
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(calls[1][0]), repository._CATALOG_MAX_DEPENDENCIES)
        self.assertEqual(wide[0].companies, ["AAPL"])
        self.assertFalse(wide[0].scope_complete)

    async def test_find_versioned_and_legacy_idempotency_without_rewriting_old_requests(self):
        legacy = {"artifact_id": None, "query": None, "kind": None, "limit": 5, "current_session_only": True}
        current = FindRequest().model_dump(mode="json") | {"_retrieval_version": 2}

        async def start(call, request, *, compatibility=None, operation="find_results"):
            return await repository.start_operation(
                OWNER, AGENT, SESSION, "test-reply", call, operation, request,
                legacy_request=compatibility,
            )

        old = await start("old-find", legacy)
        await repository.finish_operation(old["run_id"], "succeeded")
        replay = await start("old-find", current, compatibility=legacy)
        self.assertFalse(replay["created"])
        self.assertTrue(replay["legacy_find"])
        self.assertEqual(replay["run_id"], old["run_id"])
        async with self._connection() as conn:
            stored = await conn.fetchval("SELECT data_request FROM fin.task_runs WHERE run_id=$1", old["run_id"])
        self.assertEqual(json.loads(stored) if isinstance(stored, str) else stored, legacy)
        fresh = await start("new-find", current, compatibility=legacy)
        self.assertTrue(fresh["created"])
        again = await start("new-find", current, compatibility=legacy)
        self.assertFalse(again["created"])
        self.assertFalse(again["legacy_find"])
        self.assertEqual(again["run_id"], fresh["run_id"])
        await self.assert_finance_error("idempotency_conflict", start("old-find", current))
        await self.assert_finance_error("idempotency_conflict", start(
            "old-find", current | {"query": "changed"}, compatibility=legacy | {"query": "changed"},
        ))
        await self.assert_finance_error("idempotency_conflict", start(
            "new-find", current | {"limit": 10}, compatibility=legacy,
        ))
        await start("not-find", legacy, operation="data_fetch")
        await self.assert_finance_error("idempotency_conflict", start(
            "not-find", current, compatibility=legacy, operation="data_fetch",
        ))


class FakeAgentStorage:
    """Only the three provisioning APIs exist; any other API is an error."""

    def __init__(self):
        self.records = {}
        self.calls = []

    def add(self, user_id, agent_id, *, source="user", name="FinanceAgent"):
        record = SimpleNamespace(id=agent_id, user_id=user_id, source=source,
                                 data=SimpleNamespace(name=name))
        self.records[user_id, agent_id] = record
        return record

    async def get_agent(self, user_id, agent_id):
        self.calls.append(("get_agent", user_id))
        await asyncio.sleep(0)
        return self.records.get((user_id, agent_id))

    async def list_agents(self, user_id):
        self.calls.append(("list_agents", user_id))
        await asyncio.sleep(0)
        return [record for (owner, _), record in self.records.items() if owner == user_id]

    async def upsert_agent(self, user_id, record):
        self.calls.append(("upsert_agent", user_id))
        await asyncio.sleep(0)
        agent_id = "test-agent-" + uuid4().hex
        self.add(user_id, agent_id, name=record.data.name)
        return agent_id

    def __getattr__(self, name):
        raise AssertionError("Bootstrap touched unsupported storage API: " + name)


class FinanceBootstrapTests(FinanceDatabaseCase):
    async def test_per_user_mapping_reuse_and_concurrent_bootstrap(self):
        from finagent.bootstrap import ensure_finance_agent

        storage = FakeAgentStorage()
        storage.add(OWNER, "test-unrelated-agent", name="Unrelated")
        results = await asyncio.gather(*[ensure_finance_agent(OWNER, storage) for _ in range(5)])
        agent_id = results[0]["agent_id"]
        self.assertEqual({result["agent_id"] for result in results}, {agent_id})
        self.assertEqual(storage.calls.count(("upsert_agent", OWNER)), 1)
        self.assertTrue(await repository.is_finance_agent(OWNER, agent_id))
        self.assertFalse(await repository.is_finance_agent(OTHER, agent_id))
        self.assertIn((OWNER, "test-unrelated-agent"), storage.records)
        again = await ensure_finance_agent(OWNER, storage)
        self.assertEqual(again["agent_id"], agent_id)
        other = await ensure_finance_agent(OTHER, storage)
        self.assertNotEqual(other["agent_id"], agent_id)
        self.assertEqual(storage.calls.count(("upsert_agent", OWNER)), 1)
        self.assertFalse(results[0]["capabilities"]["web_search"])
        self.assertFalse(results[0]["capabilities"]["filing_text"])
        # The fixture's public session/message rows are also checked in tearDown.
        async with self._connection() as conn:
            self.assertEqual(await conn.fetchval("SELECT count(*) FROM fin.builtin_agents"), 2)
            self.assertEqual(await conn.fetchval("SELECT count(*) FROM fin.task_runs"), 0)
            self.assertGreater(await conn.fetchval("SELECT count(*) FROM fin.company_registry"), 0)

    async def test_existing_owned_agent_is_reused_and_stale_mapping_repaired(self):
        from finagent.bootstrap import ensure_finance_agent

        storage = FakeAgentStorage()
        storage.add(OWNER, "test-template-agent", source="builtin")
        storage.add(OWNER, "test-existing-finance")
        storage.add(OTHER, "test-other-finance")
        async with self._connection() as conn:
            await conn.execute("INSERT INTO fin.builtin_agents(user_id,agent_id) VALUES($1,$2)", OWNER, "test-deleted-agent")
        result = await ensure_finance_agent(OWNER, storage)
        self.assertEqual(result["agent_id"], "test-existing-finance")
        self.assertFalse(any(method == "upsert_agent" for method, _ in storage.calls))
        self.assertTrue(await repository.is_finance_agent(OWNER, "test-existing-finance"))
        self.assertFalse(await repository.is_finance_agent(OWNER, "test-other-finance"))
