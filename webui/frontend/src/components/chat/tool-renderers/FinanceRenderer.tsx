import { FinanceToolResult } from '../finance/FinanceResults';
import type { ToolRenderer } from './types';

export const FinanceRenderer: ToolRenderer = {
	getDisplayName: (call, t) => t(`finance.tool.${call.name}`),
	renderHeader: ({ call }, t) => <span>{t(`finance.tool.${call.name}`)}</span>,
	renderBody: (pair) => <FinanceToolResult pair={pair} />,
};
