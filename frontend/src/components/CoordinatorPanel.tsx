import { useEffect, useState } from 'react'
import Markdown from 'react-markdown'
import { useAgentStore } from '../stores/agentStore'

type Tab = 'progress' | 'state' | 'pipeline'

interface CoordFact {
  ts: string
  kind: string
  agent: string | null
  summary: string
}

interface CoordDecision {
  ts: string
  decision: string
  rationale: string | null
}

interface CoordStructuredState {
  path: string
  version: number
  facts: CoordFact[]
  hypothesis: string
  open_questions: string[]
  decisions: CoordDecision[]
}

interface Props {
  open: boolean
  onClose: () => void
}

export default function CoordinatorPanel({ open, onClose }: Props) {
  const [tab, setTab] = useState<Tab>('progress')
  const [mdContent, setMdContent] = useState('')
  const [structured, setStructured] = useState<CoordStructuredState | null>(
    null,
  )
  const [loading, setLoading] = useState(false)

  const workflow = useAgentStore((s) => s.workflow)
  const pipeline = useAgentStore((s) => s.pipeline)
  const decisions = useAgentStore((s) => s.decisions)

  useEffect(() => {
    if (!open) return
    if (tab === 'progress') {
      setLoading(true)
      fetch('/api/coordinator_state')
        .then((r) => r.json())
        .then((d) => {
          setMdContent(
            d.exists
              ? d.content
              : '_No scratchpad yet. The coordinator will create it on its next turn._',
          )
        })
        .catch(() => setMdContent(''))
        .finally(() => setLoading(false))
    } else if (tab === 'state') {
      setLoading(true)
      fetch('/api/coordinator_state/structured')
        .then((r) => r.json())
        .then((d) => setStructured(d as CoordStructuredState))
        .catch(() => setStructured(null))
        .finally(() => setLoading(false))
    }
  }, [open, tab])

  if (!open) return null

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
      onClick={onClose}
    >
      <div
        className="bg-gray-900 border border-gray-700 rounded-xl w-[820px] max-w-[95vw] h-[80vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="px-5 py-3 border-b border-gray-800 flex items-center justify-between shrink-0">
          <div className="flex items-center gap-3">
            <h3 className="text-white font-medium text-sm flex items-center gap-2">
              <span className="text-[9px] px-1.5 py-0.5 rounded bg-amber-700/40 text-amber-200 border border-amber-700/60">
                COORD
              </span>
              Coordinator
            </h3>
            <div className="flex gap-1">
              <TabButton active={tab === 'progress'} onClick={() => setTab('progress')}>
                Progress
              </TabButton>
              <TabButton active={tab === 'state'} onClick={() => setTab('state')}>
                State
              </TabButton>
              <TabButton
                active={tab === 'pipeline'}
                onClick={() => setTab('pipeline')}
              >
                Pipeline
              </TabButton>
            </div>
          </div>
          <button
            onClick={onClose}
            className="text-gray-500 hover:text-gray-200 text-xl leading-none"
          >
            ×
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto min-h-0">
          {loading && (
            <p className="px-5 py-4 text-sm text-gray-500">Loading…</p>
          )}

          {!loading && tab === 'progress' && (
            <div className="px-5 py-4 markdown-body text-sm text-gray-300">
              <Markdown>{mdContent}</Markdown>
            </div>
          )}

          {!loading && tab === 'state' && (
            <StateTab state={structured} decisions={decisions} />
          )}

          {!loading && tab === 'pipeline' && (
            <PipelineTab
              pipeline={pipeline}
              maxRetries={workflow?.budget?.max_stage_retries ?? 3}
            />
          )}
        </div>
      </div>
    </div>
  )
}

function TabButton({
  active,
  onClick,
  children,
}: {
  active: boolean
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <button
      onClick={onClick}
      className={`text-[11px] px-2.5 py-1 rounded-md border transition-colors ${
        active
          ? 'border-amber-700/70 bg-amber-950/30 text-amber-200'
          : 'border-gray-700 text-gray-400 hover:text-gray-200'
      }`}
    >
      {children}
    </button>
  )
}

function Collapsible({
  title,
  count,
  children,
  defaultOpen = true,
}: {
  title: string
  count?: number
  children: React.ReactNode
  defaultOpen?: boolean
}) {
  const [isOpen, setIsOpen] = useState(defaultOpen)
  return (
    <div className="border border-gray-800 rounded-lg overflow-hidden">
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="w-full px-3 py-2 flex items-center justify-between text-left bg-gray-900/60 hover:bg-gray-900"
      >
        <span className="text-[11px] uppercase tracking-wider text-gray-400 font-semibold">
          {title}
          {count !== undefined && (
            <span className="ml-2 text-gray-600">({count})</span>
          )}
        </span>
        <span className="text-gray-500 text-xs">{isOpen ? '−' : '+'}</span>
      </button>
      {isOpen && <div className="px-3 py-3 border-t border-gray-800">{children}</div>}
    </div>
  )
}

function StateTab({
  state,
  decisions,
}: {
  state: CoordStructuredState | null
  decisions: { scope: string; decision: string; rationale: string; timestamp: string }[]
}) {
  if (!state) {
    return (
      <p className="px-5 py-4 text-sm text-gray-500 italic">
        No structured state yet. The coordinator writes via{' '}
        <code>update_state</code> each turn.
      </p>
    )
  }

  // Merge live WS decisions with persisted decisions, dedupe by text+ts.
  const sessionDecisions = decisions.map((d) => ({
    ts: d.timestamp,
    decision: `[${d.scope}] ${d.decision}`,
    rationale: d.rationale,
  }))
  const allDecisions = [...state.decisions, ...sessionDecisions]
    .reduce<CoordDecision[]>((acc, d) => {
      if (acc.some((x) => x.decision === d.decision && x.ts === d.ts)) return acc
      acc.push(d)
      return acc
    }, [])
    .sort((a, b) => a.ts.localeCompare(b.ts))

  return (
    <div className="px-5 py-4 space-y-3">
      <Collapsible title="Facts" count={state.facts.length}>
        {state.facts.length === 0 ? (
          <p className="text-xs text-gray-500 italic">No facts recorded yet.</p>
        ) : (
          <ul className="space-y-2 text-xs">
            {state.facts.map((f, i) => (
              <li key={i} className="border-l-2 border-gray-700 pl-2">
                <div className="flex items-center gap-2 text-[10px] text-gray-500">
                  <span>{formatTs(f.ts)}</span>
                  <span className="px-1 py-0.5 rounded bg-gray-800 text-gray-400">
                    {f.kind}
                  </span>
                  {f.agent && (
                    <span className="text-indigo-400">{f.agent}</span>
                  )}
                </div>
                <div className="text-gray-300 mt-0.5">{f.summary}</div>
              </li>
            ))}
          </ul>
        )}
      </Collapsible>

      <Collapsible title="Hypothesis" defaultOpen={!!state.hypothesis}>
        {state.hypothesis ? (
          <p className="text-xs text-gray-300 whitespace-pre-wrap">
            {state.hypothesis}
          </p>
        ) : (
          <p className="text-xs text-gray-500 italic">
            No hypothesis yet — the coordinator replaces it with a single
            paragraph each turn.
          </p>
        )}
      </Collapsible>

      <Collapsible title="Open questions" count={state.open_questions.length}>
        {state.open_questions.length === 0 ? (
          <p className="text-xs text-gray-500 italic">
            No open questions — the coord believes it has enough to proceed.
          </p>
        ) : (
          <ul className="space-y-1 text-xs list-disc pl-4 text-gray-300">
            {state.open_questions.map((q, i) => (
              <li key={i}>{q}</li>
            ))}
          </ul>
        )}
      </Collapsible>

      <Collapsible title="Decisions" count={allDecisions.length}>
        {allDecisions.length === 0 ? (
          <p className="text-xs text-gray-500 italic">
            No decisions recorded yet.
          </p>
        ) : (
          <ul className="space-y-2 text-xs">
            {allDecisions.map((d, i) => (
              <li key={i} className="border-l-2 border-amber-700/40 pl-2">
                <div className="text-[10px] text-gray-500">{formatTs(d.ts)}</div>
                <div className="text-gray-200">{d.decision}</div>
                {d.rationale && (
                  <div className="text-[11px] text-gray-500 italic mt-0.5">
                    {d.rationale}
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}
      </Collapsible>
    </div>
  )
}

function PipelineTab({
  pipeline,
  maxRetries,
}: {
  pipeline: ReturnType<typeof useAgentStore.getState>['pipeline']
  maxRetries: number
}) {
  if (pipeline.stages.length === 0) {
    return (
      <p className="px-5 py-4 text-sm text-gray-500 italic">
        No pipeline is running. Start one via "Start Pipeline" in the header.
      </p>
    )
  }
  return (
    <div className="px-5 py-4 space-y-3">
      <div className="text-[11px] text-gray-500">
        Status:{' '}
        <span className="text-gray-200 font-semibold uppercase">
          {pipeline.status}
        </span>
        {pipeline.requirement && (
          <span className="ml-3 italic">"{pipeline.requirement}"</span>
        )}
      </div>
      {pipeline.status === 'paused' && pipeline.pauseReason && (
        <div className="text-xs text-amber-300 border border-amber-800/60 bg-amber-950/30 rounded px-3 py-2">
          Paused: {pipeline.pauseReason}
        </div>
      )}
      {pipeline.status === 'failed' && pipeline.failureReason && (
        <div className="text-xs text-rose-300 border border-rose-800/60 bg-rose-950/30 rounded px-3 py-2">
          Aborted: {pipeline.failureReason}
        </div>
      )}
      <ul className="space-y-1">
        {pipeline.stages.map((s, i) => {
          const done = i < pipeline.currentStage
          const active = i === pipeline.currentStage
          const reviewing = pipeline.stageReviewing === s.name
          const retries = pipeline.stageRetries[s.name] ?? 0
          const paused = pipeline.status === 'paused' && reviewing
          const tone = paused
            ? 'text-rose-300 border-rose-800/60 bg-rose-950/30'
            : reviewing
              ? 'text-amber-300 border-amber-800/60 bg-amber-950/30'
              : active
                ? 'text-indigo-300 border-indigo-800/60 bg-indigo-950/30'
                : done
                  ? 'text-green-400 border-green-900/60 bg-green-950/20'
                  : 'text-gray-500 border-gray-800 bg-gray-900/30'
          return (
            <li
              key={i}
              className={`flex items-center gap-3 text-xs border rounded px-3 py-2 ${tone}`}
            >
              <span className="w-6 text-right font-mono">#{i + 1}</span>
              <span className="flex-1 font-semibold uppercase">{s.name}</span>
              <span className="text-[10px] opacity-80">
                {s.agents.length} agent{s.agents.length === 1 ? '' : 's'}
                {s.parallel ? ' · parallel' : ''}
              </span>
              {reviewing && (
                <span className="text-[10px] px-1 py-0.5 rounded bg-amber-900/60 text-amber-100 border border-amber-700">
                  gate review
                </span>
              )}
              {retries > 0 && (
                <span className="text-[10px] px-1 py-0.5 rounded bg-amber-900/40 text-amber-200 border border-amber-900">
                  {retries}/{maxRetries}
                </span>
              )}
            </li>
          )
        })}
      </ul>
    </div>
  )
}

function formatTs(ts: string): string {
  const d = new Date(ts)
  if (Number.isNaN(d.getTime())) return ts
  return d.toLocaleString()
}
