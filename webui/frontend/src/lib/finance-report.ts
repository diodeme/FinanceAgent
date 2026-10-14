import type { FinanceArtifact, FinanceEnvelope } from '../api/finance';

export const REPORT_STREAM_LIMITS = { wire: 2_000_000, line: 262_144, body: 1_000_000, records: 20_000, progress: 12, progressText: 2_000 };
export type ReportStatus = 'running' | 'saving' | 'complete' | 'failed' | 'interrupted' | 'incomplete';
export type ReportSelection = { replyId: string; toolCallId: string } | { artifactId: string };
export interface DecodedReport {
	framed: boolean;
	title: string;
	body: string;
	progress: string[];
	summary: boolean;
	error: 'invalid' | 'limit' | 'truncated' | null;
}
export interface FinanceReport {
	title: string;
	text: string;
	progress?: string[];
	status: ReportStatus;
	artifact: FinanceArtifact | null;
	error: string | null;
	streamError: DecodedReport['error'];
}

function record(value: unknown): value is Record<string, unknown> {
	return value !== null && typeof value === 'object' && !Array.isArray(value);
}

/** Decode accumulated native tool text. Stateless and bounded, so replacement of
 * the live wire text by the compact terminal response cannot corrupt a cache.
 * Only a complete first start record enables framing. JSON inside body strings
 * is data; it is never recursively parsed or searched for control markers. */
export function decodeFinanceReport(text: string, finished = false): DecodedReport {
	const result: DecodedReport = { framed: false, title: '', body: '', progress: [], summary: false, error: null };
	if (text.length > REPORT_STREAM_LIMITS.wire) return { ...result, error: 'limit' };
	let offset = 0;
	let count = 0;
	const body: string[] = [];
	let bodyLength = 0;
	while (offset < text.length) {
		const newline = text.indexOf('\n', offset);
		if (newline < 0) {
			if (text.length - offset > REPORT_STREAM_LIMITS.line) result.error = 'limit';
			else if (finished) result.error = 'truncated';
			break;
		}
		if (newline - offset > REPORT_STREAM_LIMITS.line || ++count > REPORT_STREAM_LIMITS.records) {
			result.error = 'limit'; break;
		}
		const line = text.slice(offset, newline).replace(/\r$/, '');
		offset = newline + 1;
		let frame: unknown;
		try { frame = JSON.parse(line); } catch { result.error = 'invalid'; break; }
		if (!record(frame) || frame.finance_stream !== 1 || result.summary) {
			result.error = 'invalid'; break;
		}
		if (!result.framed) {
			if (frame.type !== 'start' || typeof frame.title !== 'string' || !frame.title.trim() || frame.title.length > 160) {
				result.error = 'invalid'; break;
			}
			result.framed = true;
			result.title = frame.title;
		} else if (frame.type === 'progress' && typeof frame.text === 'string') {
			if (frame.text.length > REPORT_STREAM_LIMITS.progressText) { result.error = 'limit'; break; }
			if (frame.text.trim()) {
				result.progress.push(frame.text);
				if (result.progress.length > REPORT_STREAM_LIMITS.progress) result.progress.shift();
			}
		} else if (frame.type === 'body' && typeof frame.text === 'string') {
			bodyLength += frame.text.length;
			if (bodyLength > REPORT_STREAM_LIMITS.body) { result.error = 'limit'; break; }
			body.push(frame.text);
		} else if (frame.type === 'summary' && record(frame.result)) {
			result.summary = true;
		} else {
			result.error = 'invalid'; break;
		}
	}
	result.body = body.join('');
	if (finished && result.framed && !result.summary && !result.error) result.error = 'truncated';
	return result;
}

export function reportKey(replyId: string, toolCallId: string): string {
	return JSON.stringify([replyId, toolCallId]);
}

export function reportResultText(output: unknown): string {
	if (typeof output === 'string') return output;
	if (!Array.isArray(output)) return '';
	return output.filter((block) => record(block) && block.type === 'text' && typeof block.text === 'string').map((block) => block.text).join('');
}

export function validNarrative(artifact: FinanceArtifact): boolean {
	return artifact.kind === 'narrative' && !!artifact.artifact_id && typeof artifact.text === 'string' && !!artifact.text.trim();
}

export function deriveFinanceReport({ text, state, envelope, replyFinished }: {
	text: string; state?: string; envelope: FinanceEnvelope | null; replyFinished: boolean;
}): FinanceReport {
	const failed = state === 'error' || !!envelope?.error_code;
	const interrupted = state === 'interrupted' || state === 'denied';
	const artifact = state === 'success' && !failed ? envelope?.artifacts.find(validNarrative) ?? null : null;
	// The terminal persisted narrative overrides wire snapshots and draft errors.
	if (artifact) return { title: artifact.title, text: artifact.text!, status: 'complete', artifact, error: null, streamError: null };
	const terminal = replyFinished || (!!state && state !== 'running');
	const decoded = decodeFinanceReport(text, terminal);
	const status: ReportStatus = failed ? 'failed' : interrupted ? 'interrupted' : terminal || decoded.error ? 'incomplete' : decoded.summary ? 'saving' : 'running';
	return {
		title: decoded.title, text: decoded.body, progress: decoded.progress, status, artifact: null,
		error: failed ? envelope?.message || null : null, streamError: decoded.error,
	};
}

/** Short presentation-only fallback for old titles that contain writing requests. */
export function financeReportTitle(title: string, fallback: string, companies: Iterable<string> = []): string {
	const clean = title.trim();
	if (clean && clean.length <= 64 && !/[\r\n]/.test(clean) && !/^(please\b|write\b|analy[sz]e\b|compare\b|请|分析一下|根据|基于)/i.test(clean)) return clean;
	const names = [...new Set(companies)].sort();
	return names.length > 0 ? `${names.slice(0, 3).join(' / ')}${names.length > 3 ? ' …' : ''} · ${fallback}` : fallback;
}

/** Native tool renderers do not receive a reply ID. Recover it only when the
 * exact call ID is unique in this session; ambiguity never selects a newer reply. */
export function findFinanceReportCall(toolCallId: string, reports: ReadonlyMap<string, FinanceReport>): { replyId: string; toolCallId: string } | null {
	let selection: { replyId: string; toolCallId: string } | null = null;
	for (const key of reports.keys()) {
		const [replyId, id] = JSON.parse(key) as [string, string];
		if (id !== toolCallId) continue;
		if (selection) return null;
		selection = { replyId, toolCallId };
	}
	return selection;
}

export function resolveFinanceReport(selection: ReportSelection, reports: ReadonlyMap<string, FinanceReport>, artifacts: ReadonlyMap<string, FinanceArtifact>): FinanceReport | null {
	if ('replyId' in selection) return reports.get(reportKey(selection.replyId, selection.toolCallId)) ?? null;
	const artifact = artifacts.get(selection.artifactId);
	return artifact && validNarrative(artifact)
		? { title: artifact.title, text: artifact.text!, status: 'complete', artifact, error: null, streamError: null }
		: null;
}
