import { Download, ExternalLink, Loader2 } from 'lucide-react';
import { useState } from 'react';

import type { FilingDocument } from '@/api/finance';
import { Button } from '@/components/ui/button';
import { useFinancialResults } from '@/context/FinancialResultsContext';
import { useTranslation } from '@/i18n/useI18n';
import { safeEvidenceUrl } from '@/lib/finance';

/** Render metadata only. The original HTML is available solely as an authenticated
 * attachment, never fetched for an iframe, preview or HTML injection. */
export function FilingDocumentDetails({ artifactId, document, showProvenance = false }: {
	artifactId: string; document: FilingDocument; showProvenance?: boolean;
}) {
	const finance = useFinancialResults();
	const { t } = useTranslation();
	const [downloading, setDownloading] = useState(false);
	const [error, setError] = useState<string | null>(null);
	const source = safeEvidenceUrl(document.source_url);
	const download = async () => {
		if (!finance) return;
		setDownloading(true);
		setError(null);
		try {
			await finance.downloadArtifact({ artifact_id: artifactId, kind: 'document', filename: document.primary_document, document });
		} catch (cause) {
			if (!(cause instanceof DOMException && cause.name === 'AbortError')) setError(cause instanceof Error ? cause.message : String(cause));
		} finally { setDownloading(false); }
	};
	return <section className="space-y-3 text-xs" data-finance-document={artifactId}>
		<dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 break-words">
			<dt className="text-muted-foreground">{t('finance.company')}</dt><dd>{document.company} · CIK {document.cik}</dd>
			<dt className="text-muted-foreground">{t('finance.filing')}</dt><dd>{document.form} · {document.accession}</dd>
			<dt className="text-muted-foreground">{t('finance.period')}</dt><dd>{document.period_end ?? t('finance.filingDocument.unknownPeriod')}</dd>
			{document.filed_at && <><dt className="text-muted-foreground">{t('finance.filed')}</dt><dd>{document.filed_at}</dd></>}
			<dt className="text-muted-foreground">{t('finance.filingDocument.primary')}</dt><dd className="break-all">{document.primary_document}</dd>
			<dt className="text-muted-foreground">SHA-256</dt><dd className="font-mono break-all select-text">{document.sha256}</dd>
			<dt className="text-muted-foreground">{t('finance.filingDocument.size')}</dt><dd>{document.byte_size} · {document.media_type}</dd>
			<dt className="text-muted-foreground">{t('finance.retrieved')}</dt><dd>{document.fetched_at}</dd>
			<dt className="text-muted-foreground">{t('finance.sourceId')}</dt><dd className="break-all">{document.source_document_id}</dd>
		</dl>
		<p className="text-muted-foreground">{t('finance.filingDocument.downloaded')}</p>
		<div className="flex flex-wrap items-center gap-3">
			{source?.startsWith('https:') && <a href={source} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 underline underline-offset-2">{t('finance.originalFiling')}<ExternalLink className="size-3" /></a>}
			{showProvenance && <Button size="sm" variant="outline" disabled={!finance} onClick={() => finance?.openCitation({ document_id: artifactId })}>{t('finance.filingDocument.viewSource')}</Button>}
			<Button size="sm" variant="outline" disabled={!finance || downloading} onClick={() => void download()}>{downloading ? <Loader2 className="size-4 animate-spin" /> : <Download className="size-4" />}{t('finance.filingDocument.download')}</Button>
		</div>
		{error && <p role="alert" className="text-destructive break-words">{error}</p>}
	</section>;
}
