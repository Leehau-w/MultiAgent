import { useEffect, useMemo, useState } from 'react'
import Markdown from 'react-markdown'
import { useAgentStore } from '../stores/agentStore'
import { useToastStore } from '../stores/toastStore'
import { apiFetch, apiPost } from '../utils/api'

type ViewMode = 'context' | 'coord_state'

interface HistoryEntry {
  filename: string
  timestamp: string
  size_bytes: number
}

export default function ContextViewer() {
  const { selectedAgentId, agents, contextCache } = useAgentStore()
  const [content, setContent] = useState('')
  const [loading, setLoading] = useState(false)
  const [viewAll, setViewAll] = useState(false)
  const [viewMode, setViewMode] = useState<ViewMode>('context')
  const [compacting, setCompacting] = useState(false)
  const [historyOpen, setHistoryOpen] = useState(false)
  const [confirmOpen, setConfirmOpen] = useState(false)

  const agentId = selectedAgentId
  const agent = agentId ? agents[agentId] : null
  const cached = agentId ? contextCache[agentId] : undefined
  const isCoordinator = agent?.role_id === 'coordinator'

  // Coordinators default to showing the scratchpad, since that's what
  // the coordinator is actively maintaining. Non-coordinators have no
  // scratchpad, so always show their context doc.
  useEffect(() => {
    setViewMode(isCoordinator ? 'coord_state' : 'context')
  }, [isCoordinator, agentId])

  // Resolve content: scratchpad fetch for coord_state, WS cache for context.
  useEffect(() => {
    if (!agentId) {
      setContent('')
      return
    }
    if (viewMode === 'coord_state') {
      setLoading(true)
      let cancelled = false
      fetch('/api/coordinator_state')
        .then((r) => r.json())
        .then((d) => {
          if (cancelled) return
          setContent(d.exists ? d.content : '_No scratchpad yet. The coordinator will create it on its next turn._')
        })
        .catch(() => { if (!cancelled) setContent('') })
        .finally(() => { if (!cancelled) setLoading(false) })
      return () => { cancelled = true }
    }
    if (cached !== undefined) {
      setContent(cached)
      return
    }
    setLoading(true)
    let cancelled = false
    fetch(`/api/agents/${agentId}/context`)
      .then((r) => r.json())
      .then((d) => { if (!cancelled) setContent(d.content || '') })
      .catch(() => { if (!cancelled) setContent('') })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [agentId, cached, viewMode])

  // Usage stats for the selected agent
  const usage = agent?.usage

  const handleCompact = async () => {
    if (!agentId) return
    setCompacting(true)
    const res = await apiPost(`/api/agents/${agentId}/compact`, {})
    setCompacting(false)
    setConfirmOpen(false)
    if (res.ok) {
      const data = await res.json()
      const method = data.method === 'haiku' ? 'Haiku' : 'fallback'
      useToastStore
        .getState()
        .add(
          'success',
          `Compacted via ${method}: ${data.before_bytes} → ${data.after_bytes} bytes`,
        )
      // Refresh current view. The backend has rewritten the MD, but the
      // WS context_update broadcast happens inside the coordinator loop —
      // for a manual compaction we refetch immediately.
      const ctxRes = await apiFetch(`/api/agents/${agentId}/context`)
      if (ctxRes.ok) {
        const d = await ctxRes.json()
        useAgentStore.setState((s) => ({
          contextCache: { ...s.contextCache, [agentId]: d.content || '' },
        }))
        setContent(d.content || '')
      }
    }
  }

  const canCompact =
    !!agentId && !viewAll && viewMode === 'context' && !loading && !compacting

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-2 border-b border-gray-800 shrink-0">
        <span className="text-[11px] text-gray-500 uppercase tracking-wide">
          {viewMode === 'coord_state' && !viewAll ? 'Coord Notes' : 'Context'}
          {agent ? ` - ${agent.role_name}` : ''}
        </span>
        <div className="flex items-center gap-3">
          {canCompact && (
            <>
              <button
                onClick={() => setConfirmOpen(true)}
                className="text-[11px] text-indigo-400 hover:text-indigo-300 disabled:text-gray-600"
                title="Archive current context and summarize via Haiku"
              >
                Compact
              </button>
              <button
                onClick={() => setHistoryOpen(true)}
                className="text-[11px] text-indigo-400 hover:text-indigo-300"
                title="View archived versions"
              >
                History
              </button>
            </>
          )}
          {isCoordinator && !viewAll && (
            <button
              onClick={() => setViewMode(viewMode === 'coord_state' ? 'context' : 'coord_state')}
              className="text-[11px] text-indigo-400 hover:text-indigo-300"
            >
              {viewMode === 'coord_state' ? 'Show Context' : 'Show Notes'}
            </button>
          )}
          <button
            onClick={() => setViewAll(!viewAll)}
            className="text-[11px] text-indigo-400 hover:text-indigo-300"
          >
            {viewAll ? 'Selected Only' : 'View All'}
          </button>
        </div>
      </div>

      {confirmOpen && (
        <CompactConfirm
          onCancel={() => setConfirmOpen(false)}
          onConfirm={handleCompact}
          compacting={compacting}
        />
      )}

      {historyOpen && agentId && (
        <HistoryModal agentId={agentId} onClose={() => setHistoryOpen(false)} />
      )}

      {/* Markdown content */}
      <div className="flex-1 overflow-y-auto px-4 py-3 min-h-0">
        {!agentId && !viewAll && (
          <p className="text-gray-600 text-sm italic">
            Select an agent to view its context document.
          </p>
        )}
        {loading && <p className="text-gray-500 text-sm">Loading...</p>}
        {!loading && content && !viewAll && (
          <div className="markdown-body text-sm text-gray-300">
            <Markdown>{content}</Markdown>
          </div>
        )}
        {viewAll && <AllContexts />}
      </div>

      {/* Usage stats */}
      {usage && (
        <div className="shrink-0 border-t border-gray-800 px-4 py-2 grid grid-cols-3 gap-2 text-[11px]">
          <div>
            <span className="text-gray-500">Input</span>
            <div className="text-gray-300 font-mono">{usage.input_tokens.toLocaleString()}</div>
          </div>
          <div>
            <span className="text-gray-500">Output</span>
            <div className="text-gray-300 font-mono">{usage.output_tokens.toLocaleString()}</div>
          </div>
          <div>
            <span className="text-gray-500">Cost</span>
            <div className="text-gray-300 font-mono">${usage.cost_usd.toFixed(4)}</div>
          </div>
        </div>
      )}
    </div>
  )
}

function CompactConfirm({
  onCancel,
  onConfirm,
  compacting,
}: {
  onCancel: () => void
  onConfirm: () => void
  compacting: boolean
}) {
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
      onClick={onCancel}
    >
      <div
        className="bg-gray-900 border border-gray-700 rounded-xl p-5 w-[420px] max-w-[90vw]"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="text-white font-medium mb-2">Compact context?</h3>
        <p className="text-sm text-gray-400 mb-4">
          The current document will be archived to{' '}
          <code className="text-indigo-300">context/.history/</code> and replaced
          by a shorter summary. Last 3 sections are preserved verbatim.
        </p>
        <div className="flex justify-end gap-2">
          <button
            onClick={onCancel}
            className="px-3 py-1.5 text-sm text-gray-400 hover:text-gray-200"
          >
            Cancel
          </button>
          <button
            onClick={onConfirm}
            disabled={compacting}
            className="px-4 py-1.5 bg-indigo-600 hover:bg-indigo-500 disabled:bg-gray-700 disabled:text-gray-500 text-white text-sm rounded-md"
          >
            {compacting ? 'Compacting…' : 'Compact'}
          </button>
        </div>
      </div>
    </div>
  )
}

function HistoryModal({
  agentId,
  onClose,
}: {
  agentId: string
  onClose: () => void
}) {
  const [entries, setEntries] = useState<HistoryEntry[]>([])
  const [active, setActive] = useState<string | null>(null)
  const [content, setContent] = useState('')
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    apiFetch(`/api/agents/${agentId}/history`)
      .then((r) => r.json())
      .then((d) => {
        if (cancelled) return
        setEntries(d.entries || [])
        setLoading(false)
      })
      .catch(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [agentId])

  useEffect(() => {
    if (!active) return
    let cancelled = false
    apiFetch(`/api/agents/${agentId}/history/${encodeURIComponent(active)}`)
      .then((r) => r.json())
      .then((d) => { if (!cancelled) setContent(d.content || '') })
      .catch(() => { if (!cancelled) setContent('') })
    return () => { cancelled = true }
  }, [active, agentId])

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
      onClick={onClose}
    >
      <div
        className="bg-gray-900 border border-gray-700 rounded-xl w-[780px] max-w-[95vw] h-[80vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="px-5 py-3 border-b border-gray-800 flex items-center justify-between">
          <h3 className="text-white font-medium text-sm">
            Archived contexts — {agentId}
          </h3>
          <button
            onClick={onClose}
            className="text-gray-500 hover:text-gray-300 text-lg leading-none"
          >
            &times;
          </button>
        </div>
        <div className="flex flex-1 min-h-0">
          <div className="w-[220px] border-r border-gray-800 overflow-y-auto shrink-0">
            {loading && (
              <p className="p-3 text-xs text-gray-500">Loading…</p>
            )}
            {!loading && entries.length === 0 && (
              <p className="p-3 text-xs text-gray-500">
                No archives yet. Compact the context to create one.
              </p>
            )}
            {entries.map((e) => (
              <button
                key={e.filename}
                onClick={() => setActive(e.filename)}
                className={`w-full text-left px-3 py-2 text-xs border-b border-gray-800 hover:bg-gray-800 ${
                  active === e.filename ? 'bg-gray-800 text-indigo-300' : 'text-gray-400'
                }`}
              >
                <div className="font-mono">{e.timestamp}</div>
                <div className="text-[10px] text-gray-600">{e.size_bytes} B</div>
              </button>
            ))}
          </div>
          <div className="flex-1 overflow-y-auto p-4 text-xs text-gray-300 font-mono whitespace-pre-wrap">
            {active ? content || '_Loading…_' : (
              <span className="italic text-gray-600">
                Select an archive to view its contents.
              </span>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

function AllContexts() {
  const { agents, contextCache } = useAgentStore()
  const [allContexts, setAllContexts] = useState<Record<string, string>>({})

  // Only refetch when the SET of agent IDs actually changes — otherwise any
  // unrelated WS event bumps the agents object identity and triggers a
  // full refetch for every agent.
  const idsKey = useMemo(() => Object.keys(agents).sort().join(','), [agents])

  useEffect(() => {
    const ids = idsKey ? idsKey.split(',') : []
    let cancelled = false
    Promise.all(
      ids.map((id) =>
        contextCache[id] !== undefined
          ? Promise.resolve({ id, content: contextCache[id] })
          : fetch(`/api/agents/${id}/context`)
              .then((r) => r.json())
              .then((d) => ({ id, content: d.content || '' })),
      ),
    ).then((results) => {
      if (cancelled) return
      const map: Record<string, string> = {}
      for (const { id, content } of results) map[id] = content
      setAllContexts(map)
    })
    return () => { cancelled = true }
    // contextCache changes arrive via WS and should refresh the display;
    // agent ID set changes arrive via create/delete.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [idsKey, contextCache])

  return (
    <div className="space-y-4">
      {Object.entries(allContexts).map(([id, ctx]) => (
        <div key={id} className="border border-gray-800 rounded-lg p-3">
          <div className="text-[11px] text-indigo-400 mb-2 uppercase">
            {agents[id]?.role_name || id}
          </div>
          <div className="markdown-body text-sm text-gray-300">
            <Markdown>{ctx || '_No context yet._'}</Markdown>
          </div>
        </div>
      ))}
    </div>
  )
}
