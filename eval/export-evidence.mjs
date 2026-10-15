import fs from 'node:fs';
import path from 'node:path';
import { parseArgs, parseEnv } from 'node:util';
const { values: args } = parseArgs({ options: {
  root: { type: 'string', default: 'data/eval-runs' },
  output: { type: 'string', default: 'eval/results/evidence' },
} });
if (fs.existsSync(args.output)) throw new Error('Refusing to overwrite an evidence snapshot');
const env = fs.existsSync('.env') ? parseEnv(fs.readFileSync('.env', 'utf8')) : {};
const secrets = Object.entries(env).filter(([key, value]) => /KEY|TOKEN|PASSWORD|SECRET/i.test(key) && value).map(([, value]) => value);
const redact = (text) => secrets.reduce((value, secret) => value.split(secret).join('[REDACTED]'), text);
const read = (file) => fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, 'utf8')) : null;
let count = 0;
for (const phase of fs.readdirSync(args.root).filter((name) => name.startsWith('2026-10-15-'))) {
  for (const engine of ['codex', 'finance']) {
    const directory = path.join(args.root, phase, engine);
    if (!fs.existsSync(directory) || !fs.statSync(directory).isDirectory()) continue;
    for (const caseId of fs.readdirSync(directory)) {
      const root = path.join(directory, caseId);
      const metadata = read(path.join(root, 'metadata.json'));
      if (!metadata || metadata.status === 'running') continue;
      const destination = path.join(args.output, phase.replace('2026-10-15-', ''), engine, caseId);
      fs.mkdirSync(destination, { recursive: true });
      const responsePath = path.join(root, engine === 'codex' ? 'response.md' : 'final-response.md');
      let answer = fs.existsSync(responsePath) ? fs.readFileSync(responsePath, 'utf8') : '';
      const artifacts = read(path.join(root, 'artifacts.json')) || [];
      for (const artifact of artifacts.filter((a) => a.kind === 'narrative')) answer += `\n\n---\n## Saved report: ${artifact.title}\n\n${artifact.text}\n`;
      fs.writeFileSync(path.join(destination, 'answer.md'), redact(answer), { flag: 'wx' });
      for (const file of ['answers.json', 'numeric-score.json']) {
        const source = path.join(root, file);
        if (fs.existsSync(source)) fs.writeFileSync(path.join(destination, file), redact(fs.readFileSync(source, 'utf8')), { flag: 'wx' });
      }
      const documents = artifacts.filter((a) => a.kind === 'document').map((a) => ({ artifact_id: a.artifact_id, title: a.title, source_url: a.document?.source_url, period_end: a.document?.period_end, sha256: a.document?.sha256 }));
      if (documents.length) fs.writeFileSync(path.join(destination, 'source-map.json'), JSON.stringify(documents, null, 2), { flag: 'wx' });
      count++;
    }
  }
}
console.log(JSON.stringify({ snapshots: count, output: path.resolve(args.output), containsRawModelTraces: false, containsCredentials: false }));
