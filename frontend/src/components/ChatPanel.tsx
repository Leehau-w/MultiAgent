import { useState } from 'react'
import { useAgentStore } from '../stores/agentStore'
import { apiPost } from '../utils/api'

export default function ChatPanel() {
  const { agents, selectedAgentId, selectAgent, workflow, pipeline } =
    useAgentStore()
  const [message, setMessage] = useState('')
  const [targetAgent, setTargetAgent] = useState<string>('')

  const agentList = Object.values(agents)

  // Default-to-coord when a pipeline is running: the coord is the only agent
  // prepared to handle a free-form user message (its system prompt routes
  // [USER_MESSAGE] inbox items). Any explicit selection (dropdown or
  // clicking a card) overrides.
  const coordRoleId = workflow?.coordinator?.role_id ?? 'coordinator'
  const pipelineLive =
    pipeline.status === 'running' || pipeline.status === 'paused'
  const coordAgent = pipelineLive
    ? agentList.find((a) => a.role_id === coordRoleId) ?? null
    : null

  // When a pipeline is live but the coord hasn't been auto-spawned yet (or
  // was deleted manually), we refuse to silently route the message to the
  // first agent in the roster — workers don't handle [USER_MESSAGE] and
  // the backend now rejects the send outright. Surface a disabled state
  // instead so the user sees *why* nothing happens.
  const coordMissing = pipelineLive && coordAgent === null

  const effectiveTarget = coordMissing
    ? ''
    : targetAgent ||
      selectedAgentId ||
      coordAgent?.id ||
      agentList[0]?.id ||
      ''

  // The "To: coord" badge is only meaningful when the implicit default
  // picked the coord — clicking it flips into explicit-select mode.
  const showingCoordDefault =
    !targetAgent &&
    !selectedAgentId &&
    coordAgent !== null &&
    effectiveTarget === coordAgent.id

  const handleSend = async () => {
    if (!message.trim() || !effectiveTarget) return

    await apiPost(`/api/agents/${effectiveTarget}/message`, { content: message.trim() })

    setMessage('')
    // Focus on the agent we sent to
    if (effectiveTarget !== selectedAgentId) {
      selectAgent(effectiveTarget)
    }
  }

  return (
    <div className="shrink-0 border-t border-gray-800 p-3">
      <div className="flex items-center gap-2">
        <span className="text-[11px] text-gray-500 uppercase tracking-wide shrink-0">To:</span>
        <select
          value={effectiveTarget}
          onChange={(e) => setTargetAgent(e.target.value)}
          disabled={coordMissing}
          className={`bg-gray-800 border rounded px-2 py-1 text-xs shrink-0 ${
            coordMissing
              ? 'border-gray-700 text-gray-600 cursor-not-allowed'
              : showingCoordDefault
                ? 'border-amber-700/70 text-amber-200'
                : 'border-gray-700 text-gray-200'
          }`}
          title={
            coordMissing
              ? 'Coordinator is not ready yet. Messages cannot be routed.'
              : showingCoordDefault
                ? 'Default target during a pipeline run; pick another to override.'
                : undefined
          }
        >
          {coordMissing ? (
            <option value="">Coordinator not ready</option>
          ) : (
            agentList.map((a) => (
              <option key={a.id} value={a.id}>
                {a.role_name} ({a.id})
              </option>
            ))
          )}
        </select>
        {showingCoordDefault && (
          <span className="text-[10px] text-amber-400/80 shrink-0">
            default while pipeline runs
          </span>
        )}
        {coordMissing && (
          <span className="text-[10px] text-amber-400/80 shrink-0">
            coordinator not ready yet
          </span>
        )}
        <input
          value={message}
          onChange={(e) => setMessage(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              handleSend()
            }
          }}
          placeholder={
            coordMissing
              ? 'Coordinator will accept messages once it spawns.'
              : showingCoordDefault
                ? 'Message the coordinator — it will route or reply via toast.'
                : 'Send a message to this agent...'
          }
          disabled={coordMissing}
          className="flex-1 bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-indigo-500 disabled:text-gray-500 disabled:cursor-not-allowed"
        />
        <button
          onClick={handleSend}
          disabled={!message.trim() || !effectiveTarget || coordMissing}
          className="px-3 py-1.5 bg-indigo-600 hover:bg-indigo-500 disabled:bg-gray-700 disabled:text-gray-500 text-white text-sm rounded-md transition-colors"
        >
          Send
        </button>
      </div>
    </div>
  )
}
