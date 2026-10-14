"""Versioned finance tool inputs and immutable result payloads."""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CompanyInfo(Contract):
    company: str
    cik: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    sector: str = "technology"
    fy_end_month: int = 12
    verified: bool = False
    supported: bool = True
    warning: str | None = None


class LookupRequest(Contract):
    query: str = Field(min_length=1, max_length=200)


class FetchRequest(Contract):
    companies: list[str] = Field(min_length=1, max_length=6)
    metrics: list[str] = Field(default_factory=lambda: ["revenue", "net_income"], min_length=1, max_length=12)
    quarters: int = Field(default=4, ge=1, le=8)
    start_date: date | None = None
    end_date: date | None = None
    as_of: date | None = None
    dimensions: dict[str, str] = Field(default_factory=dict)
    refresh: bool = False

    @model_validator(mode="after")
    def valid_dates(self):
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("start_date must not be after end_date")
        return self


class FilingRequest(Contract):
    companies: list[str] = Field(min_length=1, max_length=6)
    forms: list[Literal["10-K", "10-Q"]] = Field(default_factory=lambda: ["10-K", "10-Q"], min_length=1, max_length=2)
    count: int = Field(default=1, ge=1, le=4, description="Filings per company; at most eight in total.")
    start_date: date | None = Field(default=None, description="Earliest report period end, not filing date.")
    end_date: date | None = Field(default=None, description="Latest report period end, not filing date.")
    as_of: date | None = None
    accession: str | None = Field(default=None, pattern=r"^\d{10}-\d{2}-\d{6}$")
    include_amendments: bool = False
    refresh: bool = Field(default=False, description="Force redownload only when the user explicitly requests refreshed originals; finding the latest filing normally reuses verified local documents.")

    @model_validator(mode="after")
    def valid_scope(self):
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("start_date must not be after end_date")
        if len(self.companies) * self.count > 8:
            raise ValueError("Request at most eight filings in total")
        if self.accession and (len(self.companies) != 1 or self.count != 1):
            raise ValueError("An accession selects one filing for one company")
        return self


class CustomMetric(Contract):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    label: str = Field(min_length=1, max_length=100)
    formula: str = Field(min_length=1, max_length=300)


class CalculateRequest(Contract):
    data_ids: list[str] = Field(min_length=1, max_length=8)
    metrics: list[str] = Field(default_factory=list, max_length=12)
    custom_metrics: list[CustomMetric] = Field(default_factory=list, max_length=6)
    growth: Literal["none", "qoq", "yoy"] = "none"


class AnalyzeRequest(Contract):
    data_ids: list[str] = Field(default_factory=list, max_length=6)
    document_ids: list[str] = Field(default_factory=list, max_length=8)
    question: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def has_evidence(self):
        if not self.data_ids and not self.document_ids:
            raise ValueError("Provide data_ids or document_ids from saved results")
        return self


class FindRequest(Contract):
    artifact_id: str | None = Field(default=None, json_schema_extra={"deprecated": True}, description="Legacy detail lookup. Use get_result for new calls.")
    query: str | None = Field(default=None, max_length=200, description="Literal title/metadata keyword; omit for recent reports, not a natural-language search.")
    kind: Literal["dataset", "table", "narrative", "export", "document"] | None = "narrative"
    limit: int = Field(default=5, ge=1, le=10)
    current_session_only: bool = Field(default=False, description="False searches your saved results across sessions; true restricts to this session.")


class GetResultRequest(Contract):
    artifact_id: str = Field(min_length=1, description="Exact artifact_id returned by a result catalog or earlier tool response.")


class ExportRequest(Contract):
    data_ids: list[str] = Field(default_factory=list, max_length=8)
    analysis_ids: list[str] = Field(default_factory=list, max_length=6)
    title: str = Field(default="财务分析报告", min_length=1, max_length=120)

    @model_validator(mode="after")
    def has_input(self):
        if not self.data_ids and not self.analysis_ids:
            raise ValueError("Provide data_ids or analysis_ids from saved results")
        return self


class Evidence(Contract):
    source_id: str
    fact_id: int | None = None
    company: str
    concept: str
    metric: str
    period_start: date | None = None
    period_end: date
    value: str
    unit: str
    accession: str
    form: str
    filed_at: date
    retrieved_at: datetime
    api_url: str
    filing_url: str
    document_status: Literal["metadata_only", "fetched", "unavailable"] = "metadata_only"
    note: str | None = None


class Cell(Contract):
    cell_id: str
    metric: str
    label: str
    value: str | None
    unit: str
    status: Literal["available", "missing", "unsupported", "conflicting"] = "available"
    formula: str | None = None
    source_ids: list[str] = Field(default_factory=list)
    inputs: list[dict] = Field(default_factory=list)
    reason: str | None = None


class FinancialRow(Contract):
    company: str
    period_start: date | None
    period_end: date
    period_label: str
    cells: list[Cell]


class TableData(Contract):
    rows: list[FinancialRow]
    sources: dict[str, Evidence] = Field(default_factory=dict)
    # Additional comparable periods used for growth; never silently shown as
    # part of the user's requested table or included in its coverage gate.
    history_rows: list[FinancialRow] = Field(default_factory=list)
    specification: dict = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    selection_policy: str = "sec-latest-known-v1"


class Citation(Contract):
    artifact_id: str
    cell_id: str
    label: str = ""


class DocumentInfo(Contract):
    source_document_id: str
    company: str
    cik: str
    form: str
    period_end: date | None = None
    filed_at: date | None = None
    accession: str
    primary_document: str
    source_url: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_size: int = Field(ge=1)
    media_type: str = "text/html"
    fetched_at: datetime


class DocumentCitation(Contract):
    document_id: str
    label: str = ""


class Artifact(Contract):
    artifact_id: str
    run_id: str
    kind: Literal["dataset", "table", "narrative", "export", "document"]
    title: str
    created_at: datetime
    input_ids: list[str] = Field(default_factory=list)
    data: TableData | None = None
    document: DocumentInfo | None = None
    text: str | None = None
    citations: list[Citation | DocumentCitation] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    filename: str | None = None
    mime_type: str | None = None

    @model_validator(mode="after")
    def valid_document(self):
        if (self.kind == "document") != (self.document is not None):
            raise ValueError("Document artifacts require a document descriptor; other kinds must not have one")
        return self


class ResultPeriod(Contract):
    company: str
    period_start: date | None = None
    period_end: date


class ResultSummary(Contract):
    artifact_id: str
    kind: Literal["dataset", "table", "narrative", "export", "document"]
    title: str
    created_at: datetime
    companies: list[str] = Field(default_factory=list)
    periods: list[ResultPeriod] = Field(default_factory=list)
    source_session_id: str
    is_current_session: bool
    run_status: Literal["succeeded", "partial"]
    scope_complete: bool = True


class FinanceEnvelope(Contract):
    schema_version: Literal[1] = 1
    message: str = ""
    artifacts: list[Artifact] = Field(default_factory=list)
    results: list[ResultSummary] = Field(default_factory=list)
    companies: list[CompanyInfo] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    error_code: str | None = None
    partial: bool = False


class FinanceError(Exception):
    """Safe, actionable domain error that can be returned to the model/UI."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)
