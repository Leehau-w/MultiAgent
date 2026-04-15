import { useState } from 'react'
import { useAgentStore } from '../stores/agentStore'
import { apiPost } from '../utils/api'

export default function ChatPanel() {
  const { agents, selectedAgentId, selectAgent } = useAgentStore()
  const [message, setMessage] = useState('')
  const [targetAgent, setTargetAgent] = useState<string>('')

  const agentList = Object.values(agents)
  const effectiveTarget = targetAgent || selectedAgentId || agentList[0]?.id || ''

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
          className="bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs text-gray-200 shrink-0"
        >
          {agentList.map((a) => (
            <option key={a.id} value={a.id}>
              {a.role_name} ({a.id})
            </option>
          ))}
        </select>
        <input
          value={message}
          onChange={(e) => setMessage(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              handleSend()
            }
          }}
          placeholder="Send a message to this agent..."
          className="flex-1 bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-indigo-500"
        />
        <button
          onClick={handleSend}
          disabled={!message.trim() || !effectiveTarget}
          className="px-3 py-1.5 bg-indigo-600 hover:bg-indigo-500 disabled:bg-gray-700 disabled:text-gray-500 text-white text-sm rounded-md transition-colors"
        >
          Send
        </button>
      </div>
    </div>
  )
}
