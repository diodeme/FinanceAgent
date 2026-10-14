"""Deterministic official-data collection and conservative fiscal-quarter selection."""
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Awaitable, Callable

from .calculations import cell_id, decimal_text, evaluate_cell
from .contracts import Cell, CompanyInfo, Evidence, FetchRequest, FinanceError, FinancialRow, TableData
from .registry import COMPANIES, METRICS, concepts_for
from .sec_client import CONTACT_WARNING, DEFAULT_USER_AGENT, SECClient, SecPayload
from .warnings import ORIGINAL_DOCUMENT_WARNING, normalize_warnings


@dataclass(frozen=True)
class Observation:
    concept: str
    start: date | None
    end: date
    value: str
    unit: str
    accession: str
    form: str
    filed: date

    @property
    def days(self):
        return (self.end - self.start).days + 1 if self.start else 0


def _normalize(query: str) -> str:
    return " ".join(query.strip().casefold().split())


def _candidates(query: str) -> list[CompanyInfo]:
    normalized = _normalize(query)
    if not normalized:
        raise FinanceError("invalid_company", "Provide a company name or ticker.")
    exact = [company for company in COMPANIES.values() if normalized in {_normalize(alias) for alias in [company.company, company.name, *company.aliases]}]
    if exact:
        return exact
    if len(normalized) < 3:
        return []
    return [company for company in COMPANIES.values() if any(normalized in _normalize(alias) for alias in [company.name, *company.aliases])]


async def _verify(company: CompanyInfo, client: SECClient, refresh: bool = False) -> CompanyInfo:
    payload = await client.submissions(company.cik, refresh=refresh)
    data = payload.data
    try:
        cik_matches = int(data.get("cik", -1)) == int(company.cik)
    except (TypeError, ValueError):
        cik_matches = False
    tickers = {str(ticker).upper() for ticker in data.get("tickers", [])}
    allowed_tickers = {company.company, *[alias for alias in company.aliases if alias.isascii() and alias.isupper()]}
    if not cik_matches or not tickers.intersection(allowed_tickers) or not isinstance(data.get("name"), str) or not data["name"].strip():
        raise FinanceError("company_verification_failed", f"Official SEC submissions did not verify the configured identity for {company.company}.")
    return company.model_copy(update={"name": data["name"], "verified": True, "warning": CONTACT_WARNING if client.user_agent == DEFAULT_USER_AGENT else None})


async def lookup_company(query: str) -> list[CompanyInfo]:
    """Only locally configured candidates are verified; no ID invention or search fallback."""
    candidates = _candidates(query)
    if not candidates:
        raise FinanceError("unsupported_company", "No supported local identity matches this query. Coverage is AAPL, MSFT, NVDA, AMZN, GOOGL (GOOG alias), META; general company search is unavailable.")
    async with SECClient() as client:
        return [await _verify(company, client) for company in candidates]


def _observations(payload: SecPayload, as_of: date | None) -> dict[str, list[Observation]]:
    cutoff = as_of or date.today()
    result = defaultdict(list)
    # SEC companyfacts' standard-taxonomy entries are entity-wide facts. We do not
    # infer segment facts, custom extensions, EPS or share-weighted additive flows.
    facts = payload.data.get("facts", {}).get("us-gaap", {})
    needed = {concept for spec in METRICS.values() for concept in spec.concepts}
    for concept in needed:
        seen = set()
        for unit, values in facts.get(concept, {}).get("units", {}).items():
            for item in values:
                if item.get("form") not in {"10-K", "10-Q", "10-K/A", "10-Q/A"} or item.get("segment") or item.get("dimensions"):
                    continue
                try:
                    end = date.fromisoformat(item["end"])
                    filed = date.fromisoformat(item["filed"])
                    start = date.fromisoformat(item["start"]) if item.get("start") else None
                    value = Decimal(str(item["val"]))
                    if not value.is_finite() or abs(value) > Decimal("1e40") or filed > cutoff or end > cutoff or (start and start > end):
                        continue
                    accession = str(item["accn"])
                    if not accession or len(accession) > 40:
                        continue
                    observation = Observation(concept, start, end, decimal_text(value), unit, accession, item["form"], filed)
                except (KeyError, ValueError, TypeError, InvalidOperation):
                    continue
                if observation not in seen:
                    seen.add(observation)
                    result[concept].append(observation)
    return dict(result)


def _periods(observations: dict[str, list[Observation]]) -> list[tuple[date, date]]:
    """Find actual fiscal boundaries, including 52/53-week years; ignore fy/fp/frame."""
    anchors = ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet", "RevenueFromContractWithCustomerIncludingAssessedTax", "NetIncomeLoss", "ProfitLoss", "NetCashProvidedByUsedInOperatingActivities"]
    direct = defaultdict(Counter)
    annual = set()
    cumulative_ends = defaultdict(set)
    for concept in anchors:
        # Repeated comparative filings must not outvote a true boundary.
        windows = {(obs.start, obs.end) for obs in observations.get(concept, []) if obs.start and obs.unit == "USD"}
        for start, end in windows:
            days = (end - start).days + 1
            if 70 <= days <= 105:
                direct[end][start] += 1
            if 350 <= days <= 380:
                annual.add((start, end))
            if 70 <= days <= 295:
                cumulative_ends[start].add(end)
    periods = {}
    for end, counts in direct.items():
        highest = counts.most_common()
        if len(highest) == 1 or highest[0][1] > highest[1][1]:
            periods[end] = highest[0][0]
    # YTD endpoints expose real quarter boundaries even without direct cash-flow facts.
    for fiscal_start, ends in cumulative_ends.items():
        previous = fiscal_start - timedelta(days=1)
        for end in sorted(ends):
            if 70 <= (end - previous).days <= 105:
                periods.setdefault(end, previous + timedelta(days=1))
            previous = end
    for start, end in annual:
        possible = [prior for prior in periods if start <= prior < end and 70 <= (end - prior).days <= 105]
        if len(possible) == 1:
            periods.setdefault(end, possible[0] + timedelta(days=1))
    return sorted(((start, end) for end, start in periods.items()), key=lambda pair: pair[1])


def _latest(observations: list[Observation]) -> tuple[Observation | None, str | None]:
    if not observations:
        return None, "No matching observation"
    latest_date = max(obs.filed for obs in observations)
    latest = [obs for obs in observations if obs.filed == latest_date]
    amendments = [obs for obs in latest if obs.form.endswith("/A")]
    if amendments:
        latest = amendments
    if len({(obs.value, obs.unit, obs.start, obs.end) for obs in latest}) != 1:
        return None, "Conflicting values in the latest filing vintage"
    return max(latest, key=lambda obs: obs.accession), None


def _evidence(company: str, metric: str, observation: Observation, payload: SecPayload) -> Evidence:
    identity = {"company": company, "concept": observation.concept, "start": str(observation.start), "end": str(observation.end), "value": observation.value, "unit": observation.unit, "accession": observation.accession, "filed": str(observation.filed)}
    sid = "sec_" + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:32]
    cik = str(int(COMPANIES[company].cik))
    accession_compact = observation.accession.replace("-", "")
    return Evidence(
        source_id=sid, company=company, concept="us-gaap:" + observation.concept, metric=metric,
        period_start=observation.start, period_end=observation.end, value=observation.value,
        unit=observation.unit, accession=observation.accession, form=observation.form,
        filed_at=observation.filed, retrieved_at=payload.retrieved_at, api_url=payload.url,
        filing_url=f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession_compact}/{observation.accession}-index.html",
        document_status="metadata_only", note="Official companyfacts evidence; filing URL is metadata only, original document was not fetched." + (" Productive-assets cash purchases include software/other intangibles as well as PP&E; not directly identical to PP&E-only capex." if observation.concept == "PaymentsToAcquireProductiveAssets" else ""),
    )


def _base_cell(company: str, metric: str, start: date, end: date, observations: dict[str, list[Observation]], payload: SecPayload, sources: dict[str, Evidence]) -> Cell:
    spec = METRICS[metric]
    cell = Cell(cell_id=cell_id(company, end, metric), metric=metric, label=spec.label, value=None, unit=spec.unit, status="missing", reason="No compatible standard US-GAAP observation for this fiscal quarter")
    reasons = []
    for concept in concepts_for(company, metric):
        all_values = observations.get(concept, [])
        values = [obs for obs in all_values if obs.unit == spec.unit]
        if any(obs.end == end and obs.unit != spec.unit for obs in all_values):
            reasons.append("Only incompatible units may be available; expected " + spec.unit)
        exact = [obs for obs in values if obs.end == end and (obs.start is None if spec.period_type == "instant" else obs.start == start)]
        if exact:
            selected, reason = _latest(exact)
            if selected is None:
                cell.status = "conflicting"
                cell.reason = reason
                for observation in exact:
                    source = _evidence(company, metric, observation, payload)
                    sources[source.source_id] = source
                    cell.source_ids.append(source.source_id)
                    cell.inputs.append({"source_id": source.source_id, "value": source.value, "unit": source.unit, "accession": source.accession, "filed_at": str(source.filed_at)})
                return cell
            source = _evidence(company, metric, selected, payload)
            sources[source.source_id] = source
            cell.value, cell.status, cell.reason = selected.value, "available", None
            cell.source_ids = [source.source_id]
            return cell
        if spec.period_type == "instant":
            continue
        # Additive flows only. No subtraction across concepts, currencies, fiscal
        # starts, unequal boundary dates or information unavailable to the target filing.
        cumulatives = [obs for obs in values if obs.end == end and obs.start and obs.start < start and 140 <= obs.days <= 380]
        for fiscal_start in sorted({obs.start for obs in cumulatives}, reverse=True):
            current_values = [obs for obs in cumulatives if obs.start == fiscal_start]
            current, reason = _latest(current_values)
            if current is None:
                reasons.append(reason)
                continue
            prior_values = [obs for obs in values if obs.start == fiscal_start and obs.end == start - timedelta(days=1)]
            comparable = [obs for obs in prior_values if obs.filed <= current.filed]
            same_filing = [obs for obs in comparable if obs.accession == current.accession]
            previous, reason = _latest(same_filing or comparable)
            if previous is None:
                reasons.append("Missing compatible earlier YTD: " + str(reason))
                continue
            revised_later = any(obs.filed > current.filed and obs.value != previous.value for obs in prior_values)
            revised_current = len({obs.value for obs in current_values}) > 1
            if revised_later or (revised_current and not same_filing and previous.filed < current.filed):
                reasons.append("Incompatible revision vintages: cumulative amounts cannot be safely differenced")
                continue
            current_source = _evidence(company, metric, current, payload)
            prior_source = _evidence(company, metric, previous, payload)
            for source in (current_source, prior_source):
                sources[source.source_id] = source
            cell.value = decimal_text(Decimal(current.value) - Decimal(previous.value))
            cell.status, cell.reason = "available", None
            cell.formula = "current_ytd - previous_ytd"
            cell.source_ids = [current_source.source_id, prior_source.source_id]
            cell.inputs = [{"source_id": source.source_id, "metric": metric, "value": source.value, "unit": source.unit, "period_start": str(source.period_start), "period_end": str(source.period_end), "accession": source.accession, "filed_at": str(source.filed_at)} for source in (current_source, prior_source)]
            return cell
    if reasons:
        cell.reason = "; ".join(dict.fromkeys(reasons))
    return cell


def _row(company: str, start: date, end: date, metrics: list[str], observations: dict[str, list[Observation]], payload: SecPayload, sources: dict[str, Evidence]) -> FinancialRow:
    cells = {}
    def resolve(metric):
        if metric in cells:
            return
        spec = METRICS[metric]
        direct = _base_cell(company, metric, start, end, observations, payload, sources) if spec.concepts else None
        if direct and direct.status == "available":
            cells[metric] = direct
            return
        if spec.formula and (not direct or direct.status != "conflicting"):
            for dependency in spec.dependencies:
                resolve(dependency)
            cells[metric] = evaluate_cell(company, end, metric, spec.label, spec.formula, cells, spec.unit)
        else:
            cells[metric] = direct
    for metric in metrics:
        resolve(metric)
    return FinancialRow(company=company, period_start=start, period_end=end, period_label=f"Quarter ended {end.isoformat()}", cells=list(cells.values()))


def build_company_table(company: str, payload: SecPayload, request: FetchRequest) -> TableData:
    """Pure selection entry point used by deterministic fixtures and the collector."""
    observations = _observations(payload, request.as_of)
    periods = _periods(observations)
    eligible = [(start, end) for start, end in periods if (not request.start_date or end >= request.start_date) and (not request.end_date or end <= request.end_date)]
    targets = eligible[-request.quarters:]
    if not targets:
        raise FinanceError("no_periods", f"No identifiable disclosed fiscal quarters for {company} in the requested window.")
    target_ends = {end for _, end in targets}
    first = next(i for i, period in enumerate(periods) if period == targets[0])
    history = [period for period in periods[max(0, first-4):] if period[1] < targets[-1][1] and period[1] not in target_ends]
    sources = {}
    rows = [_row(company, start, end, request.metrics, observations, payload, sources) for start, end in reversed(targets)]
    history_rows = [_row(company, start, end, request.metrics, observations, payload, sources) for start, end in reversed(history)]
    missing = []
    # Explicit date ranges may intentionally contain fewer than the maximum count.
    if len(targets) < request.quarters and not (request.start_date and request.end_date):
        missing.append(f"{company}: requested {request.quarters} quarters but only {len(targets)} identifiable periods are available")
    warnings = list(missing)
    if any(source.concept == "us-gaap:PaymentsToAcquireProductiveAssets" for source in sources.values()):
        warnings.append(f"{company} capital expenditures use reported productive-asset cash purchases, including software/other intangibles where combined; PP&E-only issuers are not strictly scope-identical.")
    return TableData(rows=rows, history_rows=history_rows, sources=sources, specification={"requested_metrics": request.metrics, "coverage_missing": missing}, warnings=warnings)


async def fetch_data(request: FetchRequest, progress: Callable[[str], Awaitable[None]]) -> TableData:
    if request.dimensions:
        raise FinanceError("unsupported_dimensions", "Only consolidated entity-wide facts are supported; segment, geography, product and other dimensions are unavailable.")
    unknown = [metric for metric in request.metrics if metric not in METRICS]
    if unknown:
        raise FinanceError("unsupported_metric", "Unsupported metric(s): " + ", ".join(unknown))
    candidates = []
    for query in request.companies:
        matches = _candidates(query)
        if len(matches) != 1:
            raise FinanceError("unsupported_company" if not matches else "ambiguous_company", f"Company {query!r} must resolve to one supported configured identity; use lookup_company.")
        if matches[0].company not in {company.company for company in candidates}:
            candidates.append(matches[0])
    result = TableData(rows=[], specification={**request.model_dump(mode="json"), "companies": [company.company for company in candidates], "requested_metrics": request.metrics, "coverage_missing": [], "alignment": "Actual fiscal quarter dates; no calendar alignment or annualization."}, warnings=[ORIGINAL_DOCUMENT_WARNING])
    async with SECClient() as client:
        if client.user_agent == DEFAULT_USER_AGENT:
            result.warnings.append(CONTACT_WARNING)
        for company in candidates:
            await progress(f"Verifying {company.company} against official SEC submissions…")
            await _verify(company, client, request.refresh)
            await progress(f"Loading official {company.company} companyfacts and selecting actual fiscal periods…")
            payload = await client.companyfacts(company.cik, refresh=request.refresh)
            try:
                cik_matches = int(payload.data.get("cik", -1)) == int(company.cik)
            except (ValueError, TypeError):
                cik_matches = False
            if not cik_matches:
                raise FinanceError("company_verification_failed", f"Companyfacts CIK does not match {company.company}.")
            table = build_company_table(company.company, payload, request)
            result.rows.extend(table.rows)
            result.history_rows.extend(table.history_rows)
            result.sources.update(table.sources)
            result.warnings.extend(table.warnings)
            result.specification["coverage_missing"].extend(table.specification["coverage_missing"])
            missing = sum(cell.status != "available" for row in table.rows for cell in row.cells if cell.metric in request.metrics)
            await progress(f"{company.company}: selected {len(table.rows)} fiscal quarters; {missing} requested cells unavailable. Supporting history is kept separately.")
    result.warnings = normalize_warnings(result.warnings, companies=(row.company for row in result.rows))
    return result
