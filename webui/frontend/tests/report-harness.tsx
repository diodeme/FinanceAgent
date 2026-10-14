// Test-only Vite entry: real report/Markdown components, no chat or model service.
import { EventType } from '@agentscope-ai/agentscope/event';
import type { AgentEvent } from '@agentscope-ai/agentscope/event';
import { appendEvent, AssistantMsg } from '@agentscope-ai/agentscope/message';
import type { Msg } from '@agentscope-ai/agentscope/message';
import { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';

import { financeApi } from '@/api/finance';
import type { FinanceArtifact, ResultSummary } from '@/api/finance';
import { FinanceBubbleResults, RecoveredFinanceResults } from '@/components/chat/finance/FinanceResults';
import { Markdown } from '@/components/markdown';
import { FinancialResultsProvider, useFinancialResults } from '@/context/FinancialResultsContext';
import '@/i18n';
import '@/index.css';

const callId = 'fixture-call';
const replyId = 'fixture-reply';
const timestamp = '2026-10-14T00:00:00Z';
const call = { type: 'tool_call' as const, id: callId, name: 'data_analyze', input: JSON.stringify({ data_ids: ['dataset_fixture'], question: 'Fixture only' }), state: 'submitted' as const, created_at: timestamp };
function initial(): Msg {
	return AssistantMsg({ id: replyId, name: 'fixture', content: [call, { type: 'tool_result', id: callId, name: 'data_analyze', output: [], state: 'running', metadata: {}, created_at: timestamp }] });
}

// Simulate a transport that completes after abort, while still using mock GETs.
const originalArtifact = financeApi.artifact;
const deferred = new Map<string, { promise: Promise<void>; release: () => void }>();
financeApi.artifact = async (id, signal) => {
	const gate = deferred.get(id);
	const artifact = await originalArtifact(id, gate ? undefined : signal);
	if (gate) await gate.promise;
	return artifact;
};

function FixtureIndex() {
	const finance = useFinancialResults();
	useEffect(() => { Object.assign(window, { fixtureOpenResult: finance?.openResult }); }, [finance]);
	return <output data-testid="finance-index" data-artifact-ids={JSON.stringify([...finance!.artifacts.keys()])}
		data-recovered-ids={JSON.stringify(finance!.recoveredArtifacts.map((artifact) => artifact.artifact_id))} />;
}

function Fixture() {
	const [message, setMessage] = useState(() => {
		const saved = sessionStorage.getItem('fixture-catalog');
		return saved ? JSON.parse(saved) as Msg : initial();
	});
	const [session, setSession] = useState('fixture-session');
	const [user, setUser] = useState('fixture-user');
	useEffect(() => {
		const update = (mutate: (current: Msg) => void) => setMessage((current) => {
			const next = structuredClone(current);
			mutate(next);
			return next;
		});
		const savedTool = (name: string, artifacts: FinanceArtifact[], results?: ResultSummary[]) => {
			const message = AssistantMsg({ id: replyId, name: 'fixture', content: [
				{ ...call, name }, { type: 'tool_result', id: callId, name, output: [], state: 'success', created_at: timestamp,
					metadata: JSON.parse(JSON.stringify({ finance: { schema_version: 1, message: '', artifacts, results, companies: [], warnings: [], error_code: null } })) },
			] });
			message.finished_at = timestamp;
			setMessage(message);
			return message;
		};
		Object.assign(window, { financeFixture: {
			catalog: (results: ResultSummary[]) => sessionStorage.setItem('fixture-catalog', JSON.stringify(savedTool('find_results', [], results))),
			getResult: (artifact: FinanceArtifact) => savedTool('get_result', [artifact]),
			deferResult: (id: string) => {
				let release!: () => void;
				const promise = new Promise<void>((resolve) => { release = resolve; });
				deferred.set(id, { promise, release });
			},
			releaseResult: (id: string) => { deferred.get(id)?.release(); deferred.delete(id); },
			reset: () => { sessionStorage.removeItem('fixture-catalog'); setMessage(initial()); },
			delta: (delta: string) => update((current) => appendEvent(current, {
				type: EventType.TOOL_RESULT_TEXT_DELTA, id: crypto.randomUUID(), created_at: timestamp, reply_id: replyId, tool_call_id: callId, delta,
			} as AgentEvent)),
			finish: (artifact: FinanceArtifact) => update((current) => {
				appendEvent(current, { type: EventType.TOOL_RESULT_END, id: crypto.randomUUID(), created_at: timestamp, reply_id: replyId, tool_call_id: callId, state: 'success',
					metadata: { finance: { schema_version: 1, message: 'Saved fixture.', artifacts: [artifact], companies: [], warnings: [], error_code: null } },
				} as AgentEvent);
				current.finished_at = new Date().toISOString();
			}),
			partialDocuments: (artifacts: FinanceArtifact[]) => update((current) => {
				current.content.push(
					{ type: 'tool_call', id: 'filing-fixture-call', name: 'fetch_filings', input: JSON.stringify({ companies: ['AAPL'], count: 2 }), state: 'submitted', created_at: timestamp },
					{ type: 'tool_result', id: 'filing-fixture-call', name: 'fetch_filings', output: [], state: 'success', created_at: timestamp,
						metadata: JSON.parse(JSON.stringify({ finance: { schema_version: 1, message: 'One filing was unavailable.', artifacts, companies: [], warnings: ['One fixture filing unavailable'], error_code: null, partial: true } })) },
				);
			}),
			stop: (state: string) => update((current) => {
				const result = current.content.find((block) => block.type === 'tool_result');
				if (result?.type === 'tool_result' && state !== 'incomplete') result.state = state as typeof result.state;
				current.finished_at = new Date().toISOString();
			}),
			switchSession: () => { setSession((value) => value + '-next'); setMessage(initial()); },
			switchUser: () => { setUser((value) => value + '-next'); setMessage(initial()); },
		} });
	}, []);
	const toolResults = message.content.filter((block) => block.type === 'tool_result');
	const calls = message.content.filter((block) => block.type === 'tool_call').map((toolCall) => ({
		call: toolCall, result: toolResults.find((result) => result.id === toolCall.id),
	}));
	return <main className="mx-auto max-w-4xl p-8">
		<FinancialResultsProvider key={`${user}:${session}`} sessionId={session} messages={[message]}>
			<article data-slot="message" data-role="assistant">
				<Markdown animated isAnimating={!message.finished_at}>{'| Source |\n|---|\n| [OCF](#finance/dataset_fixture/c_fixture) · [营收](#finance/dataset_fixture/c_fixture) · [比率](#finance/dataset_fixture/c_fixture) |\n\n[Ordinary link](https://example.com)\n\n[Paragraph source](#finance/dataset_fixture/c_fixture)\n\n- [List source](#finance/dataset_fixture/c_fixture)'}</Markdown>
				<FinanceBubbleResults calls={calls} replyId={message.id} replyFinished={!!message.finished_at} />
			</article>
			<RecoveredFinanceResults />
			<FixtureIndex />
		</FinancialResultsProvider>
	</main>;
}

createRoot(document.getElementById('root')!).render(<Fixture />);
