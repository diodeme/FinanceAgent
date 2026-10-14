import type { FinanceArtifact } from '../api/finance';

export const CROSS_COMPANY_NOTICE = 'Cross-company periods retain actual fiscal dates; equal row positions do not imply calendar alignment.';
const CROSS_COMPANY_OLD = 'Cross-company quarters retain actual fiscal dates and can cover different calendar windows.';
export const SOURCE_LIMITATION = 'Original filing documents were not fetched; source filing links are metadata only.';
export const CONTACT_DIAGNOSTIC = 'SEC_USER_AGENT is using FinanceAgent/0.1; configure an honest application name and real contact email for SEC fair-access identification.';

export interface CompanyScope { companies: Set<string>; complete: boolean }
export interface FinanceNotices { limitations: string[]; diagnostics: string[] }

/** Follow only selected artifacts and their dependencies, never the global history.
 * Missing dependencies mean unknown scope, not proof of a single company. */
export function artifactCompanyScope(selected: FinanceArtifact[], artifacts: ReadonlyMap<string, FinanceArtifact>): CompanyScope {
	const companies = new Set<string>();
	const visited = new Set<string>();
	let complete = selected.length > 0;
	const visit = (artifact: FinanceArtifact) => {
		if (visited.has(artifact.artifact_id)) return;
		visited.add(artifact.artifact_id);
		if (artifact.kind === 'document' && artifact.document) {
			const company = artifact.document.company.trim().toUpperCase();
			if (company) companies.add(company);
			else complete = false;
			return;
		}
		if (artifact.data) {
			// Selected output rows define table scope. Supporting history and wider
			// input snapshots must not turn a single-company result into a comparison.
			for (const row of artifact.data.rows) {
				if (row.company) companies.add(row.company.trim().toUpperCase());
			}
			if (!artifact.data.rows.length) complete = false;
			return;
		}
		const dependencies = new Set([...(artifact.input_ids ?? []), ...(artifact.citations ?? []).map((citation) => 'document_id' in citation ? citation.document_id : citation.artifact_id)]);
		dependencies.delete(artifact.artifact_id);
		if (!artifact.data && dependencies.size === 0) complete = false;
		for (const id of dependencies) {
			const dependency = artifacts.get(id);
			if (dependency) visit(dependency);
			else complete = false;
		}
	};
	for (const artifact of selected) visit(artifact);
	if (companies.size === 0) complete = false;
	return { companies, complete };
}

export function canonicalFinanceNotice(warning: string): string {
	return warning === CROSS_COMPANY_OLD ? CROSS_COMPANY_NOTICE : warning;
}

export function normalizeFinanceNotices(warnings: readonly string[], scope: CompanyScope): FinanceNotices {
	const limitations = new Set<string>();
	const diagnostics = new Set<string>();
	for (const raw of warnings) {
		const warning = canonicalFinanceNotice(raw);
		// Do not suppress an existing caution when historical dependencies are missing.
		if (warning === CROSS_COMPANY_NOTICE && scope.complete && scope.companies.size <= 1) continue;
		if (warning === CONTACT_DIAGNOSTIC) diagnostics.add(warning);
		else limitations.add(warning);
	}
	return { limitations: [...limitations], diagnostics: [...diagnostics] };
}

/** Only this result's document inputs count; unrelated downloaded filings do not
 * change the meaning of historical numeric evidence or mutate its snapshot. */
export function hasDocumentEvidence(selected: FinanceArtifact[], artifacts: ReadonlyMap<string, FinanceArtifact>): boolean {
	const visited = new Set<string>();
	const visit = (artifact: FinanceArtifact): boolean => {
		if (visited.has(artifact.artifact_id)) return false;
		visited.add(artifact.artifact_id);
		if (artifact.kind === 'document') return !!artifact.document;
		if (artifact.data) return false;
		const ids = [...(artifact.input_ids ?? []), ...(artifact.citations ?? []).map((citation) => 'document_id' in citation ? citation.document_id : citation.artifact_id)];
		return ids.some((id) => { const input = artifacts.get(id); return input ? visit(input) : false; });
	};
	return selected.some(visit);
}

export function artifactNotices(artifact: FinanceArtifact, artifacts: ReadonlyMap<string, FinanceArtifact>): FinanceNotices {
	const warnings = [...(artifact.warnings ?? []), ...(artifact.data?.warnings ?? [])];
	return normalizeFinanceNotices(hasDocumentEvidence([artifact], artifacts) ? warnings.filter((warning) => warning !== SOURCE_LIMITATION) : warnings, artifactCompanyScope([artifact], artifacts));
}

/** Envelope notices already represented by any result stay inside that result. */
export function envelopeNotices(warnings: readonly string[], selected: FinanceArtifact[], artifacts: ReadonlyMap<string, FinanceArtifact>): FinanceNotices {
	const represented = new Set(selected.flatMap((artifact) => [...(artifact.warnings ?? []), ...(artifact.data?.warnings ?? [])]).map(canonicalFinanceNotice));
	const documentsProvided = hasDocumentEvidence(selected, artifacts);
	return normalizeFinanceNotices(warnings.filter((warning) => !represented.has(canonicalFinanceNotice(warning)) && !(documentsProvided && warning === SOURCE_LIMITATION)), artifactCompanyScope(selected, artifacts));
}

export function financeNoticeTranslation(warning: string): { key: string; params?: Record<string, string> } | null {
	if (warning === CROSS_COMPANY_NOTICE) return { key: 'finance.notices.crossCompany' };
	if (warning === SOURCE_LIMITATION) return { key: 'finance.notices.sourceLimitation' };
	if (warning === CONTACT_DIAGNOSTIC) return { key: 'finance.notices.contactDiagnostic' };
	const capex = warning.match(/^([A-Z0-9.\-]+) capital expenditures use reported productive-asset cash purchases, including software\/other intangibles where combined; PP&E-only issuers are not strictly scope-identical\.$/);
	if (capex) return { key: 'finance.notices.capex', params: { company: capex[1] } };
	const coverage = warning.match(/^([A-Z0-9.\-]+): requested (\d+) quarters but only (\d+) identifiable periods are available$/);
	if (coverage) return { key: 'finance.notices.coverage', params: { company: coverage[1], requested: coverage[2], available: coverage[3] } };
	return null;
}
