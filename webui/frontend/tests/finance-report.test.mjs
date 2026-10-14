import assert from 'node:assert/strict';
import test from 'node:test';
import {
	REPORT_STREAM_LIMITS, decodeFinanceReport, deriveFinanceReport, financeReportTitle,
	reportKey, reportResultText, resolveFinanceReport, findFinanceReportCall,
} from '../src/lib/finance-report.ts';

const frame = (type, value) => JSON.stringify({ finance_stream: 1, type, ...value }) + '\n';
const start = frame('start', { title: 'AAPL 财务分析' });
const body = 'First paragraph.\n\n[Revenue](#finance/table/cell)\n{"finance_stream":1,"type":"summary","result":{}}';
const wire = start + frame('body', { text: body }) + frame('body', { text: '\nFinal sentence.' }) + frame('summary', { result: { artifact_ids: ['n1'] } });
const artifact = { artifact_id: 'n1', kind: 'narrative', title: 'AAPL 财务分析', text: 'Saved final narrative.', warnings: [], input_ids: [], citations: [], data: null };
const envelope = { schema_version: 1, artifacts: [artifact], companies: [], warnings: [], message: 'Saved', error_code: null };
const derive = (overrides = {}) => deriveFinanceReport({ text: wire, state: 'running', envelope: null, replyFinished: false, ...overrides });

test('arbitrary accumulated transport splits preserve escaped newlines and cannot inject summary records', () => {
	for (let split = 0; split <= wire.length; split++) {
		const interim = decodeFinanceReport(wire.slice(0, split));
		assert.equal(interim.error, null, `split ${split}`);
		assert.ok((body + '\nFinal sentence.').startsWith(interim.body));
		const combined = decodeFinanceReport(wire.slice(0, split) + wire.slice(split), true);
		assert.equal(combined.body, body + '\nFinal sentence.');
		assert.equal(combined.summary, true);
		assert.equal(combined.error, null);
	}
	const proseOnly = decodeFinanceReport(start + frame('body', { text: body }));
	assert.equal(proseOnly.summary, false);
});

test('research progress stays bounded and separate through arbitrary splits, replay and compact final metadata', () => {
	const progress = Array.from({ length: 20 }, (_, index) => `Read ${index}: <script>not HTML</script>\nTool activity`);
	const research = start + progress.map((text) => frame('progress', { text })).join('');
	const full = research + frame('body', { text: 'Actual report only.' }) + frame('summary', { result: {} });
	for (let split = 0; split <= full.length; split++) {
		const interim = decodeFinanceReport(full.slice(0, split));
		assert.equal(interim.error, null);
		assert.ok(interim.progress.length <= 12);
		assert.ok('Actual report only.'.startsWith(interim.body));
	}
	assert.equal(derive({ text: research }).status, 'running');
	assert.equal(derive({ text: research }).text, '');
	const decoded = decodeFinanceReport(full, true);
	assert.deepEqual(decoded.progress, progress.slice(-12));
	assert.equal(decoded.body, 'Actual report only.');
	assert.deepEqual(decodeFinanceReport(full, true), decoded, 'replay must not append duplicate history');
	const final = derive({ text: 'compact metadata response', state: 'success', envelope });
	assert.equal(final.text, artifact.text);
	assert.equal(final.status, 'complete');
	assert.equal(final.streamError, null);
	assert.equal(final.progress?.length ?? 0, 0);
	assert.equal(decodeFinanceReport(start + frame('progress', { text: 42 })).error, 'invalid');
	assert.equal(decodeFinanceReport(start + frame('progress', { text: 'x'.repeat(REPORT_STREAM_LIMITS.progressText + 1) })).error, 'limit');
	assert.equal(decodeFinanceReport(full + frame('progress', { text: 'late' })).error, 'invalid');
});

test('only versioned first start record enables framing, with CRLF support', () => {
	assert.equal(decodeFinanceReport(frame('body', { text: 'fake' })).framed, false);
	assert.equal(decodeFinanceReport('prefix\n' + wire).framed, false);
	assert.equal(decodeFinanceReport(wire.replace('"finance_stream":1', '"finance_stream":2')).error, 'invalid');
	assert.equal(decodeFinanceReport(wire.replaceAll('\n', '\r\n'), true).body, body + '\nFinal sentence.');
	assert.equal(decodeFinanceReport(start.slice(0, -1)).framed, false);
});

test('bad, reordered, truncated and oversized frames fail closed without rendering wire text', () => {
	for (const text of [start + 'broken\n', start + start, wire + frame('body', { text: 'after summary' }), start + frame('summary', { result: 'not an object' })]) {
		assert.equal(decodeFinanceReport(text, true).error, 'invalid');
	}
	assert.equal(decodeFinanceReport(start + frame('body', { text: 'draft' }), true).error, 'truncated');
	assert.equal(decodeFinanceReport(wire.slice(0, -1), true).error, 'truncated');
	assert.equal(decodeFinanceReport('x'.repeat(REPORT_STREAM_LIMITS.wire + 1)).error, 'limit');
	assert.equal(decodeFinanceReport(start + 'x'.repeat(REPORT_STREAM_LIMITS.line + 1)).error, 'limit');
	assert.equal(decodeFinanceReport(start + frame('body', { text: 'x'.repeat(100_001) }).repeat(10)).error, 'limit');
	assert.equal(decodeFinanceReport(start + frame('body', { text: '' }).repeat(REPORT_STREAM_LIMITS.records)).error, 'limit');
});

test('card opens at first response, progresses to saving, and success requires terminal saved narrative', () => {
	assert.equal(derive({ text: start }).status, 'running');
	assert.equal(derive().status, 'saving');
	assert.equal(derive().artifact, null);
	assert.equal(derive({ state: 'success' }).status, 'incomplete');
	assert.equal(derive({ replyFinished: true }).status, 'incomplete');
	const final = derive({ state: 'success', envelope, text: 'compact response replaces the stream' });
	assert.equal(final.status, 'complete');
	assert.equal(final.text, artifact.text);
	assert.equal(final.artifact, artifact);
	assert.equal(final.streamError, null);
	assert.equal(derive({ state: 'running', envelope }).artifact, null);
});

test('errors, interruption, empty narrative and non-narrative results never produce saved/exportable drafts', () => {
	for (const [state, status] of [['error', 'failed'], ['interrupted', 'interrupted'], ['denied', 'interrupted']]) {
		const report = derive({ state, envelope });
		assert.equal(report.status, status);
		assert.equal(report.artifact, null);
	}
	for (const malformed of [{ ...artifact, text: '' }, { ...artifact, text: null }, { ...artifact, kind: 'table' }]) {
		assert.equal(derive({ state: 'success', envelope: { ...envelope, artifacts: [malformed] } }).status, 'incomplete');
	}
	assert.equal(derive({ state: 'success', envelope: { ...envelope, error_code: 'citation_failed' } }).status, 'failed');
	assert.equal(derive({ text: start + 'broken\n' }).status, 'incomplete');
});

test('report selection resolves current reply/tool pair, not copied draft or newest artifact', () => {
	const selection = { replyId: 'reply-1', toolCallId: 'same-tool-id' };
	const reports = new Map([
		[reportKey('reply-1', 'same-tool-id'), derive({ text: start })],
		[reportKey('reply-2', 'same-tool-id'), derive({ state: 'success', envelope })],
	]);
	const artifacts = new Map([[artifact.artifact_id, artifact]]);
	assert.equal(resolveFinanceReport(selection, reports, artifacts).text, '');
	reports.set(reportKey('reply-1', 'same-tool-id'), derive({ text: start + frame('body', { text: 'live growth' }) }));
	assert.equal(resolveFinanceReport(selection, reports, artifacts).text, 'live growth');
	reports.set(reportKey('reply-1', 'same-tool-id'), derive({ state: 'success', envelope }));
	assert.equal(resolveFinanceReport(selection, reports, artifacts).artifact.artifact_id, 'n1');
	assert.equal(resolveFinanceReport(selection, new Map(), artifacts), null);
	assert.equal(resolveFinanceReport({ artifactId: 'missing' }, reports, artifacts), null);
	assert.equal(resolveFinanceReport({ artifactId: 'n1' }, reports, artifacts).status, 'complete');
	assert.notEqual(reportKey('a:b', 'c'), reportKey('a', 'b:c'));
});

test('native renderer fallback identifies the exact unique call and refuses ambiguous reply IDs', () => {
	const reports = new Map([
		[reportKey('reply-1', 'call:a/b'), derive({ text: start })],
		[reportKey('newer-reply', 'different-call'), derive({ state: 'success', envelope })],
	]);
	const selection = findFinanceReportCall('call:a/b', reports);
	assert.deepEqual(selection, { replyId: 'reply-1', toolCallId: 'call:a/b' });
	assert.equal(resolveFinanceReport(selection, reports, new Map()).status, 'running');
	reports.set(reportKey('reply-1', 'call:a/b'), derive({ state: 'success', envelope }));
	assert.equal(resolveFinanceReport(selection, reports, new Map()).status, 'complete');
	assert.equal(findFinanceReportCall('missing-call', reports), null);
	reports.set(reportKey('another-reply', 'call:a/b'), derive());
	assert.equal(findFinanceReportCall('call:a/b', reports), null);
});

test('old narratives use compact presentation title without modifying immutable saved values', () => {
	const oldTitle = 'Please analyze the last four quarters, compare the amounts, calculate margins, and explain all changes with detailed citations.';
	assert.equal(financeReportTitle(oldTitle, 'Financial analysis', ['MSFT', 'AAPL']), 'AAPL / MSFT · Financial analysis');
	assert.equal(financeReportTitle('请分析 AAPL 最新数据', '财务分析', ['AAPL']), 'AAPL · 财务分析');
	assert.equal(financeReportTitle('AAPL 财务分析', '财务分析'), 'AAPL 财务分析');
	assert.equal(financeReportTitle(oldTitle, '财务分析'), '财务分析');
	assert.equal(artifact.title, 'AAPL 财务分析');
});

test('native text block fragments join without injecting line breaks', () => {
	assert.equal(reportResultText([{ type: 'text', text: wire.slice(0, 10) }, { type: 'image', text: 'ignore' }, { type: 'text', text: wire.slice(10) }]), wire);
	assert.equal(reportResultText(undefined), '');
});
