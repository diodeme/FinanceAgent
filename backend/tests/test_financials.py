"""Synthetic SEC fixtures: no network, DB, credentials or model calls required."""
import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from finagent.calculations import calculate_data, evaluate_cell, parse_formula
from finagent.contracts import Artifact, CalculateRequest, Cell, CustomMetric, FetchRequest, FinanceError
from finagent.financials import build_company_table, fetch_data, lookup_company
from finagent.registry import COMPANIES, METRICS, seed_registry
from finagent.sec_client import CONTACT_WARNING, DEFAULT_USER_AGENT, RateGate, SECClient, SecPayload
from finagent.warnings import CROSS_COMPANY_WARNING, CROSS_COMPANY_QUARTERS_ALIAS, ORIGINAL_DOCUMENT_WARNING, normalize_warnings

NOW = datetime(2025, 3, 1, tzinfo=timezone.utc)


def fact(value, start, end, filed, accession=None, form="10-Q", **extra):
    return {"val": value, **({"start": start} if start else {}), "end": end, "filed": filed,
            "accn": accession or "0000320193-" + filed.replace("-", ""), "form": form,
            # Deliberately misleading SEC filing fiscal labels: never used for dates.
            "fy": 2099, "fp": "FY", "frame": "CY2001Q1", **extra}


def payload(concepts, units=None):
    return SecPayload({"cik": 320193, "facts": {"us-gaap": {name: {"units": {(units or {}).get(name, "USD"): values}} for name, values in concepts.items()}}},
                      "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json", NOW)


def income_2023():
    return [fact(100, "2023-01-01", "2023-03-31", "2023-05-01"),
            fact(120, "2023-04-01", "2023-06-30", "2023-08-01"),
            fact(130, "2023-07-01", "2023-09-30", "2023-11-01"),
            fact(500, "2023-01-01", "2023-12-31", "2024-02-01", form="10-K")]


def table(concepts, metrics, quarters=4, **kwargs):
    return build_company_table("AAPL", payload(concepts), FetchRequest(companies=["AAPL"], metrics=metrics, quarters=quarters, **kwargs))


def get(data, end, metric):
    return next(cell for row in data.rows + data.history_rows if str(row.period_end) == end for cell in row.cells if cell.metric == metric)


def artifact(data, name="data_a"):
    return Artifact(artifact_id=name, run_id="run_a", kind="dataset", title="Fixture", created_at=NOW, data=data)


class FinancialSelectionTests(unittest.TestCase):
    def test_discrete_income_and_q4_from_annual(self):
        values = income_2023() + [fact(220, "2023-01-01", "2023-06-30", "2023-08-01")]
        data = table({"Revenues": values}, ["revenue"])
        self.assertEqual(get(data, "2023-06-30", "revenue").value, "120")
        # Need nine-month cumulative to compute Q4, not subtraction of Q3 alone.
        self.assertIsNone(get(data, "2023-12-31", "revenue").value)
        values.append(fact(350, "2023-01-01", "2023-09-30", "2023-11-01"))
        data = table({"Revenues": values}, ["revenue"])
        q4 = get(data, "2023-12-31", "revenue")
        self.assertEqual(q4.value, "150")
        self.assertEqual(q4.formula, "current_ytd - previous_ytd")
        self.assertEqual(len(q4.source_ids), 2)
        self.assertTrue(all(source.fact_id is None and source.document_status == "metadata_only" for source in data.sources.values()))

    def test_cash_flows_cross_accession_q2_q3_q4(self):
        cfo = [fact(value, "2023-01-01", end, filed, form=form) for value, end, filed, form in
               [(20, "2023-03-31", "2023-05-01", "10-Q"), (55, "2023-06-30", "2023-08-01", "10-Q"),
                (85, "2023-09-30", "2023-11-01", "10-Q"), (140, "2023-12-31", "2024-02-01", "10-K")]]
        data = table({"Revenues": income_2023(), "NetCashProvidedByUsedInOperatingActivities": cfo}, ["operating_cash_flow"])
        self.assertEqual([get(data, end, "operating_cash_flow").value for end in ["2023-03-31", "2023-06-30", "2023-09-30", "2023-12-31"]], ["20", "35", "30", "55"])
        q2 = get(data, "2023-06-30", "operating_cash_flow")
        self.assertNotEqual(q2.inputs[0]["accession"], q2.inputs[1]["accession"])

    def test_53_week_year_preserves_dates(self):
        revenue = [fact(10, "2022-09-25", "2022-12-31", "2023-02-01"),
                   fact(20, "2023-01-01", "2023-04-01", "2023-05-01"),
                   fact(30, "2023-04-02", "2023-07-01", "2023-08-01"),
                   fact(60, "2022-09-25", "2023-07-01", "2023-08-01"),
                   fact(100, "2022-09-25", "2023-09-30", "2023-11-01", form="10-K")]
        data = table({"Revenues": revenue}, ["revenue"])
        self.assertEqual(get(data, "2023-09-30", "revenue").value, "40")
        self.assertEqual(data.rows[0].period_start, date(2023, 7, 2))
        self.assertEqual(data.rows[-1].period_start, date(2022, 9, 25))

    def test_instant_is_never_subtracted(self):
        data = table({"Revenues": income_2023(), "Assets": [fact(900, None, "2023-12-31", "2024-02-01", form="10-K"), fact(700, None, "2023-09-30", "2023-11-01")]}, ["total_assets"])
        cell = get(data, "2023-12-31", "total_assets")
        self.assertEqual(cell.value, "900")
        self.assertIsNone(cell.formula)
        self.assertIsNone(get(data, "2023-03-31", "total_assets").value)

    def test_asof_and_latest_known(self):
        facts = income_2023() + [fact(125, "2023-04-01", "2023-06-30", "2024-08-01")]
        old = table({"Revenues": facts}, ["revenue"], quarters=2, as_of=date(2023, 8, 2))
        latest = table({"Revenues": facts}, ["revenue"])
        self.assertEqual(get(old, "2023-06-30", "revenue").value, "120")
        self.assertEqual(get(latest, "2023-06-30", "revenue").value, "125")
        self.assertNotEqual(get(old, "2023-06-30", "revenue").source_ids, get(latest, "2023-06-30", "revenue").source_ids)

    def test_incompatible_revision_is_missing_not_blind_latest(self):
        cfo = [fact(20, "2023-01-01", "2023-03-31", "2023-05-01"),
               fact(55, "2023-01-01", "2023-06-30", "2023-08-01"),
               fact(25, "2023-01-01", "2023-03-31", "2024-05-01")]
        data = table({"Revenues": income_2023(), "NetCashProvidedByUsedInOperatingActivities": cfo}, ["operating_cash_flow"])
        self.assertIsNone(get(data, "2023-06-30", "operating_cash_flow").value)
        self.assertIn("revision", get(data, "2023-06-30", "operating_cash_flow").reason)
        old = table({"Revenues": income_2023(), "NetCashProvidedByUsedInOperatingActivities": cfo}, ["operating_cash_flow"], quarters=2, as_of=date(2023, 8, 2))
        self.assertEqual(get(old, "2023-06-30", "operating_cash_flow").value, "35")

    def test_revised_target_requires_comparable_prior(self):
        cfo = [fact(20, "2023-01-01", "2023-03-31", "2023-05-01"), fact(55, "2023-01-01", "2023-06-30", "2023-08-01"), fact(65, "2023-01-01", "2023-06-30", "2024-08-01")]
        data = table({"Revenues": income_2023(), "NetCashProvidedByUsedInOperatingActivities": cfo}, ["operating_cash_flow"])
        self.assertIsNone(get(data, "2023-06-30", "operating_cash_flow").value)
        cfo.append(fact(25, "2023-01-01", "2023-03-31", "2024-08-01"))
        data = table({"Revenues": income_2023(), "NetCashProvidedByUsedInOperatingActivities": cfo}, ["operating_cash_flow"])
        self.assertEqual(get(data, "2023-06-30", "operating_cash_flow").value, "40")

    def test_cumulative_requires_same_fiscal_start_and_unit(self):
        cfo = [fact(20, "2023-01-02", "2023-03-31", "2023-05-01"), fact(55, "2023-01-01", "2023-06-30", "2023-08-01")]
        data = table({"Revenues": income_2023(), "NetCashProvidedByUsedInOperatingActivities": cfo}, ["operating_cash_flow"])
        self.assertIsNone(get(data, "2023-06-30", "operating_cash_flow").value)

    def test_gross_profit_fallback_and_lineage(self):
        data = table({"Revenues": income_2023(), "CostOfRevenue": [fact(70, "2023-04-01", "2023-06-30", "2023-08-01")]}, ["gross_margin"], quarters=1, end_date=date(2023, 6, 30))
        gross = get(data, "2023-06-30", "gross_profit")
        margin = get(data, "2023-06-30", "gross_margin")
        self.assertEqual(gross.value, "50")
        self.assertTrue(margin.value.startswith("0.416666"))
        self.assertEqual(margin.unit, "pure")
        self.assertEqual(set(margin.source_ids), set(gross.source_ids))
        self.assertEqual(data.specification["requested_metrics"], ["gross_margin"])

    def test_reported_gross_profit_needs_no_missing_fallback(self):
        data = table({"Revenues": income_2023(), "GrossProfit": [fact(40, "2023-04-01", "2023-06-30", "2023-08-01")]}, ["gross_profit"], quarters=1, end_date=date(2023, 6, 30))
        self.assertEqual(len(data.rows[0].cells), 1)
        self.assertEqual(data.rows[0].cells[0].value, "40")

    def test_conflicting_fact_and_missing_concept(self):
        values = income_2023() + [fact(121, "2023-04-01", "2023-06-30", "2023-08-01")]
        data = table({"Revenues": values}, ["revenue", "net_income"])
        self.assertEqual(get(data, "2023-06-30", "revenue").status, "conflicting")
        self.assertIsNone(get(data, "2023-06-30", "net_income").value)

    def test_incompatible_units_are_missing(self):
        source = payload({"Revenues": income_2023(), "Assets": [fact(99, None, "2023-12-31", "2024-02-01")]}, units={"Assets": "EUR"})
        data = build_company_table("AAPL", source, FetchRequest(companies=["AAPL"], metrics=["total_assets"], quarters=1))
        cell = get(data, "2023-12-31", "total_assets")
        self.assertIsNone(cell.value)
        self.assertIn("units", cell.reason)

    def test_productive_assets_scope_is_disclosed(self):
        source = payload({"Revenues": income_2023(), "PaymentsToAcquireProductiveAssets": [fact(20, "2023-01-01", "2023-03-31", "2023-05-01")]})
        data = build_company_table("NVDA", source, FetchRequest(companies=["NVDA"], metrics=["capital_expenditures"], quarters=1, end_date=date(2023, 3, 31)))
        self.assertEqual(get(data, "2023-03-31", "capital_expenditures").value, "20")
        self.assertTrue(any("intangibles" in warning for warning in data.warnings))
        self.assertTrue(any("intangibles" in evidence.note for evidence in data.sources.values()))

    def test_source_ids_deterministic(self):
        one = table({"Revenues": income_2023()}, ["revenue"])
        two = table({"Revenues": income_2023()}, ["revenue"])
        self.assertEqual(set(one.sources), set(two.sources))
        self.assertTrue(all(source.metric in METRICS for source in one.sources.values()))

    def test_history_not_coverage_target(self):
        data = table({"Revenues": income_2023()}, ["revenue"], quarters=1, end_date=date(2023, 9, 30))
        self.assertEqual(len(data.rows), 1)
        self.assertEqual(len(data.history_rows), 2)
        self.assertFalse(data.specification["coverage_missing"])

    def test_fewer_quarters_has_explicit_coverage_gap(self):
        data = table({"Revenues": income_2023()[:1]}, ["revenue"])
        self.assertTrue(data.specification["coverage_missing"])


class CalculationTests(unittest.TestCase):
    def test_safe_custom_ratio_preserves_sources(self):
        data = table({"Revenues": income_2023(), "NetCashProvidedByUsedInOperatingActivities": [fact(60, "2023-01-01", "2023-03-31", "2023-05-01")]}, ["revenue", "operating_cash_flow"], quarters=1, end_date=date(2023, 3, 31))
        request = CalculateRequest(data_ids=["data_a"], custom_metrics=[CustomMetric(name="cash_per_revenue", label="Cash per revenue", formula="operating_cash_flow / revenue")])
        result = calculate_data([artifact(data)], request)
        cell = get(result, "2023-03-31", "cash_per_revenue")
        self.assertEqual(cell.value, "0.6")
        self.assertEqual(cell.unit, "pure")
        self.assertEqual(len(cell.source_ids), 2)
        self.assertEqual(set(result.sources), set(data.sources))
        self.assertEqual(len(data.rows[0].cells), 2)  # input was not mutated

    def test_rejects_unsafe_and_unbounded_formulas(self):
        for formula in ["__import__('os')", "revenue.real", "revenue[0]", "2 ** 99999", "sum(revenue)", "[revenue]", "True + 1", "'x'", "+" * 15 + "revenue", "+".join(["revenue"] * 30)]:
            with self.subTest(formula=formula), self.assertRaises(FinanceError):
                parse_formula(formula)

    def test_zero_denominator_and_unit_mismatch(self):
        def cell(name, value, unit):
            return Cell(cell_id=name, metric=name, label=name, value=value, unit=unit)
        cells = {"revenue": cell("revenue", "0", "USD"), "cash": cell("cash", "1", "USD"), "ratio": cell("ratio", "0.5", "pure")}
        zero = evaluate_cell("AAPL", date(2023, 3, 31), "test", "test", "cash / revenue", cells)
        self.assertIsNone(zero.value)
        self.assertEqual(zero.reason, "Zero denominator")
        wrong = evaluate_cell("AAPL", date(2023, 3, 31), "test", "test", "cash + ratio", cells)
        self.assertIsNone(wrong.value)
        self.assertIn("units", wrong.reason)

    def test_duplicate_value_or_vintage_conflict(self):
        first = table({"Revenues": income_2023()}, ["revenue"], quarters=1, end_date=date(2023, 6, 30))
        second = table({"Revenues": income_2023() + [fact(120, "2023-04-01", "2023-06-30", "2024-08-01")]}, ["revenue"], quarters=1, end_date=date(2023, 6, 30))
        result = calculate_data([artifact(first), artifact(second, "data_b")], CalculateRequest(data_ids=["data_a", "data_b"], metrics=["revenue"]))
        self.assertEqual(get(result, "2023-06-30", "revenue").status, "conflicting")
        self.assertIsNone(get(result, "2023-06-30", "revenue").value)

    def test_merge_datasets_to_compute_builtin(self):
        one = table({"Revenues": income_2023()}, ["revenue"], quarters=1, end_date=date(2023, 6, 30))
        two = table({"Revenues": income_2023(), "OperatingIncomeLoss": [fact(30, "2023-04-01", "2023-06-30", "2023-08-01")]}, ["operating_income"], quarters=1, end_date=date(2023, 6, 30))
        result = calculate_data([artifact(one), artifact(two, "data_b")], CalculateRequest(data_ids=["data_a", "data_b"], metrics=["operating_margin"]))
        self.assertEqual(get(result, "2023-06-30", "operating_margin").value, "0.25")

    def test_yoy_uses_history_dates_and_missing_prior_is_missing(self):
        revenue = income_2023() + [fact(125, "2024-01-01", "2024-03-31", "2024-05-01")]
        data = table({"Revenues": revenue}, ["revenue"], quarters=1)
        result = calculate_data([artifact(data)], CalculateRequest(data_ids=["data_a"], metrics=["revenue"], growth="yoy"))
        self.assertEqual(get(result, "2024-03-31", "revenue_yoy").value, "0.25")
        self.assertIn("revenue_yoy", result.specification["requested_metrics"])
        self.assertEqual(len(result.rows), 1)
        data.history_rows = []
        missing = calculate_data([artifact(data)], CalculateRequest(data_ids=["data_a"], metrics=["revenue"], growth="yoy"))
        self.assertIsNone(get(missing, "2024-03-31", "revenue_yoy").value)

    def test_qoq_does_not_skip_missing_quarter(self):
        data = table({"Revenues": [income_2023()[0], income_2023()[2]]}, ["revenue"], quarters=1)
        result = calculate_data([artifact(data)], CalculateRequest(data_ids=["data_a"], metrics=["revenue"], growth="qoq"))
        self.assertIsNone(get(result, "2023-09-30", "revenue_qoq").value)

    def test_followup_fetch_can_fill_unobserved_dependency(self):
        missing = table({"Revenues": income_2023()}, ["gross_margin"], quarters=1, end_date=date(2023, 6, 30))
        added = table({"Revenues": income_2023(), "CostOfRevenue": [fact(60, "2023-04-01", "2023-06-30", "2023-08-01")]}, ["cost_of_revenue"], quarters=1, end_date=date(2023, 6, 30))
        result = calculate_data([artifact(missing), artifact(added, "data_b")], CalculateRequest(data_ids=["data_a", "data_b"], metrics=["gross_margin"]))
        self.assertEqual(get(result, "2023-06-30", "gross_margin").value, "0.5")

    def test_copied_ratio_cannot_hide_conflicting_merged_input(self):
        full = table({"Revenues": income_2023(), "OperatingIncomeLoss": [fact(30, "2023-04-01", "2023-06-30", "2023-08-01")]}, ["operating_margin"], quarters=1, end_date=date(2023, 6, 30))
        revision = table({"Revenues": income_2023() + [fact(125, "2023-04-01", "2023-06-30", "2024-08-01")]}, ["revenue"], quarters=1, end_date=date(2023, 6, 30))
        result = calculate_data([artifact(full), artifact(revision, "data_b")], CalculateRequest(data_ids=["data_a", "data_b"], metrics=["operating_margin"]))
        self.assertEqual(get(result, "2023-06-30", "operating_margin").status, "conflicting")

    def test_disjoint_metrics_with_different_periods_conflict(self):
        one = table({"Revenues": income_2023()}, ["revenue"], quarters=1)
        two = one.model_copy(deep=True)
        two.rows[0].period_start = date(2023, 10, 2)
        two.rows[0].cells = []
        with self.assertRaises(FinanceError) as caught:
            calculate_data([artifact(one), artifact(two, "data_b")], CalculateRequest(data_ids=["data_a", "data_b"], metrics=["revenue"]))
        self.assertEqual(caught.exception.code, "period_conflict")

    def test_cyclic_custom_definitions_fail(self):
        data = table({"Revenues": income_2023()}, ["revenue"], quarters=1)
        request = CalculateRequest(data_ids=["data_a"], custom_metrics=[CustomMetric(name="aa", label="a", formula="bb + revenue"), CustomMetric(name="bb", label="b", formula="aa - revenue")])
        with self.assertRaises(FinanceError):
            calculate_data([artifact(data)], request)


class WarningTests(unittest.TestCase):
    def test_only_exact_aliases_and_diagnostics_are_special(self):
        unknown = ["Unknown warning", " Unknown warning ", "Capex scope differs", "Missing revenue", "Conflicting input values"]
        raw = [CROSS_COMPANY_QUARTERS_ALIAS, CROSS_COMPANY_WARNING, CONTACT_WARNING,
               ORIGINAL_DOCUMENT_WARNING, *unknown, "Unknown warning"]
        before = raw.copy()
        normalized = normalize_warnings(raw, companies=["AAPL", "MSFT"], include_diagnostics=False)
        self.assertEqual(normalized, [CROSS_COMPANY_WARNING, ORIGINAL_DOCUMENT_WARNING, *unknown])
        self.assertEqual(raw, before)
        self.assertNotIn(CROSS_COMPANY_WARNING, normalize_warnings(raw, companies=["AAPL", "AAPL"]))
        self.assertIn(CONTACT_WARNING, normalize_warnings(raw, companies=["AAPL"]))

    def test_calculation_combines_unique_companies_and_both_warning_layers(self):
        data = table({"Revenues": income_2023()}, ["revenue"])
        first = artifact(data)
        first.warnings = ["Artifact-only limitation", CONTACT_WARNING, CROSS_COMPANY_QUARTERS_ALIAS]
        first.data.warnings = ["Table-only limitation", CROSS_COMPANY_WARNING]
        before = first.model_dump(mode="json")
        request = CalculateRequest(data_ids=["data_a"], metrics=["revenue"])
        result = calculate_data([first, first], request)
        self.assertNotIn(CROSS_COMPANY_WARNING, result.warnings)
        self.assertIn(CONTACT_WARNING, result.warnings)
        self.assertIn("Artifact-only limitation", result.warnings)
        self.assertIn("Table-only limitation", result.warnings)
        self.assertEqual(first.model_dump(mode="json"), before)
        other = artifact(data.model_copy(deep=True), "data_b")
        other.data.sources = {}  # Numeric passthrough; no conflicting synthetic source IDs.
        for row in other.data.rows + other.data.history_rows:
            row.company = "MSFT"
        result = calculate_data([first, other], CalculateRequest(data_ids=["data_a", "data_b"], metrics=["revenue"]))
        self.assertEqual(result.warnings.count(CROSS_COMPANY_WARNING), 1)
        self.assertNotIn(CROSS_COMPANY_QUARTERS_ALIAS, result.warnings)


class SECClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_warnings_follow_actual_selected_company_rows(self):
        class FakeClient:
            user_agent = DEFAULT_USER_AGENT
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def companyfacts(self, cik, **kwargs):
                value = payload({"Revenues": income_2023()})
                value.data["cik"] = int(cik)
                return value
        with patch("finagent.financials.SECClient", FakeClient), patch("finagent.financials._verify", new=AsyncMock()):
            for companies in (["AAPL"], ["AAPL", "AAPL"], ["AAPL", "MSFT"]):
                result = await fetch_data(FetchRequest(companies=companies, metrics=["revenue"]), AsyncMock())
                self.assertEqual(CROSS_COMPANY_WARNING in result.warnings, len(set(companies)) > 1)
                self.assertIn(ORIGINAL_DOCUMENT_WARNING, result.warnings)
                self.assertIn(CONTACT_WARNING, result.warnings)
                self.assertEqual(len(result.warnings), len(set(result.warnings)))


    async def test_http_cache_reuse_and_immutable_payload(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"cik": 320193, "name": "Apple Inc."})
        with tempfile.TemporaryDirectory() as directory:
            async with SECClient(cache_dir=Path(directory), transport=httpx.MockTransport(handler), rate_gate=RateGate(0)) as client:
                one = await client.submissions("0000320193")
                two = await client.submissions("0000320193")
                self.assertEqual(len(calls), 1)
                self.assertFalse(one.cached)
                self.assertTrue(two.cached)
                self.assertEqual(one.retrieved_at, two.retrieved_at)
                self.assertTrue(calls[0].headers["User-Agent"].startswith("FinanceAgent"))
                await client.submissions("0000320193", refresh=True)
                self.assertEqual(len(calls), 2)
                self.assertEqual(len(list(Path(directory).glob("*.json"))), 1)

    async def test_403_never_retries(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(403)
        with tempfile.TemporaryDirectory() as directory:
            async with SECClient(cache_dir=Path(directory), transport=httpx.MockTransport(handler), rate_gate=RateGate(0)) as client:
                with self.assertRaises(FinanceError) as caught:
                    await client.companyfacts("0000320193")
                self.assertEqual(caught.exception.code, "sec_forbidden")
                self.assertEqual(len(calls), 1)

    async def test_429_retry_after_and_bounded_transient(self):
        statuses = iter([429, 503, 200])
        def handler(request):
            return httpx.Response(next(statuses), headers={"Retry-After": "0"}, json={"cik": 320193})
        with tempfile.TemporaryDirectory() as directory:
            async with SECClient(cache_dir=Path(directory), transport=httpx.MockTransport(handler), rate_gate=RateGate(0)) as client:
                result = await client.companyfacts("0000320193")
                self.assertEqual(result.data["cik"], 320193)
        with tempfile.TemporaryDirectory() as directory:
            async with SECClient(cache_dir=Path(directory), transport=httpx.MockTransport(lambda request: httpx.Response(429, headers={"Retry-After": "60"})), rate_gate=RateGate(0)) as client:
                with self.assertRaises(FinanceError) as caught:
                    await client.companyfacts("0000320193")
                self.assertEqual(caught.exception.code, "sec_rate_limited")

    async def test_shared_rate_gate(self):
        times = []
        gate = RateGate(0.025)
        async def request():
            await gate.wait()
            times.append(asyncio.get_running_loop().time())
        await asyncio.gather(*(request() for _ in range(4)))
        self.assertTrue(all(second-first >= 0.020 for first, second in zip(times, times[1:])))

    async def test_lookup_verifies_cik_and_alias(self):
        class FakeClient:
            user_agent = "FinanceAgent/0.1"
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def submissions(self, cik, **kwargs):
                return SecPayload({"cik": 1652044, "name": "Alphabet Inc.", "tickers": ["GOOG", "GOOGL"]}, "url", NOW)
        with patch("finagent.financials.SECClient", FakeClient):
            results = await lookup_company("GOOG")
            self.assertEqual(results[0].company, "GOOGL")
            self.assertTrue(results[0].verified)
            with self.assertRaises(FinanceError):
                await lookup_company("MSFT")
            with self.assertRaises(FinanceError):
                await lookup_company("Tesla")

    async def test_unsupported_dimensions_and_metrics_no_network(self):
        for kwargs in [{"dimensions": {"segment": "cloud"}}, {"metrics": ["earnings_per_share"]}]:
            with self.assertRaises(FinanceError):
                await fetch_data(FetchRequest(companies=["AAPL"], **kwargs), AsyncMock())

    async def test_registry_seed_is_idempotent_upsert(self):
        connection = AsyncMock()
        await seed_registry(connection)
        self.assertEqual(connection.execute.await_count, len(COMPANIES) + len(METRICS) + 1)
        self.assertTrue(all("ON CONFLICT" in call.args[0] for call in connection.execute.await_args_list))


if __name__ == "__main__":
    unittest.main()
