"""Bounded SEC filing metadata selection and immutable original HTML publication.

No DOM parsing, section extraction or implicit claim that a filing was read.
"""
from dataclasses import dataclass
from datetime import date
from typing import Awaitable, Callable

from .contracts import DocumentInfo, FilingRequest, FinanceEnvelope, FinanceError
from .financials import _candidates, _verify
from .repository import save_artifact, source_document_id
from .sec_client import SECClient

MAX_OLDER_FILES = 4
MAX_METADATA_ROWS = 20000
DOCUMENT_WARNING = "Original filing bytes were downloaded; download or citation does not imply the entire filing was read."


@dataclass(frozen=True)
class Filing:
    accession: str
    form: str
    period_end: date
    filed_at: date
    primary_document: str


def select_filings(data: dict, request: FilingRequest) -> list[Filing]:
    """SEC recent and historical submissions use the same parallel-array shape."""
    if not isinstance(data, dict):
        raise FinanceError("sec_invalid_response", "SEC filing metadata is not an object.")
    names = ("accessionNumber", "form", "reportDate", "filingDate", "primaryDocument")
    columns = [data.get(name, []) for name in names]
    if any(not isinstance(column, list) for column in columns) or len({len(column) for column in columns}) != 1:
        raise FinanceError("sec_invalid_response", "SEC filing metadata arrays are malformed.")
    if len(columns[0]) > MAX_METADATA_ROWS:
        raise FinanceError("filing_lookup_budget", "SEC filing metadata exceeds the bounded row budget.")
    cutoff = request.as_of or date.today()
    selected = {}
    for accession, form, report_date, filed_date, primary in zip(*columns):
        if not isinstance(form, str):
            continue
        base = form.removesuffix("/A")
        if base not in request.forms or (form.endswith("/A") and not request.include_amendments):
            continue
        if request.accession and accession != request.accession:
            continue
        try:
            period = date.fromisoformat(report_date)
            filed = date.fromisoformat(filed_date)
        except (TypeError, ValueError):
            continue
        # start/end constrain actual report periods; as_of constrains public availability too.
        if filed > cutoff or period > cutoff or period > filed:
            continue
        if request.start_date and period < request.start_date:
            continue
        if request.end_date and period > request.end_date:
            continue
        if not isinstance(accession, str) or not isinstance(primary, str):
            continue
        selected.setdefault(accession, Filing(accession, form, period, filed, primary))
    return sorted(selected.values(), key=lambda item: (item.period_end, item.filed_at, item.accession), reverse=True)


def _older_files(files: list, request: FilingRequest) -> list[dict]:
    if not isinstance(files, list) or len(files) > 1000:
        raise FinanceError("sec_invalid_response", "SEC historical submissions index is malformed or exceeds its budget.")
    selected = []
    cutoff = request.as_of or date.today()
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise FinanceError("sec_invalid_response", "SEC historical submissions entry is malformed.")
        try:
            earliest = date.fromisoformat(item["filingFrom"])
            latest = date.fromisoformat(item["filingTo"])
        except (KeyError, TypeError, ValueError):
            # Without a valid date range we cannot safely exclude this file.
            earliest, latest = date.min, date.max
        if earliest > cutoff or (request.start_date and latest < request.start_date):
            continue
        selected.append((latest, item))
    return [item for _, item in sorted(selected, key=lambda pair: pair[0], reverse=True)]


async def locate_filings(client: SECClient, cik: str, request: FilingRequest) -> tuple[list[Filing], list[str]]:
    # Identity verification already fetched this payload; refresh is deliberately
    # false here to reuse those exact fresh metadata rather than request it twice.
    payload = await client.submissions(cik)
    filings = payload.data.get("filings", {})
    if not isinstance(filings, dict):
        raise FinanceError("sec_invalid_response", "SEC filings metadata is malformed.")
    selected = {item.accession: item for item in select_filings(filings.get("recent", {}), request)}
    notes = []
    if len(selected) < request.count:
        older = _older_files(filings.get("files", []), request)
        for file in older[:MAX_OLDER_FILES]:
            try:
                past = await client.older_submissions(cik, file["name"], refresh=request.refresh)
                for item in select_filings(past.data, request):
                    selected.setdefault(item.accession, item)
            except FinanceError as exc:
                notes.append(f"Historical metadata unavailable ({exc.code}): {exc}")
                # In particular do not keep issuing requests after a SEC 403.
                break
            if len(selected) >= request.count:
                break
        if len(selected) < request.count and len(older) > MAX_OLDER_FILES:
            notes.append(f"Historical filing lookup budget reached ({MAX_OLDER_FILES} files); coverage is incomplete.")
    result = sorted(selected.values(), key=lambda item: (item.period_end, item.filed_at, item.accession), reverse=True)[:request.count]
    if len(result) < request.count:
        notes.append(f"Located {len(result)} of {request.count} requested filings in the bounded metadata search; no complete coverage is claimed.")
    return result, notes


async def fetch_filings(user_id: str, run_id: str, request: FilingRequest,
                        progress: Callable[[str], Awaitable[None]]) -> FinanceEnvelope:
    companies = []
    for query in request.companies:
        matches = _candidates(query)
        if len(matches) != 1:
            raise FinanceError("unsupported_company", f"A unique configured SEC company is required for {query!r}.")
        if matches[0].company not in {item.company for item in companies}:
            companies.append(matches[0])
    artifacts, notes = [], []
    reused = downloaded = 0
    error_code = None
    async with SECClient() as client:
        # Fail before any network work when real SEC identification is missing.
        client.require_contact()
        for company in companies:
            try:
                await progress(f"Verifying {company.company} and locating requested SEC primary filing documents…")
                await _verify(company, client, request.refresh)
                selected, missing = await locate_filings(client, company.cik, request)
                notes.extend(f"{company.company}: {note}" for note in missing)
            except FinanceError as exc:
                notes.append(f"{company.company}: {exc.code}: {exc}")
                error_code = error_code or exc.code
                continue
            for filing in selected:
                try:
                    await progress(f"Resolving {company.company} {filing.form} {filing.period_end} original HTML bytes…")
                    original = await client.original_document(company.cik, filing.accession, filing.primary_document, refresh=request.refresh)
                    action = "Reused local cached" if original.cached else "Downloaded"
                    await progress(f"{action} {company.company} {filing.form} {filing.period_end} original HTML.")
                    document = DocumentInfo(
                        source_document_id=source_document_id(company.company, filing.accession),
                        company=company.company, cik=company.cik, form=filing.form,
                        period_end=filing.period_end, filed_at=filing.filed_at, accession=filing.accession,
                        primary_document=filing.primary_document, source_url=original.url,
                        sha256=original.sha256, byte_size=original.byte_size, fetched_at=original.retrieved_at,
                        media_type=original.media_type,
                    )
                    artifact = await save_artifact(
                        user_id, run_id, "document", f"{company.company} {filing.form} {filing.period_end}",
                        document=document, path=str(original.path), filename=filing.primary_document,
                        mime_type="application/octet-stream", warnings=[DOCUMENT_WARNING],
                    )
                    artifacts.append(artifact)
                    if original.cached:
                        reused += 1
                    else:
                        downloaded += 1
                except FinanceError as exc:
                    notes.append(f"{company.company} {filing.accession}: {exc.code}: {exc}")
                    error_code = error_code or exc.code
                    if exc.code in {"operation_finished", "not_found"}:
                        raise
                    if exc.code == "sec_forbidden":
                        break
    expected = len(companies) * request.count
    partial = bool(notes) or len(artifacts) < expected
    return FinanceEnvelope(
        message=f"Prepared {len(artifacts)} of {expected} requested original filings ({reused} reused from local cache, {downloaded} downloaded). "
                + ("Some requested scope remains unavailable; see warnings." if partial else "These are original bytes, not parsed or fully-read filings."),
        artifacts=artifacts, warnings=notes, partial=partial,
        error_code=(error_code or "filings_unavailable") if not artifacts else None,
    )
