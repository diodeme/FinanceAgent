#!/usr/bin/env node
/** Thin native-API evaluator. No model calls without --run; preflight is read-only. */
import { readFile, writeFile, appendFile, mkdir, stat } from 'node:fs/promises';
import { dirname, resolve, isAbsolute, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseArgs, parseEnv } from 'node:util';
import { randomUUID } from 'node:crypto';
import { spawn, execFileSync } from 'node:child_process';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const { values: args } = parseArgs({ options: {
  case: { type: 'string', default: 'all' }, cases: { type: 'string', default: join(root, 'eval/cases.json') },
  output: { type: 'string' }, timeout: { type: 'string' }, run: { type: 'boolean' }, preflight: { type: 'boolean' },
  'api-url': { type: 'string', default: 'http://localhost:8000' },
  'user-id': { type: 'string', default: 'dev' }, agent: { type: 'string' }, 'agent-id': { type: 'string' },
  'source-session': { type: 'string' }, 'source-session-id': { type: 'string' },
  'wsl-distro': { type: 'string', default: 'Ubuntu-24.04' }, help: { type: 'boolean' },
} });
if (args.help) {
  console.log(`Usage (Node 24, no dependencies):
  node eval/run-finance.mjs --case all --preflight
  node eval/run-finance.mjs --case quarterly-profit --output E:/eval-runs/run-001 --run
Options: --cases PATH --timeout 300 --api-url URL --user-id dev
         --agent ID --source-session ID --wsl-distro Ubuntu-24.04
--user-id/--agent/--source-session identify the READ-ONLY model-settings source.
Each real case creates a fresh evaluation user, FinanceAgent, credential and session.
The source's complete parameters are preserved; fallback is explicitly disabled.
--output must be absolute and must not exist. No chat or provider retries.
--preflight never creates users, credentials, sessions, or sends chat.
Preflight with --output writes only a new preflight.json directory; use another directory for --run.`);
  process.exit(0);
}
const secretKeys = /^(api[_-]?key|authorization|access[_-]?token|refresh[_-]?token|secret|password|database_url)$/i;
let secrets = [];
const redactText = (text) => secrets.reduce((v, secret) => v.split(secret).join('[REDACTED]'), String(text));
function safe(value) {
  if (typeof value === 'string') return redactText(value);
  if (Array.isArray(value)) return value.map(safe);
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([k, v]) =>
    [k, secretKeys.test(k) ? '[REDACTED]' : safe(v)]));
  return value;
}
function check(condition, message) { if (!condition) throw new Error(message); }
const json = (value) => JSON.stringify(safe(value), null, 2) + '\n';
const save = (path, value) => writeFile(path, json(value), { flag: 'wx' });
const sleep = (ms) => new Promise((done) => setTimeout(done, ms));
let stopRequested = false;
process.on('SIGINT', () => { stopRequested = true; });
process.on('SIGTERM', () => { stopRequested = true; });

async function api(path, { user = args['user-id'], method = 'GET', body, timeout = 15000, signal } = {}) {
  const response = await fetch(new URL(path, args['api-url']), {
    method, headers: { 'X-User-ID': user, ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}) },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
    signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(Math.max(1, timeout))]) : AbortSignal.timeout(Math.max(1, timeout)),
  });
  // Never include response bodies in errors: credential endpoints may return secrets.
  check(response.ok, `${method} ${path.split('?')[0]} returned HTTP ${response.status}`);
  return response.status === 204 ? null : response.json();
}
function assertParameters(value) {
  check(value && typeof value === 'object' && !Array.isArray(value), 'Model parameters must be an object');
  function walk(node) {
    if (!node || typeof node !== 'object') return;
    for (const [key, child] of Object.entries(node)) {
      check(!secretKeys.test(key), 'Inline secrets in model parameters are not supported');
      check(!/fallback/i.test(key), 'Fallback parameters are not supported');
      walk(child);
    }
  }
  walk(value);
  for (const key of ['model', 'base_url', 'credential_id', 'api_base', 'provider']) {
    check(!(key in value), 'Provider/model overrides in parameters are not supported');
  }
}
const normalUrl = (url) => String(url ?? '').replace(/\/+$/, '');
function credentialMatches(credential, config, env) {
  return {
    model: config.model === env.DEEPSEEK_MODEL,
    credential_type: config.type === 'deepseek_credential' && credential?.data?.type === config.type,
    base_url: normalUrl(credential?.data?.base_url) === normalUrl(env.DEEPSEEK_BASE_URL),
    api_key_matches: credential?.data?.api_key === env.DEEPSEEK_API_KEY,
  };
}

async function usage(sessionId) {
  const script = await readFile(join(root, 'eval/collect-usage.py'), 'utf8');
  // Docker isn't in Windows PATH; execute the same native container command in WSL.
  const command = process.platform === 'win32' ? 'wsl.exe' : 'docker';
  const dockerArgs = ['exec', '-i', 'financeagent-backend-1', 'python', '-I', '-', sessionId];
  const commandArgs = process.platform === 'win32'
    ? ['-d', args['wsl-distro'], '--exec', 'docker', ...dockerArgs] : dockerArgs;
  return new Promise((resolvePromise, reject) => {
    const child = spawn(command, commandArgs, { cwd: root, stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true });
    let stdout = '';
    const timer = setTimeout(() => { child.kill(); reject(new Error('Usage collector timed out')); }, 30000);
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', (part) => { stdout += part; });
    // Deliberately discard stderr, which may contain DSNs from dependency errors.
    child.stderr.resume();
    child.stdin.on('error', () => {});
    child.on('error', () => { clearTimeout(timer); reject(new Error('Cannot launch backend usage collector')); });
    child.on('close', (code) => {
      clearTimeout(timer);
      if (code !== 0) return reject(new Error('Backend usage collector failed (stderr suppressed)'));
      try { resolvePromise(JSON.parse(stdout.replace(/^\uFEFF/, ''))); }
      catch { reject(new Error('Backend usage collector returned invalid JSON')); }
    });
    child.stdin.end(script);
  });
}

async function preflight(env, selected, timeoutSeconds) {
  const health = await api('/fin/healthz');
  check(health.status === 'ok', 'Finance health check failed');
  const schema = await api('/openapi.json');
  for (const path of ['/fin/bootstrap', '/credential/', '/sessions/', '/chat/', '/sessions/{session_id}/stream', '/sessions/{session_id}/interrupt']) {
    check(schema.paths?.[path], `Required native API missing: ${path}`);
  }
  const required = schema.components?.schemas?.Msg?.required ?? [];
  check(required.every((key) => ['name', 'content', 'role'].includes(key)) && required.includes('role'), 'Msg schema changed; review input contract');
  const agents = (await api('/agent/')).agents ?? [];
  const agentId = args.agent ?? args['agent-id'] ?? agents.find((a) => a.data?.name === 'FinanceAgent')?.id;
  check(agentId && agents.some((a) => a.id === agentId && a.data?.name === 'FinanceAgent'), 'Source FinanceAgent not found for this user');
  const sessions = (await api(`/sessions/?agent_id=${encodeURIComponent(agentId)}`)).sessions ?? [];
  const requested = args['source-session'] ?? args['source-session-id'];
  const credentials = (await api('/credential/')).credentials ?? [];
  for (const credential of credentials) {
    if (typeof credential.data?.api_key === 'string' && credential.data.api_key) secrets.push(credential.data.api_key);
  }
  const source = requested ? sessions.find((s) => s.session?.id === requested) : sessions.find((s) => {
    const config = s.session?.config?.chat_model_config;
    return !s.is_running && config && Object.values(credentialMatches(credentials.find((c) => c.id === config.credential_id), config, env)).every(Boolean);
  });
  check(source, 'No idle source session matches the .env model AND actual stored credential; specify a matching --source-session (no automatic model substitution)');
  check(!source.is_running, 'Source session is running; refusing to overlap');
  const config = structuredClone(source.session.config?.chat_model_config);
  check(config && ['type', 'credential_id', 'model'].every((key) => typeof config[key] === 'string' && config[key]), 'Incomplete source model reference');
  check(config.parameters !== undefined, 'Source parameters missing; cannot attest complete parameter preservation');
  assertParameters(config.parameters);
  const matches = credentialMatches(credentials.find((c) => c.id === config.credential_id), config, env);
  check(Object.values(matches).every(Boolean), 'Actual source model/credential does not match .env; no fallback or substitution allowed');
  const usageProbe = await usage(`eval-preflight-${randomUUID()}`);
  check(usageProbe.usage?.logged_model_calls === 0, 'Read-only usage probe failed');
  let commit = null;
  try { commit = execFileSync('git', ['-C', root, 'rev-parse', 'HEAD'], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).trim(); } catch {}
  return {
    status: 'passed', mode: 'read_only_preflight', model_calls_started: 0, commit,
    api_url: args['api-url'], source_user_id: args['user-id'], source_agent_id: agentId, source_session_id: source.session.id,
    env_matches_actual_service: matches, model_config: config, parameters_preserved: true,
    fallback_chat_model_config: null, usage_collector: 'available',
    source_has_fallback: Boolean(source.session.config?.fallback_chat_model_config),
    isolation: 'fresh user + native bootstrapped FinanceAgent + credential + session per case',
    isolation_limits: 'Shared service/database and SEC caches; temporary X-User-ID identity is not an authentication boundary.',
    cases: selected.map((c) => c.id), timeout_seconds: timeoutSeconds,
    new_session_config_and_credential_revalidated_before_chat: true,
  };
}

async function openStream(user, agentId, sessionId, path) {
  const controller = new AbortController();
  const connectTimer = setTimeout(() => controller.abort(), 15000);
  let response;
  try {
    response = await fetch(new URL(`/sessions/${sessionId}/stream?agent_id=${encodeURIComponent(agentId)}`, args['api-url']), {
      headers: { 'X-User-ID': user, Accept: 'text/event-stream' }, signal: controller.signal,
    });
    check(response.ok && response.headers.get('content-type')?.includes('text/event-stream'), 'SSE connection failed');
  } catch (error) { controller.abort(); throw error; }
  finally { clearTimeout(connectTimer); }
  await writeFile(path, '', { flag: 'wx' });
  const state = { connected: true, events: 0, error: null, ended: false, tail_truncated: false };
  const done = (async () => {
    let pending = '';
    const decoder = new TextDecoder();
    try {
      for await (const chunk of response.body) {
        pending += decoder.decode(chunk, { stream: true }).replace(/\r/g, '');
        let boundary;
        while ((boundary = pending.indexOf('\n\n')) >= 0) {
          const frame = pending.slice(0, boundary);
          pending = pending.slice(boundary + 2);
          if (!frame.trim()) continue;
          const data = frame.split('\n').filter((line) => line.startsWith('data:')).map((line) => line.slice(5).trimStart()).join('\n');
          let parsed = null;
          if (data) { try { parsed = JSON.parse(data); } catch { parsed = data; } }
          await appendFile(path, JSON.stringify(safe({ received_at: new Date().toISOString(), frame, data: parsed })) + '\n');
          state.events++;
        }
      }
      if (!controller.signal.aborted) state.error = 'SSE stream ended unexpectedly';
    } catch (error) {
      if (!controller.signal.aborted) state.error = redactText(error.message);
    } finally {
      state.ended = true;
      state.tail_truncated = Boolean(pending.trim());
    }
  })();
  return { state, close: async () => { controller.abort(); await done; } };
}

async function getMessages(user, agentId, sessionId, timeout = 15000, all = false) {
  const base = `/sessions/${sessionId}/messages?agent_id=${encodeURIComponent(agentId)}&limit=100`;
  const first = await api(base, { user, timeout });
  let page = first;
  let messages = first.messages ?? [];
  const cursors = new Set();
  while (all && page.has_more) {
    const before = page.messages?.[0]?.id;
    check(before && !cursors.has(before), 'Message pagination cursor stalled');
    cursors.add(before);
    page = await api(`${base}&before=${encodeURIComponent(before)}`, { user });
    messages = [...(page.messages ?? []), ...messages];
  }
  return { ...first, messages: [...new Map(messages.map((m) => [m.id, m])).values()], has_more: all ? false : first.has_more };
}

function artifactIds(value, ids = new Set(), key = '') {
  if (typeof value === 'string') {
    if (['artifact_id', 'document_id'].includes(key) || ['artifact_ids', 'input_ids'].includes(key)) ids.add(value);
    // Tool metadata can itself be serialized JSON; parse, don't regex all UUIDs.
    if (/^\s*[\[{]/.test(value)) { try { artifactIds(JSON.parse(value), ids); } catch {} }
    for (const match of value.matchAll(/#(?:finance|filing)\/([A-Za-z0-9_-]+)/g)) ids.add(match[1]);
  } else if (Array.isArray(value)) { for (const child of value) artifactIds(child, ids, key); }
  else if (value && typeof value === 'object') { for (const [name, child] of Object.entries(value)) artifactIds(child, ids, name); }
  return ids;
}

async function collectArtifacts(user, sessionId, messages, output) {
  const listing = await api(`/fin/results?session_id=${encodeURIComponent(sessionId)}&limit=10`, { user });
  await save(join(output, 'artifacts-list.json'), listing);
  const ids = artifactIds([listing, messages]);
  const artifacts = [];
  const errors = [];
  const fetched = new Set();
  // Add transitive input artifacts too; listing alone is capped at ten.
  for (const id of ids) {
    if (fetched.has(id)) continue;
    fetched.add(id);
    try {
      const artifact = await api(`/fin/results/${encodeURIComponent(id)}`, { user });
      artifacts.push(artifact);
      artifactIds(artifact, ids);
    } catch (error) { errors.push({ artifact_id: id, error: redactText(error.message) }); }
  }
  await save(join(output, 'artifacts.json'), artifacts);
  return { list_limit: 10, discovered_ids: [...ids], saved_count: artifacts.length, errors,
    discovery: 'result listing + every message/tool-metadata JSON + transitive artifact input/citation IDs' };
}

async function runCase(testCase, output, baseline, env, timeoutSeconds) {
  const createdAt = Date.now();
  const metadata = {
    case_id: testCase.id, status: 'setting_up', started_at: new Date().toISOString(),
    user_id: `eval-${testCase.id}-${randomUUID()}`, agent_id: null, session_id: null,
    timeout_seconds: timeoutSeconds, isolation: baseline.isolation, isolation_limits: baseline.isolation_limits,
    config: null, parameters_preserved: null, fallback_chat_model_config: null,
    idle_confirmed: false, chat_submitted: false, chat_accepted: false, errors: [],
  };
  await mkdir(output);
  await save(join(output, 'case.json'), testCase);
  await writeFile(join(output, 'prompt.txt'), testCase.prompt + '\n', { flag: 'wx' });
  let stream;
  let runStarted;
  let responseSnapshot;
  const user = metadata.user_id;
  const writeMetadata = () => writeFile(join(output, 'metadata.json'), json(metadata));
  const recordError = (stage, error) => metadata.errors.push({ stage, error: redactText(error.message ?? String(error)) });
  try {
    const existing = await api('/agent/', { user });
    check(existing.total === 0 || existing.agents?.length === 0, 'Generated evaluation user already has agents');
    const bootstrap = await api('/fin/bootstrap', { user, method: 'POST' });
    metadata.agent_id = bootstrap.agent_id;
    check(metadata.agent_id, 'Bootstrap returned no agent ID');
    const created = await api('/credential/', { user, method: 'POST', body: { data: {
      type: baseline.model_config.type, api_key: env.DEEPSEEK_API_KEY, base_url: env.DEEPSEEK_BASE_URL,
    } } });
    check(created.credential_id, 'Credential creation returned no ID');
    const config = { ...structuredClone(baseline.model_config), credential_id: created.credential_id };
    const result = await api('/sessions/', { user, method: 'POST', body: {
      agent_id: metadata.agent_id, name: `eval ${testCase.id}`, chat_model_config: config, fallback_chat_model_config: null,
    } });
    metadata.session_id = result.session_id;
    check(metadata.session_id, 'Session creation returned no ID');
    await writeMetadata();
    const sessions = await api(`/sessions/?agent_id=${encodeURIComponent(metadata.agent_id)}`, { user });
    const actual = sessions.sessions?.find((s) => s.session?.id === metadata.session_id);
    check(actual && !actual.is_running, 'New evaluation session is not idle');
    const actualConfig = actual.session.config?.chat_model_config;
    const credentials = (await api('/credential/', { user })).credentials ?? [];
    metadata.env_matches_actual_service = credentialMatches(credentials.find((c) => c.id === actualConfig?.credential_id), actualConfig ?? {}, env);
    metadata.parameters_preserved = JSON.stringify(actualConfig?.parameters) === JSON.stringify(config.parameters);
    check(Object.values(metadata.env_matches_actual_service).every(Boolean), 'New session model/credential mismatch with .env');
    check(metadata.parameters_preserved, 'New session parameters differ from source');
    check(actual.session.config?.fallback_chat_model_config == null, 'New session has a fallback model');
    metadata.config = actualConfig;
    responseSnapshot = await getMessages(user, metadata.agent_id, metadata.session_id);
    check(responseSnapshot.messages.length === 0, 'Evaluation session is not empty');
    stream = await openStream(user, metadata.agent_id, metadata.session_id, join(output, 'events.jsonl'));
    check(!stopRequested, 'Runner interrupted before chat');
    metadata.status = 'running';
    runStarted = Date.now();
    metadata.chat_started_at = new Date(runStarted).toISOString();
    metadata.chat_submitted = true; // An ambiguous HTTP failure must still trigger interrupt.
    await writeMetadata();
    await api('/chat/', { user, method: 'POST', timeout: Math.min(15000, timeoutSeconds * 1000), body: {
      agent_id: metadata.agent_id, session_id: metadata.session_id,
      input: { name: 'user', role: 'user', content: [{ type: 'text', text: testCase.prompt }] },
    } });
    metadata.chat_accepted = true;
    const deadline = runStarted + timeoutSeconds * 1000;
    while (Date.now() < deadline && !stopRequested) {
      responseSnapshot = await getMessages(user, metadata.agent_id, metadata.session_id, Math.min(10000, deadline - Date.now()));
      if (responseSnapshot.is_running === false && responseSnapshot.messages.length > 0) {
        metadata.idle_confirmed = true;
        metadata.status = 'completed';
        break;
      }
      await sleep(Math.min(1000, Math.max(0, deadline - Date.now())));
    }
    if (!metadata.idle_confirmed) metadata.status = stopRequested ? 'interrupted' : 'timeout';
  } catch (error) {
    metadata.status = runStarted && Date.now() - runStarted >= timeoutSeconds * 1000 ? 'timeout' : 'error';
    recordError('run', error);
  } finally {
    if (metadata.chat_submitted && !metadata.idle_confirmed) {
      metadata.interrupt_requested_at = new Date().toISOString();
      try {
        await api(`/sessions/${metadata.session_id}/interrupt?agent_id=${encodeURIComponent(metadata.agent_id)}`, { user, method: 'POST', body: null, timeout: 10000 });
        metadata.interrupt_accepted = true;
      } catch (error) { recordError('interrupt', error); }
      const stopDeadline = Date.now() + 30000;
      // 202 is acceptance only. Confirm idle independently; never retry the chat.
      while (Date.now() < stopDeadline) {
        try {
          responseSnapshot = await getMessages(user, metadata.agent_id, metadata.session_id, Math.min(5000, stopDeadline - Date.now()));
          if (responseSnapshot.is_running === false) { metadata.idle_confirmed = true; break; }
        } catch (error) { recordError('confirm_idle', error); }
        await sleep(500);
      }
      if (!metadata.idle_confirmed) metadata.status = 'stop_unconfirmed';
    }
    metadata.wall_time_seconds = runStarted ? (Date.now() - runStarted) / 1000 : null;
    if (stream) { await stream.close(); metadata.sse = stream.state; }
    if (metadata.session_id) {
      try {
        responseSnapshot = await getMessages(user, metadata.agent_id, metadata.session_id, 15000, true);
        await save(join(output, 'messages.json'), responseSnapshot);
        const assistants = responseSnapshot.messages.filter((m) => m.role === 'assistant');
        const textual = assistants.map((m) => {
          const blocks = m.content ?? [];
          const lastTool = blocks.findLastIndex((b) => b.type === 'tool_call' || b.type === 'tool_result');
          return { message: m, text: blocks.slice(lastTool + 1).filter((b) => b.type === 'text').map((b) => b.text ?? '').join('\n') };
        }).filter((m) => m.text.trim());
        const final = textual.at(-1);
        await writeFile(join(output, 'final-response.md'), redactText(final?.text ?? '') + '\n', { flag: 'wx' });
        metadata.final_response_message_id = final?.message.id ?? null;
        metadata.message_errors = responseSnapshot.messages.filter((m) => m.error).map((m) => ({ id: m.id, error: m.error }));
        if (metadata.status === 'completed' && (!final || metadata.message_errors.length)) metadata.status = 'model_error';
        metadata.final_response_is_partial = metadata.status !== 'completed';
        metadata.artifacts = await collectArtifacts(user, metadata.session_id, responseSnapshot.messages, output);
      } catch (error) { recordError('collect_messages_artifacts', error); }
      try {
        const observed = await usage(metadata.session_id);
        await save(join(output, 'usage.json'), observed);
        metadata.usage_complete = observed.usage?.usage_complete ?? false;
        metadata.logged_model_calls = observed.usage?.logged_model_calls ?? null;
        metadata.usage_snapshot_after_idle = metadata.idle_confirmed;
      } catch (error) {
        metadata.usage_complete = false;
        await save(join(output, 'usage.json'), { usage: null, error: 'collection_failed' });
        recordError('collect_usage', error);
      }
    }
    metadata.finished_at = new Date().toISOString();
    metadata.total_wall_time_seconds = (Date.now() - createdAt) / 1000;
    await writeMetadata();
    await save(join(output, 'error.json'), { status: metadata.status, errors: metadata.errors, message_errors: metadata.message_errors ?? [] });
  }
  return metadata;
}

async function main() {
  check(Boolean(args.run) !== Boolean(args.preflight), 'Choose exactly one of --preflight or --run (paid calls require --run)');
  check(!args.run || args.output, '--run requires --output ABSOLUTE_PATH');
  if (args.output) {
    check(isAbsolute(args.output), '--output must be an absolute path');
    try { await stat(args.output); throw new Error('Output already exists; refusing to overwrite or resume an old run'); }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
    check((await stat(dirname(args.output))).isDirectory(), 'Output parent directory must already exist');
  }
  const caseFile = JSON.parse(await readFile(resolve(args.cases), 'utf8'));
  check(Array.isArray(caseFile.cases) && caseFile.cases.length, 'Cases file must have a nonempty cases array');
  const selected = caseFile.cases.filter((c) => args.case === 'all' || c.id === args.case);
  check(selected.length && new Set(selected.map((c) => c.id)).size === selected.length, 'Unknown/duplicate case ID');
  for (const c of selected) check(/^[A-Za-z0-9][A-Za-z0-9_-]*$/.test(c.id) && typeof c.prompt === 'string' && c.prompt.trim(), 'Invalid case ID or prompt');
  const timeoutSeconds = Number(args.timeout ?? caseFile.timeout_seconds ?? 300);
  check(Number.isFinite(timeoutSeconds) && timeoutSeconds > 0, '--timeout must be positive seconds');
  const env = parseEnv(await readFile(join(root, '.env'), 'utf8'));
  check(['DEEPSEEK_API_KEY', 'DEEPSEEK_BASE_URL', 'DEEPSEEK_MODEL'].every((key) => env[key]?.trim()), '.env must specify DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL and DEEPSEEK_MODEL');
  secrets = [env.DEEPSEEK_API_KEY];
  const providerUrl = new URL(env.DEEPSEEK_BASE_URL);
  check(!providerUrl.username && !providerUrl.password && !providerUrl.search && !providerUrl.hash, 'Provider base URL must not contain credentials or query parameters');
  const baseline = await preflight(env, selected, timeoutSeconds);
  console.log(json(baseline).trim());
  if (!args.output) return;
  await mkdir(args.output); // Nonrecursive/exclusive: an existing run is never reused.
  await save(join(args.output, 'preflight.json'), baseline);
  if (args.preflight) return;
  const summary = { started_at: new Date().toISOString(), commit: baseline.commit, cases: [], no_chat_retries: true };
  for (const c of selected) {
    if (stopRequested) break;
    const result = await runCase(c, join(args.output, c.id), baseline, env, timeoutSeconds);
    summary.cases.push({ case_id: c.id, user_id: result.user_id, session_id: result.session_id, status: result.status,
      wall_time_seconds: result.wall_time_seconds, idle_confirmed: result.idle_confirmed, usage_complete: result.usage_complete });
    console.log(json(summary.cases.at(-1)).trim());
    await writeFile(join(args.output, 'summary.json'), json(summary));
    if (result.status === 'stop_unconfirmed') break;
  }
  summary.finished_at = new Date().toISOString();
  summary.all_requested_cases_completed = summary.cases.length === selected.length && summary.cases.every((c) => c.status === 'completed');
  await writeFile(join(args.output, 'summary.json'), json(summary));
  if (!summary.all_requested_cases_completed) process.exitCode = 1;
}

main().catch((error) => { console.error(json({ status: 'failed', error: redactText(error.message) }).trim()); process.exitCode = 1; });
