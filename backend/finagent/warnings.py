"""Exact warning aliases and selected-data relevance; never mutate saved artifacts."""

from collections.abc import Iterable

from .sec_client import CONTACT_WARNING

CROSS_COMPANY_WARNING = "Cross-company periods retain actual fiscal dates; equal row positions do not imply calendar alignment."
CROSS_COMPANY_QUARTERS_ALIAS = "Cross-company quarters retain actual fiscal dates and can cover different calendar windows."
ORIGINAL_DOCUMENT_WARNING = "Original filing documents were not fetched; source filing links are metadata only."


def normalize_warnings(
    warnings: Iterable[str], *, companies: Iterable[str] | None = None,
    include_diagnostics: bool = True,
) -> list[str]:
    """Unknown strings survive verbatim; only known warnings have special semantics.

    None means scope is unavailable (e.g. a saved narrative alone), not zero
    companies. Collection diagnostics remain on data/source records but must not
    be mistaken for narrative or export limitations, or current configuration.
    """
    multiple = None if companies is None else len(set(companies)) > 1
    result = []
    for warning in warnings:
        if warning == CROSS_COMPANY_QUARTERS_ALIAS:
            warning = CROSS_COMPANY_WARNING
        if warning == CROSS_COMPANY_WARNING and multiple is False:
            continue
        if warning == CONTACT_WARNING and not include_diagnostics:
            continue
        if warning not in result:
            result.append(warning)
    if multiple and CROSS_COMPANY_WARNING not in result:
        result.append(CROSS_COMPANY_WARNING)
    return result


def artifact_companies(artifacts) -> set[str]:
    """Use actual selected rows, not input ID counts or unselected history."""
    artifacts = list(artifacts)
    return ({row.company for artifact in artifacts if artifact.data for row in artifact.data.rows}
            | {artifact.document.company for artifact in artifacts if artifact.document})


def artifact_warnings(artifacts, *, include_diagnostics: bool = False) -> list[str]:
    """Call after resolving narrative evidence dependencies for export scope."""
    artifacts = list(artifacts)
    documents = [artifact for artifact in artifacts if artifact.document]
    warnings = [warning for artifact in artifacts
                for warning in artifact.warnings + (artifact.data.warnings if artifact.data else [])]
    if documents and ORIGINAL_DOCUMENT_WARNING in warnings:
        warnings = [warning for warning in warnings if warning != ORIGINAL_DOCUMENT_WARNING]
        warnings.append("Structured-data snapshots retain their original metadata-only provenance. Downloaded original filings are limited to the separately listed document versions; other filings were not supplied.")
    return normalize_warnings(
        warnings, companies=artifact_companies(artifacts), include_diagnostics=include_diagnostics,
    )
