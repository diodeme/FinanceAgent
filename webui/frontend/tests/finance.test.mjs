import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';

import {
	FINANCE_TOOLS,
	financeCitationHref,
	financeCitationPlugin,
	financeToolStatus,
	formatFinanceValue,
	isFinanceArtifact,
	parseArtifactCitation,
	parseDocumentCitation,
	parseFinanceCitation,
	readFinanceEnvelope,
	requestedFinanceColumns,
	safeEvidenceUrl,
} from '../src/lib/finance.ts';

const cell = { cell_id: 'AAPL:revenue:2025-12-27', metric: 'revenue', label: 'Revenue', value: '9007199254740993.123456789', unit: 'USD', status: 'available', source_ids: ['s1'], inputs: [] };
const table = {
	rows: [{ company: 'AAPL', period_start: '2025-09-28', period_end: '2025-12-27', period_label: 'FY2026 Q1', cells: [cell, { ...cell, metric: 'cost_of_revenue', label: 'Cost', value: null, status: 'missing' }] }],
	history_rows: [{ company: 'AAPL', period_start: '2024-09-29', period_end: '2024-12-28', period_label: 'FY2025 Q1', cells: [{ ...cell, metric: 'history_only', label: 'History' }] }],
	specification: { requested_metrics: ['revenue'] },
	sources: {}, warnings: [], selection_policy: 'sec-latest-known-v1',
};
const artifact = { artifact_id: 'result-1', run_id: 'run-1', kind: 'dataset', title: 'Revenue', created_at: '2026-01-01T00:00:00Z', input_ids: [], data: table, text: null, citations: [], warnings: [], filename: null, mime_type: null };
const envelope = { schema_version: 1, message: 'Ready', artifacts: [artifact], companies: [], warnings: [], error_code: null };
const document = { source_document_id: 'shared-source', company: 'AAPL', cik: '0000320193', form: '10-Q', period_end: null, accession: '0000320193-26-000001', primary_document: 'filing.htm', source_url: 'https://www.sec.gov/Archives/edgar/data/320193/filing.htm', sha256: 'a'.repeat(64), byte_size: 1234, media_type: 'text/html', fetched_at: '2026-01-01T00:00:00Z' };
const filing = { ...artifact, artifact_id: 'owned/document %中', kind: 'document', data: null, document, citations: [] };

test('document artifacts and optional partial envelopes validate without changing old snapshots', () => {
	assert.equal(isFinanceArtifact(filing), true);
	assert.equal(isFinanceArtifact({ ...filing, document: { ...document, filed_at: '2026-01-02' } }), true);
	assert.equal(isFinanceArtifact({ ...filing, document: { ...document, filed_at: null } }), true);
	assert.equal(isFinanceArtifact({ ...filing, document: { ...document, filed_at: 42 } }), false);
	const partial = { ...envelope, artifacts: [filing], partial: true };
	assert.equal(readFinanceEnvelope({ finance: partial }), partial);
	assert.equal(readFinanceEnvelope({ finance: { ...partial, partial: 'true' } }), null);
	for (const document of [null, {}, { ...filing.document, sha256: 'invalid' }, { ...filing.document, byte_size: -1 }, { ...filing.document, byte_size: Infinity }]) {
		assert.equal(isFinanceArtifact({ ...filing, document }), false);
	}
	assert.equal(isFinanceArtifact({ ...artifact, document }), false);
	assert.equal(isFinanceArtifact({ ...artifact, document: null }), true);
	assert.equal(isFinanceArtifact({ ...artifact, citations: [{ document_id: filing.artifact_id }] }), true);
	assert.equal(isFinanceArtifact({ ...artifact, citations: [{ document_id: filing.artifact_id, cell_id: 'fake' }] }), false);
});

test('filing citations round-trip the owned artifact ID, not shared source or fake cell IDs', () => {
	const citation = { document_id: filing.artifact_id };
	const href = financeCitationHref(citation);
	assert.equal(href, '#filing/owned%2Fdocument%20%25%E4%B8%AD');
	assert.deepEqual(parseDocumentCitation(href), citation);
	assert.deepEqual(parseArtifactCitation(href), citation);
	assert.equal(parseFinanceCitation(href), null);
	for (const href of ['#filing/', '#filing/a/b', '#filing/%XX', '#filing/%00', '#filing/%7F', 'https://example.com/#filing/a', '#finance/a/b', undefined]) assert.equal(parseDocumentCitation(href), null);
	const link = { type: 'element', tagName: 'a', properties: { href } };
	financeCitationPlugin()({ type: 'root', children: [link] });
	assert.equal(link.tagName, 'finance-citation');
});

test('partial fetch keeps persisted documents accessible and old completion behavior', () => {
	const partial = { ...envelope, artifacts: [filing], partial: true, error_code: 'some_missing' };
	assert.equal(financeToolStatus('success', partial, true), 'partial');
	assert.equal(financeToolStatus('error', partial, true), 'partial');
	assert.equal(readFinanceEnvelope({ finance: partial }).artifacts[0], filing);
	assert.equal(financeToolStatus('success', envelope, true), 'complete');
	assert.equal(financeToolStatus('error', envelope, true), 'failed');
	assert.equal(financeToolStatus('interrupted', partial, true), 'interrupted');
	assert.equal(financeToolStatus('running', null, false), 'running');
});

test('reads the versioned terminal envelope, not accumulated tool text', () => {
	assert.equal(readFinanceEnvelope({ finance: envelope }), envelope);
	assert.equal(readFinanceEnvelope(JSON.stringify(envelope)), null);
	assert.equal(readFinanceEnvelope({ finance: JSON.stringify(envelope) }), null);
	assert.equal(readFinanceEnvelope({ finance: { ...envelope, schema_version: 2 } }), null);
	assert.equal(readFinanceEnvelope({ finance: { ...envelope, artifacts: [{}] } }), null);
	assert.equal(readFinanceEnvelope({ finance: { ...envelope, warnings: [null] } }), null);
});

test('citation components round-trip individually, including encoded separators', () => {
	const citation = { artifact_id: 'result/a b', cell_id: 'AAPL:收入/2025' };
	assert.deepEqual(parseFinanceCitation(financeCitationHref(citation)), citation);
	for (const href of ['https://example.com/#finance/a/b', '#normal', '#finance/a', '#finance/a/b/c', '#finance/%XX/b', '#finance/a/%00', undefined]) {
		assert.equal(parseFinanceCitation(href), null);
	}
});

test('filing UI is metadata and attachment only, with localized partial and activity labels', async () => {
	for (const path of ['../src/components/chat/finance/FilingDocumentDetails.tsx', '../src/components/chat/finance/FactProvenanceDrawer.tsx', '../src/components/chat/finance/FinanceResults.tsx']) {
		const source = await readFile(new URL(path, import.meta.url), 'utf8');
		assert.doesNotMatch(source, /dangerouslySetInnerHTML|<iframe|srcDoc\s*=/);
	}
	const context = await readFile(new URL('../src/context/FinancialResultsContext.tsx', import.meta.url), 'utf8');
	assert.match(context, /application\/octet-stream/);
	assert.match(context, /link\.download\s*=/);
	for (const language of ['en', 'zh']) {
		const { finance } = JSON.parse(await readFile(new URL(`../src/i18n/locales/${language}.json`, import.meta.url), 'utf8'));
		for (const label of [finance.status.partial, finance.tool.fetch_filings, finance.kind.document, finance.report.activity, finance.filingDocument.downloaded]) assert.ok(label);
	}
});

test('retains significant decimal digits and raw ratio units', () => {
	assert.equal(formatFinanceValue(cell.value, 'USD'), '9,007,199,254,740,993.123456789 USD');
	assert.equal(formatFinanceValue('-1200.0050', 'USD'), '-1,200.0050 USD');
	assert.equal(formatFinanceValue('0', 'USD'), '0 USD');
	assert.equal(formatFinanceValue(null, 'USD'), '—');
	assert.equal(formatFinanceValue('0.125', 'pure'), '0.125 pure');
	assert.equal(formatFinanceValue('12.50', 'percent'), '12.50%');
	assert.equal(formatFinanceValue('1.234567890123456789E+30', 'USD'), '1.234567890123456789E+30 USD');
});

test('shows requested columns only, never hidden history/dependency cells', () => {
	assert.deepEqual(requestedFinanceColumns(table), [['revenue', 'Revenue']]);
	assert.deepEqual(requestedFinanceColumns({ ...table, specification: {} }), [['revenue', 'Revenue'], ['cost_of_revenue', 'Cost']]);
	assert.deepEqual(requestedFinanceColumns(null), []);
});

test('external evidence links cannot execute script or use local schemes', () => {
	assert.equal(safeEvidenceUrl('https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json'), 'https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json');
	for (const url of ['javascript:alert(1)', 'data:text/html,hello', 'file:///secret', '/fin/results/a']) assert.equal(safeEvidenceUrl(url), undefined);
});

test('citation plugin preserves normal anchors and transforms only validated finance links', () => {
	const citation = { type: 'element', tagName: 'a', properties: { href: '#finance/result%2Fa/cell%3Ab' }, children: [{ type: 'text', value: 'Source' }] };
	const normal = { type: 'element', tagName: 'a', properties: { href: 'https://www.sec.gov/' } };
	const malformed = { type: 'element', tagName: 'a', properties: { href: '#finance/%XX/b' } };
	const tree = { type: 'root', children: [{ type: 'element', tagName: 'p', children: [citation, normal, malformed] }] };
	financeCitationPlugin()(tree);
	assert.equal(citation.tagName, 'finance-citation');
	assert.equal(citation.properties.href, '#finance/result%2Fa/cell%3Ab');
	assert.equal(citation.children[0].value, 'Source');
	assert.equal(normal.tagName, 'a');
	assert.equal(malformed.tagName, 'a');
});

test('only seven finance tools use the specialized rendering path', () => {
	assert.equal(FINANCE_TOOLS.size, 7);
	assert.equal(FINANCE_TOOLS.has('fetch_filings'), true);
	assert.equal(FINANCE_TOOLS.has('data_analyze'), true);
	assert.equal(FINANCE_TOOLS.has('UnknownTool'), false);
	assert.equal(FINANCE_TOOLS.has('Bash'), false);
});
