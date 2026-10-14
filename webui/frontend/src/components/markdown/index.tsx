import { cjk } from '@streamdown/cjk';
import { code } from '@streamdown/code';
import { math } from '@streamdown/math';
import { mermaid } from '@streamdown/mermaid';
import * as React from 'react';
import { defaultRehypePlugins, Streamdown } from 'streamdown';

import { useFinancialResults } from '@/context/FinancialResultsContext';
import { financeCitationPlugin, parseArtifactCitation } from '@/lib/finance';
import { cn } from '@/lib/utils';

const DEFAULT_PLUGINS = { code, math, cjk, mermaid };

function FinanceCitationLink(props: Record<string, unknown>) {
	const finance = useFinancialResults();
	const href = typeof props.href === 'string' ? props.href : undefined;
	const citation = parseArtifactCitation(href);
	const children = props.children as React.ReactNode;
	if (!finance || !citation) return <span>{children}</span>;
	return <a href={href} data-finance-citation="" className="font-medium text-primary underline underline-offset-2"
		onClick={(event) => { event.preventDefault(); finance.openCitation(citation); }}>
		{children}
	</a>;
}

function Markdown({
	className,
	plugins = DEFAULT_PLUGINS,
	controls = false,
	components,
	rehypePlugins,
	...props
}: React.ComponentProps<typeof Streamdown>) {
	const finance = useFinancialResults();
	const financeEnabled = finance !== null;
	const mergedComponents = React.useMemo(() => financeEnabled
		? { ...components, 'finance-citation': FinanceCitationLink } : components,
	[components, financeEnabled]);
	const mergedRehypePlugins = React.useMemo(() => financeEnabled
		? [...(rehypePlugins ?? Object.values(defaultRehypePlugins)), financeCitationPlugin] : rehypePlugins,
	[rehypePlugins, financeEnabled]);
	// Streamdown renders normal safe links as buttons, not anchors, and does not
	// forward data-slot to its root. Transform only recognized citations after
	// sanitization rather than disabling link safety or replacing normal links.
	return (
		<Streamdown
			plugins={plugins}
			controls={controls}
			className={cn('cn-markdown w-full min-w-0 overflow-hidden', className)}
			components={mergedComponents}
			rehypePlugins={mergedRehypePlugins}
			{...props}
		/>
	);
}

export { Markdown };
