import fs from 'node:fs';
import { pathToFileURL } from 'node:url';
import { parseArgs } from 'node:util';

export function cellKey(cell) {
  return [cell.company, cell.fiscal_year, cell.fiscal_quarter ?? 'FY', cell.metric].join(':');
}

export function expectedCells(testCase, golden) {
  const rows = testCase.period_type === 'year' ? golden.annual : golden.quarters;
  const selected = rows.filter((row) => !testCase.periods || testCase.periods.some((p) => p.company === row.company && p.fiscal_year === row.fiscal_year && (p.fiscal_quarter ?? null) === (row.fiscal_quarter ?? null)));
  return selected.flatMap((row) => testCase.metrics.map((metric) => ({
    company: row.company, fiscal_year: row.fiscal_year,
    ...(row.fiscal_quarter ? { fiscal_quarter: row.fiscal_quarter, period_end: row.period_end } : {}),
    metric, value: row[metric],
  })));
}

export function scoreAnswers(testCase, golden, answers, rubric) {
  const expected = expectedCells(testCase, golden);
  if (expected.length !== testCase.required_numeric_cells) throw new Error('Case required-cell count does not match gold');
  const groups = new Map();
  for (const cell of answers.cells || []) {
    if (!groups.has(cellKey(cell))) groups.set(cellKey(cell), []);
    groups.get(cellKey(cell)).push(cell);
  }
  const details = expected.map((gold) => {
    const candidates = (groups.get(cellKey(gold)) || []).filter((cell) => typeof cell.value === 'number' && Number.isFinite(cell.value));
    const tolerance = gold.metric === 'net_margin' ? rubric.numeric.absolute_tolerance_percentage_points
      : gold.metric === 'free_cash_flow_to_net_income' ? rubric.numeric.absolute_tolerance_ratio
        : rubric.numeric.absolute_tolerance_usd_millions;
    const answered = candidates.length > 0;
    const conflict = candidates.some((cell) => Math.abs(cell.value - candidates[0].value) > tolerance);
    const badPeriod = candidates.some((cell) => gold.period_end && cell.period_end && cell.period_end !== gold.period_end);
    const valuesCorrect = answered && candidates.every((cell) => Math.abs(cell.value - gold.value) <= tolerance);
    const evidencePresent = answered && candidates.every((cell) => typeof cell.evidence === 'string' && cell.evidence.trim());
    const unitConflict = candidates.some((cell) => cell.unit_conflict === true);
    const correct = valuesCorrect && !conflict && !badPeriod && !unitConflict;
    return {
      key: cellKey(gold), expected: gold.value, observed: candidates.map((cell) => cell.value),
      answered, correct, value_matches: Boolean(valuesCorrect), conflict, period_mismatch: badPeriod,
      unit_conflict: unitConflict, extraction_evidence_present: Boolean(evidencePresent),
      source_assessed: answered && candidates.every((cell) => typeof cell.provenance_supported === 'boolean'),
      source_supported: answered && candidates.every((cell) => cell.provenance_supported === true),
      evidence: candidates.map((cell) => cell.evidence || null),
    };
  });
  const answered = details.filter((cell) => cell.answered).length;
  const correct = details.filter((cell) => cell.correct).length;
  const dates = new Map();
  for (const gold of expected.filter((cell) => cell.period_end)) {
    const key = [gold.company, gold.fiscal_year, gold.fiscal_quarter].join(':');
    if (dates.has(key)) continue;
    const observed = (answers.cells || []).filter((cell) => cell.company === gold.company && cell.fiscal_year === gold.fiscal_year && cell.fiscal_quarter === gold.fiscal_quarter && cell.period_end).map((cell) => cell.period_end);
    dates.set(key, { key, expected: gold.period_end, observed: [...new Set(observed)], correct: observed.length > 0 && observed.every((value) => value === gold.period_end) });
  }
  return {
    case_id: testCase.id, required: expected.length, answered, correct,
    accuracy: answered ? correct / answered : null,
    coverage: answered / expected.length,
    correct_coverage: correct / expected.length,
    provenance_assessed: details.filter((cell) => cell.source_assessed).length,
    provenance_supported: details.filter((cell) => cell.source_supported).length,
    verified_correct_coverage: details.filter((cell) => cell.correct && cell.source_supported).length / expected.length,
    missing_extraction_evidence: details.filter((cell) => cell.answered && !cell.extraction_evidence_present).length,
    date_checks: [...dates.values()], details,
  };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const { values: args } = parseArgs({ options: {
    cases: { type: 'string', default: 'eval/cases.json' }, golden: { type: 'string', default: 'eval/golden.json' },
    rubric: { type: 'string', default: 'eval/rubric.json' }, case: { type: 'string' },
    answers: { type: 'string' }, output: { type: 'string' },
  } });
  if (!args.case || !args.answers || !args.output) throw new Error('--case, --answers and --output are required');
  const read = (file) => JSON.parse(fs.readFileSync(file, 'utf8'));
  const testCase = read(args.cases).cases.find((item) => item.id === args.case);
  if (!testCase) throw new Error('Unknown case');
  const result = scoreAnswers(testCase, read(args.golden), read(args.answers), read(args.rubric));
  fs.writeFileSync(args.output, JSON.stringify(result, null, 2));
  console.log(JSON.stringify({ case: result.case_id, correct: result.correct, answered: result.answered, required: result.required, correct_coverage: result.correct_coverage }));
}
