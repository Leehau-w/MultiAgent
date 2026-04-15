import { useState } from 'react'
import { useAgentStore } from '../stores/agentStore'
import { apiPost, apiDelete } from '../utils/api'
import type { AgentState } from '../types'

const STATUS_COLORS: Record<string, string> = {
  idle: 'bg-gray-500',
  running: 'bg-green-500 animate-pulse',
  waiting: 'bg-yellow-500',
  completed: 'bg-blue-500',
  error: 'bg-red-500',
}

const STATUS_LABELS: Record<string, string> = {
  idle: 'Idle',
  running: 'Running',
  waiting: 'Waiting',
  completed: 'Done',
  error: 'Error',
}

function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`
  return String(n)
}

interface Props {
  agent: AgentState
}

export default function AgentCard({ agent }: Props) {
  const { selectedAgentId, selectAgent } = useAgentStore()
  const [showPrompt, setShowPrompt] = useState(false)
  const [prompt, setPrompt] = useState('')
  const isSelected = selectedAgentId === agent.id
  const totalTokens = agent.usage.input_tokens + agent.usage.output_tokens

  const handleStart = async () => {
    if (!prompt.trim()) return
    const allAgents = useAgentStore.getState().agents
    const contextFrom = Object.keys(allAgents).filter((id) => id !== agent.id)

    const res = await apiPost(`/api/agents/${agent.id}/start`, {
      prompt: prompt.trim(),
      context_from: contextFrom,
    })
    if (res.ok) {
      setShowPrompt(false)
      setPrompt('')
    }
  }

  const handleStop = async () => {
    await apiPost(`/api/agents/${agent.id}/stop`, {})
  }

  const handleDelete = async () => {
    const res = await apiDelete(`/api/agents/${agent.id}`)
    if (res.ok) {
      useAgentStore.setState((s) => {
        const { [agent.id]: _, ...rest } = s.agents
        return { agents: rest }
      })
    }
  }

  return (
    <>
      <div
        onClick={() => selectAgent(isSelected ? null : agent.id)}
        className={`
          relative w-[160px] h-[88px] rounded-lg border px-3 py-2 cursor-pointer
          transition-all select-none flex flex-col justify-between group
          ${isSelected ? 'border-indigo-500 bg-indigo-500/10' : 'border-gray-700 bg-gray-900 hover:border-gray-600'}
        `}
      >
        {/* Top row: name + status */}
        <div className="flex items-center justify-between">
          <span className="text-sm font-medium text-white truncate">{agent.role_name}</span>
          <span className={`w-2 h-2 rounded-full shrink-0 ${STATUS_COLORS[agent.status]}`} />
        </div>

        {/* Agent ID */}
        <div className="text-[11px] text-gray-500 truncate">{agent.id}</div>

        {/* Bottom row: status + tokens + cost */}
        <div className="flex items-center justify-between text-[11px]">
          <span className="text-gray-400">{STATUS_LABELS[agent.status]}</span>
          <span className="text-gray-500">
            {formatTokens(totalTokens)} tok
          </span>
          <span className="text-gray-500">${agent.usage.cost_usd.toFixed(3)}</span>
        </div>

        {/* Action buttons (visible on hover or selected) */}
        <div
          className={`absolute -top-1 -right-1 flex gap-0.5 transition-opacity ${
            isSelected ? 'opacity-100' : 'opacity-0 group-hover:opacity-100'
          }`}
        >
          {agent.status === 'idle' || agent.status === 'completed' || agent.status === 'error' ? (
            <button
              onClick={(e) => { e.stopPropagation(); setShowPrompt(true) }}
              className="w-5 h-5 rounded bg-green-600 text-white text-[10px] flex items-center justify-center hover:bg-green-500"
              title="Start"
            >
              &#9654;
            </button>
          ) : (
            <button
              onClick={(e) => { e.stopPropagation(); handleStop() }}
              className="w-5 h-5 rounded bg-yellow-600 text-white text-[10px] flex items-center justify-center hover:bg-yellow-500"
              title="Stop"
            >
              &#9632;
            </button>
          )}
          <button
            onClick={(e) => { e.stopPropagation(); handleDelete() }}
            className="w-5 h-5 rounded bg-red-600 text-white text-[10px] flex items-center justify-center hover:bg-red-500"
            title="Delete"
          >
            &#10005;
          </button>
        </div>
      </div>

      {/* Start prompt modal */}
      {showPrompt && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60" onClick={() => setShowPrompt(false)}>
          <div className="bg-gray-900 border border-gray-700 rounded-xl p-5 w-[500px] max-w-[90vw]" onClick={(e) => e.stopPropagation()}>
            <h3 className="text-white font-medium mb-3">
              Start {agent.role_name} ({agent.id})
            </h3>
            <textarea
              autoFocus
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              placeholder="Enter task prompt..."
              rows={4}
              className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200 resize-none focus:outline-none focus:border-indigo-500"
              onKeyDown={(e) => {
                if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) handleStart()
              }}
            />
            <div className="flex justify-end gap-2 mt-3">
              <button onClick={() => setShowPrompt(false)} className="px-3 py-1.5 text-sm text-gray-400 hover:text-gray-200">
                Cancel
              </button>
              <button onClick={handleStart} className="px-4 py-1.5 bg-indigo-600 hover:bg-indigo-500 text-white text-sm rounded-md">
                Start (Ctrl+Enter)
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  )
}
