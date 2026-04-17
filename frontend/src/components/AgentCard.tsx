import { useEffect, useRef, useState } from 'react'
import { useAgentStore } from '../stores/agentStore'
import { apiPost, apiPut, apiDelete } from '../utils/api'
import type { AgentState, PermissionMode } from '../types'

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

const MODE_CHIP: Record<PermissionMode, { label: string; cls: string }> = {
  manual: { label: 'manual', cls: 'text-gray-300 border-gray-600' },
  workspace: { label: 'workspace', cls: 'text-yellow-300 border-yellow-700/70' },
  bypass: { label: 'bypass', cls: 'text-red-300 border-red-700/70' },
}

// Context window sizes by model keyword
const CONTEXT_WINDOWS: Record<string, number> = {
  opus: 200_000,
  sonnet: 200_000,
  haiku: 200_000,
}

function getContextWindow(model: string): number {
  const key = model.toLowerCase()
  for (const [k, v] of Object.entries(CONTEXT_WINDOWS)) {
    if (key.includes(k)) return v
  }
  return 200_000
}

const CLAUDE_SHORT_VERSIONS: Record<string, string> = {
  opus: 'Opus 4.7',
  sonnet: 'Sonnet 4.6',
  haiku: 'Haiku 4.5',
}

function formatModel(model: string): string {
  const lower = model.toLowerCase()
  if (CLAUDE_SHORT_VERSIONS[lower]) return CLAUDE_SHORT_VERSIONS[lower]
  // Full Claude IDs like "claude-opus-4-7" → "Opus 4.7"
  const claude = lower.match(/^claude-(opus|sonnet|haiku)-(\d+)-(\d+)/)
  if (claude) {
    const family = claude[1][0].toUpperCase() + claude[1].slice(1)
    return `${family} ${claude[2]}.${claude[3]}`
  }
  return model
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
  const { selectedAgentId, selectAgent, roles, globalPermissionMode } = useAgentStore()
  const [showPrompt, setShowPrompt] = useState(false)
  const [showModeMenu, setShowModeMenu] = useState(false)
  const [prompt, setPrompt] = useState('')
  const isSelected = selectedAgentId === agent.id
  const totalTokens = agent.usage.input_tokens + agent.usage.output_tokens

  const role = roles[agent.role_id]
  const model = role?.model || '?'
  const modelLabel = formatModel(model)
  const effort = role?.effort || null
  const contextMax = getContextWindow(model)
  const contextPct = contextMax > 0 ? Math.min((agent.usage.input_tokens / contextMax) * 100, 100) : 0

  const effectiveMode: PermissionMode = agent.permission_mode || globalPermissionMode
  const isOverride = agent.permission_mode !== null
  const modeChip = MODE_CHIP[effectiveMode]

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

  const setMode = async (mode: PermissionMode | null) => {
    await apiPut(`/api/agents/${agent.id}/permission_mode`, { mode })
    setShowModeMenu(false)
  }

  const modeRowRef = useRef<HTMLDivElement | null>(null)
  useEffect(() => {
    if (!showModeMenu) return
    const handler = (e: MouseEvent) => {
      if (modeRowRef.current && !modeRowRef.current.contains(e.target as Node)) {
        setShowModeMenu(false)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [showModeMenu])

  // Color the context bar based on usage
  const barColor =
    contextPct >= 90 ? 'bg-red-500' : contextPct >= 70 ? 'bg-yellow-500' : 'bg-indigo-500'

  return (
    <>
      <div
        onClick={() => selectAgent(isSelected ? null : agent.id)}
        className={`
          relative w-[180px] rounded-lg border px-3 py-2 cursor-pointer
          transition-all select-none flex flex-col gap-1 group
          ${isSelected ? 'border-indigo-500 bg-indigo-500/10' : 'border-gray-700 bg-gray-900 hover:border-gray-600'}
        `}
      >
        {/* Row 1: name + status dot */}
        <div className="flex items-center justify-between">
          <span className="text-sm font-medium text-white truncate">{agent.role_name}</span>
          <span className={`w-2 h-2 rounded-full shrink-0 ${STATUS_COLORS[agent.status]}`} />
        </div>

        {/* Row 2: model + effort + permission mode chip */}
        <div ref={modeRowRef} className="flex items-center gap-1.5 text-[10px] relative">
          <span
            className="px-1.5 py-0.5 rounded bg-gray-800 text-gray-400 border border-gray-700 truncate"
            title={model}
          >
            {modelLabel}
          </span>
          {effort && (
            <span className="px-1.5 py-0.5 rounded bg-gray-800 text-amber-400 border border-gray-700">
              {effort}
            </span>
          )}
          <button
            onClick={(e) => { e.stopPropagation(); setShowModeMenu((v) => !v) }}
            title={
              isOverride
                ? `Permission: ${modeChip.label} (override)`
                : `Permission: ${modeChip.label} (inherit global)`
            }
            className={`ml-auto px-1.5 py-0.5 rounded bg-gray-800 border ${modeChip.cls} ${
              isOverride ? '' : 'border-dashed opacity-80'
            } hover:opacity-100`}
          >
            {isOverride ? modeChip.label : `~${modeChip.label}`}
          </button>
          {showModeMenu && (
            <div
              onClick={(e) => e.stopPropagation()}
              className="absolute top-full right-0 mt-1 z-20 bg-gray-900 border border-gray-700 rounded-md shadow-lg p-1 w-[140px]"
            >
              <button
                onClick={() => setMode(null)}
                className={`w-full text-left px-2 py-1 rounded text-[10px] hover:bg-gray-800 ${
                  !isOverride ? 'text-indigo-300' : 'text-gray-300'
                }`}
              >
                Inherit global
              </button>
              {(['manual', 'workspace', 'bypass'] as PermissionMode[]).map((m) => (
                <button
                  key={m}
                  onClick={() => setMode(m)}
                  className={`w-full text-left px-2 py-1 rounded text-[10px] hover:bg-gray-800 ${
                    agent.permission_mode === m
                      ? MODE_CHIP[m].cls.split(' ')[0]
                      : 'text-gray-300'
                  }`}
                >
                  {m}
                </button>
              ))}
            </div>
          )}
        </div>

        {/* Row 3: context bar */}
        <div className="flex items-center gap-1.5">
          <div className="flex-1 h-1.5 bg-gray-800 rounded-full overflow-hidden">
            <div
              className={`h-full rounded-full transition-all duration-500 ${barColor}`}
              style={{ width: `${contextPct}%` }}
            />
          </div>
          <span className="text-[10px] text-gray-500 w-[32px] text-right shrink-0">
            {contextPct < 1 && agent.usage.input_tokens > 0
              ? '<1%'
              : `${Math.round(contextPct)}%`}
          </span>
        </div>

        {/* Row 4: status + tokens + cost */}
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
