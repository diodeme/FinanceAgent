import test from 'node:test';
import assert from 'node:assert/strict';
import { scoreAnswers } from './score.mjs';

const testCase = { id: 'test', period_type: 'quarter', metrics: ['revenue'], required_numeric_cells: 2 };
const golden = { quarters: [
  { company: 'AAPL', fiscal_year: 2025, fiscal_quarter: 1, period_end: '2024-12-28', revenue: 124300 },
  { company: 'AAPL', fiscal_year: 2025, fiscal_quarter: 2, period_end: '2025-03-29', revenue: 95359 },
] };
const rubric = { numeric: { absolute_tolerance_usd_millions: 0.5, absolute_tolerance_percentage_points: 0.1, absolute_tolerance_ratio: 0.005 } };
const answer = (index, extra = {}) => ({ ...golden.quarters[index], metric: 'revenue', value: golden.quarters[index].revenue, evidence: 'verbatim answer table row', ...extra });

test('missing answers reduce coverage instead of inflating accuracy', () => {
  const score = scoreAnswers(testCase, golden, { cells: [answer(0)] }, rubric);
  assert.equal(score.accuracy, 1);
  assert.equal(score.coverage, 0.5);
  assert.equal(score.correct_coverage, 0.5);
});
test('empty answer has unknown conditional accuracy, not perfect accuracy', () => {
  const score = scoreAnswers(testCase, golden, { cells: [] }, rubric);
  assert.equal(score.accuracy, null);
  assert.equal(score.correct_coverage, 0);
});
test('wrong period makes an otherwise correct value incorrect', () => {
  const score = scoreAnswers(testCase, golden, { cells: [answer(0, { period_end: '2025-03-29' })] }, rubric);
  assert.equal(score.correct, 0);
  assert.equal(score.details[0].period_mismatch, true);
});
test('conflicting duplicate answers are not silently selected', () => {
  const score = scoreAnswers(testCase, golden, { cells: [answer(0), answer(0, { value: 124000 })] }, rubric);
  assert.equal(score.correct, 0);
  assert.equal(score.details[0].conflict, true);
});
test('source support requires an explicit reviewed assessment', () => {
  const score = scoreAnswers(testCase, golden, { cells: [answer(0), answer(1, { provenance_supported: true })] }, rubric);
  assert.equal(score.provenance_assessed, 1);
  assert.equal(score.verified_correct_coverage, 0.5);
});
test('rejects a mismatched required-cell count', () => {
  assert.throws(() => scoreAnswers({ ...testCase, required_numeric_cells: 3 }, golden, { cells: [] }, rubric));
});
test('a matching printed number with contradictory units is not verified correct', () => {
  const score = scoreAnswers(testCase, golden, { cells: [answer(0, { unit_conflict: true })] }, rubric);
  assert.equal(score.details[0].value_matches, true);
  assert.equal(score.details[0].unit_conflict, true);
  assert.equal(score.correct, 0);
});
