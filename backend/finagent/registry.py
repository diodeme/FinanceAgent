"""Small explicit coverage registry; identities are verified at lookup/fetch time."""
from dataclasses import dataclass

from .contracts import CompanyInfo

COMPANIES: dict[str, CompanyInfo] = {
    "AAPL": CompanyInfo(company="AAPL", cik="0000320193", name="Apple Inc.", aliases=["Apple", "苹果", "苹果公司"], fy_end_month=9),
    "MSFT": CompanyInfo(company="MSFT", cik="0000789019", name="Microsoft Corporation", aliases=["Microsoft", "微软"], fy_end_month=6),
    "NVDA": CompanyInfo(company="NVDA", cik="0001045810", name="NVIDIA Corporation", aliases=["NVIDIA", "英伟达"], fy_end_month=1),
    "AMZN": CompanyInfo(company="AMZN", cik="0001018724", name="Amazon.com, Inc.", aliases=["Amazon", "亚马逊"], sector="consumer discretionary"),
    "GOOGL": CompanyInfo(company="GOOGL", cik="0001652044", name="Alphabet Inc.", aliases=["GOOG", "Alphabet", "Google", "谷歌"]),
    "META": CompanyInfo(company="META", cik="0001326801", name="Meta Platforms, Inc.", aliases=["Meta", "Facebook", "脸书"]),
}


@dataclass(frozen=True)
class Metric:
    label: str
    definition: str
    concepts: tuple[str, ...] = ()
    period_type: str = "duration"
    unit: str = "USD"
    formula: str | None = None
    dependencies: tuple[str, ...] = ()


METRICS: dict[str, Metric] = {
    "revenue": Metric("Revenue", "Consolidated US-GAAP revenue, excluding sales taxes.", ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet")),
    "cost_of_revenue": Metric("Cost of revenue", "Reported consolidated cost of revenue; company classifications can differ.", ("CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold", "CostOfSales")),
    "gross_profit": Metric("Gross profit", "Revenue less cost of revenue, or reported gross profit.", ("GrossProfit",), formula="revenue - cost_of_revenue", dependencies=("revenue", "cost_of_revenue")),
    "gross_margin": Metric("Gross margin", "Gross profit divided by revenue; a fraction, not percentage points.", unit="pure", formula="gross_profit / revenue", dependencies=("gross_profit", "revenue")),
    "operating_income": Metric("Operating income", "Consolidated US-GAAP operating income (loss).", ("OperatingIncomeLoss",)),
    "operating_margin": Metric("Operating margin", "Operating income divided by revenue; fraction.", unit="pure", formula="operating_income / revenue", dependencies=("operating_income", "revenue")),
    "net_income": Metric("Net income", "US-GAAP net income attributable to the parent, when reported.", ("NetIncomeLoss", "ProfitLoss")),
    "net_margin": Metric("Net margin", "Net income divided by revenue; fraction.", unit="pure", formula="net_income / revenue", dependencies=("net_income", "revenue")),
    "operating_cash_flow": Metric("Operating cash flow", "Net cash provided by (used in) operating activities.", ("NetCashProvidedByUsedInOperatingActivities",)),
    "capital_expenditures": Metric("Capital expenditures", "Reported cash purchases of long-lived productive assets (positive outflow); PP&E, or PP&E plus software/intangibles where reported together (NVDA/AMZN). Excludes noncash leases; issuer scopes differ.", ("PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets")),
    "free_cash_flow": Metric("Free cash flow", "Operating cash flow less cash capital expenditures; not a US-GAAP subtotal.", formula="operating_cash_flow - capital_expenditures", dependencies=("operating_cash_flow", "capital_expenditures")),
    "total_assets": Metric("Total assets", "Consolidated assets at quarter end.", ("Assets",), period_type="instant"),
    "total_liabilities": Metric("Total liabilities", "Consolidated liabilities at quarter end.", ("Liabilities",), period_type="instant"),
    "total_equity": Metric("Total equity", "Parent stockholders' equity at quarter end; excludes noncontrolling interest.", ("StockholdersEquity",), period_type="instant"),
    "cash_and_equivalents": Metric("Cash and equivalents", "Cash and cash equivalents at quarter end; excludes marketable securities and restricted cash.", ("CashAndCashEquivalentsAtCarryingValue",), period_type="instant"),
}

# Prefer each issuer's current standard tag, without using custom taxonomy guesses.
CONCEPT_PRIORITY = {
    ("AAPL", "cost_of_revenue"): ("CostOfGoodsAndServicesSold", "CostOfRevenue"),
    ("NVDA", "capital_expenditures"): ("PaymentsToAcquireProductiveAssets", "PaymentsToAcquirePropertyPlantAndEquipment"),
    ("AMZN", "capital_expenditures"): ("PaymentsToAcquireProductiveAssets", "PaymentsToAcquirePropertyPlantAndEquipment"),
    ("AMZN", "revenue"): ("RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet", "Revenues"),
    ("MSFT", "revenue"): ("RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet", "Revenues"),
}


def concepts_for(company: str, metric: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(CONCEPT_PRIORITY.get((company, metric), ()) + METRICS[metric].concepts))


def metric_prompt() -> str:
    return "Consolidated US-GAAP only; USD flows are discrete fiscal quarters, balances are quarter-end. Ratios are fractions.\n" + "\n".join(
        f"{name}: {spec.definition}" + (f" Formula: {spec.formula}; dependencies: {', '.join(spec.dependencies)}." if spec.formula else "")
        for name, spec in METRICS.items()
    ) + "\nGrowth: (current - prior) / prior; qoq needs previous quarter, yoy needs same quarter a year earlier. Missing/zero-denominator/conflicting inputs are never zero-filled."


async def seed_registry(conn) -> None:
    """Idempotently seed an asyncpg connection; no network or schema changes."""
    for company in COMPANIES.values():
        await conn.execute(
            "INSERT INTO fin.company_registry (company,cik,name_official,aliases,fy_end_month,sector) "
            "VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT (company) DO UPDATE SET "
            "cik=EXCLUDED.cik,name_official=EXCLUDED.name_official,aliases=EXCLUDED.aliases,"
            "fy_end_month=EXCLUDED.fy_end_month,sector=EXCLUDED.sector",
            company.company, company.cik, company.name, company.aliases, company.fy_end_month, company.sector,
        )
    for name, spec in METRICS.items():
        await conn.execute(
            "INSERT INTO fin.metric_registry (metric,kind,period_type,xbrl_concepts,formula,base_metrics,unit,is_core,definition) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,true,$8) ON CONFLICT (metric) DO UPDATE SET "
            "kind=EXCLUDED.kind,period_type=EXCLUDED.period_type,xbrl_concepts=EXCLUDED.xbrl_concepts,"
            "formula=EXCLUDED.formula,base_metrics=EXCLUDED.base_metrics,unit=EXCLUDED.unit,definition=EXCLUDED.definition",
            name, "derived" if spec.formula else "base", spec.period_type, list(spec.concepts),
            spec.formula, list(spec.dependencies), spec.unit, spec.definition,
        )
    await conn.execute(
        "INSERT INTO fin.source_registry (source_id,source_type,endpoint,applies_to,priority,trust_level,notes) "
        "VALUES ('sec-companyfacts','official_api','https://data.sec.gov/api/xbrl/companyfacts/',"
        "$1,1,'verified',$2) ON CONFLICT (source_id) DO UPDATE SET notes=EXCLUDED.notes,applies_to=EXCLUDED.applies_to",
        list(COMPANIES), "Official SEC structured facts. Filing links are metadata only; original documents are not fetched.",
    )
