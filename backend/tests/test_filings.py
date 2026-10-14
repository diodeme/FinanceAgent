"""Offline raw filing selection, storage, provenance and attachment regressions.

All HTTP uses MockTransport; temporary files only. No real SEC/model requests.
"""
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from finagent import filings, repository
from finagent.contracts import Artifact, DocumentInfo, FilingRequest, FinanceError
from finagent.sec_client import SECClient, RateGate, filing_url

CIK = "0000320193"
ACCESSION = "0000320193-25-000001"
NOW = datetime(2025, 5, 2, tzinfo=timezone.utc)
RAW = b'<!doctype html>\r\n<html><body>Original \xff bytes<script>alert(1)</script></body></html>'


def metadata(rows):
    keys = ("accessionNumber", "form", "reportDate", "filingDate", "primaryDocument")
    return {key: [row[index] for row in rows] for index, key in enumerate(keys)}


def document_fixture(raw=RAW):
    return DocumentInfo(
        source_document_id=repository.source_document_id("AAPL", ACCESSION), company="AAPL", cik=CIK,
        form="10-Q", period_end=date(2025, 3, 31), accession=ACCESSION,
        primary_document="aapl-20250331.htm", source_url=filing_url(CIK, ACCESSION, "aapl-20250331.htm"),
        sha256=hashlib.sha256(raw).hexdigest(), byte_size=len(raw), fetched_at=NOW,
    )


def artifact_fixture(raw=RAW):
    return Artifact(artifact_id="document_fixture", run_id="run_fixture", kind="document",
                    title="AAPL original filing", created_at=NOW, document=document_fixture(raw))


class FilingSelectionTests(unittest.TestCase):
    def test_report_period_asof_amendments_and_accession_are_distinct(self):
        rows = [
            (ACCESSION, "10-Q", "2025-03-31", "2025-05-01", "quarter.htm"),
            ("0000320193-25-000002", "10-Q/A", "2025-03-31", "2025-05-03", "amendment.htm"),
            ("0000320193-25-000003", "10-K", "2024-12-31", "2025-02-01", "annual.htm"),
            ("0000320193-25-000004", "8-K", "2025-03-31", "2025-04-01", "other.htm"),
        ]
        request = FilingRequest(companies=["AAPL"], start_date="2025-01-01", end_date="2025-03-31", as_of="2025-05-02", count=4)
        self.assertEqual([item.accession for item in filings.select_filings(metadata(rows), request)], [ACCESSION])
        request.include_amendments = True
        request.as_of = date(2025, 5, 4)
        self.assertEqual([item.form for item in filings.select_filings(metadata(rows), request)], ["10-Q/A", "10-Q"])
        request.accession = ACCESSION
        self.assertEqual(len(filings.select_filings(metadata(rows), request)), 1)
        request.as_of = date(2025, 4, 30)
        self.assertEqual(filings.select_filings(metadata(rows), request), [])

    def test_path_validation_rejects_urls_traversal_and_nonhtml(self):
        for filename in ("../a.htm", "dir/a.htm", "%2e%2e.htm", "https://evil.test/a.htm", "a.pdf", "a.htm?x=1", "a\\b.htm"):
            with self.subTest(filename=filename), self.assertRaises(FinanceError):
                filing_url(CIK, ACCESSION, filename)
        with self.assertRaises(FinanceError):
            filing_url("320193", ACCESSION, "a.htm")
        with self.assertRaises(FinanceError):
            filing_url(CIK, "../accession", "a.htm")
        self.assertEqual(filing_url(CIK, ACCESSION, "actual-primary.HTML"), "https://www.sec.gov/Archives/edgar/data/320193/000032019325000001/actual-primary.HTML")


class FilingHTTPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="finagent-original-test-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        # Synthetic identity used exclusively by MockTransport, never a live request.
        env = patch.dict(os.environ, {"FIN_DATA_DIR": directory.name, "SEC_USER_AGENT": "OfflineFixture fixture@financeagent.dev"})
        env.start()
        self.addCleanup(env.stop)

    def client(self, handler):
        return SECClient(cache_dir=self.root / "cache", filing_dir=self.root / "filings",
                         transport=httpx.MockTransport(handler), rate_gate=RateGate(0))

    async def test_original_bytes_cache_and_refresh_keep_old_hash_file(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, content=RAW if len(calls) == 1 else RAW + b"\nchanged", headers={"Content-Type": "text/html; charset=utf-8"})
        async with self.client(handler) as client:
            first = await client.original_document(CIK, ACCESSION, "aapl-20250331.htm")
            cached = await client.original_document(CIK, ACCESSION, "aapl-20250331.htm")
            refreshed = await client.original_document(CIK, ACCESSION, "aapl-20250331.htm", refresh=True)
        self.assertTrue(cached.cached)
        self.assertEqual(first.retrieved_at, cached.retrieved_at)
        self.assertEqual(len(calls), 2)
        self.assertEqual(first.path.read_bytes(), RAW)
        self.assertEqual(refreshed.path.read_bytes(), RAW + b"\nchanged")
        self.assertNotEqual(first.path, refreshed.path)
        self.assertNotIn("-index", str(calls[0].url))
        self.assertEqual(first.sha256, hashlib.sha256(RAW).hexdigest())
        self.assertEqual(calls[0].headers["accept-encoding"], "identity")

    async def test_original_cache_survives_expiry_and_new_client_without_network(self):
        calls = []
        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(200, content=RAW, headers={"Content-Type": "text/html"})
        async with self.client(handler) as client:
            first = await client.original_document(CIK, ACCESSION, "aapl-20250331.htm")
        index_path = next((self.root / "cache").glob("document-*.index"))
        index = json.loads(index_path.read_text())
        index["retrieved_at"] = NOW.isoformat()
        index_path.write_text(json.dumps(index))
        def no_network(request):
            self.fail("A verified original must not be redownloaded merely because time passed")
        async with self.client(no_network) as client:
            client.ttl = 0
            cached = await client.original_document(CIK, ACCESSION, "aapl-20250331.htm")
        self.assertTrue(cached.cached)
        self.assertEqual(cached.retrieved_at, NOW)
        self.assertEqual(cached.path, first.path)
        self.assertEqual(cached.path.read_bytes(), RAW)
        self.assertEqual(len(calls), 1)

    async def test_json_metadata_keeps_ttl_and_new_filing_has_its_own_cache_key(self):
        calls = []
        def handler(request):
            calls.append(str(request.url))
            if "/submissions/" in str(request.url):
                return httpx.Response(200, json={"cik": 320193})
            return httpx.Response(200, content=RAW, headers={"Content-Type": "text/html"})
        async with self.client(handler) as client:
            client.ttl = 0
            await client.submissions(CIK)
            metadata = await client.submissions(CIK)
            self.assertFalse(metadata.cached)
            await client.original_document(CIK, ACCESSION, "a.htm")
            other = await client.original_document(CIK, "0000320193-25-000002", "a.htm")
            self.assertFalse(other.cached)
            cached = await client.original_document(CIK, ACCESSION, "a.htm")
            self.assertTrue(cached.cached)
        self.assertEqual(len(calls), 4)

    async def test_configuration_403_media_type_and_stream_limit_fail_closed(self):
        calls = []
        def forbidden(request):
            calls.append(request)
            return httpx.Response(403)
        async with self.client(forbidden) as client:
            client.user_agent = "FinanceAgent/0.1 contact@example.com"
            with self.assertRaises(FinanceError) as caught:
                await client.original_document(CIK, ACCESSION, "a.htm")
            self.assertEqual(caught.exception.code, "sec_configuration_required")
            self.assertEqual(calls, [])
            client.user_agent = "OfflineFixture fixture@financeagent.dev"
            with self.assertRaises(FinanceError) as caught:
                await client.original_document(CIK, ACCESSION, "a.htm")
            self.assertEqual(caught.exception.code, "sec_forbidden")
            self.assertEqual(len(calls), 1)
        async with self.client(lambda r: httpx.Response(200, content=b"{}", headers={"Content-Type": "application/json"})) as client:
            with self.assertRaises(FinanceError) as caught:
                await client.original_document(CIK, ACCESSION, "a.htm")
            self.assertEqual(caught.exception.code, "unsupported_document")
        class Chunks(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"a" * 8
                yield b"b" * 8
        with patch("finagent.sec_client.MAX_RESPONSE_BYTES", 10):
            async with self.client(lambda r: httpx.Response(200, stream=Chunks(), headers={"Content-Type": "text/html"})) as client:
                with self.assertRaises(FinanceError) as caught:
                    await client.original_document(CIK, ACCESSION, "a.htm")
                self.assertEqual(caught.exception.code, "sec_response_too_large")
        self.assertFalse((self.root / "filings").exists())

    async def test_older_submissions_selection_and_bounded_missing_scope(self):
        calls = []
        recent = metadata([(ACCESSION, "10-Q", "2025-03-31", "2025-05-01", "recent.htm")])
        files = [{"name": f"CIK{CIK}-submissions-{index:03}.json", "filingFrom": "2020-01-01", "filingTo": "2024-12-31"} for index in range(1, 7)]
        def handler(request):
            calls.append(str(request.url))
            if str(request.url).endswith(f"CIK{CIK}.json"):
                return httpx.Response(200, json={"filings": {"recent": recent, "files": files}})
            return httpx.Response(200, json=metadata([]))
        request = FilingRequest(companies=["AAPL"], start_date="2023-01-01", end_date="2023-12-31")
        async with self.client(handler) as client:
            selected, notes = await filings.locate_filings(client, CIK, request)
            with self.assertRaises(FinanceError):
                await client.older_submissions(CIK, "CIK0000789019-submissions-001.json")
        self.assertEqual(selected, [])
        self.assertEqual(len(calls), 1 + filings.MAX_OLDER_FILES)
        self.assertTrue(any("budget reached" in note for note in notes))
        calls.clear()
        def found_handler(request):
            if str(request.url).endswith(f"CIK{CIK}.json"):
                return httpx.Response(200, json={"filings": {"recent": recent, "files": files}})
            return httpx.Response(200, json=metadata([("0000320193-24-000001", "10-K", "2023-12-31", "2024-02-01", "real-annual.htm")]))
        async with self.client(found_handler) as client:
            request.refresh = True
            selected, notes = await filings.locate_filings(client, CIK, request)
        self.assertEqual(selected[0].primary_document, "real-annual.htm")
        self.assertEqual(notes, [])

    async def test_partial_batch_publishes_valid_document_with_missing_company_scope(self):
        def handler(request):
            url = str(request.url)
            if "CIK0000789019" in url:
                return httpx.Response(403)
            if "submissions" in url:
                return httpx.Response(200, json={"cik": 320193, "name": "Apple Inc.", "tickers": ["AAPL"], "filings": {"recent": metadata([(ACCESSION, "10-Q", "2025-03-31", "2025-05-01", "aapl-20250331.htm")]), "files": []}})
            return httpx.Response(200, content=RAW, headers={"Content-Type": "text/html"})
        async def save(user, run, kind, title, **kwargs):
            self.assertEqual(Path(kwargs["path"]).read_bytes(), RAW)
            return Artifact(artifact_id="document_saved", run_id=run, kind=kind, title=title,
                            created_at=NOW, document=kwargs["document"], warnings=kwargs["warnings"])
        with patch("finagent.filings.SECClient", side_effect=lambda: self.client(handler)), patch("finagent.filings.save_artifact", side_effect=save):
            envelope = await filings.fetch_filings("owner", "run", FilingRequest(companies=["AAPL", "MSFT"]), AsyncMock())
            progress = AsyncMock()
            reused = await filings.fetch_filings("owner", "another-run", FilingRequest(companies=["AAPL"]), progress)
        self.assertIn("1 reused from local cache, 0 downloaded", reused.message)
        self.assertTrue(any("Reused local cached" in call.args[0] for call in progress.await_args_list))
        self.assertFalse(any("Downloading" in call.args[0] for call in progress.await_args_list))
        self.assertTrue(envelope.partial)
        self.assertEqual(len(envelope.artifacts), 1)
        self.assertIsNone(envelope.error_code)
        self.assertTrue(any("MSFT" in note and "sec_forbidden" in note for note in envelope.warnings))
        self.assertIn("1 of 2", envelope.message)

    async def test_read_document_checks_owner_status_path_size_and_sha(self):
        document = document_fixture()
        path = repository.filing_root() / f"{document.sha256}.html"
        path.parent.mkdir()
        path.write_bytes(RAW)
        row = {"path": str(path), "meta": {"document": document.model_dump(mode="json")}}
        pool = AsyncMock()
        async def select(query, artifact_id, user_id):
            self.assertIn("r.status IN ('succeeded','partial')", query)
            self.assertIn("r.user_id=$2", query)
            return row if user_id == "owner" and artifact_id == "document_fixture" else None
        pool.fetchrow.side_effect = select
        with patch("finagent.repository.get_pool", new=AsyncMock(return_value=pool)):
            self.assertEqual(await repository.read_document("owner", "document_fixture"), RAW)
            with self.assertRaises(FinanceError) as caught:
                await repository.read_document("foreign", "document_fixture")
            self.assertEqual(caught.exception.code, "not_found")
            row["path"] = str(path.parent.parent / path.name)
            with self.assertRaises(FinanceError) as caught:
                await repository.read_document("owner", "document_fixture")
            self.assertEqual(caught.exception.code, "file_unavailable")
            row["path"] = str(path)
            path.write_bytes(b"x" * len(RAW))
            with self.assertRaises(FinanceError) as caught:
                await repository.read_document("owner", "document_fixture")
            self.assertEqual(caught.exception.code, "document_integrity")
            path.write_bytes(RAW + b"x")
            with self.assertRaises(FinanceError):
                await repository.read_document("owner", "document_fixture")

    async def test_owned_provenance_and_download_never_expose_inline_html_or_path(self):
        from fastapi import FastAPI
        from agentscope.app.deps import get_current_user_id
        from finagent.routes import fin_router
        artifact = artifact_fixture()
        path = repository.filing_root() / f"{artifact.document.sha256}.html"
        path.parent.mkdir()
        path.write_bytes(RAW)
        app = FastAPI()
        app.include_router(fin_router)
        app.dependency_overrides[get_current_user_id] = lambda: "owner"
        row = {"path": str(path), "filename": "unsafe.htm", "mime_type": "text/html", "kind": "document", "meta": {"document": artifact.document.model_dump(mode="json")}}
        pool = AsyncMock()
        pool.fetchrow.return_value = row
        with patch("finagent.repository.get_pool", new=AsyncMock(return_value=pool)), patch("finagent.repository.get_artifact", new=AsyncMock(return_value=artifact)):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                provenance = await client.get("/fin/results/document_fixture/document")
                download = await client.get("/fin/artifacts/document_fixture/download")
        self.assertEqual(provenance.status_code, 200)
        self.assertEqual(set(provenance.json()), {"artifact_id", "title", "document", "warnings"})
        self.assertNotIn(str(path), provenance.text)
        self.assertEqual(download.content, RAW)
        self.assertEqual(download.headers["content-type"], "application/octet-stream")
        self.assertTrue(download.headers["content-disposition"].startswith("attachment;"))
        self.assertEqual(download.headers["cache-control"], "private, no-store")
        self.assertEqual(download.headers["x-content-type-options"], "nosniff")
        with patch("finagent.routes.document_provenance", new=AsyncMock(side_effect=FinanceError("not_found", "Not owned"))), patch("finagent.routes.artifact_file", new=AsyncMock(side_effect=FinanceError("not_found", "Not owned"))):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                self.assertEqual((await client.get("/fin/results/document_fixture/document")).status_code, 404)
                self.assertEqual((await client.get("/fin/artifacts/document_fixture/download")).status_code, 404)


if __name__ == "__main__":
    unittest.main()
