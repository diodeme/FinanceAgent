import { Download, Loader2, TriangleAlert } from 'lucide-react';
import { useState } from 'react';

import { getResultText } from '../tool-renderers/_shared';
import type { ToolCallWithResult } from '../tool-renderers/types';
import type { FinanceArtifact } from '@/api/finance';
import { FinanceNotices } from './FinanceNotices';
import { FilingDocumentDetails } from './FilingDocumentDetails';
import { FinanceReportCard } from './FinanceReport';
import { artifactNotices, envelopeNotices } from '@/lib/finance-notices';
import { deriveFinanceReport, findFinanceReportCall, reportKey, reportResultText, validNarrative } from '@/lib/finance-report';
import { Button } from '@/components/ui/button';
import { useFinancialResults } from '@/context/FinancialResultsContext';
import { useTranslation } from '@/i18n/useI18n';
import { financeCitationHref, financeToolStatus, formatFinanceValue, readFinanceEnvelope, requestedFinanceColumns } from '@/lib/finance';

export function FinanceArtifactCard({ artifact }: { artifact: FinanceArtifact }) {
	const finance = useFinancialResults();
	const { t } = useTranslation();
	const [downloading, setDownloading] = useState(false);
	const [downloadError, setDownloadError] = useState<string | null>(null);
	const table = artifact.data;
	// Dependencies/history are useful provenance, not requested result columns.
	const columns = requestedFinanceColumns(table);

	const download = async () => {
		if (!finance) return;
		setDownloading(true);
		setDownloadError(null);
		try {
			await finance.downloadArtifact(artifact);
		} catch (error) {
			if (!(error instanceof DOMException && error.name === 'AbortError')) setDownloadError(error instanceof Error ? error.message : String(error));
		} finally {
			setDownloading(false);
		}
	};

	if (artifact.kind === 'narrative') {
		const saved = validNarrative(artifact);
		return <FinanceReportCard selection={{ artifactId: artifact.artifact_id }} report={{
			title: artifact.title, text: saved ? artifact.text! : '', status: saved ? 'complete' : 'incomplete',
			artifact: saved ? artifact : null, error: null, streamError: null,
		}} />;
	}

	return (
		<section className="min-w-0 space-y-3 rounded-lg border bg-background p-3 text-sm" data-finance-artifact={artifact.artifact_id}>
			<header className="flex flex-wrap items-start justify-between gap-2">
				<div className="min-w-0"><h3 className="font-medium break-words">{artifact.title}</h3><p className="text-xs text-muted-foreground">{t(`finance.kind.${artifact.kind}`)}{artifact.created_at ? ` · ${artifact.created_at.replace('T', ' ').slice(0, 19)}` : ''}</p></div>
				{artifact.kind === 'export' && <Button size="sm" variant="outline" disabled={downloading || !finance} onClick={() => void download()}>
					{downloading ? <Loader2 className="size-4 animate-spin" /> : <Download className="size-4" />}{t('finance.download')}
				</Button>}
			</header>
			{downloadError && <p role="alert" className="text-xs text-destructive">{downloadError}</p>}
			{artifact.kind === 'export' && artifact.filename && <p className="break-all text-xs text-muted-foreground">{artifact.filename}</p>}
			{artifact.kind === 'document' && artifact.document && <FilingDocumentDetails artifactId={artifact.artifact_id} document={artifact.document} showProvenance />}
			{table && <>
				<div className="w-full overflow-x-auto">
					<table className="w-full text-xs">
						<thead><tr className="border-b text-left"><th className="p-2 font-medium">{t('finance.company')}</th><th className="p-2 font-medium">{t('finance.period')}</th>{columns.map(([metric, label]) => <th key={metric} className="p-2 text-right font-medium whitespace-nowrap">{label}</th>)}</tr></thead>
						<tbody>{table.rows.map((row, index) => <tr key={`${row.company}:${row.period_end}:${index}`} className="border-b last:border-0">
							<td className="p-2 align-top whitespace-nowrap">{row.company}</td>
							<td className="p-2 align-top whitespace-nowrap"><div>{row.period_label}</div><div className="text-muted-foreground">{row.period_start ? `${row.period_start} → ` : ''}{row.period_end}</div></td>
							{columns.map(([metric]) => {
								const cell = row.cells.find((item) => item.metric === metric);
								return <td key={metric} className="p-2 text-right align-top">
									{cell ? <button type="button" disabled={!finance} className="cursor-pointer text-right underline decoration-dotted underline-offset-4 hover:text-primary disabled:cursor-default focus-visible:outline-2 focus-visible:outline-ring" title={t('finance.viewSource')}
										onClick={() => finance?.openCitation({ artifact_id: artifact.artifact_id, cell_id: cell.cell_id })}>
										<span className="font-mono tabular-nums whitespace-nowrap">{formatFinanceValue(cell.value, cell.unit)}</span>
										{cell.status !== 'available' && <span className="block text-muted-foreground">{t(`finance.cell.${cell.status}`)}</span>}
									</button> : '—'}
								</td>;
							})}
						</tr>)}</tbody>
					</table>
				</div>
				<p className="text-xs text-muted-foreground">{t('finance.tableHint')}</p>
			</>}
			{artifact.citations?.length > 0 && <nav aria-label={t('finance.sources')} className="flex flex-wrap gap-x-3 gap-y-1 text-xs">
				{artifact.citations.map((citation, index) => <a key={`${financeCitationHref(citation)}:${index}`} href={financeCitationHref(citation)} className="underline underline-offset-2" onClick={(event) => {
					if (finance) { event.preventDefault(); finance.openCitation(citation); }
				}}>{citation.label || `${t('finance.sources')} ${index + 1}`}</a>)}
			</nav>}
			<FinanceNotices notices={artifactNotices(artifact, finance?.artifacts ?? new Map([[artifact.artifact_id, artifact]]))} />
		</section>
	);
}

export function FinanceToolResult({ pair, replyFinished = false, replyId, artifacts }: {
	pair: ToolCallWithResult;
	replyFinished?: boolean;
	replyId?: string;
	artifacts?: FinanceArtifact[];
}) {
	const { t } = useTranslation();
	const finance = useFinancialResults();
	const envelope = readFinanceEnvelope(pair.result?.metadata);
	const state = pair.result?.state;
	const status = financeToolStatus(state, envelope, replyFinished);
	const failed = status === 'failed';
	const interrupted = status === 'interrupted';
	const pending = status === 'running';
	const text = getResultText(pair.result);
	const results = artifacts ?? envelope?.artifacts ?? [];
	const artifactMap = finance?.artifacts ?? new Map(results.map((artifact) => [artifact.artifact_id, artifact]));
	const notices = envelopeNotices(envelope?.warnings ?? [], envelope?.artifacts ?? [], artifactMap);
	if (pair.call.name === 'data_analyze') {
		const callSelection = replyId ? { replyId, toolCallId: pair.call.id } : findFinanceReportCall(pair.call.id, finance?.reports ?? new Map());
		const report = (callSelection && finance?.reports.get(reportKey(callSelection.replyId, callSelection.toolCallId))) || deriveFinanceReport({
			text: reportResultText(pair.result?.output), state, envelope, replyFinished,
		});
		return <div className="min-w-0 space-y-2" data-finance-tool={pair.call.name} data-finance-status={report.status}>
			<FinanceReportCard report={report} selection={callSelection ?? (report.artifact ? { artifactId: report.artifact.artifact_id } : null)} />
			<FinanceNotices notices={notices} />
		</div>;
	}
	return (
		<div className="min-w-0 space-y-2 text-sm" data-finance-tool={pair.call.name} data-finance-status={status}>
			<div className="flex items-center gap-2 text-xs text-muted-foreground" role="status">
				{pending && <Loader2 className="size-3 animate-spin" />}
				{(failed || interrupted || status === 'incomplete' || status === 'partial') && <TriangleAlert className="size-3" />}
				<span>{t(`finance.tool.${pair.call.name}`)} · {t(`finance.status.${status}`)}</span>
			</div>
			{envelope?.message && (failed || status === 'partial' || !results.length) && <p role={failed ? 'alert' : undefined} className={failed ? 'text-destructive' : 'text-muted-foreground'}>{envelope.message}</p>}
			{!envelope && text && <div className="rounded-md border bg-muted/30 p-3">
				<p className="max-h-40 overflow-auto whitespace-pre-wrap break-words text-xs" role="log">{text}</p>
			</div>}
			{envelope?.companies.map((company) => <div key={company.company} className="rounded-md border p-2 text-xs">
				<span className="font-medium">{company.company} · {company.name}</span> · CIK {company.cik}
				<span className="ml-2 text-muted-foreground">{t(company.supported && company.verified ? 'finance.verified' : 'finance.notVerified')}</span>
				{company.warning && <p className="mt-1 text-muted-foreground">{company.warning}</p>}
			</div>)}
			{results.map((artifact) => <FinanceArtifactCard key={artifact.artifact_id} artifact={artifact} />)}
			<FinanceNotices notices={notices} />
		</div>
	);
}

/** Separate from the default collapsed tool groups; deduplicate within a reply. */
export function FinanceBubbleResults({ calls, replyFinished, replyId }: { calls: ToolCallWithResult[]; replyFinished: boolean; replyId: string }) {
	const seen = new Set<string>();
	return calls.length > 0 ? <div className="w-full min-w-0 space-y-4 py-2">{calls.map((pair) => {
		const artifacts = (readFinanceEnvelope(pair.result?.metadata)?.artifacts ?? []).filter((artifact) => {
			if (seen.has(artifact.artifact_id)) return false;
			seen.add(artifact.artifact_id);
			return true;
		});
		return <FinanceToolResult key={pair.call.id} pair={pair} replyFinished={replyFinished} replyId={replyId} artifacts={artifacts} />;
	})}</div> : null;
}

/** Recover recent committed results even if a reply was interrupted after saving,
 * or the persisted message history no longer contains its tool metadata.
 */
export function RecoveredFinanceResults() {
	const finance = useFinancialResults();
	const { t } = useTranslation();
	if (!finance || (!finance.recoveredArtifacts.length && !finance.hydrationError)) return null;
	return <section className="w-full min-w-0 space-y-3 py-3" aria-label={t('finance.savedResults')}>
		{finance.recoveredArtifacts.length > 0 && <><h3 className="text-xs text-muted-foreground">{t('finance.savedResults')}</h3>{finance.recoveredArtifacts.map((artifact) => <FinanceArtifactCard key={artifact.artifact_id} artifact={artifact} />)}</>}
		{finance.hydrationError && <p role="status" className="text-xs text-muted-foreground">{t('finance.hydrationFailed')} {finance.hydrationError}</p>}
	</section>;
}
