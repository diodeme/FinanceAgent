import fs from 'node:fs';
import path from 'node:path';
import { parseArgs } from 'node:util';
const { values: args } = parseArgs({ options: { root: { type: 'string', default: 'data/eval-runs' }, output: { type: 'string', default: 'eval/results/summary.json' } } });
const read = (file) => fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, 'utf8')) : null;
const rows = [];
for (const phase of fs.readdirSync(args.root).filter((name) => name.startsWith('2026-10-15-'))) {
  for (const engine of ['codex', 'finance']) {
    const directory = path.join(args.root, phase, engine);
    if (!fs.existsSync(directory) || !fs.statSync(directory).isDirectory()) continue;
    for (const caseId of fs.readdirSync(directory)) {
      const root = path.join(directory, caseId);
      const metadata = read(path.join(root, 'metadata.json'));
      if (!metadata) continue;
      const usageFile = read(path.join(root, 'usage.json'));
      const usage = engine === 'codex' ? metadata.usage : usageFile?.usage;
      const score = read(path.join(root, 'numeric-score.json'));
      const artifacts = read(path.join(root, 'artifacts.json'));
      rows.push({
        phase: phase.replace('2026-10-15-', ''), engine, case_id: caseId,
        status: metadata.status, model: metadata.model || metadata.config?.model,
        seconds: metadata.wall_time_seconds ?? (metadata.wall_time_ms == null ? null : metadata.wall_time_ms / 1000),
        input_tokens: usage?.input_tokens ?? null,
        output_tokens: usage?.output_tokens ?? null,
        cached_input_tokens: usage?.cached_input_tokens ?? null,
        usage_complete: metadata.usage_complete ?? usage?.usage_complete ?? false,
        numeric_correct: score?.correct ?? null,
        numeric_answered: score?.answered ?? null,
        numeric_required: score?.required ?? null,
        correct_coverage: score?.correct_coverage ?? null,
        unit_conflicts: score ? score.details.filter((c) => c.unit_conflict).length : null,
        date_correct: score ? score.date_checks.filter((c) => c.correct).length : null,
        date_required: score?.date_checks.length ?? null,
        saved_narratives: artifacts ? artifacts.filter((a) => a.kind === 'narrative').length : null,
        analysis_tool_failures: usageFile ? usageFile.steps.filter((s) => s.step_type === 'tool' && s.name === 'data_analyze' && s.status !== 'ok').length : null,
        raw_results: root.replaceAll('\\', '/'),
      });
    }
  }
}
const qualityFiles = [
  ['initial', 'eval/reviews/initial-analysis.json'],
  ['holdout', 'eval/reviews/holdout-baseline-analysis.json'],
  ['fixed2', 'eval/reviews/final-analysis.json'],
  ['holdout2', 'eval/reviews/final-holdout-analysis.json'],
];
for (const [phase, file] of qualityFiles) {
  const review = read(file);
  for (const evaluation of review?.candidates || []) {
    const engine = evaluation.candidate.replace('candidate-', '') === 'a' ? 'codex' : 'finance';
    const row = rows.find((r) => r.phase === phase && r.engine === engine && r.case_id === evaluation.case_id);
    if (row) { row.analysis_score = evaluation.total; row.analysis_score_maximum = 20; row.analysis_review = file; }
  }
}
rows.sort((a, b) => a.phase.localeCompare(b.phase) || a.engine.localeCompare(b.engine) || a.case_id.localeCompare(b.case_id));
fs.mkdirSync(path.dirname(args.output), { recursive: true });
fs.writeFileSync(args.output, JSON.stringify({ evaluation_date: '2026-10-15', baseline: 'Codex general-purpose proxy, not ChatGPT', scores_are_assistant_subjective: true, numeric_scope: 'Required cells only; extra prose calculations and claims are graded separately.', completion_note: 'Runtime completed and saved_narratives do not establish semantic completeness. The fixed2 quarterly-profit report ends mid-conclusion.', generation_configuration_note: 'At the evaluated SDK version, FinanceAgent parameters={} disables thinking; Codex returned reasoning usage under its native defaults. This is not a strictly matched reasoning/output-budget ablation. See docs/评估.md.', cache_note: 'Input tokens include cached context; Finance cached-token breakdown unavailable. Do not interpret token totals as billed cost.', rows }, null, 2));
for (const row of rows) console.log(JSON.stringify(row));
