import { useTranslation } from '@/i18n/useI18n';
import { financeNoticeTranslation } from '@/lib/finance-notices';
import type { FinanceNotices as Notices } from '@/lib/finance-notices';

export function FinanceNotices({ notices }: { notices: Notices }) {
	const { t } = useTranslation();
	const label = (notice: string) => {
		const localized = financeNoticeTranslation(notice);
		return localized ? t(localized.key, localized.params) : notice;
	};
	if (!notices.limitations.length && !notices.diagnostics.length) return null;
	return <details className="rounded-md border px-3 py-2 text-xs text-muted-foreground" data-finance-notices>
		<summary className="cursor-pointer font-medium">{t('finance.notices.title')}</summary>
		{notices.limitations.length > 0 && <ul className="mt-2 list-disc space-y-1 pl-4">{notices.limitations.map((notice) => <li key={notice}>{label(notice)}</li>)}</ul>}
		{notices.diagnostics.length > 0 && <details className="mt-2">
			<summary className="cursor-pointer">{t('finance.notices.diagnostics')}</summary>
			<p className="mt-1">{t('finance.notices.historical')}</p>
			<ul className="mt-1 list-disc space-y-1 pl-4">{notices.diagnostics.map((notice) => <li key={notice}>{label(notice)}</li>)}</ul>
		</details>}
	</details>;
}
