import assert from 'node:assert/strict';
import test from 'node:test';
import { artifactCompanyScope, artifactNotices, CONTACT_DIAGNOSTIC, CROSS_COMPANY_NOTICE, envelopeNotices, financeNoticeTranslation, normalizeFinanceNotices, SOURCE_LIMITATION } from '../src/lib/finance-notices.ts';

const oldCross = 'Cross-company quarters retain actual fiscal dates and can cover different calendar windows.';
const capex = 'MSFT capital expenditures use reported productive-asset cash purchases, including software/other intangibles where combined; PP&E-only issuers are not strictly scope-identical.';
const table = (id, company, warnings = []) => ({ artifact_id: id, kind: 'table', warnings, input_ids: [], citations: [], data: { rows: [{ company, cells: [] }], history_rows: [], sources: {}, warnings: [] } });
const narrative = (id, ids = []) => ({ artifact_id: id, kind: 'narrative', warnings: [oldCross], input_ids: ids, citations: [], data: null });
const a = table('a', 'AAPL', [oldCross, SOURCE_LIMITATION, CONTACT_DIAGNOSTIC]);
const m = table('m', 'MSFT', [CROSS_COMPANY_NOTICE]);
const n = narrative('n', ['a']);
const map = new Map([a, m, n].map((artifact) => [artifact.artifact_id, artifact]));

test('single company removes only known cross-company aliases, diagnostics stay historical and separate', () => {
	const notices = normalizeFinanceNotices([oldCross, CROSS_COMPANY_NOTICE, SOURCE_LIMITATION, SOURCE_LIMITATION, CONTACT_DIAGNOSTIC, capex, 'Missing cash flow', 'Unrecognized limitation'], artifactCompanyScope([a], map));
	assert.deepEqual(notices.limitations, [SOURCE_LIMITATION, capex, 'Missing cash flow', 'Unrecognized limitation']);
	assert.deepEqual(notices.diagnostics, [CONTACT_DIAGNOSTIC]);
	assert.equal(financeNoticeTranslation(capex).params.company, 'MSFT');
	assert.equal(financeNoticeTranslation('Unrecognized limitation'), null);
	assert.deepEqual(financeNoticeTranslation('AAPL: requested 4 quarters but only 2 identifiable periods are available'), { key: 'finance.notices.coverage', params: { company: 'AAPL', requested: '4', available: '2' } });
});

test('separately selected company tables and nested narrative/export dependencies share scope', () => {
	const combined = narrative('combined', ['a', 'm']);
	const exported = { ...narrative('exported', ['combined']), kind: 'export' };
	const all = new Map([...map, ['combined', combined], ['exported', exported]]);
	for (const selected of [[a, m], [combined], [exported]]) {
		const scope = artifactCompanyScope(selected, all);
		assert.equal(scope.complete, true);
		assert.deepEqual([...scope.companies].sort(), ['AAPL', 'MSFT']);
		assert.deepEqual(normalizeFinanceNotices([oldCross, CROSS_COMPANY_NOTICE], scope).limitations, [CROSS_COMPANY_NOTICE]);
	}
	assert.deepEqual(artifactNotices(n, all).limitations, []);
});

test('old citation dependencies count, missing scope is never silently assumed to be one company', () => {
	const cited = { ...narrative('cited'), citations: [{ artifact_id: 'a' }, { artifact_id: 'm' }] };
	assert.equal(artifactCompanyScope([cited], map).companies.size, 2);
	const missing = narrative('missing', ['a', 'unavailable']);
	const scope = artifactCompanyScope([missing], map);
	assert.equal(scope.complete, false);
	assert.deepEqual(normalizeFinanceNotices([oldCross], scope).limitations, [CROSS_COMPANY_NOTICE]);
	assert.equal(artifactCompanyScope([narrative('orphan')], map).complete, false);
	assert.equal(artifactCompanyScope([], map).complete, false);
});

test('scope uses selected rows, not wider inputs, history or unused source snapshots', () => {
	assert.deepEqual([...artifactCompanyScope([n], map).companies], ['AAPL']);
	const cycle = { ...narrative('cycle', ['a', 'cycle']), citations: [{ artifact_id: 'cycle' }] };
	assert.deepEqual([...artifactCompanyScope([cycle], new Map([...map, ['cycle', cycle]])).companies], ['AAPL']);
	const selected = table('selected', 'AAPL', [oldCross]);
	selected.input_ids = ['m', 'unavailable-parent'];
	selected.data.history_rows = [{ company: 'MSFT', cells: [] }];
	selected.data.sources = { sec: { company: 'MSFT' } };
	const scope = artifactCompanyScope([selected], map);
	assert.equal(scope.complete, true);
	assert.deepEqual([...scope.companies], ['AAPL']);
	assert.deepEqual(artifactNotices(selected, map).limitations, []);
});

test('artifact/table duplicates collapse; envelope suppresses notices represented inside artifacts', () => {
	const original = JSON.stringify(a);
	const duplicate = { ...a, data: { ...a.data, warnings: [SOURCE_LIMITATION, 'Table-only missing data'] } };
	assert.deepEqual(artifactNotices(duplicate, map).limitations, [SOURCE_LIMITATION, 'Table-only missing data']);
	assert.deepEqual(envelopeNotices([SOURCE_LIMITATION, CROSS_COMPANY_NOTICE, CONTACT_DIAGNOSTIC, 'Envelope-only failure'], [duplicate], map), { limitations: ['Envelope-only failure'], diagnostics: [] });
	assert.equal(JSON.stringify(a), original);
});

test('document company scope follows owned input and citation IDs, never unrelated history', () => {
	const filing = { artifact_id: 'owned-document', kind: 'document', document: { company: ' aapl ' }, warnings: [], input_ids: [], citations: [] };
	const docReport = { ...narrative('doc-report'), citations: [{ document_id: filing.artifact_id }] };
	const all = new Map([...map, [filing.artifact_id, filing], [docReport.artifact_id, docReport]]);
	for (const selected of [[filing], [docReport]]) {
		const scope = artifactCompanyScope(selected, all);
		assert.equal(scope.complete, true);
		assert.deepEqual([...scope.companies], ['AAPL']);
	}
	assert.equal(artifactCompanyScope([docReport], map).complete, false);
	assert.deepEqual([...artifactCompanyScope([n], all).companies], ['AAPL']);
});

test('provided document removes blanket missing-body statement only for that analysis, never historical numeric snapshots', () => {
	const filing = { artifact_id: 'doc', kind: 'document', document: { company: 'AAPL' }, warnings: [], input_ids: [], citations: [] };
	const report = { ...narrative('mixed', ['a', 'doc']), warnings: [SOURCE_LIMITATION, 'A specific source is missing'] };
	const all = new Map([...map, ['doc', filing], ['mixed', report]]);
	const before = JSON.stringify([a, report]);
	assert.deepEqual(artifactNotices(report, all).limitations, ['A specific source is missing']);
	assert.ok(artifactNotices(a, all).limitations.includes(SOURCE_LIMITATION));
	assert.deepEqual(envelopeNotices([SOURCE_LIMITATION], [report], all).limitations, []);
	assert.ok(artifactNotices({ ...report, input_ids: ['a'] }, all).limitations.includes(SOURCE_LIMITATION));
	assert.ok(artifactNotices(report, map).limitations.includes(SOURCE_LIMITATION));
	assert.equal(JSON.stringify([a, report]), before);
});

test('known wording has localization, unknown near-matches remain untouched', () => {
	for (const warning of [CROSS_COMPANY_NOTICE, SOURCE_LIMITATION, CONTACT_DIAGNOSTIC, capex]) assert.ok(financeNoticeTranslation(warning));
	const unknown = 'Cross-company periods need a custom adjustment for acquisition accounting.';
	assert.deepEqual(normalizeFinanceNotices([unknown], artifactCompanyScope([a], map)).limitations, [unknown]);
});
