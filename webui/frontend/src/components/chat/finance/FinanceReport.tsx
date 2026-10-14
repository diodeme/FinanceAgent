import { FileText, Loader2, TriangleAlert } from 'lucide-react';
import type { ReactNode } from 'react';

import { FinanceNotices } from './FinanceNotices';
import { Markdown } from '@/components/markdown';
import { Button } from '@/components/ui/button';
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet';
import { useFinancialResults } from '@/context/FinancialResultsContext';
import { useTranslation } from '@/i18n/useI18n';
import { artifactCompanyScope, artifactNotices } from '@/lib/finance-notices';
import { financeReportTitle } from '@/lib/finance-report';
import type { FinanceReport, ReportSelection } from '@/lib/finance-report';

function useReportTitle(report: FinanceReport | null) {
	const finance = useFinancialResults();
	const { t } = useTranslation();
	const companies = report?.artifact && finance ? artifactCompanyScope([report.artifact], finance.artifacts).companies : [];
	return financeReportTitle(report?.title ?? '', t('finance.report.title'), companies);
}

export function FinanceReportCard({ report, selection }: { report: FinanceReport; selection: ReportSelection | null }) {
	const finance = useFinancialResults();
	const { t } = useTranslation();
	const title = useReportTitle(report);
	const pending = report.status === 'running' || report.status === 'saving';
	return <section className="flex min-w-0 items-center gap-3 rounded-lg border bg-background p-3 text-sm"
		data-finance-report-card data-finance-report-progress-count={report.progress?.length ?? 0} data-finance-artifact={report.artifact?.artifact_id} data-finance-status={report.status} data-finance-report-text-length={report.text.length}>
		<FileText className="size-6 shrink-0 text-muted-foreground" />
		<div className="min-w-0 flex-1">
			<h3 className="font-medium break-words">{title}</h3>
			<p role="status" className="mt-1 flex items-center gap-1.5 text-xs text-muted-foreground">
				{pending && <Loader2 className="size-3 animate-spin" />}
				{!pending && report.status !== 'complete' && <TriangleAlert className="size-3" />}
				{t(`finance.status.${report.status}`)} · {t(report.artifact ? 'finance.report.saved' : 'finance.report.unsaved')}
			</p>
			{report.error && <p role="alert" className="mt-1 text-xs text-destructive break-words">{report.error}</p>}
		</div>
		<Button variant="outline" size="sm" data-finance-report-open disabled={!finance || !selection} onClick={() => selection && finance?.openReport(selection)}>{t('finance.report.open')}</Button>
	</section>;
}

/** The provenance dialog is a React descendant of the report dialog, keeping
 * Radix's modal focus/dismiss stack intact even though both use portals. */
export function FinanceReportPanel({ report, open, onClose, sourceOpen, restoreFocus, children, savedResult }: {
	report: FinanceReport | null; open: boolean; onClose: () => void; sourceOpen: boolean;
	restoreFocus: () => void; children: ReactNode;
	savedResult?: { title: string; loading: boolean; error: string | null; onRetry: () => void };
}) {
	const finance = useFinancialResults();
	const { t } = useTranslation();
	const title = useReportTitle(report);
	const status = report?.status ?? 'incomplete';
	const pending = status === 'running' || status === 'saving';
	return <Sheet open={open} onOpenChange={(next) => { if (!next && !sourceOpen) onClose(); }}>
		<SheetContent className="w-full sm:max-w-3xl overflow-y-auto" data-testid="finance-report" data-finance-status={status}
			onEscapeKeyDown={(event) => { if (sourceOpen) event.preventDefault(); }}
			onInteractOutside={(event) => { if (sourceOpen) event.preventDefault(); }}
			onCloseAutoFocus={(event) => { event.preventDefault(); restoreFocus(); }}>
			<SheetHeader className="pr-12">
				<SheetTitle>{savedResult?.title ?? title}</SheetTitle>
				<SheetDescription>{savedResult && !report ? t(savedResult.loading ? 'finance.catalog.loading' : 'finance.catalog.failed') : <>{t(`finance.status.${status}`)} · {t(report?.artifact ? 'finance.report.saved' : 'finance.report.unsaved')}</>}</SheetDescription>
			</SheetHeader>
			<div className="min-w-0 space-y-4 px-4 pb-8" data-finance-report-body>
				{savedResult?.loading && <p role="status" className="flex items-center gap-2 text-sm"><Loader2 className="size-4 animate-spin" />{t('finance.catalog.loading')}</p>}
				{savedResult?.error && <div role="alert" className="space-y-2 text-sm"><p className="text-destructive">{t('finance.catalog.failed')} {savedResult.error}</p><Button variant="outline" size="sm" onClick={savedResult.onRetry}>{t('finance.retry')}</Button></div>}
				{!report?.artifact && !savedResult && <p className="rounded-md bg-muted p-3 text-xs text-muted-foreground">{t('finance.report.draft')}</p>}
				{report?.error && <p role="alert" className="text-destructive break-words">{report.error}</p>}
				{report?.streamError && <p role="alert" className="text-destructive">{t('finance.report.streamError')}</p>}
				{!report?.artifact && !!report?.progress?.length && <section aria-label={t('finance.report.activity')} data-finance-report-progress className="rounded-md border p-3 text-xs">
					<h3 className="mb-2 font-medium">{t('finance.report.activity')}</h3>
					<ol role="log" aria-live="polite" className="space-y-1 text-muted-foreground">{report.progress.map((text, index) => <li key={index} className="whitespace-pre-wrap break-words">{text}</li>)}</ol>
				</section>}
				{report?.text ? <Markdown isAnimating={pending}>{report.text}</Markdown> : !savedResult && <p className="text-sm text-muted-foreground">{t(pending ? 'finance.report.waiting' : 'finance.report.noBody')}</p>}
				{report?.artifact && finance && <FinanceNotices notices={artifactNotices(report.artifact, finance.artifacts)} />}
			</div>
			{children}
		</SheetContent>
	</Sheet>;
}
