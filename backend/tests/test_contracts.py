"""Document additions retain the existing numeric finance contracts."""

from datetime import datetime, timezone
import unittest

from pydantic import ValidationError

from finagent.contracts import (
    AnalyzeRequest, Artifact, Citation, DocumentCitation, DocumentInfo,
    FilingRequest, FinanceEnvelope, FindRequest,
)


def document_info():
    return DocumentInfo(
        source_document_id="sec_fixture", company="AAPL", cik="0000320193",
        form="10-Q", accession="0000320193-26-000001", primary_document="fixture.htm",
        source_url="https://www.sec.gov/Archives/edgar/data/320193/000032019326000001/fixture.htm",
        sha256="a" * 64, byte_size=123, fetched_at=datetime.now(timezone.utc),
    )


class DocumentContractTests(unittest.TestCase):
    def test_analysis_accepts_numeric_document_or_mixed_inputs(self):
        self.assertEqual(AnalyzeRequest(data_ids=["table_a"], question="Compare").document_ids, [])
        self.assertEqual(AnalyzeRequest(document_ids=["document_a"], question="Read").data_ids, [])
        request = AnalyzeRequest(data_ids=["table_a"], document_ids=["document_a"], question="Explain")
        self.assertEqual(request.document_ids, ["document_a"])
        with self.assertRaises(ValidationError):
            AnalyzeRequest(question="No evidence")
        with self.assertRaises(ValidationError):
            AnalyzeRequest(document_ids=["d"], question="Read", owner="someone-else")

    def test_filing_scope_is_bounded_and_uses_report_dates(self):
        self.assertEqual(FilingRequest(companies=["AAPL"]).count, 1)
        for kwargs in [
            {"companies": ["AAPL", "MSFT", "NVDA"], "count": 4},
            {"companies": ["AAPL"], "forms": ["8-K"]},
            {"companies": ["AAPL"], "accession": "../escape"},
            {"companies": ["AAPL", "MSFT"], "accession": "0000320193-26-000001"},
            {"companies": ["AAPL"], "start_date": "2026-06-30", "end_date": "2026-03-31"},
        ]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValidationError):
                FilingRequest(**kwargs)

    def test_document_artifact_requires_pinned_descriptor(self):
        fields = dict(artifact_id="document_a", run_id="r", kind="document", title="Filing",
                      created_at=datetime.now(timezone.utc))
        with self.assertRaises(ValidationError):
            Artifact(**fields)
        artifact = Artifact(**fields, document=document_info())
        self.assertEqual(artifact.document.sha256, "a" * 64)
        with self.assertRaises(ValidationError):
            Artifact(**{**fields, "kind": "narrative"}, document=document_info())
        for update in [{"sha256": "not-a-hash"}, {"byte_size": 0}]:
            with self.subTest(update=update), self.assertRaises(ValidationError):
                DocumentInfo.model_validate({**document_info().model_dump(), **update})

    def test_cell_and_document_citations_round_trip_without_fake_cells(self):
        artifact = Artifact(
            artifact_id="narrative_a", run_id="r", kind="narrative", title="Report",
            created_at=datetime.now(timezone.utc), text="Evidence",
            citations=[Citation(artifact_id="table_a", cell_id="cell_a"),
                       DocumentCitation(document_id="document_a", label="Original filing")],
        )
        restored = Artifact.model_validate_json(artifact.model_dump_json())
        self.assertIsInstance(restored.citations[0], Citation)
        self.assertIsInstance(restored.citations[1], DocumentCitation)
        self.assertNotIn("cell_id", restored.citations[1].model_dump())
        self.assertIsNone(restored.document)

    def test_legacy_envelope_and_document_find_are_compatible(self):
        envelope = FinanceEnvelope.model_validate({"schema_version": 1, "message": "Saved", "artifacts": [], "companies": [], "warnings": []})
        self.assertFalse(envelope.partial)
        self.assertTrue(FinanceEnvelope(partial=True).partial)
        self.assertEqual(FindRequest(kind="document").kind, "document")


if __name__ == "__main__":
    unittest.main()
