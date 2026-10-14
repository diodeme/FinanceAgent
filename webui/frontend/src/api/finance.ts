import { client } from './client';

export interface CompanyInfo {
	company: string;
	cik: string;
	name: string;
	verified: boolean;
	supported: boolean;
	warning?: string | null;
}

export interface Evidence {
	source_id: string;
	fact_id: number | null;
	company: string;
	concept: string;
	metric: string;
	period_start: string | null;
	period_end: string;
	value: string;
	unit: string;
	accession: string;
	form: string;
	filed_at: string;
	retrieved_at: string;
	api_url: string;
	filing_url: string;
	document_status: 'metadata_only' | 'fetched' | 'unavailable';
	note?: string | null;
}

export interface FinanceCell {
	cell_id: string;
	metric: string;
	label: string;
	value: string | null;
	unit: string;
	status: 'available' | 'missing' | 'unsupported' | 'conflicting';
	formula?: string | null;
	source_ids: string[];
	inputs: Record<string, unknown>[];
	reason?: string | null;
}

export interface FinancialRow {
	company: string;
	period_start: string | null;
	period_end: string;
	period_label: string;
	cells: FinanceCell[];
}

export interface TableData {
	rows: FinancialRow[];
	sources: Record<string, Evidence>;
	history_rows: FinancialRow[];
	specification: Record<string, unknown>;
	warnings: string[];
	selection_policy: string;
}

export interface FinanceCitation {
	artifact_id: string;
	cell_id: string;
	label?: string;
}

export interface DocumentCitation {
	document_id: string;
	label?: string;
}

export type ArtifactCitation = FinanceCitation | DocumentCitation;

export interface FilingDocument {
	source_document_id: string;
	company: string;
	cik: string;
	form: string;
	period_end: string | null;
	filed_at?: string | null;
	accession: string;
	primary_document: string;
	source_url: string;
	sha256: string;
	byte_size: number;
	media_type: string;
	fetched_at: string;
}

export interface DocumentProvenance {
	artifact_id: string;
	title: string;
	document: FilingDocument;
	warnings: string[];
}

export interface FinanceArtifact {
	artifact_id: string;
	run_id: string;
	kind: 'dataset' | 'table' | 'narrative' | 'export' | 'document';
	title: string;
	created_at: string;
	input_ids: string[];
	data: TableData | null;
	text: string | null;
	citations: ArtifactCitation[];
	document?: FilingDocument | null;
	warnings: string[];
	filename: string | null;
	mime_type: string | null;
}

export interface FinanceEnvelope {
	schema_version: 1;
	partial?: boolean;
	message: string;
	artifacts: FinanceArtifact[];
	companies: CompanyInfo[];
	warnings: string[];
	error_code: string | null;
}

export interface CellProvenance {
	artifact_id: string;
	title: string;
	row: Omit<FinancialRow, 'cells'>;
	cell: FinanceCell;
	sources: Evidence[];
	warnings: string[];
}

// Fixed application endpoints only. Identity/authentication come from the same
// client (getUserId) as chat, never from a model-produced URL or owner ID.
async function get<T>(path: string, signal?: AbortSignal, params?: Record<string, string>) {
	const response = await client.stream(path, { signal, params, silent: true });
	return response.json() as Promise<T>;
}

export const financeApi = {
	bootstrap: () => client.post<{ agent_id: string; capabilities: Record<string, unknown> }>('/fin/bootstrap'),
	results: (sessionId: string, signal?: AbortSignal) =>
		get<{ artifacts: FinanceArtifact[] }>('/fin/results', signal, { session_id: sessionId, limit: '10' }),
	artifact: (id: string, signal?: AbortSignal) =>
		get<FinanceArtifact>(`/fin/results/${encodeURIComponent(id)}`, signal),
	cell: (citation: FinanceCitation, signal?: AbortSignal) =>
		get<CellProvenance>(`/fin/results/${encodeURIComponent(citation.artifact_id)}/cells/${encodeURIComponent(citation.cell_id)}`, signal),
	document: (id: string, signal?: AbortSignal) =>
		get<DocumentProvenance>(`/fin/results/${encodeURIComponent(id)}/document`, signal),
	download: async (id: string, signal?: AbortSignal) => {
		const response = await client.stream(`/fin/artifacts/${encodeURIComponent(id)}/download`, { signal, silent: true });
		return response.blob();
	},
};
