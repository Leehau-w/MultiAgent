import { useEffect, useState } from 'react'
import Markdown from 'react-markdown'
import { useAgentStore } from '../stores/agentStore'

export default function ContextViewer() {
  const { selectedAgentId, agents, contextCache } = useAgentStore()
  const [content, setContent] = useState('')
  const [loading, setLoading] = useState(false)
  const [viewAll, setViewAll] = useState(false)

  const agentId = selectedAgentId
  const agent = agentId ? agents[agentId] : null

  // Fetch context when agent is selected
  useEffect(() => {
    if (!agentId) return
    // Check cache first (from WebSocket updates)
    if (contextCache[agentId]) {
      setContent(contextCache[agentId])
      return
    }
    setLoading(true)
    fetch(`/api/agents/${agentId}/context`)
      .then((r) => r.json())
      .then((d) => setContent(d.content || ''))
      .catch(() => setContent(''))
      .finally(() => setLoading(false))
  }, [agentId, contextCache])

  // Update content when cache changes
  useEffect(() => {
    if (agentId && contextCache[agentId]) {
      setContent(contextCache[agentId])
    }
  }, [agentId, contextCache])

  // Usage stats for the selected agent
  const usage = agent?.usage

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-2 border-b border-gray-800 shrink-0">
        <span className="text-[11px] text-gray-500 uppercase tracking-wide">
          Context {agent ? `- ${agent.role_name}` : ''}
        </span>
        <button
          onClick={() => setViewAll(!viewAll)}
          className="text-[11px] text-indigo-400 hover:text-indigo-300"
        >
          {viewAll ? 'Selected Only' : 'View All'}
        </button>
      </div>

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

function AllContexts() {
  const { agents, contextCache } = useAgentStore()
  const [allContexts, setAllContexts] = useState<Record<string, string>>({})

  useEffect(() => {
    // Fetch all contexts
    const ids = Object.keys(agents)
    Promise.all(
      ids.map((id) =>
        contextCache[id]
          ? Promise.resolve({ id, content: contextCache[id] })
          : fetch(`/api/agents/${id}/context`)
              .then((r) => r.json())
              .then((d) => ({ id, content: d.content || '' })),
      ),
    ).then((results) => {
      const map: Record<string, string> = {}
      for (const { id, content } of results) map[id] = content
      setAllContexts(map)
    })
  }, [agents, contextCache])

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
