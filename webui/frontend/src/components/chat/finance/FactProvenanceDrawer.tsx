import { ExternalLink, Loader2 } from 'lucide-react';
import type { ReactNode } from 'react';

import type { CellProvenance, DocumentProvenance, Evidence } from '@/api/finance';
import { FilingDocumentDetails } from './FilingDocumentDetails';
import { Button } from '@/components/ui/button';
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet';
import { useTranslation } from '@/i18n/useI18n';
import { safeEvidenceUrl } from '@/lib/finance';
import { useFinancialResults } from '@/context/FinancialResultsContext';
import { artifactCompanyScope, normalizeFinanceNotices } from '@/lib/finance-notices';
import { FinanceNotices } from './FinanceNotices';

function EvidenceLink({ href, children }: { href: string; children: ReactNode }) {
	const safe = safeEvidenceUrl(href);
	return safe ? <a href={safe} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 underline underline-offset-2">
		{children}<ExternalLink className="size-3" />
	</a> : null;
}

function SourceEvidence({ source }: { source: Evidence }) {
	const { t } = useTranslation();
	const metadataNote = 'Official companyfacts evidence; filing URL is metadata only, original document was not fetched.';
	const capexNote = ' Productive-assets cash purchases include software/other intangibles as well as PP&E; not directly identical to PP&E-only capex.';
	// The document-status label already explains this exact known limitation.
	const note = source.document_status === 'metadata_only' && source.note === metadataNote ? null
		: source.note === metadataNote + capexNote ? t('finance.notices.capexEvidence') : source.note;
	return (
		<section className="space-y-2 rounded-md border p-3">
			<div className="font-medium break-words">{source.company} · {source.concept}</div>
			<dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs break-words">
				<dt className="text-muted-foreground">{t('finance.rawValue')}</dt><dd className="font-mono select-text">{source.value} {source.unit}</dd>
				<dt className="text-muted-foreground">{t('finance.period')}</dt><dd>{source.period_start ? `${source.period_start} → ` : ''}{source.period_end}</dd>
				<dt className="text-muted-foreground">{t('finance.filing')}</dt><dd>{source.form} · {source.filed_at}<br />{source.accession}</dd>
				<dt className="text-muted-foreground">{t('finance.retrieved')}</dt><dd>{source.retrieved_at}</dd>
				<dt className="text-muted-foreground">{t('finance.sourceId')}</dt><dd>{source.source_id}{source.fact_id != null ? ` · fact ${source.fact_id}` : ''}</dd>
			</dl>
			<div className="flex flex-wrap gap-3 text-xs">
				<EvidenceLink href={source.api_url}>{t('finance.apiEvidence')}</EvidenceLink>
				<EvidenceLink href={source.filing_url}>{t('finance.originalFiling')}</EvidenceLink>
			</div>
			<p className="text-xs text-muted-foreground">
				{t(`finance.document.${source.document_status}`, { defaultValue: t('finance.document.unavailable') })}
			</p>
			{note && <p className="text-xs text-muted-foreground">{note}</p>}
		</section>
	);
}

export function FactProvenanceDrawer({ open, onClose, loading, data, error, onRetry, restoreFocus }: {
	open: boolean;
	onClose: () => void;
	loading: boolean;
	data: CellProvenance | DocumentProvenance | null;
	error: string | null;
	onRetry: () => void;
	restoreFocus?: () => void;
}) {
	const { t } = useTranslation();
	const finance = useFinancialResults();
	const artifact = data && finance?.artifacts.get(data.artifact_id);
	const scope = artifact && finance ? artifactCompanyScope([artifact], finance.artifacts) : { companies: new Set<string>(), complete: false };
	return (
		<Sheet open={open} onOpenChange={(next) => !next && onClose()}>
			<SheetContent className="w-full sm:max-w-xl overflow-y-auto" data-testid="finance-provenance"
				onEscapeKeyDown={(event) => event.stopPropagation()}
				onCloseAutoFocus={(event) => { if (restoreFocus) { event.preventDefault(); restoreFocus(); } }}>
				<SheetHeader className="pr-12">
					<SheetTitle>{t(data && 'document' in data ? 'finance.filingDocument.viewSource' : 'finance.provenance')}</SheetTitle>
					<SheetDescription>{data?.title || t('finance.provenanceDescription')}</SheetDescription>
				</SheetHeader>
				<div className="px-4 pb-6 space-y-4 min-w-0">
					{loading && <p role="status" className="flex items-center gap-2"><Loader2 className="size-4 animate-spin" />{t('common.loading')}</p>}
					{error && <div role="alert" className="space-y-2"><p className="text-destructive break-words">{error}</p><Button size="sm" variant="outline" onClick={onRetry}>{t('finance.retry')}</Button></div>}
					{data && 'document' in data && <>
						<FilingDocumentDetails artifactId={data.artifact_id} document={data.document} />
						<FinanceNotices notices={normalizeFinanceNotices(data.warnings ?? [], { companies: new Set([data.document.company]), complete: true })} />
					</>}
					{data && 'cell' in data && <>
						<div className="space-y-1">
							<h3 className="font-medium">{data.row.company} · {data.cell.label}</h3>
							<p className="text-sm">{data.row.period_label} · {data.row.period_start ? `${data.row.period_start} → ` : ''}{data.row.period_end}</p>
							<p className="font-mono break-all">{data.cell.value ?? '—'} {data.cell.unit}</p>
							<p className="text-xs text-muted-foreground">{t(`finance.cell.${data.cell.status}`)}{data.cell.reason ? ` · ${data.cell.reason}` : ''}</p>
						</div>
						{data.cell.formula && <section className="space-y-1"><h4 className="font-medium">{t('finance.formula')}</h4><pre className="rounded bg-muted p-2 text-xs whitespace-pre-wrap break-words">{data.cell.formula}</pre></section>}
						{data.cell.inputs?.length > 0 && <section className="space-y-1"><h4 className="font-medium">{t('finance.inputs')}</h4><pre className="max-h-64 overflow-auto rounded bg-muted p-2 text-xs whitespace-pre-wrap break-words">{JSON.stringify(data.cell.inputs, null, 2)}</pre></section>}
						<FinanceNotices notices={normalizeFinanceNotices(data.warnings ?? [], scope)} />
						<section className="space-y-2"><h4 className="font-medium">{t('finance.sources')}</h4>
							{data.sources.length ? data.sources.map((source) => <SourceEvidence key={source.source_id} source={source} />) : <p className="text-sm text-muted-foreground">{t('finance.noSources')}</p>}
						</section>
					</>}
				</div>
			</SheetContent>
		</Sheet>
	);
}
