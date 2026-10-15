import fs from 'node:fs';
import path from 'node:path';

const [applePath, microsoftPath, outputPath] = process.argv.slice(2);
if (!applePath || !microsoftPath || !outputPath) throw new Error('Usage: node build-golden.mjs APPLE_JSON MICROSOFT_JSON OUTPUT_JSON');
const apple = JSON.parse(fs.readFileSync(applePath, 'utf8'));
const microsoft = JSON.parse(fs.readFileSync(microsoftPath, 'utf8'));
const monetary = ['revenue', 'net_income', 'operating_cash_flow', 'capital_expenditures', 'free_cash_flow'];
function values(row) {
  const v = row.values || row;
  return {
    revenue: v.revenue,
    net_income: v.net_income,
    operating_cash_flow: v.operating_cash_flow,
    capital_expenditures: v.capex,
    free_cash_flow: v.free_cash_flow,
    net_margin: v.net_income / v.revenue * 100,
    free_cash_flow_to_net_income: v.free_cash_flow / v.net_income,
  };
}
function portable(value) {
  if (Array.isArray(value)) return value.map(portable);
  if (!value || typeof value !== 'object') return value;
  const out = {};
  for (const [key, item] of Object.entries(value)) {
    if (['local_file', 'original_path', 'text_extract', 'extracted_text_path', 'temporary_root', 'pdf_text_coordinates', 'cell_html_attributes', 'stderr', 'pdf_extract_json', 'pdf_extract_text'].includes(key)) continue;
    if (typeof item === 'string' && /^[A-Z]:[\\/]/i.test(item)) continue;
    out[key] = portable(item);
  }
  return out;
}
const quarters = [apple, microsoft].flatMap((dataset) => dataset.quarters.map((row) => ({
  company: row.ticker, fiscal_year: row.fiscal_year, fiscal_quarter: row.fiscal_quarter,
  period_start: row.period_start, period_end: row.period_end, ...values(row),
  evidence: portable(row.metric_evidence || row.provenance),
})));
const annual = [
  ...apple.annual_reconciliations.map((row) => ({ company: 'AAPL', fiscal_year: row.fiscal_year, ...values(row), evidence: portable(row.evidence) })),
  ...microsoft.annual.map((row) => ({ company: 'MSFT', fiscal_year: row.fiscal_year, ...values(row), evidence: portable(row.provenance) })),
];
for (const row of [...quarters, ...annual]) {
  for (const metric of monetary) if (!Number.isFinite(row[metric])) throw new Error(`Missing gold ${row.company} ${row.fiscal_year} ${metric}`);
  if (row.operating_cash_flow - row.capital_expenditures !== row.free_cash_flow) throw new Error('FCF identity failed');
}
for (const row of annual) {
  const selected = quarters.filter((q) => q.company === row.company && q.fiscal_year === row.fiscal_year);
  if (selected.length !== 4) throw new Error('Expected four fiscal quarters');
  for (const metric of monetary) if (selected.reduce((sum, q) => sum + q[metric], 0) !== row[metric]) throw new Error(`Annual reconciliation failed: ${row.company} ${row.fiscal_year} ${metric}`);
}
const result = {
  version: 1, unit: 'USD million', accounting_basis: 'GAAP',
  independence: 'Verified from 16 original official quarterly financial statements, not from application data, companyfacts, or tested answers. Original-source comparative columns and annual totals cross-checked.',
  limitations: 'Official earnings-release statements are labelled unaudited; no separate SEC filing audit. FCF is the requested derived measure. This gold covers only AAPL/MSFT FY2024/FY2025.',
  quarters, annual,
  analysis_facts: { AAPL: portable(apple.analysis_facts), MSFT: portable(microsoft.analysis_facts) },
  sources: { AAPL: portable(apple.sources), MSFT: portable(microsoft.sources) },
  source_validation: { AAPL: portable(apple.validation), MSFT: portable(microsoft.validation) },
};
fs.mkdirSync(path.dirname(path.resolve(outputPath)), { recursive: true });
fs.writeFileSync(outputPath, JSON.stringify(result, null, 2));
console.log(JSON.stringify({ quarters: quarters.length, annual: annual.length, reconciled: true, output: path.resolve(outputPath) }));
