import type { ArtifactCitation, DocumentCitation, FilingDocument, FinanceArtifact, FinanceCitation, FinanceEnvelope, TableData } from '../api/finance';

export const FINANCE_TOOLS = new Set([
	'lookup_company', 'data_fetch', 'fetch_filings', 'calculate_metrics', 'data_analyze', 'find_results', 'export_report',
]);

function record(value: unknown): value is Record<string, unknown> {
	return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function validId(value: unknown): value is string {
	return typeof value === 'string' && value.length > 0 && !/[\u0000-\u001f\u007f]/.test(value);
}

export function isFilingDocument(value: unknown): value is FilingDocument {
	return record(value) && ['source_document_id', 'company', 'cik', 'form', 'accession', 'primary_document', 'source_url', 'media_type', 'fetched_at'].every((key) => validId(value[key])) &&
		(value.period_end === null || typeof value.period_end === 'string') &&
		(value.filed_at == null || typeof value.filed_at === 'string') &&
		typeof value.sha256 === 'string' && /^[a-f\d]{64}$/i.test(value.sha256) &&
		typeof value.byte_size === 'number' && Number.isSafeInteger(value.byte_size) && value.byte_size >= 0;
}

export function isArtifactCitation(value: unknown): value is ArtifactCitation {
	if (!record(value) || (value.label != null && typeof value.label !== 'string')) return false;
	return 'document_id' in value
		? validId(value.document_id) && !('artifact_id' in value) && !('cell_id' in value)
		: validId(value.artifact_id) && validId(value.cell_id);
}

export function isFinanceArtifact(value: unknown): value is FinanceArtifact {
	if (!record(value) || !validId(value.artifact_id) ||
		typeof value.title !== 'string' || !['dataset', 'table', 'narrative', 'export', 'document'].includes(String(value.kind))) return false;
	if (value.kind === 'document' ? !isFilingDocument(value.document) : value.document != null) return false;
	if (value.citations != null && (!Array.isArray(value.citations) || !value.citations.every(isArtifactCitation))) return false;
	if (value.data != null) {
		if (!record(value.data) || !Array.isArray(value.data.rows)) return false;
		if (!value.data.rows.every((row: unknown) => record(row) && typeof row.company === 'string' &&
			Array.isArray(row.cells) && row.cells.every((cell: unknown) => record(cell) &&
				typeof cell.cell_id === 'string' && typeof cell.metric === 'string' &&
				(cell.value === null || typeof cell.value === 'string')))) return false;
	}
	return true;
}

/** Artifact envelopes come only from terminal metadata, never from report stream records. */
export function readFinanceEnvelope(metadata: unknown): FinanceEnvelope | null {
	if (!record(metadata) || !record(metadata.finance)) return null;
	const value = metadata.finance;
	if (value.schema_version !== 1 || (value.partial !== undefined && typeof value.partial !== 'boolean') || !Array.isArray(value.artifacts) ||
		!value.artifacts.every(isFinanceArtifact) || !Array.isArray(value.companies) ||
		!value.companies.every((company: unknown) => record(company) && typeof company.company === 'string' && typeof company.name === 'string') ||
		!Array.isArray(value.warnings) || !value.warnings.every((warning) => typeof warning === 'string') ||
		typeof value.message !== 'string') return null;
	return value as unknown as FinanceEnvelope;
}

export function parseFinanceCitation(href: string | undefined): FinanceCitation | null {
	const match = href?.match(/^#finance\/([^/]+)\/([^/]+)$/);
	if (!match) return null;
	try {
		const artifact_id = decodeURIComponent(match[1]);
		const cell_id = decodeURIComponent(match[2]);
		if (!artifact_id || !cell_id || /[\u0000-\u001f\u007f]/.test(artifact_id + cell_id)) return null;
		return { artifact_id, cell_id };
	} catch {
		return null;
	}
}

export function parseDocumentCitation(href: string | undefined): DocumentCitation | null {
	const match = href?.match(/^#filing\/([^/]+)$/);
	if (!match) return null;
	try {
		const document_id = decodeURIComponent(match[1]);
		return validId(document_id) ? { document_id } : null;
	} catch { return null; }
}

export function parseArtifactCitation(href: string | undefined): ArtifactCitation | null {
	return parseFinanceCitation(href) ?? parseDocumentCitation(href);
}

export function financeCitationHref(citation: ArtifactCitation): string {
	return 'document_id' in citation ? `#filing/${encodeURIComponent(citation.document_id)}`
		: `#finance/${encodeURIComponent(citation.artifact_id)}/${encodeURIComponent(citation.cell_id)}`;
}

export function financeToolStatus(state: string | undefined, envelope: FinanceEnvelope | null, replyFinished: boolean) {
	if (state === 'interrupted' || state === 'denied') return 'interrupted';
	// A partial batch still exposes each successfully persisted document.
	if (envelope?.partial) return 'partial';
	if (state === 'error' || envelope?.error_code) return 'failed';
	if (state === 'success' && envelope) return 'complete';
	return !replyFinished && (!state || state === 'running') ? 'running' : 'incomplete';
}

interface MarkdownNode {
	type: string;
	tagName?: string;
	properties?: Record<string, unknown>;
	children?: MarkdownNode[];
}

/** Run after the existing sanitizers. Only validated citation anchors get a
 * dedicated component; ordinary links keep Streamdown's own safety renderer.
 */
export function financeCitationPlugin() {
	return (tree: MarkdownNode) => {
		const visit = (node: MarkdownNode) => {
			const href = node.properties?.href;
			if (node.type === 'element' && node.tagName === 'a' &&
				typeof href === 'string' && parseArtifactCitation(href)) {
				node.tagName = 'finance-citation';
			}
			for (const child of node.children ?? []) visit(child);
		};
		visit(tree);
	};
}

/** Group decimal strings without ever coercing financial amounts to Number.
 * Ratios ('pure') stay ratios: unit alone cannot distinguish a margin from a multiple.
 * Only explicit percent units get a % suffix, with no implicit multiplication.
 */
export function formatFinanceValue(value: string | null, unit: string): string {
	if (value === null) return '—';
	const match = value.match(/^([+-]?)(\d+)(\.\d+)?$/);
	const amount = match ? `${match[1]}${match[2].replace(/\B(?=(\d{3})+(?!\d))/g, ',')}${match[3] ?? ''}` : value;
	if (unit === '%' || unit === 'percent') return `${amount}%`;
	return unit ? `${amount} ${unit}` : amount;
}

export function requestedFinanceColumns(table: TableData | null): [string, string][] {
	const requested = table?.specification?.requested_metrics;
	const metrics = Array.isArray(requested) && requested.length > 0
		? new Set(requested.filter((metric): metric is string => typeof metric === 'string')) : null;
	return [...new Map((table?.rows ?? []).flatMap((row) => row.cells)
		.filter((cell) => !metrics || metrics.has(cell.metric))
		.map((cell) => [cell.metric, cell.label])).entries()];
}

export function safeEvidenceUrl(value: string): string | undefined {
	try {
		const url = new URL(value);
		return url.protocol === 'https:' || url.protocol === 'http:' ? url.href : undefined;
	} catch {
		return undefined;
	}
}
