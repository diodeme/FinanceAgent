import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { parseArgs, parseEnv } from 'node:util';
import { fileURLToPath, pathToFileURL } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const { values: args } = parseArgs({
  options: {
    run: { type: 'boolean', default: false },
    case: { type: 'string', default: 'all' },
    cases: { type: 'string', default: path.join(here, 'cases.json') },
    output: { type: 'string' },
    timeout: { type: 'string', default: '300' },
    env: { type: 'string', default: path.join(here, '..', '.env') },
    'codex-path': { type: 'string' },
    'sdk-path': { type: 'string' },
  },
});
const timeoutMs = Number(args.timeout) * 1000;
if (!Number.isFinite(timeoutMs) || timeoutMs <= 0 || timeoutMs > 300000) throw new Error('Timeout must be between 1 and 300 seconds');
const suite = JSON.parse(fs.readFileSync(args.cases, 'utf8'));
const cases = suite.cases.filter((item) => args.case === 'all' || item.id === args.case);
if (!cases.length) throw new Error('No matching cases');
const provider = parseEnv(fs.readFileSync(args.env, 'utf8'));
const apiKey = provider.DEEPSEEK_API_KEY;
const baseUrl = provider.DEEPSEEK_BASE_URL;
const model = provider.DEEPSEEK_MODEL;
if (!apiKey || !baseUrl || !model) throw new Error('Missing DEEPSEEK_API_KEY/BASE_URL/MODEL');
const secrets = Object.entries(provider).filter(([key, value]) => /KEY|TOKEN|PASSWORD|SECRET/i.test(key) && value).map(([, value]) => value);
function redact(value) {
  let text = typeof value === 'string' ? value : JSON.stringify(value);
  for (const secret of secrets) text = text.split(secret).join('[REDACTED]');
  return text;
}
console.log(JSON.stringify({ engine: 'codex', model, endpointHost: new URL(baseUrl).hostname, cases: cases.map((item) => item.id), timeoutMs, fullAccess: true, execute: args.run }));
if (!args.run) process.exit(0);
if (!args.output) throw new Error('--output is required with --run');
const output = path.resolve(args.output);
fs.mkdirSync(output, { recursive: true });

const { Codex } = args['sdk-path']
  ? await import(pathToFileURL(path.resolve(args['sdk-path'])).href)
  : await import('@openai/codex-sdk');
const sdkEntry = args['sdk-path'] ? path.resolve(args['sdk-path']) : fileURLToPath(import.meta.resolve('@openai/codex-sdk'));
const sdkPackage = JSON.parse(fs.readFileSync(path.resolve(path.dirname(sdkEntry), '..', 'package.json'), 'utf8'));
const cliPath = args['codex-path'];
const cliVersion = cliPath ? execFileSync(cliPath, ['--version'], { encoding: 'utf8' }).trim() : `SDK bundled @openai/codex ${sdkPackage.dependencies?.['@openai/codex'] || 'unknown'}`;
let commit = null;
try { commit = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: path.join(here, '..'), encoding: 'utf8' }).trim(); } catch {}
let anyFailure = false;

for (const item of cases) {
  const resultDir = path.join(output, item.id);
  fs.mkdirSync(resultDir); // Refuse to overwrite a previous attempt.
  const workRoot = fs.mkdtempSync(path.join(os.tmpdir(), `finance-eval-codex-${item.id}-`));
  const workspace = path.join(workRoot, 'workspace');
  const codexHome = path.join(workRoot, 'codex-home');
  fs.mkdirSync(workspace);
  fs.mkdirSync(codexHome);
  const env = {};
  for (const key of ['PATH', 'Path', 'SystemRoot', 'WINDIR', 'USERPROFILE', 'HOME', 'APPDATA', 'LOCALAPPDATA', 'TEMP', 'TMP', 'COMSPEC', 'PATHEXT', 'ProgramData', 'PSModulePath']) {
    if (process.env[key]) env[key] = process.env[key];
  }
  env.CODEX_HOME = codexHome;
  const write = (name, value) => fs.writeFileSync(path.join(resultDir, name), redact(typeof value === 'string' ? value : JSON.stringify(value, null, 2)));
  const metadata = {
    engine: 'codex', case_id: item.id, commit, model, endpoint_host: new URL(baseUrl).hostname,
    sdk_version: sdkPackage.version, cli_version: cliVersion,
    requested_sandbox: 'danger-full-access', approval_policy: 'never', web_search: 'live',
    timeout_ms: timeoutMs, automatic_retries: 0, workspace, codex_home: codexHome,
    status: 'running', usage: null, usage_complete: false,
    model_metadata_fallback: false, prompt: item.prompt,
  };
  write('metadata.json', metadata);
  write('prompt.md', item.prompt);
  const codex = new Codex({
    ...(cliPath ? { codexPathOverride: cliPath } : {}),
    apiKey, env,
    config: {
      model_provider: 'evaluation',
      model_providers: {
        evaluation: {
          name: 'Evaluation provider', base_url: baseUrl, env_key: 'CODEX_API_KEY', wire_api: 'responses',
          request_max_retries: 0, stream_max_retries: 0, stream_idle_timeout_ms: 60000,
        },
      },
    },
  });
  const thread = codex.startThread({
    model, workingDirectory: workspace, skipGitRepoCheck: true,
    sandboxMode: 'danger-full-access', approvalPolicy: 'never', networkAccessEnabled: true, webSearchMode: 'live',
  });
  const controller = new AbortController();
  const started = performance.now();
  metadata.started_at = new Date().toISOString();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const messages = [];
  let terminalFailure = false;
  try {
    const { events } = await thread.runStreamed(item.prompt, { signal: controller.signal });
    for await (const event of events) {
      fs.appendFileSync(path.join(resultDir, 'events.jsonl'), redact(event) + '\n');
      if (event.type === 'thread.started') metadata.thread_id = event.thread_id;
      if (event.type === 'item.completed' && event.item?.type === 'agent_message') messages.push(event.item.text);
      if (event.type === 'item.completed' && event.item?.type === 'error' && event.item.message?.includes('fallback metadata')) metadata.model_metadata_fallback = true;
      if (event.type === 'turn.completed') {
        metadata.status = 'completed';
        metadata.usage = event.usage;
        metadata.usage_complete = true;
      }
      if (event.type === 'turn.failed') {
        terminalFailure = true;
        metadata.error = event.error;
      }
      if (event.type === 'error') metadata.last_stream_error = event.message;
      if (['turn.completed', 'turn.failed'].includes(event.type)) console.log(redact({ case: item.id, ...event }));
    }
    if (metadata.status !== 'completed' || terminalFailure) metadata.status = 'failed';
  } catch (error) {
    metadata.status = controller.signal.aborted ? 'timeout' : 'failed';
    metadata.error = redact(error?.message || error);
  } finally {
    clearTimeout(timer);
    metadata.wall_time_ms = Math.round(performance.now() - started);
    metadata.finished_at = new Date().toISOString();
    write('response.md', metadata.status === 'completed' ? messages.at(-1) || '' : '');
    write('partial-response.md', messages.join('\n\n'));
    const sessionDir = path.join(codexHome, 'sessions');
    if (fs.existsSync(sessionDir)) {
      const paths = fs.readdirSync(sessionDir, { recursive: true }).filter((name) => name.endsWith('.jsonl'));
      const traces = [];
      for (const name of paths) {
        const text = fs.readFileSync(path.join(sessionDir, name), 'utf8');
        traces.push(text);
        for (const line of text.split('\n').filter(Boolean)) {
          try {
            const event = JSON.parse(line);
            if (event.type === 'turn_context') {
              metadata.effective_sandbox = event.payload.sandbox_policy;
              metadata.effective_model = event.payload.model;
            }
            if (event.type === 'event_msg' && event.payload?.type === 'token_count') metadata.last_usage_snapshot = event.payload.info?.total_token_usage;
          } catch {}
        }
      }
      write('rollout.jsonl', traces.join('\n'));
    }
    write('files.json', fs.readdirSync(workspace, { recursive: true }));
    write('metadata.json', metadata);
    console.log(redact({ case: item.id, status: metadata.status, wall_time_ms: metadata.wall_time_ms, usage: metadata.usage, results: resultDir }));
  }
  if (metadata.status !== 'completed') anyFailure = true;
}
if (anyFailure) process.exitCode = 2;
