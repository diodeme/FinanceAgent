import fs from 'node:fs';
import path from 'node:path';
import { parseArgs } from 'node:util';
const { values: args } = parseArgs({ options: {
  runs: { type: 'string' }, output: { type: 'string' }, 'baseline-runs': { type: 'string' },
  cases: { type: 'string', default: 'eval/cases.json' },
  golden: { type: 'string', default: 'eval/golden.json' },
} });
if (!args.runs || !args.output) throw new Error('--runs and --output are required');
fs.mkdirSync(args.output, { recursive: true });
const read = (file) => JSON.parse(fs.readFileSync(file, 'utf8'));
const cases = read(args.cases).cases;
const gold = read(args.golden);
const lean = (row) => Object.fromEntries(Object.entries(row).filter(([key]) => key !== 'evidence'));
fs.writeFileSync(path.join(args.output, 'reference.json'), JSON.stringify({ unit: gold.unit, quarters: gold.quarters.map(lean), annual: gold.annual.map(lean), analysis_facts: gold.analysis_facts, limitations: gold.limitations }, null, 2));
fs.copyFileSync(args.cases, path.join(args.output, 'cases.json'));
fs.copyFileSync('eval/rubric.json', path.join(args.output, 'rubric.json'));
// Hide engine labels, not formatting differences; this is not a strict blind review.
const engines = [['candidate-a', 'codex'], ['candidate-b', 'finance']];
for (const item of cases) {
  const directory = path.join(args.output, item.id);
  fs.mkdirSync(directory, { recursive: true });
  for (const [candidate, engine] of engines) {
    const root = path.join(engine === 'codex' ? args['baseline-runs'] || args.runs : args.runs, engine, item.id);
    const response = fs.readFileSync(path.join(root, engine === 'codex' ? 'response.md' : 'final-response.md'), 'utf8');
    let delivered = response;
    if (engine === 'finance') {
      const artifacts = read(path.join(root, 'artifacts.json'));
      for (const artifact of artifacts.filter((a) => a.kind === 'narrative')) delivered += `\n\n---\n# Additional delivered report: ${artifact.title}\n\n${artifact.text}\n`;
    }
    fs.writeFileSync(path.join(directory, `${candidate}.md`), delivered);
  }
}
console.log(JSON.stringify({ output: path.resolve(args.output), cases: cases.map((c) => c.id), labelsRemoved: true, strictlyBlind: false }));
