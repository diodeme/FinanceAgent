import type { Msg } from '@agentscope-ai/agentscope/message';
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';

import { financeApi } from '@/api/finance';
import type { ArtifactCitation, CellProvenance, DocumentProvenance, FinanceArtifact, ResultSummary } from '@/api/finance';
import { FactProvenanceDrawer } from '@/components/chat/finance/FactProvenanceDrawer';
import { FinanceReportPanel } from '@/components/chat/finance/FinanceReport';
import { FinanceResultPanel } from '@/components/chat/finance/FinanceResults';
import { useTranslation } from '@/i18n/useI18n';
import { isFinanceArtifact, mergeFinanceArtifacts, readFinanceEnvelope } from '@/lib/finance';
import { deriveFinanceReport, reportKey, reportResultText, resolveFinanceReport, validNarrative } from '@/lib/finance-report';
import type { FinanceReport, ReportSelection } from '@/lib/finance-report';

interface FinancialResultsContextValue {
	artifacts: Map<string, FinanceArtifact>;
	recoveredArtifacts: FinanceArtifact[];
	hydrationError: string | null;
	openCitation: (citation: ArtifactCitation) => void;
	openReport: (selection: ReportSelection) => void;
	openResult: (summary: ResultSummary) => void;
	reports: Map<string, FinanceReport>;
	downloadArtifact: (artifact: Pick<FinanceArtifact, 'artifact_id' | 'kind' | 'filename' | 'document'>) => Promise<void>;
}

const FinancialResultsContext = createContext<FinancialResultsContextValue | null>(null);

export function useFinancialResults() {
	return useContext(FinancialResultsContext);
}

/** Mounted with a user/session key in ChatContent. Unmount aborts all pending
 * provenance, hydration and download requests; no result crosses sessions.
 */
export function FinancialResultsProvider({ sessionId, messages, children }: {
	sessionId: string | null;
	messages: Msg[];
	children: ReactNode;
}) {
	const { t } = useTranslation();
	const [savedArtifacts, setSavedArtifacts] = useState<FinanceArtifact[]>([]);
	const [loadedArtifacts, setLoadedArtifacts] = useState(new Map<string, FinanceArtifact>());
	const [resultSelection, setResultSelection] = useState<ResultSummary | null>(null);
	const [resultLoading, setResultLoading] = useState(false);
	const [resultError, setResultError] = useState<string | null>(null);
	const resultRequest = useRef<AbortController | null>(null);
	const [hydrationError, setHydrationError] = useState<string | null>(null);
	const [citation, setCitation] = useState<ArtifactCitation | null>(null);
	const [provenance, setProvenance] = useState<CellProvenance | DocumentProvenance | null>(null);
	const [sourceError, setSourceError] = useState<string | null>(null);
	const [sourceLoading, setSourceLoading] = useState(false);
	const [reportSelection, setReportSelection] = useState<ReportSelection | null>(null);
	const citationOrigin = useRef<HTMLElement | null>(null);
	const reportOrigin = useRef<HTMLElement | null>(null);
	const pending = useRef(new Set<AbortController>());
	const sourceRequest = useRef<AbortController | null>(null);

	useEffect(() => {
		const requests = pending.current;
		const controller = new AbortController();
		requests.add(controller);
		if (sessionId) void financeApi.results(sessionId, controller.signal).then((response) => {
			if (!controller.signal.aborted) setSavedArtifacts(response.artifacts.filter(isFinanceArtifact));
		}).catch((error: unknown) => {
			if (!controller.signal.aborted) setHydrationError(error instanceof Error ? error.message : String(error));
		}).finally(() => requests.delete(controller));
		return () => {
			for (const request of requests) request.abort();
			requests.clear();
		};
	}, [sessionId]);

	const { artifacts, recoveredArtifacts } = useMemo(() => {
		const fromMessages = new Map<string, FinanceArtifact>();
		for (const message of messages) {
			for (const block of message.content) {
				if (block.type !== 'tool_result') continue;
				for (const artifact of readFinanceEnvelope(block.metadata)?.artifacts ?? []) {
					fromMessages.set(artifact.artifact_id, artifact);
				}
			}
		}
		return mergeFinanceArtifacts(savedArtifacts, [...fromMessages.values()], loadedArtifacts);
	}, [messages, savedArtifacts, loadedArtifacts]);

	const reports = useMemo(() => {
		const current = new Map<string, FinanceReport>();
		for (const message of messages) {
			const results = new Map(message.content.filter((block) => block.type === 'tool_result').map((block) => [block.id, block]));
			const ids = new Set(message.content.filter((block) => (block.type === 'tool_call' || block.type === 'tool_result') && block.name === 'data_analyze').map((block) => block.id));
			for (const id of ids) {
				const result = results.get(id);
				current.set(reportKey(message.id, id), deriveFinanceReport({
					text: reportResultText(result?.output), state: result?.state,
					envelope: readFinanceEnvelope(result?.metadata), replyFinished: !!message.finished_at,
				}));
			}
		}
		return current;
	}, [messages]);
	const selectedReport = resultSelection?.kind === 'narrative'
		? resolveFinanceReport({ artifactId: resultSelection.artifact_id }, reports, artifacts)
		: reportSelection ? resolveFinanceReport(reportSelection, reports, artifacts) : null;
	const openReport = useCallback((selection: ReportSelection) => {
		resultRequest.current?.abort();
		setResultSelection(null);
		setResultLoading(false);
		setResultError(null);
		reportOrigin.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
		setReportSelection(selection);
	}, []);

	const openCitation = useCallback((next: ArtifactCitation) => {
		if (!sourceRequest.current || sourceRequest.current.signal.aborted) {
			citationOrigin.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
		}
		sourceRequest.current?.abort();
		const controller = new AbortController();
		sourceRequest.current = controller;
		pending.current.add(controller);
		setCitation(next);
		setProvenance(null);
		setSourceError(null);
		setSourceLoading(true);
		// Server authorizes both IDs, including cross-turn narrative citations.
		const request = 'document_id' in next ? financeApi.document(next.document_id, controller.signal) : financeApi.cell(next, controller.signal);
		void request.then((response) => {
			if (!controller.signal.aborted) setProvenance(response);
		}).catch((error: unknown) => {
			if (!controller.signal.aborted) setSourceError(error instanceof Error ? error.message : String(error));
		}).finally(() => {
			pending.current.delete(controller);
			if (!controller.signal.aborted) setSourceLoading(false);
		});
	}, []);

	const closeCitation = useCallback(() => {
		sourceRequest.current?.abort();
		setCitation(null);
		setProvenance(null);
		setSourceError(null);
		setSourceLoading(false);
	}, []);

	const loadResult = useCallback((summary: ResultSummary) => {
		resultRequest.current?.abort();
		setResultError(null);
		if (artifacts.has(summary.artifact_id)) { setResultLoading(false); return; }
		const controller = new AbortController();
		resultRequest.current = controller;
		pending.current.add(controller);
		setResultLoading(true);
		void financeApi.artifact(summary.artifact_id, controller.signal).then((artifact) => {
			if (controller.signal.aborted) return;
			if (!isFinanceArtifact(artifact) || artifact.artifact_id !== summary.artifact_id || artifact.kind !== summary.kind ||
				(artifact.kind === 'narrative' && !validNarrative(artifact))) throw new Error(t('finance.catalog.invalid'));
			setLoadedArtifacts((current) => new Map(current).set(artifact.artifact_id, artifact));
		}).catch((error: unknown) => {
			if (!controller.signal.aborted) setResultError(error instanceof Error ? error.message : String(error));
		}).finally(() => {
			pending.current.delete(controller);
			if (!controller.signal.aborted) setResultLoading(false);
		});
	}, [artifacts, t]);

	const openResult = useCallback((summary: ResultSummary) => {
		reportOrigin.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
		closeCitation();
		setReportSelection(null);
		setResultSelection(summary);
		loadResult(summary);
	}, [closeCitation, loadResult]);

	const closeReport = useCallback(() => {
		resultRequest.current?.abort();
		setReportSelection(null);
		setResultSelection(null);
		setResultLoading(false);
		setResultError(null);
	}, []);

	const downloadArtifact = useCallback(async (artifact: Pick<FinanceArtifact, 'artifact_id' | 'kind' | 'filename' | 'document'>) => {
		const controller = new AbortController();
		pending.current.add(controller);
		try {
			const downloaded = await financeApi.download(artifact.artifact_id, controller.signal);
			// A filing is an attachment, never app-origin HTML (even if a server or
			// intermediary supplies an unexpected Content-Type).
			const blob = artifact.kind === 'document' ? new Blob([downloaded], { type: 'application/octet-stream' }) : downloaded;
			if (controller.signal.aborted) return;
			const url = URL.createObjectURL(blob);
			const link = document.createElement('a');
			link.href = url;
			link.download = (artifact.filename || (artifact.kind === 'document' ? artifact.document?.primary_document || 'filing.html' : 'finance-report.xlsx')).replace(/[\\/\u0000-\u001f\u007f]/g, '_');
			document.body.appendChild(link);
			link.click();
			link.remove();
			// Do not revoke until the browser has consumed the click.
			setTimeout(() => URL.revokeObjectURL(url), 1000);
		} finally {
			pending.current.delete(controller);
		}
	}, []);

	const value = useMemo(() => ({ artifacts, recoveredArtifacts, hydrationError, openCitation, openReport, openResult, reports, downloadArtifact }),
		[artifacts, recoveredArtifacts, hydrationError, openCitation, openReport, openResult, reports, downloadArtifact]);
	const sourceDrawer = <FactProvenanceDrawer open={citation !== null} onClose={closeCitation} loading={sourceLoading}
		data={provenance} error={sourceError} onRetry={() => citation && openCitation(citation)}
		restoreFocus={() => citationOrigin.current?.isConnected && citationOrigin.current.focus()} />;

	return (
		<FinancialResultsContext.Provider value={value}>
			{children}
			{resultSelection && resultSelection.kind !== 'narrative' ? <FinanceResultPanel summary={resultSelection} artifact={artifacts.get(resultSelection.artifact_id)}
				loading={resultLoading} error={resultError} onRetry={() => loadResult(resultSelection)} onClose={closeReport}
				sourceOpen={citation !== null} restoreFocus={() => reportOrigin.current?.isConnected && reportOrigin.current.focus()}>
				{sourceDrawer}
			</FinanceResultPanel> : reportSelection || resultSelection ? <FinanceReportPanel report={selectedReport} open onClose={closeReport}
				savedResult={resultSelection ? { title: resultSelection.title, loading: resultLoading, error: resultError, onRetry: () => loadResult(resultSelection) } : undefined}
				sourceOpen={citation !== null} restoreFocus={() => reportOrigin.current?.isConnected && reportOrigin.current.focus()}>
				{sourceDrawer}
			</FinanceReportPanel> : sourceDrawer}
		</FinancialResultsContext.Provider>
	);
}
